"""The metadata write queue and the `metadata_write` stage (plan §6.10), with real
exiftool on copies of the tier-1 fixtures."""

import datetime as dt
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, ItemStatus, Job, MediaType, MetadataWrite, WriteStatus
from photo_triage.files import ExifTool, MetadataError
from photo_triage.pipeline import scan, writeback
from photo_triage.pipeline.writeback import Field
from photo_triage.settings import Settings
from photo_triage.worker.queue import JobQueue, Stage

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
NOW = dt.datetime(2026, 11, 23, 12, tzinfo=dt.UTC)
OLD_NS = 1_700_000_000_123_456_789

SOURCES = {
    "jpeg": "scenes/scene-01.jpg",
    "heic": "heic/scene.heic",
    "mov": "video/clip.mov",
    "mp4": "video/clip.mp4",
}


class Writes:
    """Items with files in the library, and their queued writes."""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self.sessions = sessionmaker(engine)
        self.queue = JobQueue(lambda: NOW)

    def item(self, source: str) -> int:
        """A copy of the fixture `source` as an item, with an mtime in nanoseconds."""
        path = self.settings.photo_dir / source
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SYNTHETIC / source, path)
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, OLD_NS))
        kind = MediaType.VIDEO if path.suffix in (".mov", ".mp4") else MediaType.PHOTO
        with self.sessions.begin() as session:
            item = Item(
                content_hash="0" * 64,
                path=source,
                media_type=kind,
                file_size_bytes=stat.st_size,
                file_mtime_ns=OLD_NS,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            session.add(item)
            session.flush()
            return item.id

    def queue_write(self, item_id: int, actor: str | None = "ada", **fields: list[str]) -> int:
        with self.sessions.begin() as session:
            write = writeback.queue_write(
                session,
                self.queue,
                item_id,
                {Field(k): v for k, v in fields.items()},
                actor=actor,
                now=NOW,
            )
            session.flush()
            return write.id

    def job(self, item_id: int) -> Job:
        with self.sessions() as session:
            job = session.scalar(
                sa.select(Job).where(Job.item_id == item_id, Job.stage == Stage.METADATA_WRITE)
            )
            assert job is not None
            session.expunge(job)
            return job

    def run(self, item_id: int, writer: Any, *, enabled: bool = True) -> None:
        writeback.apply(
            self.sessions,
            self.job(item_id),
            writer,
            self.settings.photo_dir,
            enabled=enabled,
            now=NOW,
        )

    def log(self, item_id: int) -> list[MetadataWrite]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    sa.select(MetadataWrite)
                    .where(MetadataWrite.item_id == item_id)
                    .order_by(MetadataWrite.id)
                )
            )


@pytest.fixture
def writes(migrated: Engine, settings: Settings) -> Writes:
    return Writes(migrated, settings)


class Spy:
    """Counts the writes it passes on to exiftool."""

    def __init__(self, exiftool: ExifTool) -> None:
        self.exiftool = exiftool
        self.calls: list[Sequence[str]] = []

    def read(self, path: Path, tags: Sequence[str]) -> dict[str, Any]:
        return self.exiftool.read(path, tags)

    def write(self, path: Path, args: Sequence[str]) -> None:
        self.calls.append(args)
        self.exiftool.write(path, args)


@pytest.mark.parametrize("container", SOURCES)
def test_tags_and_the_reviewed_marker_round_trip(
    writes: Writes, exiftool: ExifTool, settings: Settings, container: str
) -> None:
    item_id = writes.item(SOURCES[container])
    path = settings.photo_dir / SOURCES[container]
    before = writeback.read_fields(exiftool, path)
    tags = ["dog", "crème", "Ada Lovelace"]
    writes.queue_write(item_id, tags=tags, reviewed=["duplicates"])

    writes.run(item_id, exiftool)

    after = writeback.read_fields(exiftool, path)
    assert sorted(after[Field.TAGS]) == sorted(tags)
    assert after[Field.REVIEWED] == ["duplicates"]
    assert path.stat().st_mtime_ns == OLD_NS
    [entry] = writes.log(item_id)
    assert entry.status == WriteStatus.APPLIED
    assert entry.applied_at == NOW
    assert entry.actor == "ada"
    assert entry.before == {"tags": before[Field.TAGS], "reviewed": before[Field.REVIEWED]}
    assert entry.after == {"tags": tags, "reviewed": ["duplicates"]}
    with writes.sessions() as session:
        item = session.get(Item, item_id)
        assert item is not None
        assert (item.file_size_bytes, item.file_mtime_ns) == (path.stat().st_size, OLD_NS)


def test_writing_fewer_tags_replaces_the_file_s(
    writes: Writes, exiftool: ExifTool, settings: Settings
) -> None:
    item_id = writes.item(SOURCES["jpeg"])
    writes.queue_write(item_id, tags=["a", "b", "c"])
    writes.run(item_id, exiftool)
    writes.queue_write(item_id, tags=["b"])
    writes.run(item_id, exiftool)
    writes.queue_write(item_id, tags=[])
    writes.run(item_id, exiftool)

    path = settings.photo_dir / SOURCES["jpeg"]
    assert writeback.read_fields(exiftool, path)[Field.TAGS] == []
    assert [(w.before, w.after) for w in writes.log(item_id)][1:] == [
        ({"tags": ["a", "b", "c"]}, {"tags": ["b"]}),
        ({"tags": ["b"]}, {"tags": []}),
    ]


def test_writes_queued_together_are_one_exiftool_call(
    writes: Writes, exiftool: ExifTool, settings: Settings
) -> None:
    item_id = writes.item(SOURCES["jpeg"])
    first = writes.queue_write(item_id, tags=["dog"])
    second = writes.queue_write(item_id, actor=None, reviewed=["duplicates"], tags=["dog", "cat"])
    spy = Spy(exiftool)

    writes.run(item_id, spy)

    [args] = spy.calls
    assert "-XMP-dc:Subject=cat" in args
    assert "-XMP-phototriage:Reviewed=duplicates" in args
    logged = {w.id: w for w in writes.log(item_id)}
    assert {w.status for w in logged.values()} == {WriteStatus.APPLIED}
    # Each keeps its own entry and actor, and its before is what the one ahead left.
    assert logged[first].after == {"tags": ["dog"]}
    assert logged[second].before == {"tags": ["dog"], "reviewed": []}
    assert logged[second].after == {"tags": ["dog", "cat"], "reviewed": ["duplicates"]}
    assert (logged[first].actor, logged[second].actor) == ("ada", None)


def test_one_job_per_item_however_many_writes(writes: Writes) -> None:
    item_id = writes.item(SOURCES["jpeg"])
    writes.queue_write(item_id, tags=["a"])
    writes.queue_write(item_id, tags=["b"])

    with writes.sessions() as session:
        count = session.scalar(
            sa.select(sa.func.count()).where(
                Job.item_id == item_id, Job.stage == Stage.METADATA_WRITE
            )
        )
    assert count == 1


def test_a_failed_write_is_marked_and_made_on_the_retry(
    writes: Writes, exiftool: ExifTool, settings: Settings
) -> None:
    item_id = writes.item(SOURCES["jpeg"])
    writes.queue_write(item_id, tags=["dog"])

    class Failing(Spy):
        def write(self, path: Path, args: Sequence[str]) -> None:
            raise MetadataError("exiftool failed: it was a bad day")

    with pytest.raises(MetadataError):
        writes.run(item_id, Failing(exiftool))

    assert [w.status for w in writes.log(item_id)] == [WriteStatus.ERROR]

    writes.run(item_id, exiftool)

    assert [w.status for w in writes.log(item_id)] == [WriteStatus.APPLIED]
    path = settings.photo_dir / SOURCES["jpeg"]
    assert writeback.read_fields(exiftool, path)[Field.TAGS] == ["dog"]


def test_a_file_exiftool_can_t_write_raises(
    writes: Writes, exiftool: ExifTool, settings: Settings
) -> None:
    item_id = writes.item(SOURCES["jpeg"])
    path = settings.photo_dir / SOURCES["jpeg"]
    path.write_bytes(b"not a jpeg at all")
    writes.queue_write(item_id, tags=["dog"])

    with pytest.raises(MetadataError):
        writes.run(item_id, exiftool)

    assert [w.status for w in writes.log(item_id)] == [WriteStatus.ERROR]
    assert path.read_bytes() == b"not a jpeg at all"


def test_a_dry_run_logs_the_write_and_leaves_the_file(
    writes: Writes, exiftool: ExifTool, settings: Settings
) -> None:
    item_id = writes.item(SOURCES["heic"])
    path = settings.photo_dir / SOURCES["heic"]
    content = path.read_bytes()
    writes.queue_write(item_id, tags=["dog"])
    spy = Spy(exiftool)

    writes.run(item_id, spy, enabled=False)

    assert spy.calls == []
    assert path.read_bytes() == content
    assert path.stat().st_mtime_ns == OLD_NS
    [entry] = writes.log(item_id)
    assert entry.status == WriteStatus.DRY_RUN
    assert entry.after == {"tags": ["dog"]}
    assert entry.applied_at == NOW


def test_a_write_that_changes_nothing_calls_no_exiftool(
    writes: Writes, exiftool: ExifTool, settings: Settings
) -> None:
    item_id = writes.item(SOURCES["jpeg"])
    current = writeback.read_fields(exiftool, settings.photo_dir / SOURCES["jpeg"])
    writes.queue_write(item_id, tags=list(reversed(current[Field.TAGS])))
    spy = Spy(exiftool)

    writes.run(item_id, spy)

    assert spy.calls == []
    [entry] = writes.log(item_id)
    assert (entry.status, entry.before, entry.after) == (WriteStatus.APPLIED, {}, {})


def test_an_item_not_in_the_library_isn_t_written(
    writes: Writes, exiftool: ExifTool, settings: Settings
) -> None:
    item_id = writes.item(SOURCES["jpeg"])
    writes.queue_write(item_id, tags=["dog"])
    with writes.sessions.begin() as session:
        item = session.get(Item, item_id)
        assert item is not None
        item.status = ItemStatus.TRASHED
    spy = Spy(exiftool)

    writes.run(item_id, spy)

    assert spy.calls == []
    assert [w.status for w in writes.log(item_id)] == [WriteStatus.ERROR]


def test_after_a_write_the_walk_doesn_t_scan_the_file_again(
    writes: Writes, exiftool: ExifTool, settings: Settings
) -> None:
    item_id = writes.item(SOURCES["jpeg"])
    writes.queue_write(item_id, tags=["a much longer list", "of tags", "than it had"])
    writes.run(item_id, exiftool)
    settings.trash_dir.mkdir(exist_ok=True)

    with Session(writes.engine) as session, session.begin():
        result = scan.walk(session, writes.queue, settings.photo_dir, settings.trash_dir, NOW)

    assert (result.changed, result.new) == (0, 0)


def test_a_line_break_in_a_value_is_refused(
    writes: Writes, exiftool: ExifTool, settings: Settings
) -> None:
    item_id = writes.item(SOURCES["jpeg"])
    writes.queue_write(item_id, tags=["two\nlines"])

    with pytest.raises(MetadataError, match="line break"):
        writes.run(item_id, exiftool)


def test_a_write_needs_a_field(writes: Writes) -> None:
    item_id = writes.item(SOURCES["jpeg"])

    with pytest.raises(ValueError, match="at least one field"):
        writes.queue_write(item_id)
