"""The `frames` stage over the tier-1 videos (plan §6.1): which items get frames, where
they're written, and how they're replaced."""

import datetime as dt
import logging
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from photo_triage.db.models import Item, ItemFrame, Job, JobStatus, MediaType
from photo_triage.files import ExifTool
from photo_triage.ml import FakeClip
from photo_triage.pipeline import frames
from photo_triage.pipeline.frames import FRAMES_FOLDER, frame_path, timestamps
from photo_triage.settings import Settings
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import JobQueue, RetryPolicy, Stage
from photo_triage.worker.stages import build_runners, item_stages

START = dt.datetime(2026, 11, 8, 2, tzinfo=dt.UTC)
FRAMES = 4
VIDEOS = {"video/clip.mov": 3.0, "video/clip.mp4": 2.0, "video/keywords.mp4": 2.0}
"""Each tier-1 video, and its duration in seconds. Each is 320 by 240 at 10 fps."""


class Pipeline:
    """Runs walks and every job they lead to, as the worker would."""

    def __init__(self, engine: Engine, settings: Settings, exiftool: ExifTool) -> None:
        self.engine = engine
        self.exiftool = exiftool
        self.now = START
        self.queue = JobQueue(self.clock, RetryPolicy(max_attempts=1))
        self.sessions = sessionmaker(engine)
        self.configure(settings)

    def configure(self, settings: Settings) -> None:
        self.settings = settings
        self.worker = Worker(
            self.engine,
            build_runners(
                settings, self.engine, self.queue, self.clock, self.exiftool, clip=FakeClip()
            ),
            queue=self.queue,
            clock=self.clock,
            item_stages=item_stages(settings),
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

    def frames(self, item_id: int) -> list[tuple[int, float]]:
        with self.sessions() as session:
            rows = session.execute(
                sa.select(ItemFrame.frame_index, ItemFrame.timestamp_s)
                .where(ItemFrame.item_id == item_id)
                .order_by(ItemFrame.frame_index)
            ).all()
            return [(index, at_s) for index, at_s in rows]

    def jobs(self, stage: Stage, status: JobStatus = JobStatus.DONE) -> list[Job]:
        with self.sessions() as session:
            query = sa.select(Job).where(Job.stage == stage, Job.status == status)
            return list(session.scalars(query))


@pytest.fixture
def pipeline(migrated: Engine, settings: Settings, exiftool: ExifTool) -> Pipeline:
    settings.photo_dir.mkdir(exist_ok=True)
    return Pipeline(migrated, settings.model_copy(update={"video_sample_frames": FRAMES}), exiftool)


def _files(derived_dir: Path) -> list[str]:
    root = derived_dir / FRAMES_FOLDER
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_every_video_gets_its_frames_and_nothing_else_does(
    pipeline: Pipeline, library: Path
) -> None:
    pipeline.run()

    items = pipeline.items()
    derived = pipeline.settings.derived_dir
    for path, duration_s in VIDEOS.items():
        item = items[path]
        recorded = pipeline.frames(item.id)
        assert [index for index, _ in recorded] == list(range(FRAMES)), path
        assert [t for _, t in recorded] == pytest.approx(
            timestamps(duration_s, FRAMES, min_gap_s=0.1)
        )
        for index in range(FRAMES):
            with Image.open(frame_path(derived, item.id, index)) as frame:
                frame.load()  # decodes
                assert (frame.format, frame.size) == ("JPEG", (320, 240))
    expected = sorted(f"{items[p].id}/{n}.jpg" for p in VIDEOS for n in range(FRAMES))
    assert _files(derived) == expected  # nothing for photos, and no staging left
    videos = {i.id for i in items.values() if i.media_type == MediaType.VIDEO}
    assert {job.item_id for job in pipeline.jobs(Stage.FRAMES)} == videos


def test_a_changed_video_is_sampled_again_leaving_nothing_behind(
    pipeline: Pipeline, library: Path
) -> None:
    pipeline.run()
    clip = pipeline.items()["video/clip.mov"]
    first = pipeline.frames(clip.id)

    pipeline.later()
    # The file changes in place: now the 2 s clip, not the 3 s one.
    shutil.copyfile(library / "video/clip.mp4", library / "video/clip.mov")
    pipeline.run()

    assert pipeline.items()["video/clip.mov"].id == clip.id
    again = pipeline.frames(clip.id)
    assert [t for _, t in again] == pytest.approx(timestamps(2.0, FRAMES, min_gap_s=0.1))
    assert again != first
    assert [f for f in _files(pipeline.settings.derived_dir) if f.startswith(f"{clip.id}/")] == [
        f"{clip.id}/{n}.jpg" for n in range(FRAMES)
    ]


def test_changing_the_number_of_frames_samples_every_video_again(
    pipeline: Pipeline, library: Path
) -> None:
    pipeline.run()
    pipeline.later()

    pipeline.configure(pipeline.settings.model_copy(update={"video_sample_frames": 2}))
    queued = pipeline.worker.reconcile()
    pipeline.worker.drain()

    assert queued[Stage.FRAMES] == len(VIDEOS)
    items = pipeline.items()
    for path in VIDEOS:
        assert [index for index, _ in pipeline.frames(items[path].id)] == [0, 1]
    assert _files(pipeline.settings.derived_dir) == sorted(
        f"{items[p].id}/{n}.jpg" for p in VIDEOS for n in range(2)
    )
    assert {job.stage_version for job in pipeline.jobs(Stage.FRAMES)} == {"1+2-frames"}


def test_a_video_that_cannot_be_read_fails_its_job_and_keeps_its_frames(
    pipeline: Pipeline, library: Path
) -> None:
    pipeline.run()
    clip = pipeline.items()["video/clip.mov"]
    before = pipeline.frames(clip.id)

    with pipeline.sessions.begin() as session:
        pipeline.queue.enqueue(session, Stage.FRAMES, clip.id)
    video = library / "video/clip.mov"
    video.write_bytes(video.read_bytes()[:2048])
    pipeline.worker.drain()

    [job] = pipeline.jobs(Stage.FRAMES, JobStatus.PARKED)
    assert "FrameError" in (job.last_error or "")
    assert pipeline.frames(clip.id) == before
    assert len([f for f in _files(pipeline.settings.derived_dir) if f.startswith(f"{clip.id}/")])
    assert not list((pipeline.settings.derived_dir / FRAMES_FOLDER).glob("*.staging"))


def test_a_run_cut_short_leaves_staging_that_the_next_run_clears(
    pipeline: Pipeline, library: Path
) -> None:
    pipeline.run()
    clip = pipeline.items()["video/clip.mp4"]
    leftover = pipeline.settings.derived_dir / FRAMES_FOLDER / f"{clip.id}.abc123.staging"
    leftover.mkdir()
    (leftover / "0.jpg").write_bytes(b"half")

    with pipeline.sessions.begin() as session:
        pipeline.queue.enqueue(session, Stage.FRAMES, clip.id)
    pipeline.worker.drain()

    assert not leftover.exists()


def test_a_video_gone_before_its_frames_are_sampled_is_left_for_the_next_walk(
    pipeline: Pipeline, library: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="photo_triage.pipeline.frames")
    pipeline.run()
    clip = pipeline.items()["video/clip.mp4"]
    with pipeline.sessions.begin() as session:
        pipeline.queue.enqueue(session, Stage.FRAMES, clip.id)
    (library / "video/clip.mp4").unlink()

    pipeline.worker.drain()

    assert f"Item {clip.id} is gone from video/clip.mp4" in caplog.text
    assert len(pipeline.frames(clip.id)) == FRAMES  # the last run's, until the walk


def test_a_photo_queued_by_hand_gets_no_frames(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    photo = pipeline.items()["scenes/scene-01.jpg"]
    with pipeline.sessions.begin() as session:
        pipeline.queue.enqueue(session, Stage.FRAMES, photo.id)

    pipeline.worker.drain()

    assert pipeline.frames(photo.id) == []
    assert not (pipeline.settings.derived_dir / FRAMES_FOLDER / str(photo.id)).exists()


def test_an_item_deleted_while_its_frames_were_sampled_keeps_none(
    pipeline: Pipeline, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline.run()
    clip = pipeline.items()["video/clip.mov"]
    with pipeline.sessions.begin() as session:
        pipeline.queue.enqueue(session, Stage.FRAMES, clip.id)
    real_probe = frames.video.probe

    def probe_then_delete(path: Path) -> frames.video.VideoProbe:
        found = real_probe(path)
        with pipeline.sessions.begin() as session:
            session.execute(sa.delete(Item).where(Item.id == clip.id))
        return found

    monkeypatch.setattr(frames.video, "probe", probe_then_delete)
    pipeline.worker.drain()

    root = pipeline.settings.derived_dir / FRAMES_FOLDER
    assert not list(root.glob(f"{clip.id}.*"))  # its staging is cleared
