"""How long the lightbox's neighbours take on 100k items (#115, plan §6.4).

Not in the default suite: `just bench` runs it. It writes `ITEMS` active items with
random 512-d unit vectors and a done clip job each to a migrated database, loads
the API's matrix (`photo_triage.api.similarity`), and times `Similarity.neighbours`
as the endpoint calls it: the check for new embeddings, the matrix product, the
ranking and the check that the neighbours are still in the library.
"""

import datetime as dt
import statistics
import time
import tracemalloc

import numpy as np
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.api.similarity import Similarity
from photo_triage.db.models import Embedding, Item, Job, JobStatus, MediaType
from photo_triage.pipeline.embed import to_blob
from photo_triage.worker.queue import PRIORITY, Stage

pytestmark = pytest.mark.bench

ITEMS = 100_000
DIM = 512
LIMIT = 12
QUERIES = 50
TARGET_S = 0.050
MODEL = "ViT-B-32"
NOW = dt.datetime(2026, 11, 21, tzinfo=dt.UTC)


def test_neighbours_on_100k_items(migrated: Engine) -> None:
    engine = migrated
    rng = np.random.default_rng(115)
    vectors = rng.standard_normal((ITEMS, DIM), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    with Session(engine) as session, session.begin():
        session.execute(
            Item.__table__.insert(),
            [
                {
                    "id": i + 1,
                    "content_hash": f"{i:064x}",
                    "path": f"photos/{i}.jpg",
                    "media_type": MediaType.PHOTO,
                    "status": "active",
                    "file_size_bytes": 1,
                    "file_mtime_ns": 0,
                    "first_seen_at": NOW,
                    "last_seen_at": NOW,
                    "damaged": False,
                }
                for i in range(ITEMS)
            ],
        )
        session.execute(
            Embedding.__table__.insert(),
            [
                {"item_id": i + 1, "model": MODEL, "vector": to_blob(vectors[i])}
                for i in range(ITEMS)
            ],
        )
        session.execute(
            Job.__table__.insert(),
            [
                {
                    "item_id": i + 1,
                    "stage": Stage.CLIP,
                    "priority": PRIORITY[Stage.CLIP],
                    "status": JobStatus.DONE,
                    "attempts": 0,
                    "enqueued_at": NOW,
                    "finished_at": NOW,
                }
                for i in range(ITEMS)
            ],
        )

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
