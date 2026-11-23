"""When the worker queues the `duplicates` batch job (plan §6.7, §6.11).

Grouping is due whenever a layout fit has finished since the last grouping, or there
has been none: a fit is queued whenever the embedded active items change
(`photo_triage.worker.layout_schedule`), which is when the groups can change too.
The worker checks after each layout job and each reconcile, so a library laid out
before grouping existed is grouped at the next start.

Grouping runs at its own priority, after CLIP and quality, so during a backfill it
waits until the embeddings are done.
"""

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.orm import Session

from photo_triage.db.models import JOB_IS_OPEN, Job, JobStatus
from photo_triage.worker.queue import JobQueue, Stage


def _last_done(session: Session, stage: Stage) -> dt.datetime | None:
    return session.scalar(
        sa.select(sa.func.max(Job.finished_at)).where(
            Job.stage == stage, Job.status == JobStatus.DONE
        )
    )


def due(session: Session) -> bool:
    """Whether a layout fit has finished since the last grouping, or there's been
    no grouping since a fit."""
    laid_out = _last_done(session, Stage.LAYOUT)
    grouped = _last_done(session, Stage.DUPLICATES)
    return laid_out is not None and (grouped is None or grouped < laid_out)


def waiting(session: Session) -> bool:
    """Whether a grouping is queued already, or has failed too often and is parked."""
    return bool(
        session.scalar(
            sa.select(
                sa.exists().where(
                    Job.stage == Stage.DUPLICATES,
                    sa.or_(sa.text(JOB_IS_OPEN), Job.status == JobStatus.PARKED),
                )
            )
        )
    )


def queue_if_due(session: Session, queue: JobQueue) -> bool:
    """Queue a grouping if one is due and none is waiting. Returns whether it did."""
    if waiting(session) or not due(session):
        return False
    queue.enqueue(session, Stage.DUPLICATES)
    return True
