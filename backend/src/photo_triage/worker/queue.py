"""The persistent, priority-ordered job queue on the `jobs` table (plan §6.11).

A job's life:

    pending ─claim─► running ─complete─► done
                        │
                       fail ─► error ─(backoff passes)─claim─► running ...
                        │
                        └─(after `max_attempts` failures)─► parked ─retry─► pending

Every state lives in SQLite, so nothing is lost when the process stops. Only the
latest done job is kept for each stage and item (see `complete`).

Methods take the caller's `Session` and don't commit, so enqueueing can share a
transaction with the change that caused it. Keep transactions short: SQLite has
one write lock, so the worker commits a claim before running the job (see `claim`).

`complete` and `fail` also add the run to its hour's `job_stats` row, in the same
transaction as the job's new status, so the two can't disagree after a crash.
"""

import datetime as dt
import traceback
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from photo_triage.db.models import JOB_IS_OPEN, Job, JobStats, JobStatus
from photo_triage.worker.clock import Clock, utc_now


class Stage(StrEnum):
    METADATA_WRITE = "metadata_write"
    SCAN = "scan"
    THUMBNAIL = "thumbnail"
    CLIP = "clip"
    QUALITY = "quality"
    FACES = "faces"
    RECOGNIZE = "recognize"
    LAYOUT = "layout"
    DUPLICATES = "duplicates"
    ATLASES = "atlases"
    LLM_TAG = "llm_tag"
    NOOP = "noop"
    """Does nothing, for exercising the worker by hand. Runs only when enabled
    (`WORKER_NOOP_STAGE`)."""


PRIORITY: dict[Stage, int] = {
    Stage.METADATA_WRITE: 0,
    Stage.SCAN: 1,
    Stage.THUMBNAIL: 2,
    Stage.CLIP: 3,
    Stage.QUALITY: 4,
    Stage.FACES: 5,
    Stage.RECOGNIZE: 5,
    Stage.LAYOUT: 6,  # batch jobs
    Stage.DUPLICATES: 6,
    Stage.ATLASES: 6,
    Stage.LLM_TAG: 7,
    Stage.NOOP: 8,
}
"""Lower runs first: metadata writes > scan > thumbnails > CLIP > quality > faces >
batch jobs > LLM tagging, so the map and search become usable first."""

BATCH_STAGES = frozenset({Stage.LAYOUT, Stage.DUPLICATES, Stage.ATLASES, Stage.NOOP})
"""Stages whose jobs cover the whole library rather than one item."""

LAST_ERROR_MAX_CHARS = 4096

INTERRUPTED_ERROR = (
    "Interrupted: the worker stopped while running this job "
    "(killed, crashed, or ran out of memory)."
)


class JobStateError(Exception):
    """The job isn't in a state that allows the requested transition."""


@dataclass(frozen=True)
class RetryPolicy:
    """A failed job waits `first_backoff`, doubling after each further failure up to
    `max_backoff`. It's parked once it has failed `max_attempts` times."""

    max_attempts: int = 5
    first_backoff: dt.timedelta = dt.timedelta(minutes=1)
    max_backoff: dt.timedelta = dt.timedelta(hours=1)

    def backoff(self, attempts: int) -> dt.timedelta:
        """The wait after the `attempts`-th failure."""
        wait = self.first_backoff
        for _ in range(attempts - 1):
            if wait >= self.max_backoff:
                break  # doubling without limit would overflow timedelta
            wait *= 2
        return min(wait, self.max_backoff)


def describe_error(exc: BaseException) -> str:
    """The exception and its traceback, for `last_error`.

    Long tracebacks keep their end, where the exception and the innermost frames are.
    """
    text = "".join(traceback.format_exception(exc)).rstrip()
    if len(text) <= LAST_ERROR_MAX_CHARS:
        return text
    return "…" + text[-(LAST_ERROR_MAX_CHARS - 1) :]


def _same_item(item_id: int | None) -> sa.ColumnElement[bool]:
    """Jobs for `item_id`, or batch jobs when it's None."""
    return Job.item_id.is_(None) if item_id is None else Job.item_id == item_id


class JobQueue:
    def __init__(self, clock: Clock = utc_now, retry_policy: RetryPolicy | None = None) -> None:
        self._clock = clock
        self._policy = retry_policy or RetryPolicy()

    def enqueue(self, session: Session, stage: Stage, item_id: int | None = None) -> int:
        """Queue `stage` for an item, or as a batch job when `item_id` is None.

        If the same stage is already open (pending, or waiting to retry) for that
        item, nothing is added. Returns the id of the open job either way. `noop` jobs
        are never merged, so queueing N of them for a test load gives N jobs.
        """
        if stage != Stage.NOOP:
            existing = self._open_job_id(session, stage, item_id)
            if existing is not None:
                return existing
        inserted = session.scalar(
            insert(Job)
            .values(
                item_id=item_id,
                stage=stage,
                priority=PRIORITY[stage],
                status=JobStatus.PENDING,
                attempts=0,
                enqueued_at=self._clock(),
            )
            .on_conflict_do_nothing()
            .returning(Job.id)
        )
        if inserted is not None:
            return inserted
        # Another connection queued it between our check and the insert.
        existing = self._open_job_id(session, stage, item_id)
        assert existing is not None
        return existing

    def claim(self, session: Session, stages: Collection[str] | None = None) -> Job | None:
        """Mark the next runnable job as running and return it, or None if there's
        nothing to do. Highest priority first, then oldest first. With `stages`, only
        jobs for those stages are considered.

        Commit before running the job, and again after `complete` or `fail`. Until
        then this transaction holds SQLite's only write lock, and every other writer
        (the API, the scanner enqueueing) fails after `BUSY_TIMEOUT_MS`.
        """
        now = self._clock()
        claimable = self.claimable(now)
        if stages is not None:
            claimable = claimable.where(Job.stage.in_(stages))
        next_job = claimable.limit(1).scalar_subquery()
        return session.scalars(
            sa.update(Job)
            .where(Job.id == next_job)
            .values(status=JobStatus.RUNNING, started_at=now, finished_at=None, retry_at=None)
            .returning(Job),
            execution_options={"synchronize_session": False},
        ).one_or_none()

    @staticmethod
    def claimable(now: dt.datetime) -> sa.Select[int]:
        """Ids of the jobs that may run at `now`, in the order they should.

        The status condition is literal SQL so SQLite serves this from
        `ix_jobs_open_by_priority`.
        """
        return (
            sa.select(Job.id)
            .where(sa.text(JOB_IS_OPEN), sa.or_(Job.retry_at.is_(None), Job.retry_at <= now))
            .order_by(Job.priority, Job.enqueued_at, Job.id)
        )

    def recover(self, session: Session) -> list[Job]:
        """Deal with jobs left running by a worker that stopped mid-run, and return
        them in their new state.

        Each interrupted run counts as a failed attempt, so a job that kills the
        worker every time (running it out of memory, say) is parked after
        `max_attempts` rather than retried forever. Until then the job goes back to
        pending in its old place, with no backoff, so a restart costs no time. How
        long it ran is unknown, so its `job_stats` row gets an error and no busy time.

        Call it only at worker startup: there's a single worker, so at that point
        nothing is really running.
        """
        jobs = session.scalars(
            sa.select(Job).where(Job.status == JobStatus.RUNNING).order_by(Job.id),
            execution_options={"populate_existing": True},  # claim() bypasses the session
        ).all()
        now = self._clock()
        for job in jobs:
            job.attempts += 1
            job.last_error = INTERRUPTED_ERROR
            self._record_stats(session, job.stage, now, processed=0, errors=1, duration_s=0.0)
            if job.attempts >= self._policy.max_attempts:
                job.status = JobStatus.PARKED
                job.finished_at = now
            else:
                job.status = JobStatus.PENDING
                job.started_at = None
        session.flush()
        return list(jobs)

    def complete(self, session: Session, job_id: int, duration_s: float) -> None:
        """Mark a running job done, and delete older done jobs for the same stage
        and item (or batch stage). `duration_s` is how long the run took.

        Counting done jobs then counts items finished, not runs, and the table can't
        grow past one done row per stage per item. `job_stats` keeps the history.
        Noop jobs are kept: each stands for a unit of work of its own, so the
        Activity page counts them as they finish.
        """
        job = self._get(session, job_id, JobStatus.RUNNING)
        now = self._clock()
        job.status = JobStatus.DONE
        job.finished_at = now
        self._record_stats(session, job.stage, now, processed=1, errors=0, duration_s=duration_s)
        if job.stage == Stage.NOOP:
            session.flush()
            return
        session.execute(
            sa.delete(Job).where(
                Job.status == JobStatus.DONE,
                Job.stage == job.stage,
                _same_item(job.item_id),
                Job.id != job.id,
            )
        )
        session.flush()

    def fail(
        self, session: Session, job_id: int, exc: BaseException, duration_s: float
    ) -> JobStatus:
        """Record a failed run that took `duration_s`. Returns `ERROR` if the job will
        be retried after its backoff, or `PARKED` if it has used up its attempts."""
        job = self._get(session, job_id, JobStatus.RUNNING)
        now = self._clock()
        job.attempts += 1
        job.last_error = describe_error(exc)
        job.finished_at = now
        self._record_stats(session, job.stage, now, processed=0, errors=1, duration_s=duration_s)
        if job.attempts >= self._policy.max_attempts:
            job.status = JobStatus.PARKED
        else:
            job.status = JobStatus.ERROR
            job.retry_at = now + self._policy.backoff(job.attempts)
        session.flush()
        return job.status

    def retry(self, session: Session, job_id: int) -> int:
        """Put a parked job back in the queue, behind the open jobs of its priority,
        with a fresh set of attempts.

        `last_error` is kept until the job next fails. If the same stage has been
        queued again for the item meanwhile, the parked job is dropped in favour of
        that one. Returns the id of the job that's now open.
        """
        job = self._get(session, job_id, JobStatus.PARKED)
        stage = Stage(job.stage)
        if (existing := self._open_job_id(session, stage, job.item_id)) is not None:
            session.delete(job)
            session.flush()
            return existing
        job.status = JobStatus.PENDING
        job.attempts = 0
        job.retry_at = None
        job.enqueued_at = self._clock()
        job.started_at = None
        job.finished_at = None
        session.flush()
        return job.id

    @property
    def max_attempts(self) -> int:
        return self._policy.max_attempts

    @staticmethod
    def _record_stats(
        session: Session,
        stage: str,
        finished_at: dt.datetime,
        *,
        processed: int,
        errors: int,
        duration_s: float,
    ) -> None:
        """Add a run to the `job_stats` row for its stage and the hour it finished."""
        hour_start_at = finished_at.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
        row = insert(JobStats).values(
            hour_start_at=hour_start_at,
            stage=stage,
            processed=processed,
            errors=errors,
            busy_seconds=duration_s,
        )
        session.execute(
            row.on_conflict_do_update(
                index_elements=[JobStats.hour_start_at, JobStats.stage],
                set_={
                    JobStats.processed: JobStats.processed + row.excluded.processed,
                    JobStats.errors: JobStats.errors + row.excluded.errors,
                    JobStats.busy_seconds: JobStats.busy_seconds + row.excluded.busy_seconds,
                },
            )
        )

    @staticmethod
    def _open_job_id(session: Session, stage: Stage, item_id: int | None) -> int | None:
        return session.scalar(
            sa.select(Job.id).where(
                sa.text(JOB_IS_OPEN),
                Job.stage == stage,
                _same_item(item_id),
            )
        )

    @staticmethod
    def _get(session: Session, job_id: int, expected: JobStatus) -> Job:
        job = session.get(Job, job_id, populate_existing=True)
        if job is None:
            raise JobStateError(f"job {job_id} doesn't exist")
        if job.status != expected:
            raise JobStateError(f"job {job_id} is {job.status}, not {expected}")
        return job
