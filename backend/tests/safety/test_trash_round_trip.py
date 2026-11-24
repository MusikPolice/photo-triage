"""Trash round-trip and no escape for the trash (dev-environment §7): every tier-1
fixture trashed through the API and restored is byte-identical at its original
path, with its mtime unchanged, and the moves write nowhere but `PHOTO_DIR`,
`TRASH_DIR` and `DATA_DIR`.

The trash is inside the library, `PHOTO_DIR/.photo-triage-trash`, as by default.
"""

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient
from test_no_escape import Snapshot, differences, outside, snapshot

from photo_triage.api.app import create_app
from photo_triage.db.models import Item, ItemStatus
from photo_triage.ml import FakeClip
from photo_triage.settings import TRASH_FOLDER, Settings
from photo_triage.worker.__main__ import main


def files_under(entries: Snapshot, folder: Path) -> Snapshot:
    """The files' entries: bytes, size and mtime. Folders' mtimes change with a move."""
    return {p: e for p, e in entries.items() if e.kind == "file" and Path(p).is_relative_to(folder)}


@pytest.fixture
def processed(
    migrated: Engine, settings: Settings, library: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    """The library processed by the worker, with the trash inside it."""
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
    return inside


def test_every_fixture_trashed_and_restored_is_as_it_was(
    processed: Settings, migrated: Engine, tmp_path: Path
) -> None:
    with Session(migrated) as session:
        ids = session.scalars(sa.select(Item.id).where(Item.status == ItemStatus.ACTIVE)).all()
    assert len(ids) == 30  # every tier-1 file but the PNG, which isn't a supported type
    root = tmp_path
    photos = processed.photo_dir.relative_to(root)
    trash = processed.trash_dir.relative_to(root)
    data = processed.data_dir.relative_to(root)
    before = snapshot(root)

    with TestClient(create_app(processed)) as client:
        trashed = client.post("/api/trash", json={"item_ids": ids}).json()
        assert trashed == {"moved": list(ids), "skipped": []}
        middle = snapshot(root)
        restored = client.post("/api/trash/restore", json={"item_ids": ids}).json()
        assert restored == {"moved": list(ids), "skipped": []}
    after = snapshot(root)

    # In the trash, every item's file is there, as it was, at the same relative path.
    library = files_under(before, photos)
    in_trash = files_under(middle, trash)
    assert len(library) == 31
    assert len(in_trash) == 30
    for path, entry in in_trash.items():
        assert library[str(photos / Path(path).relative_to(trash))] == entry
    left = {p for p in files_under(middle, photos) if p not in in_trash}
    assert [Path(p).suffix for p in left] == [".png"]

    # Restored: byte-identical with the same mtime, at the original paths, and the
    # trash is empty again.
    assert files_under(after, photos) == library
    assert [p for p in after if Path(p).is_relative_to(trash)] == [str(trash)]

    # No escape: only the library (which holds the trash) and DATA_DIR changed.
    changed = differences(before, middle) | differences(middle, after)
    assert outside(outside(changed, photos), data) == set()
    with Session(migrated) as session:
        statuses = set(session.scalars(sa.select(Item.status).where(Item.id.in_(ids))))
    assert statuses == {ItemStatus.ACTIVE}


def test_the_library_s_folders_stay_when_emptied(processed: Settings, migrated: Engine) -> None:
    folders = {p for p in processed.photo_dir.rglob("*") if p.is_dir()}
    with Session(migrated) as session:
        ids = session.scalars(sa.select(Item.id)).all()

    with TestClient(create_app(processed)) as client:
        client.post("/api/trash", json={"item_ids": ids})

    assert folders <= {p for p in processed.photo_dir.rglob("*") if p.is_dir()}
    left = [
        f
        for f in processed.photo_dir.rglob("*")
        if f.is_file() and not f.is_relative_to(processed.trash_dir)
    ]
    assert [f.suffix for f in left] == [".png"]
    assert os.listdir(processed.trash_dir)
