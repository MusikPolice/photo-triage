"""The `metadata_read` stage over the tier-1 fixtures: dimensions, durations, capture
times and imported keywords (plan §6.1), and how it's queued and versioned."""

import datetime as dt
import os
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from photo_triage.db.models import Item, Job, JobStatus, Tag, TagSource
from photo_triage.files import ExifTool
from photo_triage.ml import FakeClip
from photo_triage.settings import Settings
from photo_triage.worker.__main__ import main
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import JobQueue, Stage
from photo_triage.worker.reconcile import reconcile
from photo_triage.worker.stages import ItemStage, build_runners, item_stages

START = dt.datetime(2026, 10, 18, 2, tzinfo=dt.UTC)
ZONE = ZoneInfo("America/Vancouver")  # UTC-8 in December, when the clips were made
KEYWORDS = {"beach", "sunset", "café"}
MTIME = None
"""The file has no capture time, so it's the file's mtime in `ZONE`."""

# What each supported fixture says about itself, as scripts/make_synthetic_fixtures.py
# made it: width, height, duration and capture time. The truncated JPEG never gets
# this far, since it fails its scan.
EXPECTED: dict[str, tuple[int, int, float | None, dt.datetime | None]] = {
    **{
        f"burst/burst-0{n + 1}.jpg": (640, 480, None, dt.datetime(2024, 7, 4, 18, 30, n))
        for n in range(5)
    },
    "broken/damaged.jpg": (640, 480, None, dt.datetime(2012, 7, 27, 2, 24, 36)),
    "dates/create-date-only.jpg": (640, 480, None, dt.datetime(2023, 5, 6, 7, 8, 9)),
    "dates/modify-date-only.jpg": (640, 480, None, dt.datetime(2022, 3, 4, 5, 6, 7)),
    "dates/no-dates.jpg": (640, 480, None, MTIME),
    "heic/no-dates.heic": (640, 480, None, MTIME),
    "heic/scene.heic": (640, 480, None, dt.datetime(2024, 11, 1, 8)),
    "metadata/face-region.jpg": (640, 480, None, dt.datetime(2024, 9, 3, 12)),
    "metadata/keywords.heic": (640, 480, None, dt.datetime(2024, 9, 2, 12)),
    "metadata/keywords.jpg": (640, 480, None, dt.datetime(2024, 9, 1, 12)),
    "orientation/bad-orientation.jpg": (640, 480, None, dt.datetime(2024, 10, 2, 12)),
    # Stored 480 wide and 640 high, displayed turned a quarter by its orientation.
    "orientation/rotated-90.jpg": (640, 480, None, dt.datetime(2024, 10, 1, 12)),
    "quality/blurred.jpg": (640, 480, None, dt.datetime(2024, 8, 1, 10)),
    "quality/overexposed.jpg": (640, 480, None, dt.datetime(2024, 8, 1, 10, 0, 1)),
    "quality/screenshot.jpg": (780, 1688, None, dt.datetime(2024, 8, 2, 9, 41)),
    "quality/underexposed.jpg": (640, 480, None, dt.datetime(2024, 8, 1, 10, 0, 2)),
    **{
        f"scenes/scene-0{n}.jpg": (640, 480, None, dt.datetime(2024, n, 10 + n, 10 + n))
        for n in range(1, 7)
    },
    # QuickTime's CreateDate is UTC: 15:00 UTC is 07:00 in Vancouver.
    "video/clip.mov": (320, 240, 3.0, dt.datetime(2024, 12, 1, 7)),
    "video/clip.mp4": (320, 240, 2.0, dt.datetime(2024, 12, 2, 8)),
    "video/keywords.mp4": (320, 240, 2.0, dt.datetime(2024, 12, 3, 9)),
}
WITH_KEYWORDS = {"metadata/keywords.jpg", "metadata/keywords.heic", "video/keywords.mp4"}


class Pipeline:
    """Runs walks and every job they lead to, as the worker would."""

    def __init__(self, engine: Engine, settings: Settings, exiftool: ExifTool) -> None:
        self.engine = engine
        self.now = START
        self.queue = JobQueue(self.clock)
        self.sessions = sessionmaker(engine)
        self.settings = settings
        self.stages = item_stages(settings)
        self.worker = Worker(
            engine,
            build_runners(settings, engine, self.queue, self.clock, exiftool, clip=FakeClip()),
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

    def tags(self, path: str) -> list[tuple[str, TagSource]]:
        with self.sessions() as session:
            return sorted(
                session.execute(
                    sa.select(Tag.tag, Tag.source)
                    .join(Item, Item.id == Tag.item_id)
                    .where(Item.path == path)
                ).all()
            )

    def done(self, stage: Stage) -> dict[int, dt.datetime]:
        """When each item's done job for `stage` finished."""
        with self.sessions() as session:
            rows = session.execute(
                sa.select(Job.item_id, Job.finished_at).where(
                    Job.stage == stage, Job.status == JobStatus.DONE
                )
            ).all()
            return {item_id: finished for item_id, finished in rows if item_id and finished}


@pytest.fixture
def pipeline(migrated: Engine, settings: Settings, exiftool: ExifTool) -> Pipeline:
    settings.photo_dir.mkdir(exist_ok=True)
    return Pipeline(migrated, settings.model_copy(update={"tz": ZONE}), exiftool)


def _mtime_local(path: Path) -> dt.datetime:
    seconds = path.stat().st_mtime_ns // 10**9
    return dt.datetime.fromtimestamp(seconds, ZONE).replace(tzinfo=None)


def _exiftool(path: Path, *args: str) -> None:
    subprocess.run(["exiftool", "-q", "-overwrite_original", *args, str(path)], check=True)


def test_every_supported_file_has_its_known_metadata(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()

    items = pipeline.items()
    recorded = {
        path: (i.width, i.height, i.duration_s, i.taken_at_local)
        for path, i in items.items()
        if i.content_hash is not None
    }
    expected = {
        path: (w, h, duration, taken or _mtime_local(library / path))
        for path, (w, h, duration, taken) in EXPECTED.items()
    }
    assert recorded == expected
    for path in items:
        want = KEYWORDS if path in WITH_KEYWORDS else set[str]()
        assert pipeline.tags(path) == sorted((k, TagSource.IMPORTED) for k in want), path


def test_each_scan_queues_a_metadata_read_after_it(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()

    hashed = {i.id for i in pipeline.items().values() if i.content_hash is not None}
    assert set(pipeline.done(Stage.METADATA_READ)) == hashed == set(pipeline.done(Stage.SCAN))
    assert len(hashed) == len(EXPECTED)


def test_a_keyword_edit_replaces_the_imported_tags_without_a_new_hash(
    pipeline: Pipeline, library: Path
) -> None:
    pipeline.run()
    photo = library / "metadata/keywords.jpg"
    before = pipeline.items()["metadata/keywords.jpg"]
    scanned_at = pipeline.done(Stage.SCAN)[before.id]

    pipeline.later()
    # Another app edits the keywords: a new mtime, the same pixels.
    _exiftool(photo, "-IPTC:Keywords-=sunset", "-XMP-dc:Subject-=sunset", "-XMP-dc:Subject+=dog")
    pipeline.run()

    after = pipeline.items()["metadata/keywords.jpg"]
    assert pipeline.tags("metadata/keywords.jpg") == [
        ("beach", TagSource.IMPORTED),
        ("café", TagSource.IMPORTED),
        ("dog", TagSource.IMPORTED),
    ]
    assert (after.id, after.content_hash) == (before.id, before.content_hash)
    assert pipeline.done(Stage.SCAN)[after.id] > scanned_at  # it was scanned again,
    with pipeline.sessions() as session:
        changed_at = session.scalar(
            sa.select(Job.output_changed_at).where(
                Job.item_id == after.id, Job.stage == Stage.SCAN, Job.status == JobStatus.DONE
            )
        )
    assert changed_at == START  # but its output didn't change, so later stages don't run


def test_tags_that_stay_keep_when_they_were_imported(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    pipeline.later()
    _exiftool(library / "metadata/keywords.heic", "-XMP-dc:Subject+=dog")
    pipeline.run()

    with pipeline.sessions() as session:
        created = dict(
            session.execute(
                sa.select(Tag.tag, Tag.created_at)
                .join(Item, Item.id == Tag.item_id)
                .where(Item.path == "metadata/keywords.heic")
            ).all()
        )
    assert created == {"beach": START, "sunset": START, "café": START, "dog": pipeline.now}


def test_a_rotation_edit_alone_updates_the_dimensions(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    pipeline.later()
    _exiftool(library / "scenes/scene-01.jpg", "-Orientation#=8")
    pipeline.run()

    turned = pipeline.items()["scenes/scene-01.jpg"]
    assert (turned.width, turned.height) == (480, 640)


def test_bumping_its_version_reads_every_file_again_without_hashing_them(
    pipeline: Pipeline, library: Path, exiftool: ExifTool
) -> None:
    pipeline.run()
    scanned = pipeline.done(Stage.SCAN)
    read = pipeline.done(Stage.METADATA_READ)
    with pipeline.sessions.begin() as session:
        session.execute(sa.update(Item).values(width=None, taken_at_local=None))
        session.execute(sa.delete(Tag))
    pipeline.later()

    stages = {
        **pipeline.stages,
        Stage.METADATA_READ: ItemStage(code_version=3, upstream=(Stage.SCAN,)),
    }
    queued = reconcile(pipeline.engine, stages, pipeline.now)
    worker = Worker(
        pipeline.engine,
        build_runners(
            pipeline.settings,
            pipeline.engine,
            pipeline.queue,
            pipeline.clock,
            exiftool,
            clip=FakeClip(),
        ),
        queue=pipeline.queue,
        clock=pipeline.clock,
        item_stages=stages,
    )
    worker.drain()

    assert queued == {
        Stage.SCAN: 0,
        Stage.METADATA_READ: len(EXPECTED),
        Stage.FRAMES: 0,
        Stage.THUMBNAIL: 0,
        Stage.CLIP: 0,
    }
    assert pipeline.done(Stage.SCAN) == scanned  # no file hashed again
    reread = pipeline.done(Stage.METADATA_READ)
    assert set(reread) == set(read)
    assert all(reread[i] > read[i] for i in read)
    items = pipeline.items()
    assert all(items[p].width is not None and items[p].taken_at_local for p in EXPECTED)
    assert pipeline.tags("video/keywords.mp4") == sorted((k, TagSource.IMPORTED) for k in KEYWORDS)


def test_a_file_that_cannot_be_read_fails_its_job(pipeline: Pipeline, settings: Settings) -> None:
    """The job is retried after a backoff, and parked in the end, like any other."""
    clip = settings.photo_dir / "clip.mov"
    clip.write_bytes((Path(__file__).parents[1] / "fixtures/synthetic/video/clip.mov").read_bytes())
    pipeline.run()
    item = pipeline.items()["clip.mov"]
    assert item.duration_s == 3.0

    # Cut short after its scan: ffprobe can't read it.
    with pipeline.sessions.begin() as session:
        pipeline.queue.enqueue(session, Stage.METADATA_READ, item.id)
    clip.write_bytes(clip.read_bytes()[:2048])
    pipeline.worker.drain()

    with pipeline.sessions() as session:
        job = session.scalars(
            sa.select(Job).where(Job.stage == Stage.METADATA_READ, Job.status == JobStatus.ERROR)
        ).one()
    assert "MetadataError" in (job.last_error or "")


# --- The worker process ---------------------------------------------------------------


def test_the_worker_starts_one_exiftool_and_stops_it_when_it_stops(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, library: Path, migrated: Engine
) -> None:
    monkeypatch.setenv("PHOTO_DIR", str(settings.photo_dir))
    monkeypatch.setenv("TRASH_DIR", str(settings.trash_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MODELS_DIR", str(settings.models_dir))
    started: list[ExifTool] = []
    pids: set[int] = set()

    class Recorded(ExifTool):
        def __init__(self) -> None:
            super().__init__()
            started.append(self)

        def read(self, path: Path, tags: list[str]) -> dict[str, object]:  # type: ignore[override]
            result = super().read(path, tags)
            assert self.pid is not None
            pids.add(self.pid)
            return result

    monkeypatch.setattr("photo_triage.worker.__main__.ExifTool", Recorded)
    assert main(["scan"]) == 0

    assert main(["--once"]) == 0

    assert len(started) == 1
    assert len(pids) == 1  # one process served every read
    assert started[0].pid is None
    (pid,) = pids
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # exited and reaped
    with sessionmaker(migrated)() as session:
        done = session.scalar(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(Job.stage == Stage.METADATA_READ, Job.status == JobStatus.DONE)
        )
    assert done == len(EXPECTED)
