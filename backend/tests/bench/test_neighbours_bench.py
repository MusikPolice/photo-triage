"""How long the lightbox's neighbours take on 100k items (#115, plan §6.4).

Not in the default suite: `just bench` runs it. It writes `ITEMS` active items with
random 512-d unit vectors and a done clip job each to a migrated database, loads
the API's matrix (`photo_triage.api.similarity`), and times `Similarity.neighbours`
as the endpoint calls it: the check for new embeddings, the matrix product, the
ranking and the check that the neighbours are still in the library.
"""

import statistics
import time
import tracemalloc

import numpy as np
import pytest
from embedded import insert_embedded
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.api.similarity import Similarity

pytestmark = pytest.mark.bench

ITEMS = 100_000
DIM = 512
LIMIT = 12
QUERIES = 50
TARGET_S = 0.050
MODEL = "ViT-B-32"


def test_neighbours_on_100k_items(migrated: Engine) -> None:
    engine = migrated
    rng = np.random.default_rng(115)
    vectors = rng.standard_normal((ITEMS, DIM), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    insert_embedded(engine, vectors, MODEL)

    similarity = Similarity(engine, MODEL)
    with Session(engine) as session:
        tracemalloc.start()
        started_s = time.perf_counter()
        first = similarity.neighbours(session, 1, LIMIT)
        load_s = time.perf_counter() - started_s
        _, load_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert first is not None
        assert len(first) == LIMIT

        times_s = []
        for item_id in rng.integers(1, ITEMS + 1, QUERIES):
            started_s = time.perf_counter()
            found = similarity.neighbours(session, int(item_id), LIMIT)
            times_s.append(time.perf_counter() - started_s)
            assert found is not None
            assert len(found) == LIMIT

    median_s = statistics.median(times_s)
    worst_s = max(times_s)
    print(
        f"\n{ITEMS} items: first query (loading the matrix) {load_s:.2f} s; "
        f"then median {median_s * 1000:.1f} ms, worst {worst_s * 1000:.1f} ms; "
        f"matrix {similarity.nbytes / 1e6:.0f} MB, "
        f"peak while loading {load_peak_bytes / 1e6:.0f} MB"
    )
    assert median_s <= TARGET_S
