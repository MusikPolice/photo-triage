"""The `layout` batch stage: every embedded item's place on the map (plan §6.4).

UMAP projects the CLIP embeddings of the active items to 2D, with a fixed seed, so
the same embeddings give the same layout. The coordinates are moved and scaled so
the longer axis spans [0, 1] and the shorter one starts at 0, which keeps the
map's zoom levels independent of UMAP's scale.

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
"""UMAP fails on fewer points than this."""


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
    ids = [item_id for item_id, _ in rows]
    vectors = (
        np.stack([from_blob(blob) for _, blob in rows]) if rows else np.empty((0, 0), np.float32)
    )
    del rows

    started_s = time.monotonic()
    points = normalize(project(vectors))
    fit_s = time.monotonic() - started_s

    with sessions.begin() as session:
        version = _replace(session, ids, points)
    if version is None:
        logger.info("Nothing to lay out: no active item has a %s embedding", model)
    else:
        logger.info("Laid out %d items as layout version %d in %.1f s", len(ids), version, fit_s)
    return Fit(version=version, items=len(ids), fit_s=fit_s)


def project(vectors: NDArray[np.float32]) -> NDArray[np.float64]:
    """Each row's 2D position, before `normalize`."""
    count = len(vectors)
    if count < MIN_UMAP_ITEMS:
        return np.column_stack([np.arange(count, dtype=np.float64), np.zeros(count)])

    # Slow to import (numba), and only the worker needs it. It has no type stubs.
    import umap  # pyright: ignore[reportMissingTypeStubs]

    reducer = umap.UMAP(
        n_neighbors=min(N_NEIGHBORS, count - 1),
        metric="cosine",
        random_state=SEED,
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
