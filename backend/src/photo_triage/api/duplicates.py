"""Reviewing duplicate groups (plan §6.7 "Review UI"): one pending group at a time,
leased to the reviewer looking at it, and their decision on it.

- **Keep selected** moves the other members to the trash (`trash_items`) and
  resolves the group.
- **Keep all** resolves the group and queues the `duplicates` reviewed marker for
  each member's file (`photo_triage.pipeline.writeback`), so a rebuilt database
  doesn't ask again (`photo_triage.pipeline.duplicates`).
- **Skip** leaves it for later: grouping brings it back as pending once it changes.

Every decision records each member's in `reviews`, with the actor.

A lease is held by a reviewer token the page makes for itself, not by the actor:
with no sign-in every actor is "anonymous" (plan §10). `next` gives a reviewer the
group it already holds, or the oldest pending group nobody holds, and holds it for
`LEASE`. A decision on a group someone else holds is refused. The page lets its
group go (`release`) when it's closed or left.
"""

import datetime as dt
import logging
from enum import StrEnum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from photo_triage.api.deps import Actor, AppClock, DbSession, get_settings
from photo_triage.api.trash import Moved, Skipped, trash_items
from photo_triage.db.models import (
    Decision,
    DuplicateGroup,
    DuplicateMember,
    GroupStatus,
    Item,
    ItemStatus,
    MediaType,
    Review,
    ReviewKind,
)
from photo_triage.pipeline import writeback
from photo_triage.pipeline.duplicates import REVIEWED_MARKER
from photo_triage.settings import Settings
from photo_triage.worker.queue import JobQueue

logger = logging.getLogger(__name__)

router = APIRouter()

LEASE = dt.timedelta(minutes=10)
"""How long a group stays with the reviewer who was given it, undecided."""

CANDIDATES = 20
"""Pending groups looked at for one with two members still in the library."""

Reviewer = Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")]


class Member(BaseModel):
    id: int
    rank: int
    """0 is the recommended keeper."""
    media_type: MediaType
    path: str
    width: int | None
    height: int | None
    file_size_bytes: int
    taken_at_local: dt.datetime | None
    thumbnail_url: str
    view_url: str


class Group(BaseModel):
    id: int
    members: list[Member]
    """Best keeper first."""
    lease_expires_at: dt.datetime


class NextGroup(BaseModel):
    group: Group | None
    """None when there's no pending group nobody else holds."""
    pending: int
    """Groups waiting for review, this one included."""
    total: int
    """Groups of every status."""


class NextRequest(BaseModel):
    reviewer: Reviewer


class Action(StrEnum):
    KEEP_SELECTED = "keep_selected"
    KEEP_ALL = "keep_all"
    SKIP = "skip"


class DecideRequest(BaseModel):
    reviewer: Reviewer
    action: Action
    keep: list[int] = []
    """With `keep_selected`, the members to keep: at least one."""


class Decided(BaseModel):
    moved: list[int]
    """Members moved to the trash."""
    skipped: list[Skipped]
    """Members that couldn't be moved, and why. They stay in the library."""


@router.post("/duplicates/next")
def next_group(
    body: NextRequest,
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    clock: AppClock,
) -> NextGroup:
    """The group for this reviewer to look at, now held for them for `LEASE`."""
    now = clock()
    with session.begin():
        free = sa.or_(
            DuplicateGroup.leased_to.is_(None),
            DuplicateGroup.leased_to == body.reviewer,
            DuplicateGroup.lease_expires_at <= now,
        )
        candidates = session.scalars(
            sa.select(DuplicateGroup)
            .where(DuplicateGroup.status == GroupStatus.PENDING, free)
            .order_by((DuplicateGroup.leased_to == body.reviewer).desc(), DuplicateGroup.id)
            .limit(CANDIDATES)
        ).all()
        found: Group | None = None
        for group in candidates:
            members = _members(session, settings, group.id)
            if len(members) < 2:
                continue  # members trashed since grouping; the next grouping drops it
            expires_at = now + LEASE
            group.leased_to, group.lease_expires_at = body.reviewer, expires_at
            found = Group(id=group.id, members=members, lease_expires_at=expires_at)
            break
        pending = session.scalar(
            sa.select(sa.func.count())
            .select_from(DuplicateGroup)
            .where(DuplicateGroup.status == GroupStatus.PENDING)
        )
        total = session.scalar(sa.select(sa.func.count()).select_from(DuplicateGroup))
    return NextGroup(group=found, pending=pending or 0, total=total or 0)


@router.post(
    "/duplicates/{group_id}/decide",
    responses={
        404: {"description": "No such group"},
        409: {"description": "Decided already, or another reviewer has it"},
    },
)
def decide(
    group_id: int,
    body: DecideRequest,
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Actor,
    clock: AppClock,
) -> Decided:
    """Keep the selected members and trash the rest, keep them all, or skip."""
    now = clock()
    with session.begin():
        group = session.get(DuplicateGroup, group_id)
        if group is None:
            raise HTTPException(404, "No such group")
        if group.status != GroupStatus.PENDING:
            raise HTTPException(409, f"The group is {group.status.value} already")
        held = group.lease_expires_at is not None and group.lease_expires_at > now
        if held and group.leased_to != body.reviewer:
            raise HTTPException(409, "Another reviewer has this group")
        member_ids = [m.id for m in _members(session, settings, group_id)]
    keep = list(dict.fromkeys(body.keep))
    if body.action == Action.KEEP_SELECTED:
        if not keep or not set(keep) <= set(member_ids):
            raise HTTPException(422, "Keep at least one of the group's members, and only those")
        rest = [i for i in member_ids if i not in keep]
        moved = (
            trash_items(session, settings, rest, actor=actor, now=now)
            if rest
            else Moved(moved=[], skipped=[])
        )
    else:
        moved = Moved(moved=[], skipped=[])

    with session.begin():
        group = session.get(DuplicateGroup, group_id)
        assert group is not None
        group.leased_to = group.lease_expires_at = None
        group.actor, group.resolved_at = actor, now
        if body.action == Action.SKIP:
            group.status = GroupStatus.SKIPPED
            decisions = dict.fromkeys(member_ids, Decision.SKIP)
        else:
            group.status = GroupStatus.RESOLVED
            decisions = dict.fromkeys(member_ids, Decision.KEEP)
            decisions.update(dict.fromkeys(moved.moved, Decision.DELETE))
        session.add_all(
            Review(
                item_id=item_id,
                kind=ReviewKind.DUPLICATE,
                decision=decision,
                actor=actor,
                decided_at=now,
            )
            for item_id, decision in decisions.items()
        )
        if body.action == Action.KEEP_ALL:
            _mark_reviewed(session, member_ids, actor=actor, now=now)
    logger.info(
        "%s decided duplicate group %d: %s%s",
        actor,
        group_id,
        body.action.value,
        f", trashed {len(moved.moved)}" if moved.moved else "",
    )
    return Decided(moved=moved.moved, skipped=moved.skipped)


class ReleaseRequest(BaseModel):
    reviewer: Reviewer


@router.post("/duplicates/{group_id}/release", status_code=204)
def release(group_id: int, body: ReleaseRequest, session: DbSession) -> None:
    """Let another reviewer have the group, if this reviewer holds it."""
    with session.begin():
        session.execute(
            sa.update(DuplicateGroup)
            .where(DuplicateGroup.id == group_id, DuplicateGroup.leased_to == body.reviewer)
            .values(leased_to=None, lease_expires_at=None)
        )


def _members(session: Session, settings: Settings, group_id: int) -> list[Member]:
    """The group's members still in the library, best keeper first."""
    size = settings.thumbnail_size
    rows = session.execute(
        sa.select(Item, DuplicateMember.rank)
        .join(DuplicateMember, DuplicateMember.item_id == Item.id)
        .where(DuplicateMember.group_id == group_id, Item.status == ItemStatus.ACTIVE)
        .order_by(DuplicateMember.rank)
    ).all()
    return [
        Member(
            id=item.id,
            rank=rank,
            media_type=item.media_type,
            path=item.path,
            width=item.width,
            height=item.height,
            file_size_bytes=item.file_size_bytes,
            taken_at_local=item.taken_at_local,
            thumbnail_url=f"/api/items/{item.id}/thumbnail?size={size}",
            view_url=f"/api/items/{item.id}/view",
        )
        for item, rank in rows
    ]


def _mark_reviewed(session: Session, item_ids: list[int], *, actor: str, now: dt.datetime) -> None:
    """Queue the `duplicates` reviewed marker for each item's file that lacks it."""
    queue = JobQueue(lambda: now)
    for item in session.scalars(sa.select(Item).where(Item.id.in_(item_ids))):
        kinds = list(item.reviewed or [])
        if REVIEWED_MARKER in kinds:
            continue
        writeback.queue_write(
            session,
            queue,
            item.id,
            {writeback.Field.REVIEWED: [*kinds, REVIEWED_MARKER]},
            actor=actor,
            now=now,
        )
