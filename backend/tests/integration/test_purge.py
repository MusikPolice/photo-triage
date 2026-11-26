"""The `purge` batch job (plan §6.8): trashed files past `TRASH_RETENTION_DAYS` are
deleted with their derived files and their items marked purged, and nothing younger
is touched. With `EXIF_WRITES_ENABLED` off it only logs. Queued daily
(`photo_triage.worker.purge_schedule`)."""

import datetime as dt
import logging
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vectors import Library

from photo_triage.db.models import Item, ItemFrame, ItemStatus, Job, JobStatus, TrashEntry
from photo_triage.files import move_to_trash
from photo_triage.pipeline import purge
from photo_triage.pipeline.frames import frame_path
from photo_triage.pipeline.thumbnails import Thumbnail, thumbnail_path
from photo_triage.settings import Settings
from photo_triage.worker import purge_schedule
from photo_triage.worker.__main__ import main
from photo_triage.worker.queue import PRIORITY, JobQueue, Stage

NOW = dt.datetime(2026, 11, 24, 12, tzinfo=dt.UTC)
SECOND = dt.timedelta(seconds=1)


def trashed(settings: Settings, lib: Library, purge_after: dt.datetime) -> int:
    """An item in the trash, with thumbnails and a frame, due for purging at
    `purge_after`."""
    item_id = lib.add(None)
    with Session(lib.engine) as session, session.begin():
        item = session.get(Item, item_id)
        assert item is not None
        source = settings.photo_dir / item.path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"item {item_id}".encode())
        trash_path = move_to_trash(settings.photo_dir, settings.trash_dir, item.path)
        item.status = ItemStatus.TRASHED
        session.add(
            TrashEntry(
                item_id=item_id,
                original_path=item.path,
                trash_path=trash_path,
                trashed_at=purge_after - dt.timedelta(days=30),
                actor="ada",
                purge_after=purge_after,
            )
        )
        session.add(ItemFrame(item_id=item_id, frame_index=0, timestamp_s=0.0))
    for path in (*derived_files(settings, item_id), frame_path(settings.derived_dir, item_id, 0)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"derived")
    return item_id


def derived_files(settings: Settings, item_id: int) -> list[Path]:
    return [thumbnail_path(settings.derived_dir, item_id, kind) for kind in Thumbnail]


def in_trash(settings: Settings, engine: Engine, item_id: int) -> Path:
    with Session(engine) as session:
        entry = session.get(TrashEntry, item_id)
        assert entry is not None
        return settings.trash_dir / entry.trash_path


def state(engine: Engine, item_id: int) -> tuple[ItemStatus, bool, int]:
    """The item's status, whether it has a trash entry, and how many frame rows."""
    with Session(engine) as session:
        item = session.get(Item, item_id)
        assert item is not None
        frames = session.scalar(
            sa.select(sa.func.count()).select_from(ItemFrame).where(ItemFrame.item_id == item_id)
        )
        return item.status, session.get(TrashEntry, item_id) is not None, frames or 0


def run(
    engine: Engine, settings: Settings, *, enabled: bool = True, now: dt.datetime = NOW
) -> purge.Purged:
    return purge.purge(
        sessionmaker(engine), settings.trash_dir, settings.derived_dir, enabled=enabled, now=now
    )


def test_only_entries_past_retention_are_purged(
    lib: Library, migrated: Engine, settings: Settings
) -> None:
    young = trashed(settings, lib, NOW + SECOND)
    old = trashed(settings, lib, NOW - SECOND)
    young_file, old_file = in_trash(settings, migrated, young), in_trash(settings, migrated, old)

    assert run(migrated, settings) == purge.Purged(purged=1, missing=0, dry_run=0)

    assert not old_file.exists()
    assert state(migrated, old) == (ItemStatus.PURGED, False, 0)
    assert not any(p.exists() for p in derived_files(settings, old))
    assert not frame_path(settings.derived_dir, old, 0).parent.exists()

    assert young_file.read_bytes() == f"item {young}".encode()
    assert state(migrated, young) == (ItemStatus.TRASHED, True, 1)
    assert all(p.exists() for p in derived_files(settings, young))
    assert frame_path(settings.derived_dir, young, 0).exists()


def test_an_entry_due_exactly_now_is_purged(
    lib: Library, migrated: Engine, settings: Settings
) -> None:
    item_id = trashed(settings, lib, NOW)

    run(migrated, settings)

    assert state(migrated, item_id)[0] == ItemStatus.PURGED


def test_a_file_already_gone_marks_its_item_purged_with_a_warning(
    lib: Library, migrated: Engine, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    item_id = trashed(settings, lib, NOW - SECOND)
    in_trash(settings, migrated, item_id).unlink()

    with caplog.at_level(logging.WARNING):
        result = run(migrated, settings)

    assert result == purge.Purged(purged=0, missing=1, dry_run=0)
    assert state(migrated, item_id) == (ItemStatus.PURGED, False, 0)
    assert f"Item {item_id}'s file" in caplog.text and "already gone" in caplog.text


def test_with_writes_off_nothing_is_deleted_and_each_file_is_logged(
    lib: Library, migrated: Engine, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    item_id = trashed(settings, lib, NOW - dt.timedelta(days=3))
    path = in_trash(settings, migrated, item_id)

    with caplog.at_level(logging.INFO):
        result = run(migrated, settings, enabled=False)

    assert result == purge.Purged(purged=0, missing=0, dry_run=1)
    assert path.exists()
    assert state(migrated, item_id) == (ItemStatus.TRASHED, True, 1)
    assert all(p.exists() for p in derived_files(settings, item_id))
    relative = path.relative_to(settings.trash_dir).as_posix()
    assert f"Dry run: would purge {relative} from the trash (item {item_id})" in caplog.text


def test_a_file_that_can_t_be_purged_fails_the_job_after_the_rest(
    lib: Library, migrated: Engine, settings: Settings
) -> None:
    stuck = trashed(settings, lib, NOW - 2 * SECOND)
    fine = trashed(settings, lib, NOW - SECOND)
    path = in_trash(settings, migrated, stuck)
    path.unlink()
    path.mkdir()  # a folder where its file was: refused, and left

    with pytest.raises(purge.PurgeError, match="1 trashed files couldn't be purged"):
        run(migrated, settings)

    assert path.is_dir()
    assert state(migrated, stuck) == (ItemStatus.TRASHED, True, 1)
    assert state(migrated, fine)[0] == ItemStatus.PURGED


def test_an_item_restored_since_the_list_was_read_is_left(
    lib: Library, migrated: Engine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    item_id = trashed(settings, lib, NOW - SECOND)
    path = in_trash(settings, migrated, item_id)
    with Session(migrated) as session:
        listed = purge.due(session, NOW)
    with Session(migrated) as session, session.begin():
        session.execute(sa.delete(TrashEntry))
    monkeypatch.setattr(purge, "due", lambda session, now: listed)

    assert run(migrated, settings) == purge.Purged(purged=0, missing=0, dry_run=0)

    assert path.exists()


def test_nothing_due_does_nothing(migrated: Engine, settings: Settings) -> None:
    assert run(migrated, settings) == purge.Purged(purged=0, missing=0, dry_run=0)
    assert run(migrated, settings, enabled=False) == purge.Purged(purged=0, missing=0, dry_run=0)


# --- When it's queued --------------------------------------------------------------


def _job(session: Session, status: JobStatus, at: dt.datetime) -> None:
    session.add(
        Job(
            stage=Stage.PURGE,
            priority=PRIORITY[Stage.PURGE],
            status=status,
            enqueued_at=at,
            finished_at=at if status in (JobStatus.DONE, JobStatus.PARKED) else None,
        )
    )


def _purges(session: Session) -> int:
    return session.scalar(sa.select(sa.func.count()).where(Job.stage == Stage.PURGE)) or 0


def test_a_purge_is_due_at_first_and_then_a_day_after_the_last(migrated: Engine) -> None:
    queue = JobQueue(lambda: NOW)
    with Session(migrated) as session, session.begin():
        assert purge_schedule.queue_if_due(session, queue, NOW) is None
        assert _purges(session) == 1
        # Open: not queued again, however late.
        assert purge_schedule.queue_if_due(session, queue, NOW + dt.timedelta(days=3)) is None
        assert _purges(session) == 1

        session.execute(sa.delete(Job))
        _job(session, JobStatus.DONE, NOW)
        later = NOW + dt.timedelta(hours=23)
        assert purge_schedule.queue_if_due(session, queue, later) == NOW + dt.timedelta(days=1)
        assert _purges(session) == 1
        assert purge_schedule.queue_if_due(session, queue, NOW + dt.timedelta(days=1)) is None
        assert _purges(session) == 2


@pytest.mark.parametrize("status", [JobStatus.RUNNING, JobStatus.ERROR, JobStatus.PENDING])
def test_an_unfinished_purge_isn_t_doubled(migrated: Engine, status: JobStatus) -> None:
    with Session(migrated) as session, session.begin():
        _job(session, status, NOW - dt.timedelta(days=2))
        assert purge_schedule.is_open(session)
        assert purge_schedule.queue_if_due(session, JobQueue(lambda: NOW), NOW) is None
        assert _purges(session) == 1


def test_a_parked_purge_is_tried_again_a_day_later(migrated: Engine) -> None:
    queue = JobQueue(lambda: NOW)
    with Session(migrated) as session, session.begin():
        _job(session, JobStatus.PARKED, NOW - dt.timedelta(hours=1))
        assert purge_schedule.queue_if_due(session, queue, NOW) is not None
        assert _purges(session) == 1
        assert purge_schedule.queue_if_due(session, queue, NOW + dt.timedelta(days=1)) is None
        assert _purges(session) == 2


def test_the_next_purge_shown_is_a_day_after_the_last(migrated: Engine) -> None:
    assert purge_schedule.next_at(None) is None
    assert purge_schedule.next_at(NOW) == NOW + dt.timedelta(days=1)


def test_the_worker_purges_at_start_by_its_clock(
    lib: Library,
    migrated: Engine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FAKE_NOW` sets the worker's clock, which runs on from there in real time: an
    entry due a minute after it stays, and one due a second before it goes. (The
    exact boundary is `test_only_entries_past_retention_are_purged`'s.)"""
    young = trashed(settings, lib, NOW + dt.timedelta(minutes=1))
    old = trashed(settings, lib, NOW - SECOND)
    for name in ("PHOTO_DIR", "TRASH_DIR", "DATA_DIR", "MODELS_DIR"):
        monkeypatch.setenv(name, str(getattr(settings, name.lower())))
    monkeypatch.setenv("FAKE_NOW", NOW.isoformat())
    monkeypatch.setenv("EXIF_WRITES_ENABLED", "true")

    assert main(["--once"]) == 0

    assert state(migrated, old)[0] == ItemStatus.PURGED
    assert state(migrated, young)[0] == ItemStatus.TRASHED
    with Session(migrated) as session:
        statuses = session.scalars(sa.select(Job.status).where(Job.stage == Stage.PURGE))
        assert list(statuses) == [JobStatus.DONE]
