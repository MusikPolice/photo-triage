"""The runner for each stage the worker can run (plan §6.11).

`walk` and `scan` always run. `noop` exists only when `WORKER_NOOP_STAGE` is on.
"""

import logging
import time
from collections.abc import Callable, Mapping

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from photo_triage.db.models import Job
from photo_triage.pipeline import scan
from photo_triage.settings import Settings
from photo_triage.worker.clock import Clock
from photo_triage.worker.queue import JobQueue, Stage

logger = logging.getLogger(__name__)

Runner = Callable[[Job], None]
"""Does one job's work, given the claimed job (detached from its session). Raises if
the work failed: the queue retries the job after a backoff, or parks it."""

NOOP_RUN_S = 0.2
"""How long a `noop` job takes, so progress is visible when exercising by hand."""


def run_noop(job: Job) -> None:
    time.sleep(NOOP_RUN_S)


def build_runners(
    settings: Settings, engine: Engine, queue: JobQueue, clock: Clock
) -> Mapping[str, Runner]:
    """The stages this worker runs. Jobs for any other stage stay in the queue."""
    sessions = sessionmaker(engine)

    def run_walk(job: Job) -> None:
        with sessions.begin() as session:
            result = scan.walk(session, queue, settings.photo_dir, settings.trash_dir, clock())
        logger.info(
            "Walked %s: %d supported files, %d new, %d changed, %d missing, %d found again",
            settings.photo_dir,
            result.found,
            result.new,
            result.changed,
            result.missing,
            result.returned,
        )

    def run_scan(job: Job) -> None:
        scan.scan(sessions, job, settings.photo_dir, clock())

    runners: dict[str, Runner] = {Stage.WALK: run_walk, Stage.SCAN: run_scan}
    if settings.worker_noop_stage:
        runners[Stage.NOOP] = run_noop
    return runners
