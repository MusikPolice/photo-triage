"""When the worker queues the `purge` batch job (plan §6.8, §6.11): once a day.

The purge is due when none is open and none has been queued or finished in the last
`PURGE_INTERVAL`, so the worker purges when it starts and a day after each run. A
purge that failed until it was parked is tried again a day after it was queued,
rather than straight away. Retention is counted in days, so running daily keeps an
item in the trash at most a day past its `purge_after`.

The worker checks before each claim, like the scheduled walk, but looks in the
database only once the next purge is due.
"""

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.orm import Session

from photo_triage.db.models import Job, JobStatus
from photo_triage.worker.queue import JobQueue, Stage

PURGE_INTERVAL = dt.timedelta(days=1)


def last_seen(session: Session) -> dt.datetime | None:
    """When a purge was last queued or finished, or None if there's never been one."""
    enqueued, finished = session.execute(
        sa.select(sa.func.max(Job.enqueued_at), sa.func.max(Job.finished_at)).where(
            Job.stage == Stage.PURGE
        )
    ).one()
    times = [t for t in (enqueued, finished) if t is not None]
    return max(times) if times else None


def next_at(last_finished_at: dt.datetime | None) -> dt.datetime | None:
    """When the next purge is due, for the Activity page: a day after the last run
    finished, or None before the first, which the worker queues when it starts. It
    doesn't move with the clock, so an unchanged page isn't sent again."""
    return None if last_finished_at is None else last_finished_at + PURGE_INTERVAL


UNFINISHED = (JobStatus.PENDING, JobStatus.ERROR, JobStatus.RUNNING)


def is_open(session: Session) -> bool:
    """Whether a purge is waiting, retrying or running."""
    return bool(
        session.scalar(
            sa.select(sa.exists().where(Job.stage == Stage.PURGE, Job.status.in_(UNFINISHED)))
        )
    )


def queue_if_due(session: Session, queue: JobQueue, now: dt.datetime) -> dt.datetime | None:
    """Queue a purge if one is due. Returns when it's next due, or None if one is
    open (or was just queued), so it's due a day after that one."""
    if is_open(session):
        return None
    last = last_seen(session)
    if last is not None and now < last + PURGE_INTERVAL:
        return last + PURGE_INTERVAL
    queue.enqueue(session, Stage.PURGE)
    return None
