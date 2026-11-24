"""The write log (plan §6.10): every metadata write queued, newest first, with the
values before and after, for audit. `metadata_writes` is the queue and the log in
one (`photo_triage.pipeline.writeback`)."""

import datetime as dt
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from photo_triage.api.deps import DbSession, get_settings
from photo_triage.db.models import Item, MetadataWrite, WriteStatus
from photo_triage.settings import Settings

router = APIRouter()

PAGE_SIZE = 100
MAX_PAGE_SIZE = 1_000


class LoggedWrite(BaseModel):
    id: int
    item_id: int
    path: str
    """The item's file, relative to `PHOTO_DIR`."""
    fields: dict[str, Any]
    """The values asked for, by field (`tags`, `reviewed`)."""
    before: dict[str, Any] | None
    """The changed fields' values in the file before, once it's been made or logged."""
    after: dict[str, Any] | None
    """Their values as written, or as they would have been in a dry run."""
    status: WriteStatus
    actor: str | None
    """Who asked for it, or None when the pipeline did."""
    created_at: dt.datetime
    applied_at: dt.datetime | None


class WriteLog(BaseModel):
    items: list[LoggedWrite]
    """Newest first."""
    total: int
    """Writes with the status asked for (or all), on every page."""
    writes_enabled: bool
    """`EXIF_WRITES_ENABLED`: off, writes are logged as dry runs and not made."""


@router.get("/metadata-writes")
def write_log(
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    status: WriteStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WriteLog:
    """The write log, newest first, optionally only writes with `status`."""
    where = [] if status is None else [MetadataWrite.status == status]
    rows = session.execute(
        sa.select(MetadataWrite, Item.path)
        .join(Item, Item.id == MetadataWrite.item_id)
        .where(*where)
        .order_by(MetadataWrite.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    total = session.scalar(sa.select(sa.func.count()).select_from(MetadataWrite).where(*where))
    return WriteLog(
        items=[
            LoggedWrite(
                id=write.id,
                item_id=write.item_id,
                path=path,
                fields=write.fields,
                before=write.before,
                after=write.after,
                status=write.status,
                actor=write.actor,
                created_at=write.created_at,
                applied_at=write.applied_at,
            )
            for write, path in rows
        ],
        total=total or 0,
        writes_enabled=settings.exif_writes_enabled,
    )
