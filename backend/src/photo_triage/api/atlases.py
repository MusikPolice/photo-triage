"""The map's atlases (plan §6.2): `GET /api/map/atlases`, the manifest saying which
sheet and cell holds each item's 64 px thumbnail, and
`GET /api/map/atlases/{version}/{sheet}.jpg`, the sheets. The format is in
`photo_triage.pipeline.atlases`.

The manifest is the newest complete set's, which may be a layout version behind the
points while the atlases job runs: the manifest says which version it's for. The
sheets' URLs carry the layout version, so a new set never reuses an old URL. Both
are kept by the browser and checked with an ETag before use.
"""

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import FileResponse, Response

from photo_triage.api.deps import get_settings
from photo_triage.settings import Settings
from photo_triage.worker.atlas_schedule import MANIFEST, current_set, set_path

router = APIRouter()

CACHE_CONTROL = "private, no-cache"
EMPTY_MANIFEST_VERSION = 0


def _etag(stat: os.stat_result) -> str:
    return f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'


def _not_modified(request: Request, etag: str) -> bool:
    return etag in request.headers.get("if-none-match", "")


@router.get(
    "/map/atlases",
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "The newest set's manifest, in the binary format described "
            "in `photo_triage.pipeline.atlases`, or an empty one before the first set",
        },
        304: {"description": "Unchanged since the copy the browser has"},
    },
)
def manifest(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> Response:
    """Which sheet and cell holds each item's 64 px thumbnail."""
    # Imported here: the pipeline module brings in Pillow and NumPy.
    from photo_triage.pipeline import atlases

    version = current_set(settings.derived_dir)
    if version is None:
        body = atlases.encode_manifest(atlases.Manifest(EMPTY_MANIFEST_VERSION, 0, []))
        return Response(
            body, media_type="application/octet-stream", headers={"Cache-Control": "no-store"}
        )
    path = set_path(settings.derived_dir, version) / MANIFEST
    try:
        stat = path.stat()
    except FileNotFoundError:  # replaced by a newer set meanwhile
        raise HTTPException(503, "The atlases are being replaced; try again") from None
    headers = {"Cache-Control": CACHE_CONTROL, "ETag": _etag(stat)}
    if _not_modified(request, headers["ETag"]):
        return Response(status_code=304, headers=headers)
    return FileResponse(
        path, media_type="application/octet-stream", headers=headers, stat_result=stat
    )


@router.get(
    "/map/atlases/{version}/{sheet}.jpg",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "The sheet, a JPEG"},
        304: {"description": "Unchanged since the copy the browser has"},
        404: {"description": "No such set or sheet"},
    },
)
def sheet(
    request: Request,
    version: int,
    sheet: int,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """One sheet of layout `version`'s set."""
    if version < 1 or sheet < 0:
        raise HTTPException(404, "No such sheet")
    path = set_path(settings.derived_dir, version) / f"{sheet}.jpg"
    try:
        stat = path.stat()
    except FileNotFoundError:
        raise HTTPException(404, "No such sheet") from None
    headers = {"Cache-Control": CACHE_CONTROL, "ETag": _etag(stat)}
    if _not_modified(request, headers["ETag"]):
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="image/jpeg", headers=headers, stat_result=stat)
