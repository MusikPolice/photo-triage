"""The runner for each stage the worker can run, and the item stages that reconcile
keeps current (plan §6.11).

`walk`, `scan`, `metadata_read`, `frames`, `thumbnail` and `clip` always run. `noop`
exists only when `WORKER_NOOP_STAGE` is on.

Nothing here imports NumPy or ONNX Runtime until a job needs them, so the worker can
apply `WORKER_THREADS` first (`photo_triage.worker.__main__.limit_threads`).
"""

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from photo_triage.db.models import Job, MediaType
from photo_triage.files import MetadataReader
from photo_triage.pipeline import frames, metadata, scan, thumbnails
from photo_triage.settings import Settings
from photo_triage.worker.clock import Clock
from photo_triage.worker.queue import JobQueue, Stage

if TYPE_CHECKING:
    from photo_triage.ml import ClipEmbedder

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
    """Stages whose output this one reads. Each must be an item stage too. One that
    doesn't apply to an item (`media_types`) isn't waited for."""
    settings_fingerprint: str = ""
    media_types: frozenset[MediaType] | None = None
    """The kinds of item it runs for, if not every kind (`frames`: videos)."""

    @property
    def version(self) -> str:
        """As recorded on its jobs: `3`, or `3+ViT-B-32` with a fingerprint."""
        if not self.settings_fingerprint:
            return str(self.code_version)
        return f"{self.code_version}+{self.settings_fingerprint}"


def item_stages(settings: Settings) -> Mapping[str, ItemStage]:
    """The item stages, with their versions. Each later stage's issue adds its own."""
    return {
        # 2: scans damaged photos and records them (#95), so those parked at 1 run again.
        Stage.SCAN: ItemStage(code_version=2),
        # Separate from scan, so a new field to import re-reads every file without
        # hashing it again.
        # 2: records the orientation.
        Stage.METADATA_READ: ItemStage(code_version=2, upstream=(Stage.SCAN,)),
        # 2: times end where the last frame starts, for clips under a second (#96).
        Stage.FRAMES: ItemStage(
            code_version=2,
            upstream=(Stage.SCAN,),
            settings_fingerprint=f"{settings.video_sample_frames}-frames",
            media_types=frozenset({MediaType.VIDEO}),
        ),
        # Metadata for the orientation; frames for a video's picture.
        Stage.THUMBNAIL: ItemStage(
            code_version=1,
            upstream=(Stage.SCAN, Stage.METADATA_READ, Stage.FRAMES),
            settings_fingerprint=f"{settings.thumbnail_size}px",
        ),
        # The photo, turned by its orientation, or a video's frames. After the
        # thumbnails, which the map needs first (plan §6.11).
        Stage.CLIP: ItemStage(
            code_version=1,
            upstream=(Stage.SCAN, Stage.METADATA_READ, Stage.FRAMES, Stage.THUMBNAIL),
            settings_fingerprint=settings.clip_model,
        ),
    }


NOOP_RUN_S = 0.2
"""How long a `noop` job takes, so progress is visible when exercising by hand."""


def run_noop(job: Job) -> None:
    time.sleep(NOOP_RUN_S)


def build_runners(
    settings: Settings,
    engine: Engine,
    queue: JobQueue,
    clock: Clock,
    metadata_reader: MetadataReader,
    *,
    clip: "ClipEmbedder | None" = None,
) -> Mapping[str, Runner]:
    """The stages this worker runs. Jobs for any other stage stay in the queue.
    The caller owns `metadata_reader`, and closes it once the worker stops.

    `clip` embeds for the `clip` stage. By default it's `CLIP_MODEL` from
    `MODELS_DIR`, loaded by the first `clip` job, so a worker with no models still
    runs the other stages; each `clip` job fails, saying where the models should be."""
    sessions = sessionmaker(engine)
    embedder = clip

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
        return scan.scan(sessions, queue, job, settings.photo_dir, clock())

    def run_metadata_read(job: Job) -> bool:
        return metadata.record(
            sessions, job, metadata_reader, settings.photo_dir, settings.tz, clock()
        )

    def run_frames(job: Job) -> bool | None:
        return frames.sample(
            sessions,
            job,
            settings.photo_dir,
            settings.derived_dir,
            settings.video_sample_frames,
        )

    def run_thumbnail(job: Job) -> bool:
        return thumbnails.make(
            sessions, job, settings.photo_dir, settings.derived_dir, settings.thumbnail_size
        )

    def run_clip(job: Job) -> bool:
        # Here, not at the top: see the module docstring.
        from photo_triage.ml import OnnxClip
        from photo_triage.pipeline import embed

        nonlocal embedder
        if embedder is None:
            embedder = OnnxClip(settings.models_dir, settings.clip_model, settings.worker_threads)
        return embed.embed(sessions, job, embedder, settings.photo_dir, settings.derived_dir)

    runners: dict[str, Runner] = {
        Stage.WALK: run_walk,
        Stage.SCAN: run_scan,
        Stage.METADATA_READ: run_metadata_read,
        Stage.FRAMES: run_frames,
        Stage.THUMBNAIL: run_thumbnail,
        Stage.CLIP: run_clip,
    }
    if settings.worker_noop_stage:
        runners[Stage.NOOP] = run_noop
    return runners
