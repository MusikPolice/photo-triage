"""Reconciling item stages with what they've produced (plan §6.11), with test-only
stages registered over a migrated database."""

import datetime as dt
import logging
from collections.abc import Callable, Mapping

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.db.models import Item, ItemStatus, Job, JobStatus, MediaType
from photo_triage.settings import Settings
from photo_triage.worker.__main__ import main
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import JobQueue, RetryPolicy, Stage
from photo_triage.worker.reconcile import RegistryError, due, ordered, reconcile
from photo_triage.worker.stages import ItemStage, item_stages

START = dt.datetime(2026, 10, 25, 2, tzinfo=dt.UTC)
ITEMS = 4

SCAN = ItemStage(code_version=1)
THUMBNAIL = ItemStage(code_version=1, upstream=(Stage.SCAN,))
CLIP = ItemStage(code_version=1, upstream=(Stage.THUMBNAIL,))


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> dt.datetime:
        return self.now


class Runner:
    """Records the jobs it ran, each taking a second. `changed` is what it reports."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.ran: list[tuple[str, int | None]] = []
        self.changed = True
        self.failing = False

    def __call__(self, job: Job) -> bool:
        self.clock.now += dt.timedelta(seconds=1)
        self.ran.append((job.stage, job.item_id))
        if self.failing:
            raise OSError("can't decode")
        return self.changed


class Pipeline:
    """A worker running `Runner` for scan, thumbnail and CLIP, with the item stages
    that `register` sets."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.clock = Clock()
        self.runner = Runner(self.clock)
        self.queue = JobQueue(self.clock, RetryPolicy(max_attempts=1))
        self.register({Stage.SCAN: SCAN})

    def register(self, stages: Mapping[str, ItemStage]) -> None:
        self.worker = Worker(
            self.engine,
            dict.fromkeys([Stage.SCAN, Stage.THUMBNAIL, Stage.CLIP], self.runner),
            queue=self.queue,
            clock=self.clock,
            item_stages=stages,
        )

    def reconcile(self) -> dict[str, int]:
        return self.worker.reconcile()

    def run(self) -> list[tuple[str, int | None]]:
        """Run every queued job, and return the ones that ran."""
        self.runner.ran = []
        self.worker.drain()
        return self.runner.ran

    def done(self, stage: Stage) -> dict[int, str | None]:
        """The version of each item's done job for `stage`."""
        with Session(self.engine) as session:
            rows = session.execute(
                sa.select(Job.item_id, Job.stage_version).where(
                    Job.stage == stage, Job.status == JobStatus.DONE
                )
            )
            return {item_id: version for item_id, version in rows if item_id is not None}


def _items(engine: Engine, count: int, status: ItemStatus = ItemStatus.ACTIVE) -> list[int]:
    with Session(engine) as session:
        items = [
            Item(
                path=f"{status}-{n}.jpg",
                media_type=MediaType.PHOTO,
                file_size_bytes=1,
                file_mtime_ns=0,
                first_seen_at=START,
                last_seen_at=START,
                status=status,
            )
            for n in range(count)
        ]
        session.add_all(items)
        session.commit()
        return [item.id for item in items]


@pytest.fixture
def pipeline(migrated: Engine) -> Pipeline:
    return Pipeline(migrated)


@pytest.fixture
def items(migrated: Engine) -> list[int]:
    return _items(migrated, ITEMS)


def _ran(stage: Stage, items: list[int]) -> list[tuple[str, int | None]]:
    return [(stage, item) for item in items]


def test_a_new_stage_backfills_every_processed_item(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    _items(migrated, 2, ItemStatus.MISSING)
    _items(migrated, 2, ItemStatus.TRASHED)
    assert pipeline.reconcile() == {Stage.SCAN: ITEMS}  # only active items
    assert pipeline.run() == _ran(Stage.SCAN, items)

    pipeline.register({Stage.SCAN: SCAN, Stage.THUMBNAIL: THUMBNAIL, Stage.CLIP: CLIP})
    # CLIP waits for thumbnails, which none of the items has yet.
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: ITEMS, Stage.CLIP: 0}
    assert pipeline.run() == _ran(Stage.THUMBNAIL, items)
    assert pipeline.done(Stage.THUMBNAIL) == dict.fromkeys(items, "1")
    assert pipeline.reconcile()[Stage.CLIP] == ITEMS


def test_reconciling_twice_queues_nothing_the_second_time(
    pipeline: Pipeline, items: list[int]
) -> None:
    pipeline.register({Stage.SCAN: SCAN, Stage.THUMBNAIL: THUMBNAIL})
    assert pipeline.reconcile() == {Stage.SCAN: ITEMS, Stage.THUMBNAIL: 0}
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 0}
    pipeline.run()
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: ITEMS}
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 0}
    pipeline.run()
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 0}


def _process(pipeline: Pipeline, stages: Mapping[str, ItemStage]) -> None:
    """Register `stages` and run them on every item, until nothing is due."""
    pipeline.register(stages)
    while any(pipeline.reconcile().values()):
        pipeline.run()


def test_bumping_a_version_re_runs_the_stage_then_its_downstream_stages(
    pipeline: Pipeline, items: list[int]
) -> None:
    _process(pipeline, {Stage.SCAN: SCAN, Stage.THUMBNAIL: THUMBNAIL, Stage.CLIP: CLIP})

    fixed = ItemStage(code_version=2, upstream=(Stage.SCAN,))
    pipeline.register({Stage.SCAN: SCAN, Stage.THUMBNAIL: fixed, Stage.CLIP: CLIP})
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: ITEMS, Stage.CLIP: 0}
    assert pipeline.run() == _ran(Stage.THUMBNAIL, items)
    assert pipeline.done(Stage.THUMBNAIL) == dict.fromkeys(items, "2")
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 0, Stage.CLIP: ITEMS}
    assert pipeline.run() == _ran(Stage.CLIP, items)
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 0, Stage.CLIP: 0}


def test_a_changed_setting_re_runs_the_stage(pipeline: Pipeline, items: list[int]) -> None:
    clip = ItemStage(code_version=1, upstream=(Stage.SCAN,), settings_fingerprint="ViT-B-32")
    _process(pipeline, {Stage.SCAN: SCAN, Stage.CLIP: clip})
    assert pipeline.done(Stage.CLIP) == dict.fromkeys(items, "1+ViT-B-32")

    other = ItemStage(code_version=1, upstream=(Stage.SCAN,), settings_fingerprint="ViT-L-14")
    pipeline.register({Stage.SCAN: SCAN, Stage.CLIP: other})
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.CLIP: ITEMS}


def test_a_re_run_that_reproduces_its_output_leaves_downstream_stages_alone(
    pipeline: Pipeline, items: list[int]
) -> None:
    stages: dict[str, ItemStage] = {Stage.SCAN: SCAN, Stage.THUMBNAIL: THUMBNAIL, Stage.CLIP: CLIP}
    _process(pipeline, stages)

    rescan = ItemStage(code_version=2)
    pipeline.register({**stages, Stage.SCAN: rescan})
    pipeline.runner.changed = False  # e.g. the file moved: same content hash
    assert pipeline.reconcile()[Stage.SCAN] == ITEMS
    pipeline.run()
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 0, Stage.CLIP: 0}


def test_new_upstream_output_re_runs_the_downstream_stage(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    _process(pipeline, {Stage.SCAN: SCAN, Stage.THUMBNAIL: THUMBNAIL})
    changed = items[0]  # its file changed, so the walk queued a scan
    with Session(migrated) as session, session.begin():
        pipeline.queue.enqueue(session, Stage.SCAN, changed)
    assert pipeline.run() == [(Stage.SCAN, changed)]
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 1}
    assert pipeline.run() == [(Stage.THUMBNAIL, changed)]


def test_a_deleted_pending_job_is_queued_again(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    _process(pipeline, {Stage.SCAN: SCAN})
    pipeline.register({Stage.SCAN: SCAN, Stage.THUMBNAIL: THUMBNAIL})
    pipeline.reconcile()
    lost = items[1]
    with Session(migrated) as session, session.begin():
        session.execute(sa.delete(Job).where(Job.stage == Stage.THUMBNAIL, Job.item_id == lost))
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 1}
    assert sorted(item for _, item in pipeline.run() if item is not None) == items


def test_a_parked_job_is_not_queued_again_until_its_version_changes(
    pipeline: Pipeline, items: list[int]
) -> None:
    _process(pipeline, {Stage.SCAN: SCAN})
    pipeline.register({Stage.SCAN: SCAN, Stage.THUMBNAIL: THUMBNAIL})
    pipeline.reconcile()
    pipeline.runner.failing = True
    pipeline.run()  # one attempt, so each is parked
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 0}

    fixed = ItemStage(code_version=2, upstream=(Stage.SCAN,))
    pipeline.register({Stage.SCAN: SCAN, Stage.THUMBNAIL: fixed})
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: ITEMS}
    pipeline.runner.failing = False
    pipeline.run()
    assert pipeline.done(Stage.THUMBNAIL) == dict.fromkeys(items, "2")


def test_a_running_job_is_not_queued_again(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    with Session(migrated) as session, session.begin():
        pipeline.queue.enqueue(session, Stage.SCAN, items[0])
        pipeline.queue.claim(session)
    assert pipeline.reconcile() == {Stage.SCAN: ITEMS - 1}


def test_due_reads_each_items_jobs_through_the_item_stage_index(migrated: Engine) -> None:
    sql = due(Stage.CLIP, CLIP).compile(migrated, compile_kwargs={"literal_binds": True})
    with migrated.connect() as conn:
        details = [row[3] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}")]
    assert [d for d in details if d.startswith("SCAN")] == ["SCAN items"], details
    # Every job lookup is one item's jobs for a stage, not every job of a stage.
    lookups = [d for d in details if d.startswith("SEARCH")]
    assert len(lookups) == 4, details
    index = "USING INDEX ix_jobs_item_stage_status (item_id=? AND stage=?"
    assert all(index in d for d in lookups), details


def test_the_registry_must_name_item_stages_without_cycles() -> None:
    assert ordered({Stage.CLIP: CLIP, Stage.THUMBNAIL: THUMBNAIL, Stage.SCAN: SCAN}) == [
        Stage.SCAN,
        Stage.THUMBNAIL,
        Stage.CLIP,
    ]
    with pytest.raises(RegistryError, match="thumbnail, which isn't an item stage"):
        ordered({Stage.CLIP: CLIP})
    loop = ItemStage(code_version=1, upstream=(Stage.CLIP,))
    with pytest.raises(RegistryError, match="depend on each other"):
        ordered({Stage.THUMBNAIL: loop, Stage.CLIP: CLIP})


def test_versions_carry_their_settings_fingerprint() -> None:
    assert ItemStage(code_version=3).version == "3"
    assert ItemStage(code_version=3, settings_fingerprint="ViT-B-32").version == "3+ViT-B-32"


def test_scan_is_the_first_registered_stage(settings: Settings) -> None:
    assert item_stages(settings) == {Stage.SCAN: ItemStage(code_version=1)}


def test_reconciling_logs_what_it_queued(
    migrated: Engine, items: list[int], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="photo_triage.worker.reconcile")
    reconcile(migrated, {Stage.SCAN: SCAN}, START)
    reconcile(migrated, {Stage.SCAN: SCAN}, START)
    assert caplog.messages == [
        f"Reconciled stages, queued {ITEMS} scan",
        "Reconciled stages, nothing due",
    ]


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Settings:
    monkeypatch.setenv("PHOTO_DIR", str(settings.photo_dir))
    monkeypatch.setenv("TRASH_DIR", str(settings.trash_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    settings.photo_dir.mkdir()
    return settings


def _pending(engine: Engine, stage: Stage) -> int:
    with Session(engine) as session:
        return (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(Job)
                .where(Job.stage == stage, Job.status == JobStatus.PENDING)
            )
            or 0
        )


@pytest.mark.usefixtures("worker_env")
def test_the_reconcile_command_queues_what_is_due(migrated: Engine, items: list[int]) -> None:
    assert main(["reconcile"]) == 0
    assert _pending(migrated, Stage.SCAN) == ITEMS


@pytest.mark.usefixtures("worker_env")
def test_the_worker_reconciles_at_startup(migrated: Engine, items: list[int]) -> None:
    runner = Runner(Clock())
    assert main(["--once"], runners={Stage.SCAN: runner}, stages={Stage.SCAN: SCAN}) == 0
    assert sorted(runner.ran) == _ran(Stage.SCAN, items)


@pytest.mark.usefixtures("worker_env")
def test_the_worker_reconciles_after_each_walk(migrated: Engine, items: list[int]) -> None:
    with Session(migrated) as session, session.begin():
        JobQueue().enqueue(session, Stage.WALK)
    runners: dict[str, Callable[[Job], None]] = {Stage.WALK: lambda job: None}
    # No item stages at startup, then a walk with scan registered.
    assert main(["--once"], runners=runners, stages={}) == 0
    assert _pending(migrated, Stage.SCAN) == 0
    with Session(migrated) as session, session.begin():
        JobQueue().enqueue(session, Stage.WALK)
    worker = Worker(migrated, runners, item_stages={Stage.SCAN: SCAN})
    assert worker.run_one()
    assert _pending(migrated, Stage.SCAN) == ITEMS
