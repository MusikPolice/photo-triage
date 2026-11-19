"""`GET /api/map/points`: the layout as a compact binary array (plan §6.4)."""

import datetime as dt
import struct
from pathlib import Path

import numpy as np
import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from photo_triage.api import map_points
from photo_triage.api.app import create_app
from photo_triage.db.models import Item, ItemStatus, Layout, MediaType
from photo_triage.ml import FakeClip
from photo_triage.settings import Settings
from photo_triage.worker.__main__ import main

pytestmark = pytest.mark.anyio

START = dt.datetime(2026, 11, 18, 2, tzinfo=dt.UTC)


class Points:
    """A decoded response, read as the browser reads it."""

    def __init__(self, body: bytes) -> None:
        magic, self.format, reserved, self.version, count = struct.unpack_from("<4sHHII", body)
        assert (magic, reserved) == (b"PTMP", 0)
        assert len(body) == 16 + 17 * count
        columns = np.frombuffer(body, dtype="<u4", count=count, offset=16)
        self.ids = [int(i) for i in columns]
        self.x = np.frombuffer(body, dtype="<f4", count=count, offset=16 + 4 * count)
        self.y = np.frombuffer(body, dtype="<f4", count=count, offset=16 + 8 * count)
        self.days = np.frombuffer(body, dtype="<i4", count=count, offset=16 + 12 * count)
        self.kinds = np.frombuffer(body, dtype="u1", count=count, offset=16 + 16 * count)


@pytest.fixture
def client(migrated: Engine, settings: Settings) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app(settings)), base_url="http://test")


def _add(
    engine: Engine,
    count: int,
    *,
    version: int = 1,
    taken: dt.datetime | None = None,
    kind: MediaType = MediaType.PHOTO,
) -> list[int]:
    """`count` items laid out in layout `version`."""
    ids: list[int] = []
    with sessionmaker(engine).begin() as session:
        first = session.scalar(sa.select(sa.func.count(Item.id))) or 0
        for n in range(first, first + count):
            item = Item(
                content_hash=f"{n:064x}",
                path=f"photos/{n}.jpg",
                media_type=kind,
                file_size_bytes=1,
                file_mtime_ns=0,
                first_seen_at=START,
                last_seen_at=START,
                taken_at_local=taken,
            )
            session.add(item)
            session.flush()
            session.add(Layout(layout_version=version, item_id=item.id, x=n / 100, y=0.5))
            ids.append(item.id)
    return ids


async def _points(client: AsyncClient) -> Points:
    response = await client.get("/api/map/points")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    return Points(response.content)


async def test_with_no_layout_there_are_no_points(client: AsyncClient) -> None:
    points = await _points(client)
    assert (points.format, points.version, points.ids) == (1, 0, [])


async def test_each_point_has_its_id_place_day_and_type(
    client: AsyncClient, migrated: Engine
) -> None:
    photo = _add(migrated, 1, taken=dt.datetime(2019, 4, 15, 23, 59))
    video = _add(migrated, 1, taken=dt.datetime(1969, 12, 31, 12), kind=MediaType.VIDEO)
    undated = _add(migrated, 1)

    points = await _points(client)

    assert points.version == 1
    assert points.ids == photo + video + undated
    assert points.x.tolist() == pytest.approx([0, 0.01, 0.02])
    assert points.y.tolist() == pytest.approx([0.5] * 3)
    assert points.days.tolist() == [
        (dt.date(2019, 4, 15) - dt.date(1970, 1, 1)).days,
        -1,
        map_points.NO_DAY,
    ]
    assert points.kinds.tolist() == [0, 1, 0]


async def test_items_no_longer_active_are_left_out(client: AsyncClient, migrated: Engine) -> None:
    ids = _add(migrated, 4)
    with sessionmaker(migrated).begin() as session:
        session.get_one(Item, ids[0]).status = ItemStatus.TRASHED
        session.get_one(Item, ids[1]).status = ItemStatus.MISSING

    assert (await _points(client)).ids == ids[2:]


async def test_only_the_newest_layout_is_served(client: AsyncClient, migrated: Engine) -> None:
    _add(migrated, 2, version=1)
    newer = _add(migrated, 1, version=2)
    points = await _points(client)
    assert (points.version, points.ids) == (2, newer)


async def test_an_unchanged_layout_is_a_304(client: AsyncClient, migrated: Engine) -> None:
    _add(migrated, 3)
    first = await client.get("/api/map/points")
    assert first.headers["cache-control"] == "private, no-cache"
    etag = first.headers["etag"]

    again = await client.get("/api/map/points", headers={"If-None-Match": etag})
    assert again.status_code == 304

    _add(migrated, 1)  # same version, another point
    changed = await client.get("/api/map/points", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag


def test_100k_points_come_to_under_2_mb() -> None:
    rows = [(n, 0.5, 0.5, START, MediaType.PHOTO) for n in range(100_000)]
    assert len(map_points.encode(1, rows)) <= 2_000_000


def test_reading_the_points_uses_the_layouts_primary_key(migrated: Engine) -> None:
    query = map_points.points_query(1).compile(migrated, compile_kwargs={"literal_binds": True})
    with migrated.connect() as connection:
        plan = [row[-1] for row in connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {query}")]
    # The layout by its primary key, already in item order, and each item by its own.
    # sqlite_autoindex_layout_1 is the primary key on (layout_version, item_id).
    assert "SEARCH layout USING INDEX sqlite_autoindex_layout_1 (layout_version=?)" in plan
    assert any(step.startswith("SEARCH items USING INTEGER PRIMARY KEY") for step in plan), plan
    assert not any(step.startswith(("SCAN", "USE TEMP B-TREE")) for step in plan), plan


async def test_the_tier_1_fixtures_are_served_once_laid_out(
    migrated: Engine,
    settings: Settings,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "PHOTO_DIR": settings.photo_dir,
        "TRASH_DIR": settings.trash_dir,
        "DATA_DIR": settings.data_dir,
        "MODELS_DIR": settings.models_dir,
        "CLIP_MODEL": "fake",
    }.items():
        monkeypatch.setenv(name, str(value))
    # Its scan waits to retry, which holds the fit back (see test_layout.py).
    (library / "broken" / "truncated.jpg").unlink()
    assert main(["scan"]) == 0
    assert main(["--once"], clip=FakeClip()) == 0
    with sessionmaker(migrated)() as session:
        items = {
            item.id: item
            for item in session.scalars(sa.select(Item).join(Layout, Layout.item_id == Item.id))
        }
    client = AsyncClient(transport=ASGITransport(app=create_app(settings)), base_url="http://test")

    points = await _points(client)

    assert points.version == 1
    assert points.ids == sorted(items)
    assert points.x.min() >= 0 and points.x.max() <= 1
    assert points.y.min() >= 0 and points.y.max() <= 1
    assert max(points.x.max(), points.y.max()) == pytest.approx(1)
    for item_id, day, kind in zip(points.ids, points.days, points.kinds, strict=True):
        item = items[item_id]
        assert item.taken_at_local is not None
        assert day == (item.taken_at_local.date() - dt.date(1970, 1, 1)).days
        assert kind == (item.media_type == MediaType.VIDEO)
    assert set(points.kinds.tolist()) == {0, 1}
