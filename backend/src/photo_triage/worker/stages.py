"""The runner for each stage the worker can run (plan §6.11).

The real stages arrive in Phase 2. Until then only `noop` exists, and only when
`WORKER_NOOP_STAGE` is on.
"""

import time
from collections.abc import Callable, Mapping

from photo_triage.db.models import Job
from photo_triage.settings import Settings
from photo_triage.worker.queue import Stage

Runner = Callable[[Job], None]
"""Does one job's work, given the claimed job (detached from its session). Raises if
the work failed: the queue retries the job after a backoff, or parks it."""

NOOP_RUN_S = 0.2
"""How long a `noop` job takes, so progress is visible when exercising by hand."""


def run_noop(job: Job) -> None:
    time.sleep(NOOP_RUN_S)


def build_runners(settings: Settings) -> Mapping[str, Runner]:
    """The stages this worker runs. Jobs for any other stage stay in the queue."""
    runners: dict[str, Runner] = {}
    if settings.worker_noop_stage:
        runners[Stage.NOOP] = run_noop
    return runners
