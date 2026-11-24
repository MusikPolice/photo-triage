"""The trash (plan §6.8) through the API: `POST /api/trash` moves items' files into
`TRASH_DIR`, `POST /api/trash/restore` moves them back, and `GET /api/trash` lists
what's there. The file moves themselves are `tests/unit/test_trash_files.py`'s."""

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import AsyncClient
from PIL import Image
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from vectors import DIM, NOW, Library, unit

from photo_triage.api import trash as trash_api
from photo_triage.db.models import Item, ItemStatus, Job, JobStatus, Layout, TrashEntry
from photo_triage.pipeline import scan
from photo_triage.settings import Settings
from photo_triage.worker.queue import PRIORITY, JobQueue, Stage

pytestmark = pytest.mark.anyio

AT = dt.datetime(2026, 11, 23, 12, tzinfo=dt.UTC)


@pytest.fixture
def clock(app: FastAPI) -> None:
    app.state.clock = lambda: AT


def place(settings: Settings, lib: Library, item_id: int) -> Path:
    """Write the item's file where `Library` says it is."""
    with Session(lib.engine) as session:
        item = session.get(Item, item_id)
        assert item is not None
        path = settings.photo_dir / item.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"item {item_id}".encode())
    return path


def item(engine: Engine, item_id: int) -> Item:
    with Session(engine, expire_on_commit=False) as session:
        found = session.get(Item, item_id)
        assert found is not None
        return found


async def post(client: AsyncClient, url: str, ids: list[int]) -> dict[str, Any]:
    response = await client.post(url, json={"item_ids": ids})
    assert response.status_code == 200, response.text
    return response.json()


async def test_trashing_moves_the_file_and_records_who_and_when(
    client: AsyncClient, lib: Library, settings: Settings, clock: None
) -> None:
    item_id = lib.add(None)
    path = place(settings, lib, item_id)
    mtime_ns = path.stat().st_mtime_ns

    result = await post(client, "/api/trash", [item_id])

    assert result == {"moved": [item_id], "skipped": []}
    relative = path.relative_to(settings.photo_dir)
    assert not path.exists()
    in_trash = settings.trash_dir / relative
    assert in_trash.read_bytes() == f"item {item_id}".encode()
    assert in_trash.stat().st_mtime_ns == mtime_ns
    assert item(lib.engine, item_id).status == ItemStatus.TRASHED
    with Session(lib.engine) as session:
        entry = session.get(TrashEntry, item_id)
        assert entry is not None
        assert (entry.original_path, entry.trash_path) == (str(relative), str(relative))
        assert (entry.actor, entry.trashed_at) == ("anonymous", AT)
        assert entry.purge_after == AT + dt.timedelta(days=settings.trash_retention_days)


async def test_restoring_puts_it_back(
    client: AsyncClient, lib: Library, settings: Settings, clock: None
) -> None:
    item_id = lib.add(None)
    path = place(settings, lib, item_id)
    await post(client, "/api/trash", [item_id])

    result = await post(client, "/api/trash/restore", [item_id])

    assert result == {"moved": [item_id], "skipped": []}
    assert path.read_bytes() == f"item {item_id}".encode()
    assert item(lib.engine, item_id).status == ItemStatus.ACTIVE
    with Session(lib.engine) as session:
        assert session.get(TrashEntry, item_id) is None
    # The trash keeps no empty folders behind.
    assert list(settings.trash_dir.iterdir()) == []


async def test_items_that_can_t_be_trashed_are_skipped_with_why(
    client: AsyncClient, lib: Library, settings: Settings, clock: None
) -> None:
    ok = lib.add(None)
    place(settings, lib, ok)
    gone = lib.add(None)  # no file
    missing = lib.add(None)
    lib.set_status(missing, ItemStatus.MISSING)
    twice = lib.add(None)
    place(settings, lib, twice)
    await post(client, "/api/trash", [twice])

    result = await post(client, "/api/trash", [ok, gone, missing, twice, 999, ok])

    assert result["moved"] == [ok]
    reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert reasons == {
        gone: "Its file has gone from the library",
        missing: "It's missing, not in the library",
        twice: "It's already in the trash",
        999: "No such item",
    }
    assert item(lib.engine, gone).status == ItemStatus.ACTIVE


async def test_a_restore_whose_place_is_taken_is_skipped(
    client: AsyncClient, lib: Library, settings: Settings, clock: None
) -> None:
    item_id = lib.add(None)
    path = place(settings, lib, item_id)
    await post(client, "/api/trash", [item_id])
    path.write_bytes(b"a new file at the same path")
    never = lib.add(None)

    result = await post(client, "/api/trash/restore", [item_id, never])

    assert result["moved"] == []
    reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert "already exists" in reasons[item_id]
    assert reasons[never] == "It isn't in the trash"
    assert path.read_bytes() == b"a new file at the same path"
    assert item(lib.engine, item_id).status == ItemStatus.TRASHED


async def test_a_failed_commit_moves_the_files_back(
    client: AsyncClient,
    lib: Library,
    settings: Settings,
    clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = lib.add(None), lib.add(None)
    paths = [place(settings, lib, first), place(settings, lib, second)]

    def fail_after_moving(**kwargs: Any) -> Any:
        raise RuntimeError("the database went away")

    # Raised inside the transaction, after both files have moved.
    monkeypatch.setattr(trash_api, "Skipped", fail_after_moving)

    response = await client.post("/api/trash", json={"item_ids": [first, second, 999]})

    assert response.status_code == 500

    assert all(p.exists() for p in paths)
    assert item(lib.engine, first).status == ItemStatus.ACTIVE
    assert list(settings.trash_dir.rglob("*.jpg")) == []


async def test_trashing_drops_the_item_s_open_jobs(
    client: AsyncClient, lib: Library, settings: Settings, clock: None
) -> None:
    item_id = lib.add(unit(1))
    place(settings, lib, item_id)
    with Session(lib.engine) as session, session.begin():
        for stage, status in [(Stage.THUMBNAIL, JobStatus.PENDING), (Stage.SCAN, JobStatus.ERROR)]:
            session.add(
                Job(
                    item_id=item_id,
                    stage=stage,
                    priority=PRIORITY[stage],
                    status=status,
                    enqueued_at=NOW,
                )
            )

    await post(client, "/api/trash", [item_id])

    with Session(lib.engine) as session:
        statuses = session.scalars(sa.select(Job.status).where(Job.item_id == item_id)).all()
    assert statuses == [JobStatus.DONE]  # its clip job stays, for a restore


async def test_the_trash_lists_the_newest_first(
    client: AsyncClient, lib: Library, settings: Settings, app: FastAPI
) -> None:
    ids = [lib.add(None) for _ in range(3)]
    for n, item_id in enumerate(ids):
        place(settings, lib, item_id)
        app.state.clock = lambda n=n: AT + dt.timedelta(hours=n)
        await post(client, "/api/trash", [item_id])

    page = (await client.get("/api/trash", params={"limit": 2})).json()

    assert page["total"] == 3
    assert [i["id"] for i in page["items"]] == [ids[2], ids[1]]
    newest = page["items"][0]
    assert newest["media_type"] == "photo"
    assert newest["original_path"] == f"photos/{ids[2]}.jpg"
    assert newest["actor"] == "anonymous"
    thumbnail = f"/api/items/{ids[2]}/thumbnail?size={settings.thumbnail_size}"
    assert newest["thumbnail_url"] == thumbnail
    assert dt.datetime.fromisoformat(newest["purge_after"]) - dt.datetime.fromisoformat(
        newest["trashed_at"]
    ) == dt.timedelta(days=30)
    rest = (await client.get("/api/trash", params={"limit": 2, "offset": 2})).json()
    assert [i["id"] for i in rest["items"]] == [ids[0]]


@pytest.mark.parametrize("ids", [[], list(range(trash_api.MAX_ITEMS + 1))])
async def test_a_request_needs_some_items_and_not_too_many(
    client: AsyncClient, migrated: Engine, ids: list[int]
) -> None:
    assert (await client.post("/api/trash", json={"item_ids": ids})).status_code == 422


class Encoder:
    """Embeds "cats" as the first axis."""

    model = "test"
    dim = DIM
    image_size = 224
    search_floor = 0.5

    def embedder(self) -> "Encoder":
        return self

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        raise AssertionError("search embeds no pictures")

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([unit(1) for _ in texts])


async def test_a_trashed_item_leaves_the_map_search_and_similar_items(
    client: AsyncClient,
    app: FastAPI,
    lib: Library,
    settings: Settings,
    fresh: None,
    clock: None,
) -> None:
    app.state.text_encoder = Encoder()
    kept, trashed = lib.add(unit(1)), lib.add(unit(1, 0.1))
    with Session(lib.engine) as session, session.begin():
        for item_id in (kept, trashed):
            session.add(Layout(layout_version=1, item_id=item_id, x=0.5, y=0.5))
    place(settings, lib, trashed)

    async def shown() -> tuple[int, list[int], list[int]]:
        points = await client.get("/api/map/points")
        count = int.from_bytes(points.content[12:16], "little")
        search = (await client.get("/api/search", params={"q": "cats"})).json()
        near = (await client.get(f"/api/items/{kept}/neighbours")).json()
        return count, [m["id"] for m in search["matches"]], [n["id"] for n in near["items"]]

    assert await shown() == (2, [kept, trashed], [trashed])

    await post(client, "/api/trash", [trashed])

    assert await shown() == (1, [kept], [])
    assert (await client.get(f"/api/items/{trashed}")).status_code == 404


async def test_the_library_walk_leaves_a_trashed_item_alone(
    client: AsyncClient, lib: Library, settings: Settings, migrated: Engine, clock: None
) -> None:
    item_id = lib.add(None)
    place(settings, lib, item_id)
    await post(client, "/api/trash", [item_id])
    settings.trash_dir.mkdir(exist_ok=True)

    with Session(migrated) as session, session.begin():
        result = scan.walk(session, JobQueue(), settings.photo_dir, settings.trash_dir, AT)

    assert result.missing == 0
    assert item(migrated, item_id).status == ItemStatus.TRASHED
