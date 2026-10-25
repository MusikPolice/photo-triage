"""The library scanner: walk, hash, moves, copies and missing files (plan §6.1)."""

import datetime as dt
import logging
import os
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, ItemStatus, Job, JobStatus, MediaType
from photo_triage.pipeline import scan
from photo_triage.pipeline.identity import HashError
from photo_triage.settings import TRASH_FOLDER, Settings
from photo_triage.worker.__main__ import main
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import JobQueue, RetryPolicy, Stage
from photo_triage.worker.stages import build_runners, item_stages

START = dt.datetime(2026, 10, 18, 2, tzinfo=dt.UTC)

SUPPORTED = 28
"""Tier-1 fixtures the scanner registers: all 30 but the PNG and the truncated JPEG,
which fails its scan."""


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> dt.datetime:
        return self.now


class Scanner:
    """Runs walks and scan jobs against a migrated database, as the worker would."""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.clock = Clock()
        self.queue = JobQueue(self.clock, RetryPolicy(max_attempts=2))
        self.sessions = sessionmaker(engine)
        self.settings = settings
        self.worker = Worker(
            engine,
            build_runners(settings, engine, self.queue, self.clock),
            queue=self.queue,
            clock=self.clock,
            item_stages=item_stages(settings),
        )

    def walk(self) -> scan.WalkResult:
        with self.sessions.begin() as session:
            return scan.walk(
                session, self.queue, self.settings.photo_dir, self.settings.trash_dir, self.clock()
            )

    def run(self) -> None:
        """A walk, then every scan job it queued."""
        with self.sessions.begin() as session:
            self.queue.enqueue(session, Stage.WALK)
        self.worker.drain()

    def later(self, by: dt.timedelta = dt.timedelta(days=1)) -> None:
        self.clock.now += by

    def items(self) -> dict[str, Item]:
        with self.sessions() as session:
            return {i.path: i for i in session.scalars(sa.select(Item))}

    def output_changed_at(self, path: str) -> dt.datetime | None:
        """When the scan of the item at `path` last changed its content hash."""
        with self.sessions() as session:
            return session.scalar(
                sa.select(Job.output_changed_at)
                .join(Item, Item.id == Job.item_id)
                .where(Item.path == path, Job.stage == Stage.SCAN, Job.status == JobStatus.DONE)
            )

    def open_scans(self) -> int:
        with self.sessions() as session:
            return (
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Job)
                    .where(Job.stage == Stage.SCAN, Job.status == JobStatus.PENDING)
                )
                or 0
            )


@pytest.fixture
def scanner(migrated: Engine, settings: Settings) -> Scanner:
    settings.photo_dir.mkdir(exist_ok=True)
    return Scanner(migrated, settings)


def _photo(path: Path, color: str = "red", size: tuple[int, int] = (8, 8)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, quality=95)
    return path


def _move(library: Path, old: str, new: str) -> None:
    (library / new).parent.mkdir(parents=True, exist_ok=True)
    os.rename(library / old, library / new)


# --- The walk -------------------------------------------------------------------------


def test_registers_every_supported_file_and_skips_the_rest(
    scanner: Scanner, library: Path, settings: Settings
) -> None:
    _photo(library / TRASH_FOLDER / "trashed.jpg")
    scanner.settings = settings.model_copy(update={"trash_dir": library / TRASH_FOLDER})
    (library / "notes.txt").write_text("not a photo")
    (library / "link.jpg").symlink_to(library / "scenes/scene-01.jpg")

    result = scanner.walk()

    items = scanner.items()
    assert result.found == len(items) == SUPPORTED + 1  # with the truncated JPEG
    assert result.new == len(items)
    assert "broken/unsupported.png" not in items
    assert f"{TRASH_FOLDER}/trashed.jpg" not in items
    assert "link.jpg" not in items
    assert all(i.content_hash is None for i in items.values())  # hashed by the scan jobs
    assert items["video/clip.mov"].media_type == MediaType.VIDEO
    assert items["heic/scene.heic"].media_type == MediaType.PHOTO
    clip = (library / "video/clip.mov").stat()
    assert (items["video/clip.mov"].file_size_bytes, items["video/clip.mov"].file_mtime_ns) == (
        clip.st_size,
        clip.st_mtime_ns,
    )
    assert scanner.open_scans() == len(items)


def test_the_scan_jobs_hash_every_file_but_the_corrupt_one(scanner: Scanner, library: Path) -> None:
    scanner.run()

    items = scanner.items()
    hashed = {p for p, i in items.items() if i.content_hash is not None}
    assert hashed == set(items) - {"broken/truncated.jpg"}
    assert len(hashed) == SUPPORTED
    assert len({items[p].content_hash for p in hashed}) == SUPPORTED


def test_a_second_walk_with_nothing_changed_queues_no_scans(
    scanner: Scanner, library: Path
) -> None:
    scanner.run()
    first = scanner.items()
    scanner.later()

    result = scanner.walk()

    assert (result.new, result.changed, result.missing) == (0, 0, 0)
    assert scanner.open_scans() == 0
    after = scanner.items()
    assert {p: i.id for p, i in after.items()} == {p: i.id for p, i in first.items()}
    assert {i.last_seen_at for i in after.values()} == {scanner.clock()}


def test_a_folder_that_cannot_be_listed_is_skipped(
    scanner: Scanner, library: Path, caplog: pytest.LogCaptureFixture
) -> None:
    locked = library / "scenes"
    locked.chmod(0)
    try:
        result = scanner.walk()
    finally:
        locked.chmod(0o755)

    assert result.found == SUPPORTED + 1 - 6
    assert "Can't list" in caplog.text


# --- Changes, moves and copies --------------------------------------------------------


def test_a_moved_file_keeps_its_item(scanner: Scanner, library: Path) -> None:
    scanner.run()
    before = scanner.items()["scenes/scene-02.jpg"]
    scanner.later()

    _move(library, "scenes/scene-02.jpg", "2024/renamed.jpg")
    scanner.run()

    items = scanner.items()
    assert "scenes/scene-02.jpg" not in items
    moved = items["2024/renamed.jpg"]
    assert (moved.id, moved.content_hash, moved.first_seen_at) == (
        before.id,
        before.content_hash,
        before.first_seen_at,
    )
    assert (moved.status, moved.missing_since, moved.last_seen_at) == (
        ItemStatus.ACTIVE,
        None,
        scanner.clock(),
    )
    assert len(items) == SUPPORTED + 1  # the placeholder is gone
    # Same content, so the stages after scan aren't run again.
    assert scanner.output_changed_at("2024/renamed.jpg") == START


def test_a_move_keeps_the_items_jobs_and_records_the_scan_against_it(
    scanner: Scanner, library: Path
) -> None:
    scanner.run()
    item_id = scanner.items()["scenes/scene-02.jpg"].id
    with scanner.sessions.begin() as session:
        scanner.queue.enqueue(session, Stage.THUMBNAIL, item_id)

    _move(library, "scenes/scene-02.jpg", "elsewhere.jpg")
    scanner.run()

    with scanner.sessions() as session:
        jobs = session.execute(
            sa.select(Job.stage, Job.status).where(Job.item_id == item_id).order_by(Job.stage)
        ).all()
    assert jobs == [(Stage.SCAN, JobStatus.DONE), (Stage.THUMBNAIL, JobStatus.PENDING)]


def test_a_missing_file_is_marked_and_comes_back_at_its_path(
    scanner: Scanner, library: Path, tmp_path: Path
) -> None:
    scanner.run()
    item_id = scanner.items()["scenes/scene-03.jpg"].id
    away = tmp_path / "away.jpg"
    os.rename(library / "scenes/scene-03.jpg", away)
    scanner.later()

    result = scanner.walk()

    gone = scanner.items()["scenes/scene-03.jpg"]
    assert result.missing == 1
    assert (gone.status, gone.missing_since) == (ItemStatus.MISSING, scanner.clock())
    found_at = scanner.clock()

    scanner.later()
    os.rename(away, library / "scenes/scene-03.jpg")
    result = scanner.walk()

    back = scanner.items()["scenes/scene-03.jpg"]
    assert result.returned == 1
    assert (back.id, back.status, back.missing_since) == (item_id, ItemStatus.ACTIVE, None)
    assert back.last_seen_at > found_at
    assert scanner.open_scans() == 0  # unchanged, so not rehashed


def test_a_missing_file_that_turns_up_at_a_new_path_is_active_again(
    scanner: Scanner, library: Path, tmp_path: Path
) -> None:
    scanner.run()
    item_id = scanner.items()["scenes/scene-04.jpg"].id
    away = tmp_path / "away.jpg"
    os.rename(library / "scenes/scene-04.jpg", away)
    scanner.later()
    scanner.run()
    assert scanner.items()["scenes/scene-04.jpg"].status == ItemStatus.MISSING

    scanner.later()
    (library / "found").mkdir()
    os.rename(away, library / "found/scene-04.jpg")
    scanner.run()

    items = scanner.items()
    assert "scenes/scene-04.jpg" not in items
    assert (items["found/scene-04.jpg"].id, items["found/scene-04.jpg"].status) == (
        item_id,
        ItemStatus.ACTIVE,
    )


def test_an_exact_copy_is_an_item_of_its_own(scanner: Scanner, library: Path) -> None:
    scanner.run()
    shutil.copy2(library / "scenes/scene-05.jpg", library / "copy.jpg")
    scanner.later()

    scanner.run()
    scanner.later()
    scanner.run()  # and stays one: no moving back and forth

    items = scanner.items()
    original, copy = items["scenes/scene-05.jpg"], items["copy.jpg"]
    assert original.id != copy.id
    assert original.content_hash == copy.content_hash
    assert {original.status, copy.status} == {ItemStatus.ACTIVE}
    assert scanner.open_scans() == 0


def test_a_file_moved_after_the_walk_is_still_a_move(scanner: Scanner, library: Path) -> None:
    """The walk saw both paths, so the old item isn't missing when the scan runs."""
    scanner.run()
    item_id = scanner.items()["scenes/scene-06.jpg"].id
    shutil.copy2(library / "scenes/scene-06.jpg", library / "new.jpg")
    scanner.walk()
    (library / "scenes/scene-06.jpg").unlink()  # the move finishing

    scanner.worker.drain()

    items = scanner.items()
    assert items["new.jpg"].id == item_id
    assert "scenes/scene-06.jpg" not in items


def test_a_file_changed_in_place_keeps_its_item_and_takes_the_new_hash(
    scanner: Scanner, settings: Settings
) -> None:
    photo = _photo(settings.photo_dir / "edited.jpg", "red")
    scanner.run()
    before = scanner.items()["edited.jpg"]

    scanner.later()
    _photo(photo, "blue", size=(9, 9))
    os.utime(photo, ns=(before.file_mtime_ns + 10**9, before.file_mtime_ns + 10**9))
    result = scanner.walk()
    assert result.changed == 1
    scanner.worker.drain()

    after = scanner.items()["edited.jpg"]
    assert after.id == before.id
    assert after.content_hash != before.content_hash
    assert after.file_mtime_ns == before.file_mtime_ns + 10**9
    assert after.file_size_bytes == photo.stat().st_size
    assert scanner.output_changed_at("edited.jpg") == START + dt.timedelta(days=1)


def test_a_changed_mtime_alone_rehashes_to_the_same_hash(
    scanner: Scanner, settings: Settings
) -> None:
    photo = _photo(settings.photo_dir / "touched.jpg")
    scanner.run()
    before = scanner.items()["touched.jpg"]

    os.utime(photo, ns=(0, before.file_mtime_ns + 1))
    scanner.later()
    scanner.run()

    after = scanner.items()["touched.jpg"]
    assert (after.id, after.content_hash) == (before.id, before.content_hash)
    assert after.file_mtime_ns == before.file_mtime_ns + 1
    assert scanner.output_changed_at("touched.jpg") == START


# --- Placeholders ---------------------------------------------------------------------


def test_a_placeholder_whose_file_goes_before_its_scan_is_dropped_by_the_next_walk(
    scanner: Scanner, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    photo = _photo(settings.photo_dir / "brief.jpg")
    scanner.walk()
    photo.unlink()
    caplog.set_level(logging.INFO)

    scanner.worker.drain()  # the scan finds nothing

    assert "gone from brief.jpg before its scan" in caplog.text
    assert scanner.items()["brief.jpg"].content_hash is None
    result = scanner.walk()
    assert result.missing == 1
    assert scanner.items() == {}  # never hashed, so deleted outright


def test_a_scan_for_an_item_deleted_since_does_nothing(
    scanner: Scanner, settings: Settings
) -> None:
    _photo(settings.photo_dir / "a.jpg")
    scanner.walk()
    with scanner.sessions() as session:
        job = session.scalars(sa.select(Job)).one()
        session.expunge(job)
    with scanner.sessions.begin() as session:
        session.execute(sa.delete(Item))

    scan.scan(scanner.sessions, job, settings.photo_dir, scanner.clock())

    assert scanner.items() == {}


def test_a_scan_whose_item_moves_while_hashing_records_nothing(
    scanner: Scanner, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _photo(settings.photo_dir / "a.jpg")
    scanner.walk()
    with scanner.sessions() as session:
        job = session.scalars(sa.select(Job)).one()
        session.expunge(job)
    real_hash = scan.identity.content_hash

    def hash_while_moving(path: Path, kind: MediaType) -> str:
        with scanner.sessions.begin() as session:
            session.execute(sa.update(Item).values(path="b.jpg"))
        return real_hash(path, kind)

    monkeypatch.setattr(scan.identity, "content_hash", hash_while_moving)

    scan.scan(scanner.sessions, job, settings.photo_dir, scanner.clock())

    assert scanner.items()["b.jpg"].content_hash is None


def test_the_corrupt_file_is_retried_then_parked_without_stopping_the_rest(
    scanner: Scanner, library: Path
) -> None:
    scanner.run()

    with scanner.sessions() as session:
        corrupt = session.scalars(
            sa.select(Job)
            .join(Item, Item.id == Job.item_id)
            .where(Item.path == "broken/truncated.jpg")
        ).one()
        statuses = session.execute(
            sa.select(Job.status, sa.func.count())
            .where(Job.stage == Stage.SCAN)
            .group_by(Job.status)
        ).all()
    assert (corrupt.status, corrupt.attempts) == (JobStatus.ERROR, 1)
    assert "HashError" in (corrupt.last_error or "")
    assert dict(statuses) == {JobStatus.DONE: SUPPORTED, JobStatus.ERROR: 1}

    scanner.later(dt.timedelta(hours=1))  # past the backoff
    scanner.worker.drain()

    with scanner.sessions() as session:
        parked = session.get_one(Job, corrupt.id)
    assert (parked.status, parked.attempts) == (JobStatus.PARKED, 2)
    assert scanner.items()["broken/truncated.jpg"].content_hash is None
    scanner.later()
    scanner.walk()
    assert scanner.open_scans() == 0  # unchanged, so not tried again


def test_scan_raises_hash_error_for_a_corrupt_file(scanner: Scanner, library: Path) -> None:
    scanner.walk()
    with scanner.sessions() as session:
        job = session.scalars(
            sa.select(Job)
            .join(Item, Item.id == Job.item_id)
            .where(Item.path == "broken/truncated.jpg")
        ).one()
        session.expunge(job)

    with pytest.raises(HashError):
        scan.scan(scanner.sessions, job, library, scanner.clock())


# --- The worker -----------------------------------------------------------------------


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Settings:
    monkeypatch.setenv("PHOTO_DIR", str(settings.photo_dir))
    monkeypatch.setenv("TRASH_DIR", str(settings.trash_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    return settings


def _scan_jobs(engine: Engine) -> list[JobStatus]:
    with Session(engine) as session:
        return list(session.scalars(sa.select(Job.status).where(Job.stage == Stage.SCAN)))


def test_worker_once_registers_every_supported_file(
    migrated: Engine, worker_env: Settings, library: Path
) -> None:
    assert main(["scan"]) == 0
    assert main(["--once"]) == 0

    with Session(migrated) as session:
        hashed = session.scalar(
            sa.select(sa.func.count()).select_from(Item).where(Item.content_hash.is_not(None))
        )
    assert hashed == SUPPORTED
    assert sorted(_scan_jobs(migrated)) == sorted([JobStatus.DONE] * SUPPORTED + [JobStatus.ERROR])
    log = (worker_env.data_dir / "logs/worker.log").read_text()
    assert f"{SUPPORTED + 1} supported files, {SUPPORTED + 1} new" in log

    assert main(["scan"]) == 0
    assert main(["--once"]) == 0

    # The second walk queued nothing: the scan jobs are as the first run left them.
    assert sorted(_scan_jobs(migrated)) == sorted([JobStatus.DONE] * SUPPORTED + [JobStatus.ERROR])
    assert (
        f"{SUPPORTED + 1} supported files, 0 new, 0 changed, 0 missing"
        in (worker_env.data_dir / "logs/worker.log").read_text()
    )


def test_scan_command_queues_one_walk(migrated: Engine, worker_env: Settings) -> None:
    assert main(["scan"]) == 0
    assert main(["scan"]) == 0

    with Session(migrated) as session:
        walks = session.scalars(sa.select(Job).where(Job.stage == Stage.WALK)).all()
    assert [(j.status, j.item_id) for j in walks] == [(JobStatus.PENDING, None)]
    assert not (worker_env.data_dir / "logs/worker.log").exists()  # stderr only
