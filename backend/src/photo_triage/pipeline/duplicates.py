"""The `duplicates` batch job: grouping near-identical items by their CLIP
embeddings (plan §6.7).

Every pair of active items whose embeddings' cosine similarity is at least
`DUPLICATE_SIMILARITY_THRESHOLD` is linked, and the connected components of two or
more items become groups. The similarities are computed a block of rows at a time
against the rows after them, so memory stays at `BLOCK_ROWS` by N floats.

Within a group, members are ranked for a keeper, best first: most pixels, then the
largest file, then the earliest capture date (undated last), then the lowest id.
Phase 5 ranks by quality score instead.

Grouping again keeps decisions. A component takes over the oldest existing group
it shares an item with, keeping its id. That group keeps its status, resolved or
skipped, while the component brings it no new member; one that does reopens it as
pending. Other groups the component absorbs are deleted. A pending or skipped group
that no component touches any more is deleted; a resolved one is kept as it was, as
the record of the decision.
"""

import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import sqlalchemy as sa
from numpy.typing import NDArray
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import DuplicateGroup, DuplicateMember, GroupStatus, Item
from photo_triage.pipeline.embed import from_blob
from photo_triage.worker.layout_schedule import embedded_items

logger = logging.getLogger(__name__)

BLOCK_ROWS = 256
"""Rows of the similarity matrix computed at once: 100 MB against 100k items."""


@dataclass(frozen=True)
class Grouping:
    groups: int
    """Groups after grouping, of every status."""
    items: int
    """Items in a group now."""
    pending: int


def similar_pairs(vectors: NDArray[np.float32], threshold: float) -> NDArray[np.int64]:
    """Each pair of rows (i < j) of unit `vectors` whose dot product is at least
    `threshold`, as an (n, 2) array."""
    count = len(vectors)
    found: list[NDArray[np.int64]] = []
    for start in range(0, count, BLOCK_ROWS):
        block = vectors[start : start + BLOCK_ROWS]
        sims = block @ vectors[start:].T
        rows, cols = np.nonzero(sims >= threshold)
        cols += start
        rows += start
        upper = cols > rows
        found.append(np.stack([rows[upper], cols[upper]], axis=1).astype(np.int64))
    return np.concatenate(found) if found else np.empty((0, 2), np.int64)


def components(count: int, pairs: Iterable[tuple[int, int]]) -> list[list[int]]:
    """The connected components of two or more of `count` nodes linked by `pairs`,
    each sorted, in order of their smallest node."""
    parent = list(range(count))

    def root(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for a, b in pairs:
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[int]] = {}
    for node in range(count):
        groups.setdefault(root(node), []).append(node)
    return [members for members in groups.values() if len(members) > 1]


def rank(items: Sequence[Item]) -> list[Item]:
    """`items`, best keeper first (see the module docstring)."""

    def key(item: Item) -> tuple[int, int, int, dt.datetime, int]:
        pixels = (item.width or 0) * (item.height or 0)
        taken = item.taken_at_local
        return (-pixels, -item.file_size_bytes, taken is None, taken or dt.datetime.min, item.id)

    return sorted(items, key=key)


def group(
    sessions: sessionmaker[Session], model: str, threshold: float, now: dt.datetime
) -> Grouping:
    """Group the active items embedded with `model`, and store the groups."""
    with sessions() as session:
        rows = session.execute(embedded_items(model)).all()
    ids = [item_id for item_id, _ in rows]
    if rows:
        vectors = np.stack([from_blob(blob) for _, blob in rows])
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        pairs = similar_pairs(vectors, threshold)
        del vectors
    else:
        pairs = np.empty((0, 2), np.int64)
    found = [
        [ids[i] for i in members]
        for members in components(len(ids), map(tuple, pairs.tolist()))  # pyright: ignore[reportArgumentType]
    ]
    with sessions.begin() as session:
        result = _store(session, found, now)
    logger.info(
        "Grouped duplicates at %.3f: %d pairs, %d groups (%d pending) of %d items",
        threshold,
        len(pairs),
        result.groups,
        result.pending,
        result.items,
    )
    return result


def _store(session: Session, found: list[list[int]], now: dt.datetime) -> Grouping:
    existing: dict[int, DuplicateGroup] = {
        g.id: g for g in session.scalars(sa.select(DuplicateGroup))
    }
    members: dict[int, set[int]] = {}
    group_of: dict[int, list[int]] = {}
    for group_id, item_id in session.execute(
        sa.select(DuplicateMember.group_id, DuplicateMember.item_id)
    ):
        members.setdefault(group_id, set()).add(item_id)
        group_of.setdefault(item_id, []).append(group_id)

    items = {
        i.id: i
        for chunk in _chunks([i for c in found for i in c], 500)
        for i in session.scalars(sa.select(Item).where(Item.id.in_(chunk)))
    }
    kept: set[int] = set()
    for component in found:
        overlapping = sorted({g for i in component for g in group_of.get(i, [])} - kept)
        new = set(component)
        if overlapping:
            group = existing[overlapping[0]]
            if not new <= members.get(group.id, set()):
                # A new member: the decision was about other items.
                group.status, group.resolved_at, group.actor = GroupStatus.PENDING, None, None
            for absorbed in overlapping[1:]:
                session.delete(existing.pop(absorbed))
                members.pop(absorbed, None)
            if members.get(group.id) != new:
                session.execute(
                    sa.delete(DuplicateMember).where(DuplicateMember.group_id == group.id)
                )
                session.flush()
                _add_members(session, group.id, component, items)
            else:
                _rerank(session, group.id, component, items)
        else:
            group = DuplicateGroup(status=GroupStatus.PENDING, created_at=now)
            session.add(group)
            session.flush()
            existing[group.id] = group
            _add_members(session, group.id, component, items)
        kept.add(group.id)
    for group_id, group in list(existing.items()):
        if group_id not in kept and group.status != GroupStatus.RESOLVED:
            session.delete(group)
            del existing[group_id]
    session.flush()
    pending = sum(g.status == GroupStatus.PENDING for g in existing.values())
    return Grouping(groups=len(existing), items=sum(map(len, found)), pending=pending)


def _add_members(
    session: Session, group_id: int, component: list[int], items: dict[int, Item]
) -> None:
    session.add_all(
        DuplicateMember(group_id=group_id, item_id=item.id, rank=n)
        for n, item in enumerate(rank([items[i] for i in component]))
    )


def _rerank(session: Session, group_id: int, component: list[int], items: dict[int, Item]) -> None:
    for n, item in enumerate(rank([items[i] for i in component])):
        member = session.get(DuplicateMember, (group_id, item.id))
        if member is not None and member.rank != n:
            member.rank = n


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
