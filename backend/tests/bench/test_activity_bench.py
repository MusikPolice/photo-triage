"""How long `read_activity` takes on a large `jobs` table (#24).

Not in the default suite: `just bench` runs it. It builds a migrated database with
`JOBS` jobs, most of them done, as Phase 2 leaves the table, then times the read.
"""

import datetime as dt
import random
import statistics
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.api.activity import read_activity
from photo_triage.db.engine import open_database
from photo_triage.db.models import JobStatus
from photo_triage.settings import Settings
from photo_triage.worker.queue import PRIORITY, Stage

pytestmark = pytest.mark.bench

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
JOBS = 1_000_000
RUNS = 20
TARGET_S = 0.050

NOW = dt.datetime(2026, 10, 16, 12, tzinfo=dt.UTC)

# Mostly done, as after a pass over the library, with a tail of open and failed jobs.
STATUS_WEIGHTS = {
    JobStatus.DONE: 90,
    JobStatus.PENDING: 8,
    JobStatus.ERROR: 1,
    JobStatus.PARKED: 1,
}


@pytest.fixture(scope="module")
def big(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    data_dir = tmp_path_factory.mktemp("data")
    engine = open_database(data_dir)
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")
    rng = random.Random(24)
    stages = [s for s in Stage if s != Stage.NOOP]
    statuses = list(STATUS_WEIGHTS)
    weights = list(STATUS_WEIGHTS.values())
    enqueued = NOW.replace(tzinfo=None).isoformat(sep=" ")
    rows = (
        (stage, PRIORITY[stage], status, enqueued)
        for stage in stages
        for status in rng.choices(statuses, weights, k=JOBS // len(stages))
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO jobs (stage, priority, status, attempts, enqueued_at) "
            "VALUES (?, ?, ?, 0, ?)",
            list(rows),
        )
    with engine.connect() as conn:
        conn.exec_driver_sql("ANALYZE")
        conn.commit()
    return engine


def test_read_activity_on_a_large_jobs_table(
    big: Engine, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    timings: list[float] = []
    with Session(big) as session:
        read_activity(session, settings, NOW, NOW)  # warm the page cache
        for _ in range(RUNS):
            start = time.perf_counter()
            read_activity(session, settings, NOW, NOW)
            timings.append(time.perf_counter() - start)
            session.rollback()

    median = statistics.median(timings)
    with capsys.disabled():
        print(
            f"\nread_activity over {JOBS:,} jobs, {RUNS} runs: "
            f"median {median * 1000:.1f} ms, "
            f"min {min(timings) * 1000:.1f} ms, max {max(timings) * 1000:.1f} ms"
        )
    assert median <= TARGET_S
