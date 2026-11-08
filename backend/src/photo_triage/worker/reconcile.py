"""Reconcile each item stage with what it has already produced (plan §6.11).

Stages queue the next one when they finish, so new items flow straight through.
Reconcile is the backfill and the safety net. It queues every item stage that's
**due** for an item:

- the item is active;
- every upstream stage has a done job for it;
- the stage has no parked job at its current version, and no done job at its
  current version that finished after every upstream stage's output last changed.

So deploying a new stage backfills the library, bumping a stage's version (a fix,
or a setting its output depends on) re-runs it everywhere, and a job lost between
two stages is queued again. A parked job stays parked until the version changes.

It runs at worker startup, after each walk, and by hand with `just worker
reconcile`. Each stage's jobs go in with one statement, in its own transaction,
upstream stages first. An open job for the same stage and item is left alone,
through the unique open-job index.
"""

import datetime as dt
import graphlib
import logging
from collections.abc import Mapping

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import aliased, sessionmaker

from photo_triage.db.columns import UTCDateTime
from photo_triage.db.models import Item, ItemStatus, Job, JobStatus
from photo_triage.worker.queue import PRIORITY, Stage
from photo_triage.worker.stages import ItemStage

logger = logging.getLogger(__name__)


class RegistryError(Exception):
    """The item stages name an upstream that isn't one, or depend on each other."""


def ordered(stages: Mapping[str, ItemStage]) -> list[str]:
    """The item stages, each after its upstream stages."""
    graph = {name: stage.upstream for name, stage in stages.items()}
    for name, upstream in graph.items():
        if missing := [u for u in upstream if u not in stages]:
            raise RegistryError(f"{name} reads {', '.join(missing)}, which isn't an item stage")
    try:
        return list(graphlib.TopologicalSorter(graph).static_order())
    except graphlib.CycleError as e:
        raise RegistryError(f"item stages depend on each other: {e.args[1]}") from e


def due(name: str, stage: ItemStage) -> sa.Select[int]:
    """Ids of the active items for which `stage` is due, as described above."""
    upstream_done = [aliased(Job, name=f"upstream_{n}") for n in range(len(stage.upstream))]
    current = aliased(Job, name="current")
    query = sa.select(Item.id).where(Item.status == ItemStatus.ACTIVE)
    for upstream, job in zip(stage.upstream, upstream_done, strict=True):
        query = query.where(
            sa.exists().where(
                job.item_id == Item.id, job.stage == upstream, job.status == JobStatus.DONE
            )
        )
    newer_upstream_output = [
        sa.exists().where(
            job.item_id == current.item_id,
            job.stage == upstream,
            job.status == JobStatus.DONE,
            job.output_changed_at > current.finished_at,
        )
        for upstream, job in zip(stage.upstream, upstream_done, strict=True)
    ]
    return query.where(
        ~sa.exists().where(
            current.item_id == Item.id,
            current.stage == name,
            current.stage_version == stage.version,
            sa.or_(
                current.status == JobStatus.PARKED,
                sa.and_(current.status == JobStatus.DONE, *(~e for e in newer_upstream_output)),
            ),
        ),
        # Open jobs are skipped by the unique index; a running one is skipped here.
        ~sa.exists().where(
            current.item_id == Item.id,
            current.stage == name,
            current.status == JobStatus.RUNNING,
        ),
    )


def reconcile(engine: Engine, stages: Mapping[str, ItemStage], now: dt.datetime) -> dict[str, int]:
    """Queue every due item stage, and return how many jobs each got."""
    sessions = sessionmaker(engine)
    queued: dict[str, int] = {}
    for name in ordered(stages):
        due_items = due(name, stages[name]).subquery()
        insert = (
            sa.insert(Job)
            .from_select(
                ["item_id", "stage", "priority", "status", "attempts", "enqueued_at"],
                sa.select(
                    due_items.c.id,
                    sa.literal(name, Job.stage.type),
                    sa.literal(PRIORITY[Stage(name)]),
                    sa.literal(JobStatus.PENDING, Job.status.type),
                    sa.literal(0),
                    sa.literal(now, UTCDateTime()),
                ),
            )
            .prefix_with("OR IGNORE")  # an open job for the item is already queued
        )
        with sessions.begin() as session:
            result: CursorResult[tuple[()]] = session.execute(insert)  # pyright: ignore[reportAssignmentType]
            queued[name] = result.rowcount
    if any(queued.values()):
        logger.info(
            "Reconciled stages, queued %s",
            ", ".join(f"{n} {name}" for name, n in queued.items() if n),
        )
    else:
        logger.info("Reconciled stages, nothing due")
    return queued
