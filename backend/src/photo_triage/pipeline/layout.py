"""The `layout` batch stage: every embedded item's place on the map (plan §6.4).

UMAP projects the CLIP embeddings of the active items to 2D, with a fixed seed, so
the same embeddings give the same layout. The coordinates are moved and scaled so
the longer axis spans [0, 1] and the shorter one starts at 0, which keeps the
map's zoom levels independent of UMAP's scale.

Each re-fit is **warm-started** from the current layout, so the map doesn't
reshuffle when a nightly scan adds a few photos. UMAP starts from every laid-out
item's current place, and each new item starts at the average place of its
`INIT_NEIGHBOURS` nearest laid-out items by CLIP similarity. The result is then
rotated, scaled and shifted onto the current layout (`align`), so the map keeps its
orientation. A first fit, or one with too few items in common with the current
layout, starts from UMAP's own spectral layout.

UMAP can't fit fewer than `MIN_UMAP_ITEMS`, so up to three items are set out on a
line instead.

A fit reads the embeddings in one transaction, runs UMAP outside any, and writes
the new version in a third, removing the old one in the same transaction. So the
API always sees one complete layout, and the database isn't locked while UMAP runs.
When `photo_triage.worker.layout_schedule` queues a fit is up to it.
"""

import logging
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import numpy as np
import sqlalchemy as sa
from numpy.typing import NDArray
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, Layout
from photo_triage.pipeline.embed import from_blob
from photo_triage.worker.layout_schedule import embedded_items, latest_version

logger = logging.getLogger(__name__)

SEED = 42
"""UMAP's `random_state`: the same embeddings give the same layout."""

N_NEIGHBORS = 15
"""umap-learn's default. Tuning it is plan §12."""

MIN_UMAP_ITEMS = 4
"""UMAP fails on fewer points than this. A warm start needs this many items in
common with the current layout too."""

INIT_NEIGHBOURS = 5
"""How many of a new item's nearest laid-out items its starting place averages."""

_SIMILARITY_CHUNK = 1024
"""New items compared with the laid-out ones at a time, to bound memory: a 1024 by
33k float32 similarity block is about 135 MB."""


@dataclass(frozen=True)
class Fit:
    version: int | None
    """None when there was nothing to lay out, and the old layout was removed."""
    items: int
    fit_s: float


def fit(sessions: sessionmaker[Session], model: str) -> Fit:
    """Lay out every active item embedded with `model`, as a new version."""
    with sessions() as session:
        rows = session.execute(embedded_items(model)).all()
        current = _current_layout(session)
    ids = [item_id for item_id, _ in rows]
    vectors = (
        np.stack([from_blob(blob) for _, blob in rows]) if rows else np.empty((0, 0), np.float32)
    )
    del rows

    started_s = time.monotonic()
    points = normalize(place(ids, vectors, current))
    fit_s = time.monotonic() - started_s

    with sessions.begin() as session:
        version = _replace(session, ids, points)
    if version is None:
        logger.info("Nothing to lay out: no active item has a %s embedding", model)
    else:
        logger.info("Laid out %d items as layout version %d in %.1f s", len(ids), version, fit_s)
    return Fit(version=version, items=len(ids), fit_s=fit_s)


def place(
    ids: list[int], vectors: NDArray[np.float32], current: dict[int, tuple[float, float]]
) -> NDArray[np.float64]:
    """Each item's 2D position, before `normalize`: warm-started from and aligned to
    `current` (item id to place) when enough items are in common, else a cold fit."""
    laid_out = np.array([item_id in current for item_id in ids], dtype=bool)
    if len(ids) < MIN_UMAP_ITEMS or laid_out.sum() < MIN_UMAP_ITEMS:
        return project(vectors)
    old = np.array([current[item_id] for item_id in ids if item_id in current])
    init = np.empty((len(ids), 2))
    init[laid_out] = old
    init[~laid_out] = neighbour_places(vectors[laid_out], old, vectors[~laid_out])
    points = project(vectors, init=init)
    return align(points[laid_out], old)(points)


def neighbour_places(
    laid_out: NDArray[np.float32], places: NDArray[np.float64], new: NDArray[np.float32]
) -> NDArray[np.float64]:
    """Each new vector's starting place: the average place of its `INIT_NEIGHBOURS`
    most similar laid-out vectors (cosine)."""
    k = min(INIT_NEIGHBOURS, len(laid_out))
    known = _unit(laid_out)
    out = np.empty((len(new), 2))
    for start in range(0, len(new), _SIMILARITY_CHUNK):
        similarity = _unit(new[start : start + _SIMILARITY_CHUNK]) @ known.T
        nearest = np.argpartition(-similarity, k - 1, axis=1)[:, :k]
        out[start : start + _SIMILARITY_CHUNK] = places[nearest].mean(axis=1)
    return out


def align(
    moving: NDArray[np.float64], fixed: NDArray[np.float64]
) -> Callable[[NDArray[np.float64]], NDArray[np.float64]]:
    """The rotation (or reflection), uniform scale and shift that best maps `moving`
    onto `fixed` in least squares (Procrustes), as a function to apply to points."""
    moving_mean, fixed_mean = moving.mean(axis=0), fixed.mean(axis=0)
    x, y = moving - moving_mean, fixed - fixed_mean
    u, s, vt = np.linalg.svd(x.T @ y)
    spread = float((x**2).sum())
    scale = float(s.sum()) / spread if spread > 0 else 1.0
    rotation = u @ vt
    return lambda points: (points - moving_mean) @ rotation * scale + fixed_mean


def project(
    vectors: NDArray[np.float32], init: NDArray[np.float64] | None = None
) -> NDArray[np.float64]:
    """Each row's 2D position, before `normalize`. UMAP starts from `init` if given,
    one place per row, or else from its spectral layout."""
    count = len(vectors)
    if count < MIN_UMAP_ITEMS:
        return np.column_stack([np.arange(count, dtype=np.float64), np.zeros(count)])

    # Slow to import (numba), and only the worker needs it. It has no type stubs.
    import umap  # pyright: ignore[reportMissingTypeStubs]

    reducer = umap.UMAP(
        n_neighbors=min(N_NEIGHBORS, count - 1),
        metric="cosine",
        random_state=SEED,
        # umap-learn annotates `init` as str, but documents and accepts an array.
        init="spectral" if init is None else init,  # pyright: ignore[reportArgumentType]
    )
    with warnings.catch_warnings():
        # "n_jobs value 1 overridden to 1 by setting random_state": expected.
        warnings.filterwarnings("ignore", message=".*overridden to 1 by setting random_state")
        # An ndarray for dense input, whatever its loose return annotation says.
        projected = cast(
            NDArray[np.float32],
            reducer.fit_transform(vectors),  # pyright: ignore[reportUnknownMemberType]
        )
    return np.asarray(projected, dtype=np.float64)


def normalize(points: NDArray[np.float64]) -> NDArray[np.float64]:
    """Shift to start at 0 on each axis, and scale so the longer axis spans [0, 1].
    A single point, or points all in one place, end up at the origin."""
    if len(points) == 0:
        return points
    shifted = points - points.min(axis=0)
    span = float(shifted.max())
    return shifted / span if span > 0 else shifted


def _unit(vectors: NDArray[np.float32]) -> NDArray[np.float32]:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return (vectors / np.where(norms > 0, norms, 1)).astype(np.float32)


def _current_layout(session: Session) -> dict[int, tuple[float, float]]:
    """The current layout's place for each item, or nothing if there's none."""
    version = session.scalar(latest_version())
    rows = session.execute(
        sa.select(Layout.item_id, Layout.x, Layout.y).where(Layout.layout_version == version)
    )
    return {item_id: (x, y) for item_id, x, y in rows}


def _replace(session: Session, ids: list[int], points: NDArray[np.float64]) -> int | None:
    """Write `points` as the next version and remove every older one. Items deleted
    while UMAP ran are left out. Returns the new version, or None if there was
    nothing to write."""
    previous = session.scalar(latest_version())
    session.execute(sa.delete(Layout))
    if not ids:
        return None
    existing = set(session.scalars(sa.select(Item.id).where(Item.id.in_(ids))))
    version = (previous or 0) + 1
    session.execute(
        sa.insert(Layout),
        [
            {"layout_version": version, "item_id": item_id, "x": float(x), "y": float(y)}
            for item_id, (x, y) in zip(ids, points, strict=True)
            if item_id in existing
        ],
    )
    return version
