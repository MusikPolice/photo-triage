"""Metadata write-back's safety tests (dev-environment §7), over every tier-1 item,
through the worker's own `main`:

- **Dry run is inert:** with `EXIF_WRITES_ENABLED` off, writes queued for every
  item are logged as a dry run, and every file's bytes and mtime, and every
  folder's, are as they were.
- **Pixel preservation:** with it on, every file's content hash (its decoded pixels,
  or a video's media streams) and exact mtime are unchanged by the writes.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from test_no_escape import differences, outside, snapshot

from photo_triage.db.models import Item, MetadataWrite, WriteStatus
from photo_triage.ml import FakeClip
from photo_triage.pipeline import identity, writeback
from photo_triage.pipeline.writeback import Field
from photo_triage.settings import Settings
from photo_triage.worker.__main__ import main
from photo_triage.worker.clock import utc_now
from photo_triage.worker.queue import JobQueue

TAGS = ["dog", "crème", "Ada Lovelace"]


@pytest.fixture
def processed(
    migrated: Engine, settings: Settings, library: Path, monkeypatch: pytest.MonkeyPatch
) -> list[Item]:
    """The library processed by the worker, then a write queued for every item."""
    settings.trash_dir.mkdir()
    for name in ("PHOTO_DIR", "TRASH_DIR", "DATA_DIR", "MODELS_DIR"):
        monkeypatch.setenv(name, str(getattr(settings, name.lower())))
    assert main(["scan"]) == 0
    assert main(["--once"], clip=FakeClip()) == 0
    queue = JobQueue()
    with Session(migrated, expire_on_commit=False) as session, session.begin():
        items = list(session.scalars(sa.select(Item)))
        for item in items:
            writeback.queue_write(
                session,
                queue,
                item.id,
                {Field.TAGS: TAGS, Field.REVIEWED: ["duplicates"]},
                actor="safety",
                now=utc_now(),
            )
    assert len(items) == 30  # every tier-1 file but the PNG
    return items


def _statuses(engine: Engine) -> list[WriteStatus]:
    with Session(engine) as session:
        return list(session.scalars(sa.select(MetadataWrite.status)))


def test_a_dry_run_changes_no_file(
    processed: list[Item], settings: Settings, migrated: Engine, tmp_path: Path
) -> None:
    before = snapshot(tmp_path)

    assert main(["--once"], clip=FakeClip()) == 0

    after = snapshot(tmp_path)
    data = settings.data_dir.relative_to(tmp_path)
    assert outside(differences(before, after), data) == set()
    assert _statuses(migrated) == [WriteStatus.DRY_RUN] * len(processed)


def test_writes_keep_every_file_s_pixels_and_mtime(
    processed: list[Item],
    settings: Settings,
    migrated: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXIF_WRITES_ENABLED", "true")
    before = {
        item.id: (identity.hash_content(settings.photo_dir / item.path, item.media_type), item)
        for item in processed
        if item.content_hash is not None  # not the truncated JPEG, which can't be hashed
    }
    mtimes = {i.id: (settings.photo_dir / i.path).stat().st_mtime_ns for i in processed}

    assert main(["--once"], clip=FakeClip()) == 0

    statuses = _statuses(migrated)
    assert statuses.count(WriteStatus.APPLIED) >= len(before)
    for content, item in before.values():
        path = settings.photo_dir / item.path
        assert identity.hash_content(path, item.media_type) == content, item.path
        assert content[0] == item.content_hash
    for item in processed:
        assert (settings.photo_dir / item.path).stat().st_mtime_ns == mtimes[item.id], item.path
