"""The `clip` stage over the tier-1 fixtures, with the fake embedder (plan §6.3):
which items get an embedding, what it's made from, and what replaces it."""

import datetime as dt
import subprocess
from pathlib import Path

import numpy as np
import pytest
import sqlalchemy as sa
from PIL import Image, ImageOps
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Embedding, Item, ItemFrame, Job, JobStatus, MediaType
from photo_triage.files import ExifTool
from photo_triage.ml import ClipEmbedder, FakeClip
from photo_triage.pipeline.embed import from_blob, mean_vector
from photo_triage.pipeline.frames import frame_path
from photo_triage.settings import Settings
from photo_triage.worker.__main__ import main
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import JobQueue, RetryPolicy, Stage
from photo_triage.worker.stages import ItemStage, build_runners, item_stages

START = dt.datetime(2026, 11, 8, 2, tzinfo=dt.UTC)
SUPPORTED = 29
"""Tier-1 files the scanner takes: every one but the truncated JPEG and the PNG. The
damaged JPEG is among them."""


class Pipeline:
    """Runs walks and every job they lead to, as the worker would."""

    def __init__(self, engine: Engine, settings: Settings, exiftool: ExifTool) -> None:
        self.engine = engine
        self.exiftool = exiftool
        self.now = START
        self.queue = JobQueue(self.clock, RetryPolicy(max_attempts=1))
        self.sessions = sessionmaker(engine)
        self.configure(settings)

    def configure(
        self,
        settings: Settings,
        embedder: ClipEmbedder | None = None,
        *,
        without: Stage | None = None,
        **stages: ItemStage,
    ) -> None:
        """`embedder` is `FakeClip()` unless given; `without` leaves a stage's jobs
        queued."""
        self.settings = settings
        self.stages = {**item_stages(settings), **stages}
        runners = build_runners(
            settings,
            self.engine,
            self.queue,
            self.clock,
            self.exiftool,
            clip=FakeClip() if embedder is None else embedder,
        )
        self.worker = Worker(
            self.engine,
            {name: run for name, run in runners.items() if name != without},
            queue=self.queue,
            clock=self.clock,
            item_stages=self.stages,
        )

    def clock(self) -> dt.datetime:
        return self.now

    def run(self) -> None:
        with self.sessions.begin() as session:
            self.queue.enqueue(session, Stage.WALK)
        self.worker.drain()

    def later(self) -> None:
        self.now += dt.timedelta(days=1)

    def items(self) -> dict[str, Item]:
        with self.sessions() as session:
            return {i.path: i for i in session.scalars(sa.select(Item))}

    def embeddings(self) -> dict[int, Embedding]:
        with self.sessions() as session:
            return {e.item_id: e for e in session.scalars(sa.select(Embedding))}

    def jobs(self, stage: Stage, status: JobStatus = JobStatus.DONE) -> dict[int, Job]:
        with self.sessions() as session:
            query = sa.select(Job).where(Job.stage == stage, Job.status == status)
            return {job.item_id: job for job in session.scalars(query) if job.item_id}


@pytest.fixture
def pipeline(migrated: Engine, settings: Settings, exiftool: ExifTool) -> Pipeline:
    settings.photo_dir.mkdir(exist_ok=True)
    return Pipeline(migrated, settings, exiftool)


def _open(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def _turn(path: Path, orientation: int) -> None:
    command = ["exiftool", "-q", "-overwrite_original", f"-Orientation#={orientation}"]
    subprocess.run([*command, str(path)], check=True)


def _vector(pipeline: "Pipeline", item_id: int) -> np.ndarray:
    return from_blob(pipeline.embeddings()[item_id].vector)


def _hashed(engine: Engine) -> set[int]:
    with Session(engine) as session:
        return set(session.scalars(sa.select(Item.id).where(Item.content_hash.is_not(None))))


def test_worker_once_gives_every_supported_item_one_embedding(
    migrated: Engine, settings: Settings, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHOTO_DIR", str(settings.photo_dir))
    monkeypatch.setenv("TRASH_DIR", str(settings.trash_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MODELS_DIR", str(settings.models_dir))

    assert main(["scan"]) == 0
    assert main(["--once"], clip=FakeClip()) == 0

    with Session(migrated) as session:
        rows = list(session.scalars(sa.select(Embedding)))
    supported = _hashed(migrated)
    assert len(supported) == SUPPORTED
    assert sorted(e.item_id for e in rows) == sorted(supported)  # exactly one each
    for row in rows:
        assert row.model == "fake"
        assert len(row.vector) == 512 * 4
        assert np.isclose(np.linalg.norm(from_blob(row.vector)), 1.0, rtol=1e-6)


def test_a_finished_thumbnail_queues_a_clip_job(pipeline: Pipeline, library: Path) -> None:
    # The reconcile after the walk runs before any item has its thumbnails, so each
    # clip job is queued by its item's thumbnail job finishing.
    pipeline.configure(pipeline.settings, without=Stage.CLIP)
    pipeline.run()

    pending = pipeline.jobs(Stage.CLIP, JobStatus.PENDING)
    assert set(pending) == set(pipeline.jobs(Stage.THUMBNAIL)) == _hashed(pipeline.engine)
    assert pipeline.embeddings() == {}


def test_a_photo_is_embedded_from_the_original_turned_upright(
    pipeline: Pipeline, library: Path
) -> None:
    path = library / "scenes/scene-01.jpg"
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-Orientation#=6", str(path)], check=True
    )
    pipeline.run()
    scene = pipeline.items()["scenes/scene-01.jpg"]

    upright = _open(path)
    assert upright.size == (480, 640)
    np.testing.assert_allclose(
        _vector(pipeline, scene.id), FakeClip().embed_images([upright])[0], atol=1e-7
    )


def test_turning_a_photo_alone_embeds_it_again(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    scene = pipeline.items()["scenes/scene-01.jpg"]
    before = _vector(pipeline, scene.id)
    pipeline.later()

    path = library / "scenes/scene-01.jpg"
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-Orientation#=3", str(path)], check=True
    )
    pipeline.run()

    after = _vector(pipeline, scene.id)
    assert not np.allclose(after, before)
    np.testing.assert_allclose(after, FakeClip().embed_images([_open(path)])[0], atol=1e-7)


def test_a_video_is_the_mean_of_its_frames(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    videos = [i for i in pipeline.items().values() if i.media_type == MediaType.VIDEO]
    assert videos

    embeddings = pipeline.embeddings()
    for video in videos:
        with pipeline.sessions() as session:
            indexes = session.scalars(
                sa.select(ItemFrame.frame_index)
                .where(ItemFrame.item_id == video.id)
                .order_by(ItemFrame.frame_index)
            ).all()
        assert len(indexes) == 5
        frames = [_open(frame_path(pipeline.settings.derived_dir, video.id, i)) for i in indexes]
        expected = mean_vector(FakeClip().embed_images(frames))
        np.testing.assert_allclose(from_blob(embeddings[video.id].vector), expected, atol=1e-7)


def test_a_changed_file_is_embedded_again(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    items = pipeline.items()
    scene, other = items["scenes/scene-01.jpg"], items["scenes/scene-02.jpg"]
    before = pipeline.embeddings()
    assert before[scene.id].vector != before[other.id].vector
    pipeline.later()

    (library / "scenes/scene-01.jpg").write_bytes((library / "scenes/scene-02.jpg").read_bytes())
    pipeline.run()

    after = pipeline.embeddings()
    assert len(after) == SUPPORTED  # replaced, not added to
    assert after[scene.id].vector == after[other.id].vector  # the same picture now
    assert pipeline.jobs(Stage.CLIP)[scene.id].finished_at == pipeline.now
    assert pipeline.jobs(Stage.CLIP)[other.id].finished_at == START


def test_a_new_clip_model_embeds_every_item_again(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    pipeline.later()

    other = pipeline.settings.model_copy(update={"clip_model": "ViT-B-16"})
    pipeline.configure(other, FakeClip(model="ViT-B-16"))
    assert pipeline.worker.reconcile() == {
        **dict.fromkeys(pipeline.stages, 0),
        Stage.CLIP: SUPPORTED,
    }
    pipeline.worker.drain()

    after = pipeline.embeddings()
    assert len(after) == SUPPORTED
    assert {e.model for e in after.values()} == {"ViT-B-16"}
    assert {j.stage_version for j in pipeline.jobs(Stage.CLIP).values()} == {"1+ViT-B-16"}
    assert {j.output_changed_at for j in pipeline.jobs(Stage.CLIP).values()} == {pipeline.now}


def test_embedding_again_to_the_same_vector_reports_no_change(
    pipeline: Pipeline, library: Path
) -> None:
    pipeline.run()
    before = pipeline.embeddings()
    pipeline.later()

    bumped = ItemStage(
        code_version=2,
        upstream=(Stage.SCAN, Stage.METADATA_READ, Stage.FRAMES, Stage.THUMBNAIL),
        settings_fingerprint="ViT-B-32",
    )
    pipeline.configure(pipeline.settings, clip=bumped)
    assert pipeline.worker.reconcile()[Stage.CLIP] == SUPPORTED
    pipeline.worker.drain()

    again = pipeline.jobs(Stage.CLIP)
    assert {j.stage_version for j in again.values()} == {"2+ViT-B-32"}
    assert {j.output_changed_at for j in again.values()} == {START}
    assert {i: e.vector for i, e in pipeline.embeddings().items()} == {
        i: e.vector for i, e in before.items()
    }


def test_without_the_models_each_clip_job_fails_saying_where_they_go(
    pipeline: Pipeline, library: Path, migrated: Engine, settings: Settings, exiftool: ExifTool
) -> None:
    worker = Worker(
        migrated,
        build_runners(settings, migrated, pipeline.queue, pipeline.clock, exiftool),
        queue=pipeline.queue,
        clock=pipeline.clock,
        item_stages=item_stages(settings),
    )
    with pipeline.sessions.begin() as session:
        pipeline.queue.enqueue(session, Stage.WALK)
    worker.drain()

    parked = pipeline.jobs(Stage.CLIP, JobStatus.PARKED)
    assert set(parked) == _hashed(migrated)
    error = next(iter(parked.values())).last_error
    assert error is not None
    assert f"No exported CLIP model at {settings.models_dir / 'clip/ViT-B-32'}" in error
    assert pipeline.embeddings() == {}
    # Everything before it still ran.
    assert set(pipeline.jobs(Stage.THUMBNAIL)) == _hashed(migrated)
