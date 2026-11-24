"""`POST /api/tags` (plan §6.4, §6.10): a tag added to or removed from items, with a
metadata write of each changed item's whole tag list. The write itself is
`test_writeback.py`'s."""

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vectors import Library

from photo_triage.api import tags as tags_api
from photo_triage.db.models import Item, ItemStatus, Job, MetadataWrite, Tag, TagSource
from photo_triage.files import ExifTool
from photo_triage.pipeline import writeback
from photo_triage.settings import Settings
from photo_triage.worker.queue import Stage

pytestmark = pytest.mark.anyio

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
AT = dt.datetime(2026, 11, 24, 12, tzinfo=dt.UTC)


@pytest.fixture
def clock(app: FastAPI) -> None:
    app.state.clock = lambda: AT


def tag(engine: Engine, item_id: int, value: str, source: TagSource) -> None:
    with Session(engine) as session, session.begin():
        session.add(Tag(item_id=item_id, source=source, tag=value, created_at=AT))


def tags_of(engine: Engine, item_id: int) -> set[tuple[str, TagSource, str | None]]:
    with Session(engine) as session:
        rows = session.execute(
            sa.select(Tag.tag, Tag.source, Tag.actor).where(Tag.item_id == item_id)
        )
        return {(t, s, a) for t, s, a in rows}


def queued(engine: Engine) -> dict[int, list[dict[str, Any]]]:
    """Each item's queued writes' fields, in order."""
    with Session(engine) as session:
        found: dict[int, list[dict[str, Any]]] = {}
        for write in session.scalars(sa.select(MetadataWrite).order_by(MetadataWrite.id)):
            found.setdefault(write.item_id, []).append(write.fields)
        return found


async def change(client: AsyncClient, ids: list[int], value: str, action: str) -> dict[str, Any]:
    response = await client.post(
        "/api/tags", json={"item_ids": ids, "tag": value, "action": action}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_adding_a_tag_records_who_and_queues_each_item_s_whole_list(
    client: AsyncClient, lib: Library, migrated: Engine, clock: None
) -> None:
    a, b = lib.add(None), lib.add(None)
    tag(migrated, a, "beach", TagSource.IMPORTED)
    tag(migrated, b, "Zoo", TagSource.LLM)

    result = await change(client, [a, b], "  dog ", "add")

    assert result == {"changed": [a, b], "unchanged": [], "skipped": []}
    assert ("dog", TagSource.USER, "anonymous") in tags_of(migrated, a)
    assert queued(migrated) == {a: [{"tags": ["beach", "dog"]}], b: [{"tags": ["dog", "Zoo"]}]}
    with Session(migrated) as session:
        jobs = session.scalars(sa.select(Job.item_id).where(Job.stage == Stage.METADATA_WRITE))
        assert sorted(jobs) == [a, b]
        write = session.scalars(sa.select(MetadataWrite)).first()
        assert write is not None and (write.actor, write.created_at) == ("anonymous", AT)


async def test_items_that_already_have_the_tag_get_no_write(
    client: AsyncClient, lib: Library, migrated: Engine
) -> None:
    has, imported, lacks = lib.add(None), lib.add(None), lib.add(None)
    tag(migrated, has, "dog", TagSource.USER)
    tag(migrated, imported, "dog", TagSource.IMPORTED)

    result = await change(client, [has, imported, lacks], "dog", "add")

    assert result == {"changed": [lacks], "unchanged": [has, imported], "skipped": []}
    assert set(queued(migrated)) == {lacks}


async def test_removing_a_tag_items_lack_queues_no_write(
    client: AsyncClient, lib: Library, migrated: Engine
) -> None:
    item_id = lib.add(None)
    tag(migrated, item_id, "beach", TagSource.IMPORTED)

    result = await change(client, [item_id], "dog", "remove")

    assert result == {"changed": [], "unchanged": [item_id], "skipped": []}
    assert queued(migrated) == {}


async def test_a_removed_imported_tag_is_gone_from_every_source_and_the_write(
    client: AsyncClient, lib: Library, migrated: Engine
) -> None:
    item_id = lib.add(None)
    for value, source in [
        ("beach", TagSource.IMPORTED),
        ("dog", TagSource.IMPORTED),
        ("dog", TagSource.LLM),
        ("sunset", TagSource.USER),
    ]:
        tag(migrated, item_id, value, source)

    result = await change(client, [item_id], "dog", "remove")

    assert result["changed"] == [item_id]
    assert {t for t, _, _ in tags_of(migrated, item_id)} == {"beach", "sunset"}
    assert queued(migrated) == {item_id: [{"tags": ["beach", "sunset"]}]}


async def test_removing_the_last_tag_writes_an_empty_list(
    client: AsyncClient, lib: Library, migrated: Engine
) -> None:
    item_id = lib.add(None)
    tag(migrated, item_id, "dog", TagSource.IMPORTED)

    await change(client, [item_id], "dog", "remove")

    assert queued(migrated) == {item_id: [{"tags": []}]}


async def test_items_not_in_the_library_are_skipped(
    client: AsyncClient, lib: Library, migrated: Engine
) -> None:
    active, trashed = lib.add(None), lib.add(None)
    lib.set_status(trashed, ItemStatus.TRASHED)

    result = await change(client, [active, trashed, 999, active], "dog", "add")

    assert result == {
        "changed": [active],
        "unchanged": [],
        "skipped": [
            {"id": trashed, "reason": "It's trashed"},
            {"id": 999, "reason": "No such item"},
        ],
    }


@pytest.mark.parametrize(
    "value",
    ["", "   ", "x" * (tags_api.MAX_TAG_CHARS + 1), "dog, cat"],
)
async def test_a_bad_tag_is_refused(
    client: AsyncClient, lib: Library, migrated: Engine, value: str
) -> None:
    item_id = lib.add(None)
    response = await client.post(
        "/api/tags", json={"item_ids": [item_id], "tag": value, "action": "add"}
    )
    assert response.status_code == 422
    assert queued(migrated) == {}


async def test_a_tag_at_the_length_limit_is_taken(
    client: AsyncClient, lib: Library, migrated: Engine
) -> None:
    item_id = lib.add(None)
    longest = "x" * tags_api.MAX_TAG_CHARS
    assert (await change(client, [item_id], longest, "add"))["changed"] == [item_id]


@pytest.mark.parametrize(
    "body",
    [
        {"item_ids": [], "tag": "dog", "action": "add"},
        {"item_ids": [1], "tag": "dog", "action": "rename"},
        {"item_ids": list(range(tags_api.MAX_ITEMS + 1)), "tag": "dog", "action": "add"},
    ],
)
async def test_a_bad_request_is_refused(
    client: AsyncClient, migrated: Engine, body: dict[str, Any]
) -> None:
    assert (await client.post("/api/tags", json=body)).status_code == 422


async def test_the_item_s_details_list_its_tags_from_every_source(
    client: AsyncClient, lib: Library, migrated: Engine
) -> None:
    item_id = lib.add(None)
    for value, source in [
        ("zebra", TagSource.IMPORTED),
        ("Apple", TagSource.USER),
        ("zebra", TagSource.LLM),
        ("mango", TagSource.LLM),
    ]:
        tag(migrated, item_id, value, source)

    details = (await client.get(f"/api/items/{item_id}")).json()

    assert details["tags"] == ["Apple", "mango", "zebra"]


async def test_an_added_tag_reads_back_from_the_file_once_written(
    client: AsyncClient,
    migrated: Engine,
    settings: Settings,
    exiftool: ExifTool,
) -> None:
    """Tier 1, with real exiftool: the fixture's own keywords stay, and the new one
    joins them."""
    source = "metadata/keywords.jpg"
    path = settings.photo_dir / source
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SYNTHETIC / source, path)
    imported = writeback.read_fields(exiftool, path)[writeback.Field.TAGS]
    assert imported  # the fixture has keywords
    stat = path.stat()
    sessions = sessionmaker(migrated)
    with sessions.begin() as session:
        item = Item(
            content_hash="0" * 64,
            path=source,
            media_type="photo",
            file_size_bytes=stat.st_size,
            file_mtime_ns=stat.st_mtime_ns,
            first_seen_at=AT,
            last_seen_at=AT,
        )
        session.add(item)
        session.flush()
        item_id = item.id
        session.add_all(
            Tag(item_id=item_id, source=TagSource.IMPORTED, tag=k, created_at=AT) for k in imported
        )

    await change(client, [item_id], "photo-triage test", "add")
    with sessions() as session:
        job = session.scalar(sa.select(Job).where(Job.stage == Stage.METADATA_WRITE))
        assert job is not None
        session.expunge(job)
    writeback.apply(sessions, job, exiftool, settings.photo_dir, enabled=True, now=AT)

    written = writeback.read_fields(exiftool, path)[writeback.Field.TAGS]
    assert sorted(written) == sorted([*imported, "photo-triage test"])
