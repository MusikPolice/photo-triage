"""The trash (plan §6.8): moving items to it and back, and listing what's in it.

A trashed item's file is renamed into `TRASH_DIR` at the same relative path
(`photo_triage.files.move_to_trash`), and the item becomes `trashed`, so the map,
search and similar items leave it out, and the library walk leaves it alone. Its
derived files and its place on the map stay, so a restore brings it straight back.
The purge, after `TRASH_RETENTION_DAYS`, deletes it for good.

Each request moves its files one at a time inside one transaction. If the
transaction fails to commit, the files moved so far are moved back.
"""

import datetime as dt
import logging
from collections.abc import Callable
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from photo_triage.api.deps import Actor, AppClock, DbSession, get_settings
from photo_triage.db.models import JOB_IS_OPEN, Item, ItemStatus, Job, MediaType, TrashEntry
from photo_triage.files import TrashError, move_to_trash, restore_from_trash
from photo_triage.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_ITEMS = 10_000
"""Items in one request: a lasso round a large part of the map."""

PAGE_SIZE = 200
MAX_PAGE_SIZE = 1_000


class ItemIds(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=MAX_ITEMS)


class Skipped(BaseModel):
    id: int
    reason: str
    """Why it wasn't moved, for people."""


class Moved(BaseModel):
    """What a trash or restore request did, item by item."""

    moved: list[int]
    """The items moved, in the order asked."""
    skipped: list[Skipped]
    """The items left where they were, and why."""


class TrashedItem(BaseModel):
    id: int
    media_type: MediaType
    original_path: str
    """Where it was in the library, and goes back to on a restore."""
    trashed_at: dt.datetime
    actor: str
    """Who trashed it."""
    purge_after: dt.datetime
    """When the purge deletes it for good."""
    thumbnail_url: str
    """Its large thumbnail (`THUMBNAIL_SIZE`), kept while it's in the trash."""


class TrashPage(BaseModel):
    items: list[TrashedItem]
    """Newest first."""
    total: int
    """Items in the trash, on every page."""


@router.post("/trash")
def trash(
    body: ItemIds,
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Actor,
    clock: AppClock,
) -> Moved:
    """Move items to the trash. An item already in the trash, or not in the library,
    or whose file has gone, is skipped with the reason."""
    now = clock()
    purge_after = now + dt.timedelta(days=settings.trash_retention_days)

    def move(item: Item) -> Callable[[], None] | str:
        if item.status == ItemStatus.TRASHED:
            return "It's already in the trash"
        if item.status != ItemStatus.ACTIVE:
            return f"It's {item.status.value}, not in the library"
        original = item.path
        try:
            trash_path = move_to_trash(settings.photo_dir, settings.trash_dir, original)
        except FileNotFoundError:
            return "Its file has gone from the library"
        session.add(
            TrashEntry(
                item_id=item.id,
                original_path=original,
                trash_path=trash_path,
                trashed_at=now,
                actor=actor,
                purge_after=purge_after,
            )
        )
        item.status = ItemStatus.TRASHED
        # Its file has gone from where the stages would read it.
        session.execute(sa.delete(Job).where(Job.item_id == item.id, sa.text(JOB_IS_OPEN)))
        logger.info("%s trashed item %d, %s", actor, item.id, original)
        return lambda: restore_from_trash(
            settings.photo_dir, settings.trash_dir, trash_path, original
        )

    return _move_each(session, body.item_ids, move)


@router.post("/trash/restore")
def restore(
    body: ItemIds,
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Actor,
) -> Moved:
    """Move items back from the trash to where they were. One that isn't in the trash,
    or whose place in the library is taken, is skipped with the reason."""

    def move(item: Item) -> Callable[[], None] | str:
        entry = session.get(TrashEntry, item.id)
        if item.status != ItemStatus.TRASHED or entry is None:
            return "It isn't in the trash"
        try:
            restore_from_trash(
                settings.photo_dir, settings.trash_dir, entry.trash_path, entry.original_path
            )
        except FileNotFoundError:
            return "Its file has gone from the trash"
        item.path = entry.original_path
        item.status = ItemStatus.ACTIVE
        session.delete(entry)
        logger.info("%s restored item %d to %s", actor, item.id, entry.original_path)
        original = entry.original_path

        def undo() -> None:
            # Back into the trash, where the entry, not deleted after all, says it is.
            move_to_trash(settings.photo_dir, settings.trash_dir, original)

        return undo

    return _move_each(session, body.item_ids, move)


@router.get("/trash")
def trashed(
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TrashPage:
    """What's in the trash, the most recently trashed first."""
    rows = session.execute(
        sa.select(TrashEntry, Item.media_type)
        .join(Item, Item.id == TrashEntry.item_id)
        .order_by(TrashEntry.trashed_at.desc(), TrashEntry.item_id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    total = session.scalar(sa.select(sa.func.count()).select_from(TrashEntry)) or 0
    return TrashPage(
        items=[
            TrashedItem(
                id=entry.item_id,
                media_type=media_type,
                original_path=entry.original_path,
                trashed_at=entry.trashed_at,
                actor=entry.actor,
                purge_after=entry.purge_after,
                thumbnail_url=(
                    f"/api/items/{entry.item_id}/thumbnail?size={settings.thumbnail_size}"
                ),
            )
            for entry, media_type in rows
        ],
        total=total,
    )


def _move_each(
    session: Session, item_ids: list[int], move: Callable[[Item], Callable[[], None] | str]
) -> Moved:
    """Run `move` on each item in one transaction. It returns how to undo its file
    move, which runs if the transaction doesn't commit, or why it skipped the item."""
    moved: list[int] = []
    skipped: list[Skipped] = []
    undo: list[Callable[[], None]] = []
    try:
        with session.begin():
            items = _items(session, item_ids)
            for item_id in dict.fromkeys(item_ids):
                item = items.get(item_id)
                if item is None:
                    skipped.append(Skipped(id=item_id, reason="No such item"))
                    continue
                try:
                    result = move(item)
                except (TrashError, OSError) as e:
                    logger.warning("Couldn't move item %d: %s", item_id, e)
                    skipped.append(Skipped(id=item_id, reason=str(e)))
                    continue
                if isinstance(result, str):
                    skipped.append(Skipped(id=item_id, reason=result))
                else:
                    moved.append(item_id)
                    undo.append(result)
    except BaseException:
        for back in reversed(undo):
            try:
                back()
            except (OSError, TrashError):
                logger.exception("Couldn't move a file back after a failed transaction")
        raise
    return Moved(moved=moved, skipped=skipped)


def _items(session: Session, item_ids: list[int]) -> dict[int, Item]:
    items: dict[int, Item] = {}
    unique = list(dict.fromkeys(item_ids))
    for start in range(0, len(unique), 500):
        chunk = unique[start : start + 500]
        items.update((i.id, i) for i in session.scalars(sa.select(Item).where(Item.id.in_(chunk))))
    return items
