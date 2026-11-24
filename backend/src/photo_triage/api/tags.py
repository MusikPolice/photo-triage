"""Adding and removing a tag on items (plan §6.4 bulk actions, §6.10).

An item's tags are its rows in `tags` from every source: imported from the file,
added by people, or suggested by the LLM. Adding a tag records it with source
`user` and the actor. Removing one deletes it from every source, since the file
holds one flat list. Each item whose tags change gets a metadata write of its whole
new list (`photo_triage.pipeline.writeback`), which replaces the file's keywords.
An item that already has the tag, or lacks the one removed, is left alone, with no
write.
"""

import logging
from enum import StrEnum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter
from pydantic import AfterValidator, BaseModel, Field

from photo_triage.api.deps import Actor, AppClock, DbSession
from photo_triage.api.items import item_tags
from photo_triage.db.models import Item, ItemStatus, Tag, TagSource
from photo_triage.pipeline import writeback
from photo_triage.worker.queue import JobQueue

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_ITEMS = 10_000
"""Items in one request: a lasso round a large part of the map."""

MAX_TAG_CHARS = 100


def _tag(value: str) -> str:
    tag = value.strip()
    if not tag:
        raise ValueError("A tag can't be empty")
    if len(tag) > MAX_TAG_CHARS:
        raise ValueError(f"A tag can't be longer than {MAX_TAG_CHARS} characters")
    if "," in tag:
        # A video's keywords are one comma-separated string, read back split.
        raise ValueError("A tag can't contain a comma")
    return tag


class TagAction(StrEnum):
    ADD = "add"
    REMOVE = "remove"


class TagChange(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=MAX_ITEMS)
    tag: Annotated[str, AfterValidator(_tag)]
    """Trimmed. Not empty, at most `MAX_TAG_CHARS`, and no commas."""
    action: TagAction


class Skipped(BaseModel):
    id: int
    reason: str


class TagsChanged(BaseModel):
    changed: list[int]
    """Items whose tags changed, each with a metadata write queued."""
    unchanged: list[int]
    """Items that already had the tag, or lacked the one removed."""
    skipped: list[Skipped]
    """Items not in the library, and why."""


@router.post("/tags")
def change_tags(body: TagChange, session: DbSession, actor: Actor, clock: AppClock) -> TagsChanged:
    """Add a tag to items, or remove one, and queue writing each changed item's tags
    to its file."""
    now = clock()
    queue = JobQueue(clock)
    changed: list[int] = []
    unchanged: list[int] = []
    skipped: list[Skipped] = []
    with session.begin():
        ids = list(dict.fromkeys(body.item_ids))
        items = {
            item.id: item
            for start in range(0, len(ids), 500)
            for item in session.scalars(
                sa.select(Item).where(Item.id.in_(ids[start : start + 500]))
            )
        }
        for item_id in ids:
            item = items.get(item_id)
            if item is None:
                skipped.append(Skipped(id=item_id, reason="No such item"))
                continue
            if item.status != ItemStatus.ACTIVE:
                skipped.append(Skipped(id=item_id, reason=f"It's {item.status.value}"))
                continue
            tags = item_tags(session, item_id)
            if (body.tag in tags) == (body.action == TagAction.ADD):
                unchanged.append(item_id)
                continue
            if body.action == TagAction.ADD:
                session.add(
                    Tag(
                        item_id=item_id,
                        source=TagSource.USER,
                        tag=body.tag,
                        created_at=now,
                        actor=actor,
                    )
                )
                tags = sorted([*tags, body.tag], key=lambda t: (t.lower(), t))
            else:
                session.execute(sa.delete(Tag).where(Tag.item_id == item_id, Tag.tag == body.tag))
                tags.remove(body.tag)
            writeback.queue_write(
                session, queue, item_id, {writeback.Field.TAGS: tags}, actor=actor, now=now
            )
            changed.append(item_id)
    if changed:
        verb = "added" if body.action == TagAction.ADD else "removed"
        logger.info("%s %s tag %r on %d items", actor, verb, body.tag, len(changed))
    return TagsChanged(changed=changed, unchanged=unchanged, skipped=skipped)
