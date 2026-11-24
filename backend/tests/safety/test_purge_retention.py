"""The purge removes only what's past retention (dev-environment §7): every tier-1
fixture trashed through the API, half of them long enough ago to be due. A purge
removes exactly those from `TRASH_DIR`, and nothing else changes outside `DATA_DIR`.
With `EXIF_WRITES_ENABLED` off, the purge removes nothing at all.

The trash is inside the library, `PHOTO_DIR/.photo-triage-trash`, as by default.
"""

import datetime as dt
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient
from test_no_escape import differences, outside, snapshot

from photo_triage.api.app import create_app
from photo_triage.db.models import Item, ItemStatus, TrashEntry
from photo_triage.ml import FakeClip
from photo_triage.pipeline import purge
from photo_triage.settings import TRASH_FOLDER, Settings
from photo_triage.worker.__main__ import main

NOW = dt.datetime(2026, 11, 24, 12, tzinfo=dt.UTC)


@pytest.fixture
def halves(
    migrated: Engine,
    settings: Settings,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Settings, list[int], list[int]]:
    """The processed library, every item trashed: the first half 31 days before NOW,
    so they're due, and the rest 29 days before, so they aren't."""
    inside = Settings(
        photo_dir=settings.photo_dir,
        trash_dir=settings.photo_dir / TRASH_FOLDER,
        data_dir=settings.data_dir,
        models_dir=settings.models_dir,
    )
    for name in ("PHOTO_DIR", "TRASH_DIR", "DATA_DIR", "MODELS_DIR"):
        monkeypatch.setenv(name, str(getattr(inside, name.lower())))
    assert main(["scan"]) == 0
    assert main(["--once"], clip=FakeClip()) == 0
    with Session(migrated) as session:
        ids = list(
            session.scalars(
                sa.select(Item.id).where(Item.status == ItemStatus.ACTIVE).order_by(Item.id)
            )
        )
    due, kept = ids[: len(ids) // 2], ids[len(ids) // 2 :]
    app = create_app(inside)
    with TestClient(app) as client:
        for group, days in ((due, 31), (kept, 29)):
            app.state.clock = lambda days=days: NOW - dt.timedelta(days=days)
            moved = client.post("/api/trash", json={"item_ids": group}).json()
            assert moved == {"moved": group, "skipped": []}
    return inside, due, kept


def trash_files(settings: Settings, engine: Engine, ids: list[int]) -> set[str]:
    with Session(engine) as session:
        paths = session.scalars(
            sa.select(TrashEntry.trash_path).where(TrashEntry.item_id.in_(ids))
        ).all()
    return {str(settings.trash_dir / p) for p in paths}


def test_a_purge_removes_only_what_s_past_retention(
    halves: tuple[Settings, list[int], list[int]], migrated: Engine, tmp_path: Path
) -> None:
    settings, due, kept = halves
    due_files = trash_files(settings, migrated, due)
    kept_files = trash_files(settings, migrated, kept)
    assert len(due_files) == len(due) > 0 and len(kept_files) == len(kept) > 0
    data = settings.data_dir.relative_to(tmp_path)
    before = snapshot(tmp_path)

    result = purge.purge(
        sessionmaker(migrated), settings.trash_dir, settings.derived_dir, enabled=True, now=NOW
    )

    after = snapshot(tmp_path)
    assert result == purge.Purged(purged=len(due), missing=0, dry_run=0)
    changed = outside(differences(before, after), data)
    removed_files = {str(tmp_path / p) for p in changed if before[p].kind == "file"}
    assert removed_files == due_files
    # Every other change is a trash folder the purge emptied and removed, or the
    # folder that held it.
    for path in changed:
        full = tmp_path / path
        assert full.is_relative_to(settings.trash_dir)
        assert str(full) in due_files or before[path].kind == "dir"
    assert all(Path(p).read_bytes() for p in kept_files)


def test_with_writes_off_a_purge_removes_nothing(
    halves: tuple[Settings, list[int], list[int]], migrated: Engine, tmp_path: Path
) -> None:
    settings, due, _ = halves
    data = settings.data_dir.relative_to(tmp_path)
    before = snapshot(tmp_path)

    result = purge.purge(
        sessionmaker(migrated), settings.trash_dir, settings.derived_dir, enabled=False, now=NOW
    )

    assert result == purge.Purged(purged=0, missing=0, dry_run=len(due))
    assert outside(differences(before, snapshot(tmp_path)), data) == set()
