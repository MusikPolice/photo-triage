"""Reviewing duplicate groups (plan §6.7 "Review UI", §10 leases): `POST
/api/duplicates/next` leases a pending group to a reviewer, and `POST
/api/duplicates/{id}/decide` keeps the selected members and trashes the rest, keeps
them all (queuing the reviewed marker), or skips. The marker read back from a file
makes grouping leave its group resolved (#156)."""

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
from vectors import NOW, Library, unit

from photo_triage.api import duplicates as review_api
from photo_triage.db.models import (
    DuplicateGroup,
    DuplicateMember,
    GroupStatus,
    Item,
    ItemStatus,
    MediaType,
    MetadataWrite,
    Review,
    TrashEntry,
)
from photo_triage.files import ExifTool
from photo_triage.pipeline import duplicates, metadata, writeback
from photo_triage.settings import Settings

pytestmark = pytest.mark.anyio

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
AT = dt.datetime(2026, 11, 24, 12, tzinfo=dt.UTC)
ADA, BOB = "reviewer-ada-0001", "reviewer-bob-0002"


class Clock:
    def __init__(self) -> None:
        self.now = AT

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture
def clock(app: FastAPI) -> Clock:
    clock = Clock()
    app.state.clock = clock
    return clock


def make_group(lib: Library, settings: Settings, size: int = 2) -> tuple[int, list[int]]:
    """A pending group of `size` items with files in the library, best first."""
    ids = [lib.add(None) for _ in range(size)]
    with Session(lib.engine) as session, session.begin():
        group = DuplicateGroup(status=GroupStatus.PENDING, created_at=AT)
        session.add(group)
        session.flush()
        for rank, item_id in enumerate(ids):
            session.add(DuplicateMember(group_id=group.id, item_id=item_id, rank=rank))
            item = session.get(Item, item_id)
            assert item is not None
            path = settings.photo_dir / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"item {item_id}".encode())
        return group.id, ids


async def next_group(client: AsyncClient, reviewer: str) -> dict[str, Any]:
    response = await client.post("/api/duplicates/next", json={"reviewer": reviewer})
    assert response.status_code == 200, response.text
    return response.json()


async def decide(
    client: AsyncClient, group_id: int, action: str, keep: list[int] | None = None, **extra: Any
) -> Any:
    body = {"reviewer": extra.get("reviewer", ADA), "action": action, "keep": keep or []}
    return await client.post(f"/api/duplicates/{group_id}/decide", json=body)


def group_row(engine: Engine, group_id: int) -> DuplicateGroup:
    with Session(engine, expire_on_commit=False) as session:
        group = session.get(DuplicateGroup, group_id)
        assert group is not None
        return group


def reviews(engine: Engine) -> dict[int, tuple[str, str, str]]:
    with Session(engine) as session:
        return {
            r.item_id: (r.kind.value, r.decision.value, r.actor)
            for r in session.scalars(sa.select(Review))
        }


# --- Leases --------------------------------------------------------------------------


async def test_a_reviewer_gets_the_oldest_group_and_keeps_it(
    client: AsyncClient, lib: Library, settings: Settings, clock: Clock
) -> None:
    first, ids = make_group(lib, settings)
    make_group(lib, settings)

    found = await next_group(client, ADA)

    assert (found["pending"], found["total"]) == (2, 2)
    group = found["group"]
    assert group["id"] == first
    assert [m["id"] for m in group["members"]] == ids
    assert group["members"][0]["rank"] == 0
    assert group["members"][0]["thumbnail_url"].startswith(f"/api/items/{ids[0]}/thumbnail")
    assert group["lease_expires_at"] == "2026-11-24T12:10:00Z"
    # Asking again, say after a reload, gives the same group back.
    assert (await next_group(client, ADA))["group"]["id"] == first


async def test_a_second_reviewer_doesn_t_get_a_leased_group(
    client: AsyncClient, lib: Library, settings: Settings, clock: Clock
) -> None:
    first, _ = make_group(lib, settings)
    second, _ = make_group(lib, settings)

    assert (await next_group(client, ADA))["group"]["id"] == first
    assert (await next_group(client, BOB))["group"]["id"] == second
    # Nothing left for a third.
    assert (await next_group(client, "reviewer-cy-0003"))["group"] is None


async def test_a_lease_expires(
    client: AsyncClient, lib: Library, settings: Settings, clock: Clock
) -> None:
    first, _ = make_group(lib, settings)
    await next_group(client, ADA)

    clock.now = AT + review_api.LEASE - dt.timedelta(seconds=1)
    assert (await next_group(client, BOB))["group"] is None
    clock.now = AT + review_api.LEASE
    assert (await next_group(client, BOB))["group"]["id"] == first
    # And Ada, coming back, can't decide on it now.
    assert (await decide(client, first, "skip")).status_code == 409


async def test_a_released_group_goes_to_the_next_reviewer(
    client: AsyncClient, lib: Library, settings: Settings, clock: Clock
) -> None:
    first, _ = make_group(lib, settings)
    await next_group(client, ADA)

    # Someone else's release does nothing.
    bob = await client.post(f"/api/duplicates/{first}/release", json={"reviewer": BOB})
    assert bob.status_code == 204
    assert (await next_group(client, BOB))["group"] is None
    ada = await client.post(f"/api/duplicates/{first}/release", json={"reviewer": ADA})
    assert ada.status_code == 204
    assert (await next_group(client, BOB))["group"]["id"] == first


async def test_a_group_with_one_member_left_is_passed_over(
    client: AsyncClient, lib: Library, settings: Settings, clock: Clock
) -> None:
    _, ids = make_group(lib, settings)
    lib.set_status(ids[1], ItemStatus.TRASHED)
    second, _ = make_group(lib, settings)

    assert (await next_group(client, ADA))["group"]["id"] == second


async def test_no_pending_groups(client: AsyncClient, migrated: Engine, clock: Clock) -> None:
    assert await next_group(client, ADA) == {"group": None, "pending": 0, "total": 0}


@pytest.mark.parametrize("reviewer", ["short", "x" * 65, "has spaces in it"])
async def test_a_bad_reviewer_token_is_refused(
    client: AsyncClient, migrated: Engine, reviewer: str
) -> None:
    response = await client.post("/api/duplicates/next", json={"reviewer": reviewer})
    assert response.status_code == 422


# --- Decisions -----------------------------------------------------------------------


async def test_keep_selected_trashes_the_rest(
    client: AsyncClient, lib: Library, settings: Settings, migrated: Engine, clock: Clock
) -> None:
    group_id, (best, second, third) = make_group(lib, settings, 3)
    await next_group(client, ADA)

    response = await decide(client, group_id, "keep_selected", [second])

    assert response.status_code == 200, response.text
    assert response.json() == {"moved": [best, third], "skipped": []}
    with Session(migrated) as session:
        statuses = {i.id: i.status for i in session.scalars(sa.select(Item))}
        entries = session.scalars(sa.select(TrashEntry.item_id)).all()
    assert statuses == {
        best: ItemStatus.TRASHED,
        second: ItemStatus.ACTIVE,
        third: ItemStatus.TRASHED,
    }
    assert sorted(entries) == sorted([best, third])
    group = group_row(migrated, group_id)
    assert (group.status, group.actor, group.resolved_at) == (GroupStatus.RESOLVED, "anonymous", AT)
    assert group.leased_to is None
    assert reviews(migrated) == {
        best: ("duplicate", "delete", "anonymous"),
        second: ("duplicate", "keep", "anonymous"),
        third: ("duplicate", "delete", "anonymous"),
    }


@pytest.mark.parametrize("keep", [[], [999]])
async def test_keep_selected_needs_members_to_keep(
    client: AsyncClient, lib: Library, settings: Settings, migrated: Engine, keep: list[int]
) -> None:
    group_id, _ = make_group(lib, settings)
    assert (await decide(client, group_id, "keep_selected", keep)).status_code == 422
    assert group_row(migrated, group_id).status == GroupStatus.PENDING


async def test_keep_all_queues_the_reviewed_marker(
    client: AsyncClient, lib: Library, settings: Settings, migrated: Engine, clock: Clock
) -> None:
    group_id, (a, b) = make_group(lib, settings)
    with Session(migrated) as session, session.begin():
        item = session.get(Item, b)
        assert item is not None
        item.reviewed = ["quality", "duplicates"]  # marked already: no write

    response = await decide(client, group_id, "keep_all")

    assert response.json() == {"moved": [], "skipped": []}
    assert group_row(migrated, group_id).status == GroupStatus.RESOLVED
    with Session(migrated) as session:
        writes = [(w.item_id, w.fields, w.actor) for w in session.scalars(sa.select(MetadataWrite))]
    assert writes == [(a, {"reviewed": ["duplicates"]}, "anonymous")]
    assert reviews(migrated) == {
        a: ("duplicate", "keep", "anonymous"),
        b: ("duplicate", "keep", "anonymous"),
    }


async def test_skip_leaves_the_group_for_later(
    client: AsyncClient, lib: Library, settings: Settings, migrated: Engine, clock: Clock
) -> None:
    group_id, (a, b) = make_group(lib, settings)
    await next_group(client, ADA)

    assert (await decide(client, group_id, "skip")).status_code == 200

    group = group_row(migrated, group_id)
    assert (group.status, group.leased_to) == (GroupStatus.SKIPPED, None)
    assert reviews(migrated) == {
        a: ("duplicate", "skip", "anonymous"),
        b: ("duplicate", "skip", "anonymous"),
    }
    assert (await next_group(client, ADA))["group"] is None


async def test_a_decided_group_can_t_be_decided_again(
    client: AsyncClient, lib: Library, settings: Settings, clock: Clock
) -> None:
    group_id, _ = make_group(lib, settings)
    await decide(client, group_id, "skip")
    assert (await decide(client, group_id, "keep_all")).status_code == 409


async def test_another_reviewer_s_group_can_t_be_decided(
    client: AsyncClient, lib: Library, settings: Settings, clock: Clock
) -> None:
    group_id, _ = make_group(lib, settings)
    await next_group(client, ADA)
    response = await decide(client, group_id, "keep_all", reviewer=BOB)
    assert response.status_code == 409
    assert (await decide(client, group_id, "keep_all")).status_code == 200


async def test_an_unknown_group_is_not_found(client: AsyncClient, migrated: Engine) -> None:
    assert (await decide(client, 999, "skip")).status_code == 404


# --- The marker, read back -----------------------------------------------------------


def test_metadata_read_records_the_reviewed_marker(
    settings: Settings, exiftool: ExifTool, tmp_path: Path
) -> None:
    path = tmp_path / "scene.jpg"
    shutil.copy(SYNTHETIC / "scenes/scene-01.jpg", path)
    exiftool.write(
        path,
        writeback.exiftool_args(
            writeback.Container.JPEG,
            {
                writeback.Field.REVIEWED: ["quality", "duplicates"],
            },
        ),
    )

    found = metadata.read(path, MediaType.PHOTO, exiftool, settings.tz, path.stat().st_mtime_ns)

    assert found.reviewed == ("quality", "duplicates")


def test_a_group_already_marked_kept_starts_resolved(lib: Library, migrated: Engine) -> None:
    marked = [lib.add(unit(1, 0.01 * n)) for n in range(2)]
    unmarked = [lib.add(unit(0, 1, 0.01 * n)) for n in range(2)]
    with Session(migrated) as session, session.begin():
        for item_id in [*marked, unmarked[0]]:
            item = session.get(Item, item_id)
            assert item is not None
            item.reviewed = [duplicates.REVIEWED_MARKER]

    duplicates.group(sessionmaker(migrated), lib.model, 0.95, NOW)

    with Session(migrated) as session:
        found = {
            tuple(
                sorted(
                    session.scalars(
                        sa.select(DuplicateMember.item_id).where(DuplicateMember.group_id == g.id)
                    )
                )
            ): g.status
            for g in session.scalars(sa.select(DuplicateGroup))
        }
    assert found == {tuple(marked): GroupStatus.RESOLVED, tuple(unmarked): GroupStatus.PENDING}
