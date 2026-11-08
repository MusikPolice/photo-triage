"""Items' files for the browser: thumbnails so far (plan §6.2).

A thumbnail keeps its URL when it's redrawn (a rotation, or a new
`THUMBNAIL_SIZE`), so browsers keep it but ask again each time it's shown, and get
a 304 while it's unchanged.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import FileResponse, Response

from photo_triage.api.deps import get_settings
from photo_triage.pipeline.thumbnails import MAP_SIZE, Thumbnail, thumbnail_path
from photo_triage.settings import Settings

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
