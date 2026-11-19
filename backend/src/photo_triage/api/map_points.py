"""`GET /api/map/points`: the current layout, for the map (plan §6.4).

The browser takes every point at once as one compact binary array, which it reads
with typed arrays and no parsing. Little-endian throughout:

    offset  size  field
    0       4     magic, b"PTMP"
    4       2     u16 format version, `FORMAT_VERSION`
    6       2     u16 reserved, 0
    8       4     u32 layout version, or 0 when there's no layout yet
    12      4     u32 N, the number of points
    16      4N    u32 item ids, ascending
    16+4N   4N    f32 x, in [0, 1] (the longer axis spans it)
    16+8N   4N    f32 y
    16+12N  4N    i32 capture day: days from 1970-01-01 to `taken_at_local`'s date,
                  or `NO_DAY` when it's unknown
    16+16N  N     u8 media type: 0 photo, 1 video

So 17 bytes a point, 1.7 MB for 100k, and every column starts on a 4-byte
boundary. The layout version also goes in the ETag, so a browser that has this
layout gets a 304. Capture dates change without a new layout (a re-read of the
metadata), so the ETag also covers the body.
"""

import datetime as dt
import hashlib
import struct
from collections.abc import Sequence

import numpy as np
import sqlalchemy as sa
from fastapi import APIRouter, Request
from starlette.responses import Response

from photo_triage.api.deps import DbSession
from photo_triage.db.models import Item, ItemStatus, Layout, MediaType
from photo_triage.worker.layout_schedule import latest_version

router = APIRouter()

MAGIC = b"PTMP"
FORMAT_VERSION = 1
HEADER = struct.Struct("<4sHHII")
NO_DAY = -(2**31)
"""The capture day of an item with no `taken_at_local`."""
MEDIA_TYPES = {MediaType.PHOTO: 0, MediaType.VIDEO: 1}
CACHE_CONTROL = "private, no-cache"
_EPOCH = dt.date(1970, 1, 1)


def points_query(version: int) -> sa.Select[int, float, float, dt.datetime | None, MediaType]:
    """The points of layout `version` whose items are still active, by item id."""
    return (
        sa.select(Layout.item_id, Layout.x, Layout.y, Item.taken_at_local, Item.media_type)
        .join(Item, Item.id == Layout.item_id)
        .where(Layout.layout_version == version, Item.status == ItemStatus.ACTIVE)
        .order_by(Layout.item_id)
    )


type Row = tuple[int, float, float, dt.datetime | None, MediaType]
"""Item id, x, y, capture time and media type."""


def encode(version: int, rows: Sequence[Row]) -> bytes:
    """The binary array in the module docstring."""
    count = len(rows)
    ids = np.fromiter((r[0] for r in rows), dtype="<u4", count=count)
    xs = np.fromiter((r[1] for r in rows), dtype="<f4", count=count)
    ys = np.fromiter((r[2] for r in rows), dtype="<f4", count=count)
    days = np.fromiter(
        (NO_DAY if r[3] is None else (r[3].date() - _EPOCH).days for r in rows),
        dtype="<i4",
        count=count,
    )
    kinds = np.fromiter((MEDIA_TYPES[r[4]] for r in rows), dtype="u1", count=count)
    header = HEADER.pack(MAGIC, FORMAT_VERSION, 0, version, count)
    return b"".join([header, *(a.tobytes() for a in (ids, xs, ys, days, kinds))])


@router.get(
    "/map/points",
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "Every point of the current layout, in the binary format "
            "described in `photo_triage.api.map_points`",
        },
        304: {"description": "Unchanged since the copy the browser has"},
    },
)
def points(request: Request, session: DbSession) -> Response:
    """The current map layout as a compact binary array: item id, x, y, capture day
    and media type per point."""
    version = session.scalar(latest_version()) or 0
    rows = session.execute(points_query(version)).all()
    body = encode(version, rows)
    etag = f'"{version:x}-{hashlib.blake2b(body, digest_size=8).hexdigest()}"'
    headers = {"Cache-Control": CACHE_CONTROL, "ETag": etag}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/octet-stream", headers=headers)
