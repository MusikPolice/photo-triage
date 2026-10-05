"""The worker loop: claim the next job, run it, record the result (plan §6.11).

There's a single worker. Each job takes three short transactions: the claim is
committed before the job runs, and the result straight after, so the API and the
scanner can write to the database while a job runs. If the process dies mid-run, the
job stays `running` until `recover` puts it back at the next start.
"""

import logging
import threading
import time
from collections import Counter
from collections.abc import Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from photo_triage.db.models import JOB_IS_OPEN, Job, JobStatus
from photo_triage.worker.clock import Timer
from photo_triage.worker.queue import JobQueue
from photo_triage.worker.stages import Runner

logger = logging.getLogger(__name__)

SUMMARY_INTERVAL_S = 300.0
"""How often a progress summary is logged at INFO while jobs are running."""

IDLE_POLL_S = 2.0
"""How long the worker waits before looking again when there's nothing to run."""


class Worker:
    def __init__(
        self,
        engine: Engine,
        runners: Mapping[str, Runner],
        *,
        queue: JobQueue | None = None,
        timer: Timer = time.monotonic,
        summary_interval_s: float = SUMMARY_INTERVAL_S,
    ) -> None:
        self._sessions = sessionmaker(engine, expire_on_commit=False)
        self._runners = dict(runners)
        self._queue = queue or JobQueue()
        self._timer = timer
        self._progress = _Progress(timer, summary_interval_s)

    def recover(self) -> int:
        """Count jobs left running when the worker last stopped as failed attempts,
        requeueing or parking them. Call at startup. Returns how many there were."""
        with self._sessions.begin() as session:
            jobs = self._queue.recover(session)
        for job in jobs:
            if job.status == JobStatus.PARKED:
                # No traceback: the process that ran it is gone.
                logger.error(
                    "Parked %s after %d failed attempts, the last interrupted when the "
                    "worker stopped",
                    _describe(job),
                    job.attempts,
                )
            else:
                logger.warning(
                    "%s was interrupted when the worker stopped (attempt %d of %d), queued again",
                    _describe(job).capitalize(),
                    job.attempts,
                    self._queue.max_attempts,
                )
        return len(jobs)

    def run_one(self) -> bool:
        """Claim the next job for a stage this worker runs, run it, and record the
        result. Returns False if there was nothing to run."""
        with self._sessions.begin() as session:
            job = self._queue.claim(session, stages=list(self._runners))
        if job is None:
            return False

        logger.debug("Running %s", _describe(job))
        started_s = self._timer()
        try:
            self._runners[job.stage](job)
        except Exception as exc:
            self._record_failure(job, exc, self._timer() - started_s)
        else:
            duration_s = self._timer() - started_s
            with self._sessions.begin() as session:
                self._queue.complete(session, job.id, duration_s)
            logger.debug("Finished %s in %.3f s", _describe(job), duration_s)
            self._progress.processed[job.stage] += 1
        self._summarize_if_due()
        return True

    def drain(self) -> int:
        """Run jobs until none is ready (`--once`). Returns how many ran.

        Jobs waiting out a backoff aren't ready, so they're left for a later run.
        """
        count = 0
        while self.run_one():
            count += 1
        self._summarize()
        return count

    def run(self, stop: threading.Event, idle_poll_s: float = IDLE_POLL_S) -> None:
        """Run jobs until `stop` is set. The job in progress is finished first."""
        while not stop.is_set():
            if not self.run_one():
                self._summarize_if_due()
                stop.wait(idle_poll_s)
        self._summarize()

    def _record_failure(self, job: Job, exc: Exception, duration_s: float) -> None:
        with self._sessions.begin() as session:
            status = self._queue.fail(session, job.id, exc, duration_s)
            failed = session.get_one(Job, job.id)
            attempts, retry_at = failed.attempts, failed.retry_at
        self._progress.errors[job.stage] += 1
        if status == JobStatus.PARKED:
            logger.error(
                "Parked %s after %d failed attempts", _describe(job), attempts, exc_info=exc
            )
        else:
            assert retry_at is not None
            logger.warning(
                "%s failed (attempt %d of %d), retrying after %s: %s",
                _describe(job).capitalize(),
                attempts,
                self._queue.max_attempts,
                retry_at.isoformat().replace("+00:00", "Z"),
                _one_line(exc),
            )

    def _summarize_if_due(self) -> None:
        if self._progress.due():
            self._summarize()

    def _summarize(self) -> None:
        """Log what was done since the last summary, if anything."""
        progress = self._progress
        if not progress.processed and not progress.errors:
            progress.restart()
            return
        stages = sorted(progress.processed.keys() | progress.errors.keys())
        done = "; ".join(
            f"{stage} {progress.processed[stage]} done, {progress.errors[stage]} failed"
            for stage in stages
        )
        with self._sessions() as session:
            open_jobs = session.scalar(
                sa.select(sa.func.count()).select_from(Job).where(sa.text(JOB_IS_OPEN))
            )
        logger.info(
            "Progress in the last %.0f s: %s. %d job(s) waiting.",
            progress.elapsed_s(),
            done,
            open_jobs,
        )
        progress.restart()


class _Progress:
    """Runs per stage since the last summary."""

    def __init__(self, timer: Timer, interval_s: float) -> None:
        self._timer = timer
        self._interval_s = interval_s
        self.restart()

    def restart(self) -> None:
        self.processed: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self._since_s = self._timer()

    def elapsed_s(self) -> float:
        return self._timer() - self._since_s

    def due(self) -> bool:
        return self.elapsed_s() >= self._interval_s


def _describe(job: Job) -> str:
    item = "batch" if job.item_id is None else f"item {job.item_id}"
    return f"{job.stage} job {job.id} ({item})"


def _one_line(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())
