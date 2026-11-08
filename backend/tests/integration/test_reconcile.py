"""Reconciling item stages with what they've produced, and chaining each stage to the
next as its jobs finish (plan §6.11), with test-only stages registered over a
migrated database."""

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
from photo_triage.worker.reconcile import RegistryError, chain, due, ordered, reconcile
from photo_triage.worker.stages import ItemStage, item_stages

START = dt.datetime(2026, 10, 25, 2, tzinfo=dt.UTC)
ITEMS = 4

SCAN = ItemStage(code_version=1)
THUMBNAIL = ItemStage(code_version=1, upstream=(Stage.SCAN,))
CLIP = ItemStage(code_version=1, upstream=(Stage.THUMBNAIL,))
CHAIN: dict[str, ItemStage] = {Stage.SCAN: SCAN, Stage.THUMBNAIL: THUMBNAIL, Stage.CLIP: CLIP}


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
        self.during: Callable[[Job], None] | None = None
        """Called inside each run, e.g. to change the database under the job."""

    def __call__(self, job: Job) -> bool:
        self.clock.now += dt.timedelta(seconds=1)
        self.ran.append((job.stage, job.item_id))
        if self.during is not None:
            self.during(job)
        if self.failing:
            raise OSError("can't decode")
        return self.changed


class Pipeline:
    """A worker running `Runner` for scan, thumbnail, CLIP and quality, with the item
    stages that `register` sets."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.clock = Clock()
        self.runner = Runner(self.clock)
        self.queue = JobQueue(self.clock, RetryPolicy(max_attempts=1))
        self.register({Stage.SCAN: SCAN})

    def register(self, stages: Mapping[str, ItemStage]) -> None:
        self.worker = Worker(
            self.engine,
            dict.fromkeys([Stage.SCAN, Stage.THUMBNAIL, Stage.CLIP, Stage.QUALITY], self.runner),
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


def _items(
    engine: Engine,
    count: int,
    status: ItemStatus = ItemStatus.ACTIVE,
    kind: MediaType = MediaType.PHOTO,
) -> list[int]:
    with Session(engine) as session:
        items = [
            Item(
                path=f"{status}-{kind}-{n}",
                media_type=kind,
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
    # Each thumbnail queues the item's CLIP job as it finishes.
    assert pipeline.run() == _ran(Stage.THUMBNAIL, items) + _ran(Stage.CLIP, items)
    assert pipeline.done(Stage.THUMBNAIL) == dict.fromkeys(items, "1")
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 0, Stage.CLIP: 0}


def test_reconciling_twice_queues_nothing_the_second_time(
    pipeline: Pipeline, items: list[int]
) -> None:
    assert pipeline.reconcile() == {Stage.SCAN: ITEMS}
    assert pipeline.reconcile() == {Stage.SCAN: 0}
    pipeline.run()
    pipeline.register({Stage.SCAN: SCAN, Stage.THUMBNAIL: THUMBNAIL})  # added later
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
    assert pipeline.run() == _ran(Stage.THUMBNAIL, items) + _ran(Stage.CLIP, items)
    assert pipeline.done(Stage.THUMBNAIL) == dict.fromkeys(items, "2")
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
    assert pipeline.run() == [(Stage.SCAN, changed), (Stage.THUMBNAIL, changed)]
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.THUMBNAIL: 0}


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
    sql = due(Stage.CLIP, CHAIN).compile(migrated, compile_kwargs={"literal_binds": True})
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


def test_scan_reading_metadata_and_frames_are_registered_stages(settings: Settings) -> None:
    assert item_stages(settings) == {
        Stage.SCAN: ItemStage(code_version=1),
        Stage.METADATA_READ: ItemStage(code_version=1, upstream=(Stage.SCAN,)),
        Stage.FRAMES: ItemStage(
            code_version=1,
            upstream=(Stage.SCAN,),
            settings_fingerprint="5-frames",
            media_types=frozenset({MediaType.VIDEO}),
        ),
    }


def test_the_number_of_frames_is_part_of_the_frames_version(settings: Settings) -> None:
    more = settings.model_copy(update={"video_sample_frames": 8})
    assert item_stages(settings)[Stage.FRAMES].version == "1+5-frames"
    assert item_stages(more)[Stage.FRAMES].version == "1+8-frames"


# --- Stages for some kinds of item -----------------------------------------------------

VIDEOS_ONLY = ItemStage(
    code_version=1, upstream=(Stage.SCAN,), media_types=frozenset({MediaType.VIDEO})
)
"""Stands for `frames`, here registered as the thumbnail stage's upstream."""
BY_KIND: dict[str, ItemStage] = {
    Stage.SCAN: SCAN,
    Stage.QUALITY: VIDEOS_ONLY,
    Stage.THUMBNAIL: ItemStage(code_version=1, upstream=(Stage.SCAN, Stage.QUALITY)),
}


def test_a_stage_for_videos_is_queued_only_for_videos(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    videos = _items(migrated, 2, kind=MediaType.VIDEO)
    pipeline.register({Stage.SCAN: SCAN, Stage.QUALITY: VIDEOS_ONLY})
    pipeline.reconcile()
    pipeline.run()

    assert set(pipeline.done(Stage.QUALITY)) == set(videos)
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.QUALITY: 0}


def test_a_stage_after_one_for_videos_waits_for_it_only_on_videos(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    videos = _items(migrated, 2, kind=MediaType.VIDEO)
    pipeline.register(BY_KIND)
    _queue_scans(pipeline, migrated, [*items, *videos])

    ran = pipeline.run()

    # Each photo goes straight on; each video only after its frames.
    for photo in items:
        assert ran.index((Stage.THUMBNAIL, photo)) > ran.index((Stage.SCAN, photo))
        assert (Stage.QUALITY, photo) not in ran
    for clip in videos:
        assert ran.index((Stage.THUMBNAIL, clip)) > ran.index((Stage.QUALITY, clip))
    assert set(pipeline.done(Stage.THUMBNAIL)) == {*items, *videos}
    assert pipeline.reconcile() == dict.fromkeys(BY_KIND, 0)


def test_a_video_whose_frames_are_missing_waits_for_them(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    [clip] = _items(migrated, 1, kind=MediaType.VIDEO)
    pipeline.register({Stage.SCAN: SCAN})
    _queue_scans(pipeline, migrated, [*items, clip])
    pipeline.run()

    pipeline.register(BY_KIND)
    assert pipeline.reconcile() == {Stage.SCAN: 0, Stage.QUALITY: 1, Stage.THUMBNAIL: ITEMS}
    pipeline.run()
    assert set(pipeline.done(Stage.THUMBNAIL)) == {*items, clip}


def test_kinds_keep_due_on_the_item_stage_index(migrated: Engine) -> None:
    for query in (due(Stage.THUMBNAIL, BY_KIND), due(Stage.THUMBNAIL, BY_KIND).where(Item.id == 1)):
        sql = query.compile(migrated, compile_kwargs={"literal_binds": True})
        with migrated.connect() as conn:
            details = [row[3] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}")]
        assert not [d for d in details if d.startswith("SCAN") and "jobs" in d], details
        index = "USING INDEX ix_jobs_item_stage_status (item_id=? AND stage=?"
        assert all(index in d for d in details if d.startswith("SEARCH") and "jobs" in d), details


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


# --- Chaining -------------------------------------------------------------------------


def _queue_scans(pipeline: Pipeline, engine: Engine, items: list[int]) -> None:
    with Session(engine) as session, session.begin():
        for item in items:
            pipeline.queue.enqueue(session, Stage.SCAN, item)


def test_new_items_flow_through_every_stage_without_a_reconcile(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    pipeline.register(CHAIN)
    _queue_scans(pipeline, migrated, items)  # as a walk does

    ran = pipeline.run()

    assert sorted(ran) == sorted(
        _ran(Stage.SCAN, items) + _ran(Stage.THUMBNAIL, items) + _ran(Stage.CLIP, items)
    )
    for item in items:  # each after the one it reads
        order = [stage for stage, i in ran if i == item]
        assert order == [Stage.SCAN, Stage.THUMBNAIL, Stage.CLIP]
    assert pipeline.reconcile() == dict.fromkeys(CHAIN, 0)


def test_unchanged_output_queues_nothing_after_it_that_is_already_done(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    pipeline.register(CHAIN)
    _queue_scans(pipeline, migrated, items)
    pipeline.run()

    pipeline.runner.changed = False  # e.g. only the file's metadata changed
    _queue_scans(pipeline, migrated, items[:1])
    assert pipeline.run() == [(Stage.SCAN, items[0])]


def test_unchanged_output_still_queues_a_stage_that_never_ran(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    pipeline.register({Stage.SCAN: SCAN})
    _queue_scans(pipeline, migrated, items)
    pipeline.run()
    pipeline.register(CHAIN)  # thumbnails and CLIP deployed, not yet reconciled

    pipeline.runner.changed = False
    _queue_scans(pipeline, migrated, items[:1])

    assert pipeline.run() == [
        (Stage.SCAN, items[0]),
        (Stage.THUMBNAIL, items[0]),
        (Stage.CLIP, items[0]),
    ]


def test_a_stage_reading_two_others_waits_for_both(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    both = ItemStage(code_version=1, upstream=(Stage.THUMBNAIL, Stage.CLIP))
    clip_from_scan = ItemStage(code_version=1, upstream=(Stage.SCAN,))
    pipeline.register(
        {
            Stage.SCAN: SCAN,
            Stage.THUMBNAIL: THUMBNAIL,
            Stage.CLIP: clip_from_scan,
            Stage.QUALITY: both,
        }
    )
    _queue_scans(pipeline, migrated, items[:1])

    ran = [stage for stage, _ in pipeline.run()]

    # Thumbnail runs first (higher priority), and quality isn't due until CLIP is done.
    assert ran == [Stage.SCAN, Stage.THUMBNAIL, Stage.CLIP, Stage.QUALITY]


def test_a_job_moved_to_another_item_chains_for_that_item(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    pipeline.register({Stage.SCAN: SCAN, Stage.THUMBNAIL: THUMBNAIL})
    placeholder, moved = items[0], items[1]

    def merge(job: Job) -> None:  # as scan does when the placeholder's file moved
        if job.stage == Stage.SCAN:
            with Session(migrated) as session, session.begin():
                session.execute(sa.update(Job).where(Job.id == job.id).values(item_id=moved))
                session.execute(sa.delete(Item).where(Item.id == placeholder))

    pipeline.runner.during = merge
    _queue_scans(pipeline, migrated, [placeholder])

    assert pipeline.run() == [(Stage.SCAN, placeholder), (Stage.THUMBNAIL, moved)]


def test_a_job_whose_item_was_deleted_while_it_ran_is_dropped(
    pipeline: Pipeline, items: list[int], migrated: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    pipeline.register(CHAIN)
    gone = items[0]

    def delete(job: Job) -> None:
        with Session(migrated) as session, session.begin():
            session.execute(sa.delete(Item).where(Item.id == gone))

    pipeline.runner.during = delete
    _queue_scans(pipeline, migrated, [gone])
    caplog.set_level(logging.INFO, logger="photo_triage.worker.loop")

    assert pipeline.run() == [(Stage.SCAN, gone)]
    assert "but its item is gone" in caplog.text
    with Session(migrated) as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Job)) == 0


def test_chain_queues_only_the_stages_straight_after(
    pipeline: Pipeline, items: list[int], migrated: Engine
) -> None:
    pipeline.register({Stage.SCAN: SCAN})
    _queue_scans(pipeline, migrated, items[:1])
    pipeline.run()
    with Session(migrated) as session, session.begin():
        assert chain(session, CHAIN, Stage.SCAN, items[0], START) == [Stage.THUMBNAIL]
        assert chain(session, CHAIN, Stage.SCAN, items[0], START) == []  # already open
        assert chain(session, CHAIN, Stage.CLIP, items[0], START) == []  # nothing after


def test_chaining_reads_the_items_jobs_through_the_item_stage_index(migrated: Engine) -> None:
    query = due(Stage.CLIP, CHAIN).where(Item.id == 1)
    sql = query.compile(migrated, compile_kwargs={"literal_binds": True})
    with migrated.connect() as conn:
        details = [row[3] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}")]
    assert [d for d in details if d.startswith("SCAN")] == [], details
    assert any(d.startswith("SEARCH items USING INTEGER PRIMARY KEY") for d in details), details
    index = "USING INDEX ix_jobs_item_stage_status (item_id=? AND stage=?"
    assert all(index in d for d in details if d.startswith("SEARCH") and "jobs" in d), details
