"""When the worker queues a layout fit (plan §6.4, §6.11).

A fit is **due** when the active items embedded with `CLIP_MODEL` aren't the ones
in the current layout, and either:

- the embedding pipeline has drained: no `walk`, `scan`, `metadata_read`,
  `frames`, `thumbnail` or `clip` job is open (pending, running or waiting to
  retry), so a nightly scan gets one fit, after its last item is embedded; or
- while it hasn't, enough items have been embedded since the last fit:
  `GROWTH_FRACTION` of the layout, and at least `GROWTH_MIN_ITEMS`. So the map
  appears during the first backfill and grows with it, in about ten fits over
  33k items, rather than dozens of tiny ones.

The worker checks after each job of those stages, and after each reconcile. The
drained check is one indexed lookup, and compares the two sets only once it
passes. The growth check counts both sets, so it runs at most once per
`GROWTH_CHECK_INTERVAL_S` of the worker's time.

Nothing here imports NumPy (see `photo_triage.worker.stages`).
"""

import math

import sqlalchemy as sa
from sqlalchemy.orm import Session

from photo_triage.db.models import (
    JOB_IS_OPEN,
    Embedding,
    Item,
    ItemStatus,
    Job,
    JobStatus,
    Layout,
)
from photo_triage.worker.queue import JobQueue, Stage

EMBEDDING_PIPELINE = (
    Stage.WALK,
    Stage.SCAN,
    Stage.METADATA_READ,
    Stage.FRAMES,
    Stage.THUMBNAIL,
    Stage.CLIP,
)
"""The stages that lead to an item's embedding."""

GROWTH_FRACTION = 0.25
GROWTH_MIN_ITEMS = 1000
GROWTH_CHECK_INTERVAL_S = 60.0


def embedded_items(model: str) -> sa.Select[int, bytes]:
    """Each active item embedded with `model`, and its vector, by item id."""
    return (
        sa.select(Item.id, Embedding.vector)
        .join(Embedding, Embedding.item_id == Item.id)
        .where(Item.status == ItemStatus.ACTIVE, Embedding.model == model)
        .order_by(Item.id)
    )


def latest_version() -> sa.Select[int | None]:
    """The current layout's version, or None if there's none. There's only ever one
    version (`photo_triage.pipeline.layout`), but this reads the newest to be safe."""
    return sa.select(sa.func.max(Layout.layout_version))


def drained(session: Session) -> bool:
    """Whether no job in `EMBEDDING_PIPELINE` is open."""
    return not session.scalar(
        sa.select(sa.exists().where(Job.stage.in_(EMBEDDING_PIPELINE), sa.text(JOB_IS_OPEN)))
    )


def _embedded_ids(model: str) -> sa.Select[int]:
    return (
        sa.select(Item.id)
        .join(Embedding, Embedding.item_id == Item.id)
        .where(Item.status == ItemStatus.ACTIVE, Embedding.model == model)
    )


def _laid_out_ids(session: Session) -> sa.Select[int]:
    version = session.scalar(latest_version())
    return sa.select(Layout.item_id).where(Layout.layout_version == (version or 0))


def changed(session: Session, model: str) -> bool:
    """Whether the embedded active items differ from the current layout's."""
    embedded, laid_out = _embedded_ids(model), _laid_out_ids(session)
    return bool(
        session.scalar(sa.select(sa.exists(embedded.except_(laid_out))))
        or session.scalar(sa.select(sa.exists(laid_out.except_(embedded))))
    )


def grown(session: Session, model: str) -> bool:
    """Whether enough items have been embedded since the last fit to fit again
    before the pipeline drains."""
    embedded = session.scalar(
        sa.select(sa.func.count()).select_from(_embedded_ids(model).subquery())
    )
    laid_out = session.scalar(
        sa.select(sa.func.count()).select_from(_laid_out_ids(session).subquery())
    )
    assert embedded is not None and laid_out is not None
    needed = max(GROWTH_MIN_ITEMS, math.ceil(GROWTH_FRACTION * laid_out))
    return embedded - laid_out >= needed


def layout_waiting(session: Session) -> bool:
    """Whether a fit is already queued, or one has failed too often and is parked.
    A parked fit waits for someone to retry it, rather than being queued again after
    every job."""
    return bool(
        session.scalar(
            sa.select(
                sa.exists().where(
                    Job.stage == Stage.LAYOUT,
                    sa.or_(sa.text(JOB_IS_OPEN), Job.status == JobStatus.PARKED),
                )
            )
        )
    )


def queue_if_due(session: Session, queue: JobQueue, model: str, *, check_growth: bool) -> bool:
    """Queue a layout fit if one is due and none is waiting (`layout_waiting`).
    Returns whether it queued one. Without `check_growth`, only the drained rule is
    checked."""
    if layout_waiting(session):
        return False
    due = changed(session, model) if drained(session) else check_growth and grown(session, model)
    if due:
        queue.enqueue(session, Stage.LAYOUT)
    return due
