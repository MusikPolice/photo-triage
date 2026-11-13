"""Walks of the library on `SCAN_SCHEDULE`, and by hand (plan §6.1).

The schedule needs no state of its own: the `walk` jobs say whether the latest
scheduled time has been seen to. It has if a walk was queued or finished at or
after that time, or one is still to finish. So a time missed while the worker was
stopped gets one walk when it starts, however many times were missed, and a walk
already open when a time comes isn't doubled.

The worker queues the walk whatever its state. Quiet hours and pauses only delay
running it.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session

from photo_triage.db.models import Job, JobStatus
from photo_triage.schedule import Schedule
from photo_triage.worker.queue import JobQueue, Stage

UNFINISHED = (JobStatus.PENDING, JobStatus.ERROR, JobStatus.RUNNING)
"""A walk in one of these will still run, or is running."""


def walk_unfinished(session: Session) -> bool:
    return (
        session.scalar(
            sa.select(Job.id).where(Job.stage == Stage.WALK, Job.status.in_(UNFINISHED)).limit(1)
        )
        is not None
    )


def queue_walk(session: Session, queue: JobQueue, actor: str | None = None) -> int | None:
    """Queue a walk, unless one is waiting or running. Returns its id, or None."""
    if walk_unfinished(session):
        return None
    return queue.enqueue(session, Stage.WALK, actor=actor)


def scheduled_walk_due(
    session: Session, schedule: Schedule, zone: ZoneInfo, now: dt.datetime
) -> dt.datetime | None:
    """The scheduled time, if a walk is due for it at `now`: no walk has been
    queued or finished since the latest scheduled time, and none is unfinished."""
    due_at = schedule.previous_at(now, zone)
    seen_to = session.scalar(
        sa.select(Job.id)
        .where(
            Job.stage == Stage.WALK,
            sa.or_(
                Job.status.in_(UNFINISHED),
                Job.enqueued_at >= due_at,
                Job.finished_at >= due_at,
            ),
        )
        .limit(1)
    )
    return due_at if seen_to is None else None


def last_walk(session: Session) -> Job | None:
    """The latest finished walk. The queue keeps only one done walk."""
    return session.scalar(
        sa.select(Job)
        .where(Job.stage == Stage.WALK, Job.status == JobStatus.DONE)
        .order_by(Job.finished_at.desc())
        .limit(1)
    )
