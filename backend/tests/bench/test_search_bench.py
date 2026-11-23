"""How long a search takes on 100k items, text encoding included (#116, plan §6.5).

Not in the default suite: `just bench` runs it, and it skips without the exported
CLIP model. It writes `ITEMS` active items to a migrated database, embeds each
query with the API's text encoder (one thread, as the app's one CPU allows), and
times what `GET /api/search` does: encoding the query, the check for new
embeddings, the matrix product, ranking what's above the floor and checking the
matches are still in the library.

Random vectors score about 0 against any text, so `NEAR_FRACTION` of the items are
made near one of the queries instead, as a library holds many pictures of what's
searched for. CLIP's text vectors are alike, so the items made near one query
match others too: each query matches 6,000 to 7,000 items, a heavy case.
"""

import statistics
import time

import numpy as np
import pytest
from embedded import insert_embedded
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.api.similarity import Similarity
from photo_triage.api.text_encoder import TextEncoder
from photo_triage.ml.clip import model_dir
from photo_triage.settings import DEFAULT_MODELS_DIR, Settings

pytestmark = pytest.mark.bench

ITEMS = 100_000
DIM = 512
MODEL = "ViT-B-32"
QUERIES = [
    "a cat",
    "the beach",
    "snow",
    "a birthday cake",
    "a christmas tree",
    "a baby",
    "fireworks",
    "mountains",
    "food on a plate",
    "a sunset",
]
NEAR_FRACTION = 0.1
ROUNDS = 5
TARGET_S = 0.300


@pytest.mark.skipif(
    not (model_dir(DEFAULT_MODELS_DIR, MODEL) / "text.onnx").is_file(),
    reason="needs the exported CLIP model (scripts/bootstrap.sh)",
)
def test_search_on_100k_items(migrated: Engine, settings: Settings) -> None:
    encoder = TextEncoder(
        settings.model_copy(update={"models_dir": DEFAULT_MODELS_DIR, "clip_model": MODEL})
    )
    embedder = encoder.embedder()
    texts = embedder.embed_texts(QUERIES)

    rng = np.random.default_rng(116)
    vectors = rng.standard_normal((ITEMS, DIM), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    near = rng.random(ITEMS) < NEAR_FRACTION
    # cos ≈ 0.3 to its query: a little above the floor, like a real match.
    which = rng.integers(0, len(QUERIES), ITEMS)
    vectors[near] = 0.3 * texts[which[near]] + 0.954 * vectors[near]
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    insert_embedded(migrated, vectors, MODEL)

    similarity = Similarity(migrated, MODEL)
    with Session(migrated) as session:
        similarity.matches(session, texts[0], embedder.search_floor)  # loads the matrix
        times_s: list[float] = []
        counts: list[int] = []
        for _ in range(ROUNDS):
            for query in QUERIES:
                started_s = time.perf_counter()
                vector = embedder.embed_texts([query])[0]
                found = similarity.matches(session, vector, embedder.search_floor)
                times_s.append(time.perf_counter() - started_s)
                counts.append(len(found))

    median_s = statistics.median(times_s)
    worst_s = max(times_s)
    print(
        f"\n{ITEMS} items, {len(times_s)} searches of {min(counts)} to {max(counts)} matches: "
        f"median {median_s * 1000:.0f} ms, worst {worst_s * 1000:.0f} ms"
    )
    assert min(counts) > 0
    assert worst_s <= TARGET_S
