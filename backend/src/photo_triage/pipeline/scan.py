"""The library scanner: the `walk` batch job and the per-file `scan` job (plan §6.1).

A **walk** lists the supported files under `PHOTO_DIR`, skipping `TRASH_DIR`, and
compares them with `items` by `(path, mtime, size)`:

- a new path gets a placeholder item, with no content hash yet, and a scan job;
- a known path whose mtime or size changed gets a scan job;
- an active item whose path wasn't found is marked missing, and a placeholder that
  was never hashed is deleted, since it has no history;
- a missing item found again at its path is active again.

A **scan** hashes one file's content (`identity.content_hash`). For a placeholder,
a hash that belongs to an item whose file is gone (usually marked missing by the
same walk) means the file moved: that item takes the new path and becomes active,
keeping its history, and the placeholder is deleted. Otherwise, including when the
item with that hash is still at its own path (an exact copy), the placeholder keeps
the hash and becomes an item of its own. A known item whose file changed in place
takes its new hash and stays the same item.

Both only read files. Deleting a placeholder is a database delete.
"""

import datetime as dt
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, ItemStatus, Job, MediaType
from photo_triage.pipeline import identity
from photo_triage.worker.queue import JobQueue, Stage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FoundFile:
    path: str
    """Relative to `PHOTO_DIR`, with `/` separators, as `Item.path` holds it."""
    media_type: MediaType
    file_size_bytes: int
    file_mtime_ns: int


@dataclass(frozen=True)
class WalkResult:
    found: int
    new: int
    changed: int
    missing: int
    returned: int
    """Missing items found again at their old path."""


def find_files(photo_dir: Path, trash_dir: Path) -> Iterator[FoundFile]:
    """Every supported file under `photo_dir`, not following symlinks and not
    entering `trash_dir`. A folder that can't be listed is logged and skipped."""
    trash = trash_dir.resolve()
    pending = [photo_dir.resolve()]
    while pending:
        folder = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError as e:
            logger.warning("Can't list %s, skipping it: %s", folder, e)
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if Path(entry.path) != trash:
                    pending.append(Path(entry.path))
                continue
            kind = identity.media_type(Path(entry.name))
            if kind is None or not entry.is_file(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
            yield FoundFile(
                path=PurePosixPath(Path(entry.path).relative_to(photo_dir.resolve())).as_posix(),
                media_type=kind,
                file_size_bytes=stat.st_size,
                file_mtime_ns=stat.st_mtime_ns,
            )


def walk(
    session: Session,
    queue: JobQueue,
    photo_dir: Path,
    trash_dir: Path,
    now: dt.datetime,
) -> WalkResult:
    """Compare the files under `photo_dir` with `items`, and queue a scan job for each
    new or changed file. Lists the files before writing anything, so the write lock
    is held only for the comparison."""
    files = {f.path: f for f in find_files(photo_dir, trash_dir)}
    # Trashed and purged items aren't under PHOTO_DIR's walk, so they're left alone.
    items = {
        item.path: item
        for item in session.scalars(
            sa.select(Item).where(Item.status.in_([ItemStatus.ACTIVE, ItemStatus.MISSING]))
        )
    }
    new = changed = returned = missing = 0
    to_scan: list[Item] = []
    for path, found in files.items():
        item = items.get(path)
        if item is None:
            item = Item(
                path=path,
                media_type=found.media_type,
                file_size_bytes=found.file_size_bytes,
                file_mtime_ns=found.file_mtime_ns,
                first_seen_at=now,
                last_seen_at=now,
                status=ItemStatus.ACTIVE,
            )
            session.add(item)
            to_scan.append(item)
            new += 1
            continue
        item.last_seen_at = now
        if item.status == ItemStatus.MISSING:
            item.status = ItemStatus.ACTIVE
            item.missing_since = None
            returned += 1
        if (item.file_size_bytes, item.file_mtime_ns) != (
            found.file_size_bytes,
            found.file_mtime_ns,
        ):
            # The scan job records the new size and mtime once it has hashed the file.
            to_scan.append(item)
            changed += 1
    for path, item in items.items():
        if path in files or item.status != ItemStatus.ACTIVE:
            continue
        if item.content_hash is None:
            session.delete(item)  # never hashed, so nothing to keep
        else:
            item.status = ItemStatus.MISSING
            item.missing_since = now
        missing += 1
    session.flush()
    for item in to_scan:
        queue.enqueue(session, Stage.SCAN, item.id)
    return WalkResult(
        found=len(files), new=new, changed=changed, missing=missing, returned=returned
    )


def scan(sessions: sessionmaker[Session], job: Job, photo_dir: Path, now: dt.datetime) -> bool:
    """Hash the file of `job`'s item, and treat it as a move if it's a placeholder
    whose hash belongs to an item whose file is gone. Raises `identity.HashError` if
    the file can't be decoded, so the job is retried and then parked.

    Returns whether the item's content hash changed, so later stages run again only
    for new content: a move, or a file whose metadata alone was edited, keeps it.

    The hashing happens between two short transactions, so the API can write
    meanwhile."""
    assert job.item_id is not None
    with sessions() as session:
        item = session.get(Item, job.item_id)
        if item is None:
            return False  # deleted since the job was queued
        relative, kind = item.path, item.media_type
    path = photo_dir / relative
    try:
        stat = path.stat()
    except FileNotFoundError:
        # Gone since the walk. The next walk deals with it as with any missing file.
        logger.info("Item %d is gone from %s before its scan", job.item_id, relative)
        return False
    # Stat before hashing: if the file changes meanwhile, the next walk sees it.
    content_hash = identity.content_hash(path, kind)

    with sessions.begin() as session:
        item = session.get(Item, job.item_id)
        if item is None or item.path != relative:
            return False  # changed meanwhile; the next walk catches up
        item.file_size_bytes = stat.st_size
        item.file_mtime_ns = stat.st_mtime_ns
        item.last_seen_at = now
        if item.content_hash is None:
            moved = _moved_from(session, content_hash, photo_dir)
            if moved is not None:
                _move(session, job, placeholder=item, moved=moved, now=now)
                return False
        changed = item.content_hash != content_hash
        item.content_hash = content_hash
        return changed


def _moved_from(session: Session, content_hash: str, photo_dir: Path) -> Item | None:
    """The item a placeholder with this hash was moved from, if any: a missing one
    (missing longest first), or else an active one whose file is gone since the walk
    that found the placeholder. An active item still at its path is a copy's original."""
    candidates = session.scalars(
        sa.select(Item)
        .where(
            Item.content_hash == content_hash,
            Item.status.in_([ItemStatus.MISSING, ItemStatus.ACTIVE]),
        )
        .order_by(Item.missing_since.is_(None), Item.missing_since, Item.id)
    ).all()
    for candidate in candidates:
        if candidate.status == ItemStatus.MISSING or not (photo_dir / candidate.path).exists():
            return candidate
    return None


def _move(session: Session, job: Job, *, placeholder: Item, moved: Item, now: dt.datetime) -> None:
    """`moved` takes over the placeholder's path and file, and the placeholder goes."""
    logger.info("Item %d moved from %s to %s", moved.id, moved.path, placeholder.path)
    # The running job would be deleted with its item, so it becomes the moved item's.
    session.execute(sa.update(Job).where(Job.id == job.id).values(item_id=moved.id))
    moved.path = placeholder.path
    moved.media_type = placeholder.media_type
    moved.file_size_bytes = placeholder.file_size_bytes
    moved.file_mtime_ns = placeholder.file_mtime_ns
    moved.last_seen_at = now
    moved.status = ItemStatus.ACTIVE
    moved.missing_since = None
    session.delete(placeholder)
