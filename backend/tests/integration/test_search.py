"""Searching with text (plan §6.5 step 3, §6.4 "Search"): `GET /api/search` embeds
the query with CLIP's text encoder and returns the items at or above the floor,
best first, from the API's in-memory matrix."""

from collections.abc import Sequence

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from vectors import DIM, Library, unit

from photo_triage.api.search import MAX_QUERY_CHARS
from photo_triage.api.text_encoder import TextEncoder
from photo_triage.db.models import ItemStatus
from photo_triage.ml import FakeClip
from photo_triage.settings import Settings

pytestmark = pytest.mark.anyio

FLOOR = 0.5


class Encoder:
    """Embeds the texts it was given vectors for, with its own floor."""

    def __init__(self, vectors: dict[str, np.ndarray], floor: float = FLOOR) -> None:
        self.vectors = vectors
        self.floor = floor
        self.texts: list[str] = []

    def embedder(self) -> "Encoder":
        return self

    model = "test"
    dim = DIM
    image_size = 224

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        raise AssertionError("search embeds no pictures")

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        self.texts.extend(texts)
        return np.stack([self.vectors[t] for t in texts])

    @property
    def search_floor(self) -> float:
        return self.floor


@pytest.fixture
def encoder(app: FastAPI) -> Encoder:
    encoder = Encoder({"cats": unit(1, 0), "dogs": unit(0, 1), "nothing": unit(0, 0, 1)})
    app.state.text_encoder = encoder
    return encoder


async def search(client: AsyncClient, q: str) -> dict:
    response = await client.get("/api/search", params={"q": q})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.usefixtures("fresh")
async def test_matches_come_best_first_with_their_scores(
    client: AsyncClient, lib: Library, encoder: Encoder
) -> None:
    middling = lib.add(unit(1, 1))  # 0.71
    best = lib.add(unit(1, 0.1))  # 0.99
    lib.add(unit(0, 1))  # 0: a dog
    near_miss = lib.add(unit(1, 2))  # 0.45, under the floor
    exact = lib.add(unit(1, 0))  # 1

    found = await search(client, "cats")

    assert found["query"] == "cats"
    assert found["floor"] == FLOOR
    assert [m["id"] for m in found["matches"]] == [exact, best, middling]
    assert near_miss not in [m["id"] for m in found["matches"]]
    scores = [m["score"] for m in found["matches"]]
    assert scores == pytest.approx([1, 0.995, 0.707], abs=1e-3)
    assert encoder.texts == ["cats"]


@pytest.mark.usefixtures("fresh")
async def test_a_match_at_the_floor_counts(client: AsyncClient, lib: Library, app: FastAPI) -> None:
    item = lib.add(unit(1, 0))
    app.state.text_encoder = Encoder({"cats": unit(1, 0)}, floor=1.0 - 1e-6)

    assert [m["id"] for m in (await search(client, "cats"))["matches"]] == [item]


@pytest.mark.usefixtures("fresh", "encoder")
async def test_nothing_above_the_floor_is_no_matches(client: AsyncClient, lib: Library) -> None:
    lib.add(unit(1, 0))

    assert (await search(client, "nothing"))["matches"] == []


@pytest.mark.usefixtures("fresh", "encoder")
async def test_trashed_and_missing_items_aren_t_matches(client: AsyncClient, lib: Library) -> None:
    kept = lib.add(unit(1, 0))
    trashed = lib.add(unit(1, 0.1))
    missing = lib.add(unit(1, 0.2))
    assert len((await search(client, "cats"))["matches"]) == 3  # loaded with all three

    lib.set_status(trashed, ItemStatus.TRASHED)
    lib.set_status(missing, ItemStatus.MISSING)

    assert [m["id"] for m in (await search(client, "cats"))["matches"]] == [kept]


@pytest.mark.usefixtures("fresh", "encoder")
async def test_only_the_current_model_s_vectors_are_searched(
    client: AsyncClient, lib: Library
) -> None:
    current = lib.add(unit(1, 0))
    lib.add(unit(1, 0), model="another-model")

    assert [m["id"] for m in (await search(client, "cats"))["matches"]] == [current]


@pytest.mark.usefixtures("fresh", "encoder")
async def test_items_embedded_later_are_found(client: AsyncClient, lib: Library) -> None:
    first = lib.add(unit(1, 0))
    assert [m["id"] for m in (await search(client, "cats"))["matches"]] == [first]

    later = lib.add(unit(1, 0.01))

    assert [m["id"] for m in (await search(client, "cats"))["matches"]] == [first, later]


@pytest.mark.usefixtures("migrated", "encoder")
async def test_an_empty_library_has_no_matches(client: AsyncClient) -> None:
    assert (await search(client, "cats"))["matches"] == []


@pytest.mark.usefixtures("fresh", "encoder")
async def test_the_query_is_trimmed(client: AsyncClient, lib: Library) -> None:
    item = lib.add(unit(1, 0))

    found = await search(client, "  cats \n")

    assert found["query"] == "cats"
    assert [m["id"] for m in found["matches"]] == [item]


@pytest.mark.usefixtures("encoder")
@pytest.mark.parametrize("q", ["", "   ", "x" * (MAX_QUERY_CHARS + 1)])
async def test_an_empty_or_overlong_query_is_refused(client: AsyncClient, q: str) -> None:
    assert (await client.get("/api/search", params={"q": q})).status_code == 422
    assert (await client.get("/api/search")).status_code == 422


async def test_without_the_model_search_says_where_it_should_be(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.get("/api/search", params={"q": "cats"})

    assert response.status_code == 503
    assert str(settings.models_dir) in response.json()["detail"]


async def test_the_fake_embedder_searches_without_weights(
    app: FastAPI, settings: Settings, lib: Library
) -> None:
    """`WORKER_FAKE_CLIP`, as in `just e2e`: its vectors are random, so at its floor
    of 0 about half the items match."""
    fake = settings.model_copy(update={"worker_fake_clip": True})
    app.state.text_encoder = TextEncoder(fake)
    rng = np.random.default_rng(116)
    for _ in range(40):
        vector = rng.standard_normal(DIM).astype(np.float32)
        lib.add(vector / np.linalg.norm(vector))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        found = await search(client, "cats")

    assert found["floor"] == 0
    scores = [m["score"] for m in found["matches"]]
    assert 5 < len(scores) < 35
    assert scores == sorted(scores, reverse=True)
    assert min(scores) >= 0


def test_the_encoder_is_loaded_once(settings: Settings) -> None:
    encoder = TextEncoder(settings.model_copy(update={"worker_fake_clip": True}))

    first = encoder.embedder()

    assert isinstance(first, FakeClip)
    assert encoder.embedder() is first
