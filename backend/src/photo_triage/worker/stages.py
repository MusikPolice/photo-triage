"""The runner for each stage the worker can run, and the item stages that reconcile
keeps current (plan §6.11).

`walk` and `scan` always run. `noop` exists only when `WORKER_NOOP_STAGE` is on.
"""

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from photo_triage.db.models import Job
from photo_triage.pipeline import scan
from photo_triage.settings import Settings
from photo_triage.worker.clock import Clock
from photo_triage.worker.queue import JobQueue, Stage

logger = logging.getLogger(__name__)

Runner = Callable[[Job], bool | None]
"""Does one job's work, given the claimed job (detached from its session). Raises if
the work failed: the queue retries the job after a backoff, or parks it.

Returns False if the run left the item's output as the last run made it, so the
stages that read it needn't run again. True or None means it may have changed."""


@dataclass(frozen=True)
class ItemStage:
    """A stage that runs once per item, which `photo_triage.worker.reconcile` keeps
    current: it's due for every active item whose upstream stages are done, until it
    has a done job at `version` that's newer than its upstream stages' output.

    Bump `code_version` when a fix or improvement should re-run the stage on the
    whole library. `settings_fingerprint` holds the settings its output depends on
    (`CLIP_MODEL` for CLIP), so changing one re-runs it too.
    """

    code_version: int
    upstream: tuple[Stage, ...] = ()
    """Stages whose output this one reads. Each must be an item stage too."""
    settings_fingerprint: str = ""

    @property
    def version(self) -> str:
        """As recorded on its jobs: `3`, or `3+ViT-B-32` with a fingerprint."""
        if not self.settings_fingerprint:
            return str(self.code_version)
        return f"{self.code_version}+{self.settings_fingerprint}"


def item_stages(settings: Settings) -> Mapping[str, ItemStage]:
    """The item stages, with their versions. Each later stage's issue adds its own."""
    return {Stage.SCAN: ItemStage(code_version=1)}


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

    def run_scan(job: Job) -> bool:
        return scan.scan(sessions, job, settings.photo_dir, clock())

    runners: dict[str, Runner] = {Stage.WALK: run_walk, Stage.SCAN: run_scan}
    if settings.worker_noop_stage:
        runners[Stage.NOOP] = run_noop
    return runners
