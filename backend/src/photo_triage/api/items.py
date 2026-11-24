"""Items for the browser: their details, their thumbnails (plan §6.2) and the
picture or video itself, for the lightbox (plan §6.4).

A thumbnail keeps its URL when it's redrawn (a rotation, or a new
`THUMBNAIL_SIZE`), so browsers keep it but ask again each time it's shown, and get
a 304 while it's unchanged. The original is likewise, and a converted one
(`photo_triage.pipeline.viewing`) is converted again only if the original changed.
"""

import asyncio
import datetime as dt
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated, Any

import anyio
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from photo_triage.api.deps import DbSession, get_settings, get_similarity
from photo_triage.api.similarity import Similarity
from photo_triage.db.models import Item, ItemStatus, MediaType, Tag
from photo_triage.pipeline import viewing
from photo_triage.pipeline.thumbnails import MAP_SIZE, Thumbnail, thumbnail_path
from photo_triage.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter()

CACHE_CONTROL = "private, no-cache"
"""Kept by the browser, but checked with the server before each use."""


@router.get(
    "/items/{item_id}/thumbnail",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "The thumbnail, a JPEG"},
        304: {"description": "Unchanged since the copy the browser has"},
        404: {"description": "No such item, size or thumbnail"},
    },
)
def thumbnail(
    request: Request,
    item_id: int,
    size: int,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """The item's thumbnail, `size` pixels on the long edge: `THUMBNAIL_SIZE` (400
    by default) or 64, the map's."""
    kinds = {settings.thumbnail_size: Thumbnail.LARGE, MAP_SIZE: Thumbnail.MAP}
    if size not in kinds:
        raise HTTPException(404, f"Thumbnails are {settings.thumbnail_size} or {MAP_SIZE} px")
    path = thumbnail_path(settings.derived_dir, item_id, kinds[size])
    try:
        stat = path.stat()
    except FileNotFoundError:
        raise HTTPException(404, "No thumbnail for that item") from None
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    headers = {"Cache-Control": CACHE_CONTROL, "ETag": etag}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="image/jpeg", headers=headers, stat_result=stat)


class ItemDetails(BaseModel):
    id: int
    path: str
    """Relative to the library's folder."""
    media_type: MediaType
    taken_at_local: dt.datetime | None
    """Capture time as the camera recorded it, with no timezone, or None if unknown."""
    width: int | None
    height: int | None
    """Pixels, upright."""
    duration_s: float | None
    """Videos only."""
    file_size_bytes: int
    damaged: bool
    """A photo cut short: only the top of its picture is there."""
    tags: list[str]
    """Its tags from every source, imported from the file, added by people or by the
    LLM: each once, alphabetically, ignoring case."""


def item_tags(session: Session, item_id: int) -> list[str]:
    """The item's tags from every source, each once, alphabetically, ignoring case."""
    return list(
        session.scalars(
            sa.select(Tag.tag)
            .where(Tag.item_id == item_id)
            .distinct()
            .order_by(sa.func.lower(Tag.tag), Tag.tag)
        )
    )


def _active(session: DbSession, item_id: int) -> Item:
    item = session.get(Item, item_id)
    if item is None or item.status != ItemStatus.ACTIVE:
        raise HTTPException(404, "No such item")
    return item


@router.get("/items/{item_id}", responses={404: {"description": "No such item"}})
def details(item_id: int, session: DbSession) -> ItemDetails:
    """What the lightbox shows about an item."""
    item = _active(session, item_id)
    tags = item_tags(session, item_id)
    return ItemDetails(
        id=item.id,
        path=item.path,
        media_type=item.media_type,
        taken_at_local=item.taken_at_local,
        width=item.width,
        height=item.height,
        duration_s=item.duration_s,
        file_size_bytes=item.file_size_bytes,
        damaged=item.damaged,
        tags=tags,
    )


class Neighbour(BaseModel):
    id: int
    media_type: MediaType
    thumbnail_url: str
    """Its large thumbnail (`THUMBNAIL_SIZE`)."""


class Neighbours(BaseModel):
    embedded: bool
    """False until the item's CLIP embedding is made: it has no neighbours yet."""
    items: list[Neighbour]
    """Most similar first."""


MAX_NEIGHBOURS = 50


@router.get("/items/{item_id}/neighbours", responses={404: {"description": "No such item"}})
def neighbours(
    item_id: int,
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    similarity: Annotated[Similarity, Depends(get_similarity)],
    limit: Annotated[int, Query(ge=1, le=MAX_NEIGHBOURS)] = 12,
) -> Neighbours:
    """The items most like this one by CLIP (cosine similarity), for the lightbox's
    strip: a walk through the library (plan §6.4)."""
    _active(session, item_id)
    found = similarity.neighbours(session, item_id, limit)
    if found is None:
        return Neighbours(embedded=False, items=[])
    media = {
        item_id: media_type
        for item_id, media_type in session.execute(
            sa.select(Item.id, Item.media_type).where(Item.id.in_(found))
        )
    }
    size = settings.thumbnail_size
    return Neighbours(
        embedded=True,
        items=[
            Neighbour(
                id=i,
                media_type=media[i],
                thumbnail_url=f"/api/items/{i}/thumbnail?size={size}",
            )
            for i in found
            if i in media
        ],
    )


def _original(settings: Settings, item: Item) -> tuple[Path, os.stat_result]:
    """The item's file, which must be inside the library."""
    library = settings.photo_dir.resolve()
    path = (library / item.path).resolve()
    if not path.is_relative_to(library):
        logger.warning("Item %d's path %r is outside the library", item.id, item.path)
        raise HTTPException(404, "No such file")
    try:
        return path, path.stat()
    except FileNotFoundError:
        raise HTTPException(404, "The file is gone") from None


VIDEO_TYPE = "video/mp4"
"""For MOVs too: browsers play H.264 in either, and some refuse `video/quicktime`."""
CHUNK_BYTES = 64 * 1024


@router.get(
    "/items/{item_id}/view",
    response_class=Response,
    responses={
        200: {
            "content": {"image/jpeg": {}, VIDEO_TYPE: {}},
            "description": "The picture as a JPEG, or the video as an MP4 (fragmented, "
            "if converted)",
        },
        206: {"description": "Part of a video that needs no conversion"},
        304: {"description": "Unchanged since the copy the browser has"},
        404: {"description": "No such item, or its file is gone"},
        422: {"description": "The file can't be read"},
    },
)
async def view(
    request: Request,
    item_id: int,
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """The item's picture or video, in a format every browser shows."""
    item = _active(session, item_id)
    path, stat = _original(settings, item)
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    headers = {"Cache-Control": CACHE_CONTROL, "ETag": etag}
    is_video = item.media_type == MediaType.VIDEO
    if not is_video and path.suffix.lower() in {".jpg", ".jpeg"}:
        return FileResponse(path, media_type="image/jpeg", headers=headers, stat_result=stat)
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    try:
        if not is_video:
            jpeg = await anyio.to_thread.run_sync(viewing.heic_as_jpeg, path)
            return Response(jpeg, media_type="image/jpeg", headers=headers)
        codecs = await anyio.to_thread.run_sync(viewing.codecs, path)
    except viewing.ViewError as e:
        raise HTTPException(422, str(e)) from e
    if codecs.playable:
        return FileResponse(path, media_type=VIDEO_TYPE, headers=headers, stat_result=stat)
    logger.info("Converting item %d (%s, %s) for the browser", item.id, codecs.video, codecs.audio)
    # Not kept: a converted stream can't be resumed from the middle.
    return ClosingStreamingResponse(
        converted(path), media_type=VIDEO_TYPE, headers={"Cache-Control": "no-store"}
    )


class ClosingStreamingResponse(StreamingResponse):
    """A streaming response that closes its stream however the response ends.

    Starlette's leaves the stream suspended when the browser disconnects under ASGI
    2.4 (uvicorn's), as sending fails mid-loop, so a conversion would run on until
    the stream was garbage collected."""

    def __init__(self, stream: AsyncGenerator[bytes], **kwargs: Any) -> None:
        super().__init__(stream, **kwargs)
        self._stream = stream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._stream.aclose()


async def converted(path: Path) -> AsyncGenerator[bytes]:
    """The video at `path`, converted as it's read. ffmpeg is stopped if the reader
    stops early: when the browser disconnects, or closes the lightbox."""
    process = await asyncio.create_subprocess_exec(
        *viewing.conversion_command(path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    # Read alongside, so ffmpeg never waits on a full stderr pipe.
    errors = asyncio.create_task(process.stderr.read())
    try:
        while chunk := await process.stdout.read(CHUNK_BYTES):
            yield chunk
        if await process.wait() != 0:
            error = (await errors).decode(errors="replace").strip()
            logger.warning("Converting %s failed: %s", path.name, error)
    finally:
        errors.cancel()
        if process.returncode is None:
            process.kill()
            # Reaped even while the request is being cancelled.
            with anyio.CancelScope(shield=True):
                await process.wait()
            logger.debug("Stopped converting %s: the browser stopped reading", path.name)
