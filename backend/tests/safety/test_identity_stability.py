"""Identity stability (dev-environment §7): an item's content hash survives metadata
writes and moves, and a moved file keeps its item and so its history."""

import datetime as dt
import os
import subprocess
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.db.models import Item, ItemStatus
from photo_triage.settings import Settings
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import JobQueue, Stage
from photo_triage.worker.stages import build_runners

CORRUPT = "broken/truncated.jpg"


def _scan(engine: Engine, settings: Settings, now: dt.datetime) -> dict[str, tuple[int, str]]:
    """Walk and scan the library, returning each active item's id and hash by path."""

    def clock() -> dt.datetime:
        return now

    queue = JobQueue(clock)
    with Session(engine) as session, session.begin():
        queue.enqueue(session, Stage.WALK)
    Worker(engine, build_runners(settings, engine, queue, clock), queue=queue, clock=clock).drain()
    with Session(engine) as session:
        items = session.scalars(sa.select(Item).where(Item.status == ItemStatus.ACTIVE)).all()
        return {i.path: (i.id, i.content_hash or "") for i in items if i.path != CORRUPT}


def test_hashes_survive_metadata_writes_and_moves_and_items_keep_their_ids(
    migrated: Engine, settings: Settings, library: Path
) -> None:
    start = dt.datetime(2026, 10, 18, tzinfo=dt.UTC)
    before = _scan(migrated, settings, start)
    assert before and all(h for _, h in before.values())

    # Metadata written by another tool, which changes every file's bytes and mtime,
    # then every file moved to a new folder and name.
    files = [library / p for p in before]
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-XMP-dc:Subject=moved", "-Artist=Someone"]
        + [str(f) for f in files],
        check=True,
    )
    moved = {}
    for path in before:
        new = f"reorganised/{path.replace('/', '-')}"
        (library / new).parent.mkdir(exist_ok=True)
        os.rename(library / path, library / new)
        moved[new] = path

    after = _scan(migrated, settings, start + dt.timedelta(days=1))

    assert set(after) == set(moved)
    assert {new: after[new] for new in moved} == {new: before[old] for new, old in moved.items()}
    with Session(migrated) as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Item)) == len(before) + 1
