"""`GET /api/metadata-writes`, the write log (plan §6.10): every write, newest first,
with its file and its values before and after, paged and filtered by status."""

import datetime as dt
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from vectors import Library

from photo_triage.api.app import create_app
from photo_triage.db.models import Item, MetadataWrite, WriteStatus
from photo_triage.settings import Settings

pytestmark = pytest.mark.anyio

AT = dt.datetime(2026, 11, 24, 12, tzinfo=dt.UTC)


def log(engine: Engine, item_id: int, status: WriteStatus, n: int) -> int:
    with Session(engine) as session, session.begin():
        write = MetadataWrite(
            item_id=item_id,
            fields={"tags": [f"tag {n}"]},
            before=None if status == WriteStatus.QUEUED else {"tags": []},
            after=None if status == WriteStatus.QUEUED else {"tags": [f"tag {n}"]},
            status=status,
            actor="ada" if n % 2 else None,
            created_at=AT + dt.timedelta(minutes=n),
            applied_at=None if status == WriteStatus.QUEUED else AT + dt.timedelta(minutes=n + 1),
        )
        session.add(write)
        session.flush()
        return write.id


async def get(client: AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/api/metadata-writes", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_log_lists_writes_newest_first_with_their_values(
    client: AsyncClient, lib: Library, migrated: Engine
) -> None:
    item_id = lib.add(None)
    with Session(migrated) as session:
        item = session.get(Item, item_id)
        assert item is not None
        path = item.path
    first = log(migrated, item_id, WriteStatus.DRY_RUN, 1)
    second = log(migrated, item_id, WriteStatus.QUEUED, 2)

    page = await get(client)

    assert page["total"] == 2
    assert page["writes_enabled"] is False
    assert [w["id"] for w in page["items"]] == [second, first]
    assert page["items"][1] == {
        "id": first,
        "item_id": item_id,
        "path": path,
        "fields": {"tags": ["tag 1"]},
        "before": {"tags": []},
        "after": {"tags": ["tag 1"]},
        "status": "dry_run",
        "actor": "ada",
        "created_at": "2026-11-24T12:01:00Z",
        "applied_at": "2026-11-24T12:02:00Z",
    }
    assert page["items"][0]["before"] is None and page["items"][0]["actor"] is None


async def test_the_log_pages_and_filters_by_status(
    client: AsyncClient, lib: Library, migrated: Engine
) -> None:
    item_id = lib.add(None)
    statuses = [WriteStatus.DRY_RUN, WriteStatus.APPLIED, WriteStatus.DRY_RUN, WriteStatus.ERROR]
    ids = [log(migrated, item_id, s, n) for n, s in enumerate(statuses)]

    dry = await get(client, status="dry_run")
    assert (dry["total"], [w["id"] for w in dry["items"]]) == (2, [ids[2], ids[0]])

    first = await get(client, limit=3)
    rest = await get(client, limit=3, offset=3)
    assert [w["id"] for w in first["items"]] == ids[:0:-1]
    assert [w["id"] for w in rest["items"]] == [ids[0]]
    assert first["total"] == rest["total"] == 4

    assert (await get(client, status="queued")) == {
        "items": [],
        "total": 0,
        "writes_enabled": False,
    }


async def test_the_log_says_when_writes_are_on(migrated: Engine, settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"exif_writes_enabled": True}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await get(client))["writes_enabled"] is True


@pytest.mark.parametrize("params", [{"status": "done"}, {"limit": 0}, {"offset": -1}])
async def test_bad_parameters_are_refused(
    client: AsyncClient, migrated: Engine, params: dict[str, Any]
) -> None:
    assert (await client.get("/api/metadata-writes", params=params)).status_code == 422
