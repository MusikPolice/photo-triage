"""How long reconcile takes on a processed library when nothing is due (#48).

Not in the default suite: `just bench` runs it. It builds a migrated database with
`ITEMS` active items, each with a done job for three chained item stages, as a
library looks once Phase 2 has run, then times a reconcile that finds nothing to do.
There's no `ANALYZE`, as on a real database, so the planner has no statistics.
"""

import datetime as dt
import statistics
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from photo_triage.db.engine import open_database
from photo_triage.worker.queue import PRIORITY, Stage
from photo_triage.worker.reconcile import reconcile
from photo_triage.worker.stages import ItemStage

pytestmark = pytest.mark.bench

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
ITEMS = 100_000
RUNS = 5
TARGET_S = 5.0

NOW = dt.datetime(2026, 10, 25, 12, tzinfo=dt.UTC)

STAGES: dict[str, ItemStage] = {
    Stage.SCAN: ItemStage(code_version=1),
    Stage.THUMBNAIL: ItemStage(code_version=1, upstream=(Stage.SCAN,)),
    Stage.CLIP: ItemStage(code_version=1, upstream=(Stage.THUMBNAIL,)),
}


@pytest.fixture(scope="module")
def processed(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    data_dir = tmp_path_factory.mktemp("data")
    engine = open_database(data_dir)
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")
    seen = NOW.replace(tzinfo=None).isoformat(sep=" ")
    items = [(n, f"{n:064x}", f"{n // 1000}/{n}.jpg", seen, seen) for n in range(1, ITEMS + 1)]
    # Each stage finished a second after the one before it.
    jobs = [
        (
            item_id,
            stage,
            PRIORITY[Stage(stage)],
            seen,
            done := (NOW + dt.timedelta(seconds=n)).replace(tzinfo=None).isoformat(sep=" "),
            STAGES[stage].version,
            done,
        )
        for item_id in range(1, ITEMS + 1)
        for n, stage in enumerate(STAGES)
    ]
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO items (id, content_hash, path, media_type, file_size_bytes, "
            "file_mtime_ns, first_seen_at, last_seen_at, status) "
            "VALUES (?, ?, ?, 'photo', 1, 0, ?, ?, 'active')",
            items,
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (item_id, stage, priority, status, attempts, enqueued_at, "
            "finished_at, stage_version, output_changed_at) "
            "VALUES (?, ?, ?, 'done', 0, ?, ?, ?, ?)",
            jobs,
        )
    return engine


def test_reconcile_with_nothing_due_on_a_large_library(
    processed: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert reconcile(processed, STAGES, NOW) == dict.fromkeys(STAGES, 0)  # and warm the cache
    timings: list[float] = []
    for _ in range(RUNS):
        start = time.perf_counter()
        reconcile(processed, STAGES, NOW)
        timings.append(time.perf_counter() - start)
    median, worst = statistics.median(timings), max(timings)
    with capsys.disabled():
        print(
            f"\nreconcile, {ITEMS:,} items x {len(STAGES)} stages, nothing due: "
            f"median {median:.3f} s, worst {worst:.3f} s (target {TARGET_S} s)"
        )
    assert worst <= TARGET_S
