"""An item's nearest CLIP neighbours (plan §6.4 "Lightbox & similarity walk"), from
the API's in-memory matrix (`photo_triage.api.similarity`)."""

import datetime as dt

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.api.similarity import Similarity
from photo_triage.db.models import Embedding, Item, ItemStatus, Job, JobStatus, MediaType
from photo_triage.pipeline.embed import to_blob
from photo_triage.settings import Settings
from photo_triage.worker.queue import PRIORITY, Stage

pytestmark = pytest.mark.anyio

NOW = dt.datetime(2026, 11, 21, tzinfo=dt.UTC)
DIM = 512


def unit(*weights: float) -> np.ndarray:
    """A unit vector with these first components."""
    vector = np.zeros(DIM, np.float32)
    vector[: len(weights)] = weights
    return vector / np.linalg.norm(vector)


class Library:
    def __init__(self, engine: Engine, model: str) -> None:
        self.engine = engine
        self.model = model
        self._next = 0

    def add(
        self,
        vector: np.ndarray | None,
        *,
        model: str | None = None,
        media_type: MediaType = MediaType.PHOTO,
    ) -> int:
        """An active item, embedded with `vector` unless None, and its done clip job."""
        self._next += 1
        with Session(self.engine) as session, session.begin():
            item = Item(
                content_hash=f"{self._next:064x}",
                path=f"photos/{self._next}.jpg",
                media_type=media_type,
                file_size_bytes=1,
                file_mtime_ns=0,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            session.add(item)
            session.flush()
            if vector is not None:
                session.add(
                    Embedding(item_id=item.id, model=model or self.model, vector=to_blob(vector))
                )
                session.add(
                    Job(
                        item_id=item.id,
                        stage=Stage.CLIP,
                        priority=PRIORITY[Stage.CLIP],
                        status=JobStatus.DONE,
                        enqueued_at=NOW,
                        finished_at=NOW,
                    )
                )
            return item.id

    def set_status(self, item_id: int, status: ItemStatus) -> None:
        with Session(self.engine) as session, session.begin():
            item = session.get(Item, item_id)
            assert item is not None
            item.status = status


@pytest.fixture
def lib(migrated: Engine, settings: Settings) -> Library:
    return Library(migrated, settings.clip_model)


@pytest.fixture
def fresh(app: FastAPI, migrated: Engine, settings: Settings) -> None:
    """Read the matrix again on every request in which the worker has embedded."""
    app.state.similarity = Similarity(migrated, settings.clip_model, reload_min_interval_s=0)


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
