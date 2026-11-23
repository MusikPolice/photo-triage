"""How long duplicate grouping takes on 100k items (#155, plan §6.7).

Not in the default suite: `just bench` runs it. It writes `ITEMS` active items with
random unit vectors, which are nowhere near each other, then makes `COPIES` of them
near copies of others, in bursts like a phone's, and times `duplicates.group`:
reading the vectors, the blockwise similarities, the components and storing the
groups. Target: under 2 minutes, on the worker's 4 threads.
"""

import time

import numpy as np
import pytest
import sqlalchemy as sa
from embedded import NOW, insert_embedded
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import DuplicateMember
from photo_triage.pipeline import duplicates

pytestmark = pytest.mark.bench

ITEMS = 100_000
DIM = 512
MODEL = "ViT-B-32"
COPIES = 5_000
"""Items made near copies of another, in bursts of 1 to 4."""
TARGET_S = 120.0


def test_grouping_100k_items(migrated: Engine) -> None:
    rng = np.random.default_rng(155)
    vectors = rng.normal(size=(ITEMS, DIM)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    row = 0
    made = 0
    while made < COPIES:
        burst = int(rng.integers(1, 5))
        for n in range(1, burst + 1):
            noise = rng.normal(scale=0.005, size=DIM).astype(np.float32)
            copy = vectors[row] + noise
            vectors[row + n] = copy / np.linalg.norm(copy)
        made += burst
        row += burst + 1 + int(rng.integers(0, 15))
    insert_embedded(migrated, vectors, MODEL)

    started = time.perf_counter()
    result = duplicates.group(sessionmaker(migrated), MODEL, 0.95, NOW)
    elapsed_s = time.perf_counter() - started

    with Session(migrated) as session:
        grouped = session.scalar(sa.select(sa.func.count()).select_from(DuplicateMember))
    print(f"\n{result} in {elapsed_s:.1f} s; {grouped} memberships")
    assert result.items == grouped
    assert result.items >= COPIES
    assert elapsed_s < TARGET_S
