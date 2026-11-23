"""An item's nearest CLIP neighbours (plan §6.4 "Lightbox & similarity walk"), from
the API's in-memory matrix (`photo_triage.api.similarity`)."""

import pytest
from httpx import AsyncClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from vectors import Library, unit

from photo_triage.api.similarity import Similarity
from photo_triage.db.models import ItemStatus
from photo_triage.settings import Settings

pytestmark = pytest.mark.anyio


async def neighbour_ids(client: AsyncClient, item_id: int, limit: int = 12) -> list[int]:
    response = await client.get(f"/api/items/{item_id}/neighbours", params={"limit": limit})
    assert response.status_code == 200, response.text
    return [n["id"] for n in response.json()["items"]]


async def test_the_most_similar_come_first(
    client: AsyncClient, lib: Library, settings: Settings
) -> None:
    item = lib.add(unit(1, 0))
    far = lib.add(unit(0, 1))
    near = lib.add(unit(1, 0.1))
    middle = lib.add(unit(1, 1))

    response = await client.get(f"/api/items/{item}/neighbours")

    assert response.json() == {
        "embedded": True,
        "items": [
            {
                "id": i,
                "media_type": "photo",
                "thumbnail_url": f"/api/items/{i}/thumbnail?size={settings.thumbnail_size}",
            }
            for i in (near, middle, far)
        ],
    }


async def test_limit_caps_the_strip(client: AsyncClient, lib: Library) -> None:
    item = lib.add(unit(1))
    others = [lib.add(unit(1, k / 10)) for k in range(1, 6)]

    assert await neighbour_ids(client, item, limit=2) == others[:2]
    assert (await client.get(f"/api/items/{item}/neighbours?limit=0")).status_code == 422
    assert (await client.get(f"/api/items/{item}/neighbours?limit=51")).status_code == 422


async def test_items_that_left_the_library_are_not_neighbours(
    client: AsyncClient, lib: Library
) -> None:
    item = lib.add(unit(1))
    trashed = lib.add(unit(1, 0.1))
    kept = lib.add(unit(1, 0.5))
    assert await neighbour_ids(client, item) == [trashed, kept]  # loaded with both

    lib.set_status(trashed, ItemStatus.TRASHED)

    assert await neighbour_ids(client, item) == [kept]
    assert (await client.get(f"/api/items/{trashed}/neighbours")).status_code == 404


async def test_another_model_s_vectors_are_left_out(client: AsyncClient, lib: Library) -> None:
    item = lib.add(unit(1))
    lib.add(unit(1, 0.1), model="another-model")
    same = lib.add(unit(0, 1))

    assert await neighbour_ids(client, item) == [same]


async def test_an_item_not_embedded_yet_has_none(client: AsyncClient, lib: Library) -> None:
    lib.add(unit(1))
    waiting = lib.add(None)

    response = await client.get(f"/api/items/{waiting}/neighbours")

    assert response.json() == {"embedded": False, "items": []}


async def test_an_unknown_item_is_not_found(client: AsyncClient, migrated: Engine) -> None:
    assert (await client.get("/api/items/999/neighbours")).status_code == 404


async def test_a_new_embedding_shows_up_without_a_restart(
    client: AsyncClient, lib: Library, fresh: None
) -> None:
    item = lib.add(unit(1))
    other = lib.add(unit(0, 1))
    assert await neighbour_ids(client, item) == [other]

    # The worker embeds a close match, in its own process.
    close = lib.add(unit(1, 0.05))

    assert await neighbour_ids(client, item) == [close, other]
    assert await neighbour_ids(client, close) == [item, other]


def test_the_matrix_is_read_again_at_most_once_per_interval(migrated: Engine, lib: Library) -> None:
    clock = [0.0]
    similarity = Similarity(
        migrated, lib.model, reload_min_interval_s=10, monotonic=lambda: clock[0]
    )
    item = lib.add(unit(1))
    with Session(migrated) as session:
        assert similarity.neighbours(session, item, 5) == []

        close = lib.add(unit(1, 0.1))
        clock[0] = 5.0
        assert similarity.neighbours(session, item, 5) == []  # still the old matrix
        clock[0] = 10.0
        assert similarity.neighbours(session, item, 5) == [close]
