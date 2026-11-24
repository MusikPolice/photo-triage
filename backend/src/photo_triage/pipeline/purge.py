"""The `purge` batch job: deleting trashed files for good once they've been in the
trash `TRASH_RETENTION_DAYS` (plan §6.8).

Each trash entry whose `purge_after` has passed loses its file
(`photo_triage.files.purge_from_trash`) and its derived files: thumbnails and video
frames. Its item becomes `purged` and the entry is deleted. The item's row stays, as
the record that it was there. Each item is done on its own, so a run cut short
leaves every item either purged or still in the trash, and a file deleted just
before the cut is found gone next time and its item marked purged then.

With `EXIF_WRITES_ENABLED` off (the default), the purge is a dry run: it logs each
file it would delete and deletes nothing, so nothing that can't be undone happens
to a library file until writes are enabled.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, ItemFrame, ItemStatus, TrashEntry
from photo_triage.files import TrashError, purge_from_trash, remove_derived
from photo_triage.pipeline.frames import frames_dir
from photo_triage.pipeline.thumbnails import Thumbnail, thumbnail_path

logger = logging.getLogger(__name__)


class PurgeError(Exception):
    """Some files couldn't be purged. The rest were."""


@dataclass(frozen=True)
class Purged:
    purged: int
    """Items purged, their files deleted."""
    missing: int
    """Items purged whose files had already gone from the trash."""
    dry_run: int
    """Items past retention left in the trash, since writes are off."""


@dataclass(frozen=True)
class _Due:
    item_id: int
    trash_path: str
    original_path: str


def due(session: Session, now: dt.datetime) -> list[_Due]:
    """The trash entries past retention at `now`, the longest overdue first."""
    rows = session.execute(
        sa.select(TrashEntry.item_id, TrashEntry.trash_path, TrashEntry.original_path)
        .where(TrashEntry.purge_after <= now)
        .order_by(TrashEntry.purge_after, TrashEntry.item_id)
    )
    return [_Due(*row) for row in rows]


def purge(
    sessions: sessionmaker[Session],
    trash_dir: Path,
    derived_dir: Path,
    *,
    enabled: bool,
    now: dt.datetime,
) -> Purged:
    """Purge the trash entries past retention at `now`, or with `enabled` off, log
    what would be. Raises `PurgeError` after the rest if any file couldn't be."""
    with sessions() as session:
        entries = due(session, now)
    if not enabled:
        for entry in entries:
            logger.info(
                "Dry run: would purge %s from the trash (item %d); EXIF_WRITES_ENABLED is off",
                entry.trash_path,
                entry.item_id,
            )
        if entries:
            logger.info("Dry run: %d trashed items past retention left in the trash", len(entries))
        return Purged(purged=0, missing=0, dry_run=len(entries))

    purged = missing = 0
    failed: list[str] = []
    for entry in entries:
        with sessions() as session:
            # Restored since the list was read: leave it be.
            if session.get(TrashEntry, entry.item_id) is None:
                continue
        try:
            deleted = purge_from_trash(trash_dir, entry.trash_path, entry.original_path)
        except (TrashError, OSError) as e:
            logger.error("Couldn't purge item %d: %s", entry.item_id, e)
            failed.append(entry.trash_path)
            continue
        if not deleted:
            logger.warning(
                "Item %d's file %s had already gone from the trash; marking it purged",
                entry.item_id,
                entry.trash_path,
            )
        _remove_derived(derived_dir, entry.item_id)
        with sessions.begin() as session:
            item = session.get(Item, entry.item_id)
            if item is not None:
                item.status = ItemStatus.PURGED
            session.execute(sa.delete(ItemFrame).where(ItemFrame.item_id == entry.item_id))
            session.execute(sa.delete(TrashEntry).where(TrashEntry.item_id == entry.item_id))
        logger.info("Purged item %d, %s", entry.item_id, entry.trash_path)
        if deleted:
            purged += 1
        else:
            missing += 1
    if failed:
        raise PurgeError(f"{len(failed)} trashed files couldn't be purged: {', '.join(failed)}")
    return Purged(purged=purged, missing=missing, dry_run=0)


def _remove_derived(derived_dir: Path, item_id: int) -> None:
    for kind in Thumbnail:
        remove_derived(derived_dir, thumbnail_path(derived_dir, item_id, kind))
    remove_derived(derived_dir, frames_dir(derived_dir, item_id))
