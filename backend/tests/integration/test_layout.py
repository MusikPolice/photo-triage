"""The `layout` batch stage (plan §6.4): when a fit is due, what it writes, and the
worker queueing it."""

import datetime as dt
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from photo_triage.db.models import Embedding, Item, ItemStatus, Job, JobStatus, Layout, MediaType
from photo_triage.ml import FakeClip
from photo_triage.pipeline import layout
from photo_triage.pipeline.embed import to_blob
from photo_triage.settings import Settings
from photo_triage.worker import layout_schedule
from photo_triage.worker.__main__ import main
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import PRIORITY, JobQueue, RetryPolicy, Stage
from photo_triage.worker.stages import Runner

START = dt.datetime(2026, 11, 18, 2, tzinfo=dt.UTC)
MODEL = "fake"


class Library:
    """Items with embeddings, layouts and jobs, written straight to the database."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.sessions = sessionmaker(engine)
        self.queue = JobQueue(lambda: START)
        self._rng = np.random.default_rng(0)
        self._next = 0

    def add(
        self,
        count: int,
        *,
        model: str = MODEL,
        embedded: bool = True,
        vectors: np.ndarray | None = None,
    ) -> list[int]:
        """`count` active items, embedded with `model` unless not `embedded`, with
        `vectors` (one row each) or random unit vectors."""
        ids: list[int] = []
        with self.sessions.begin() as session:
            for index in range(count):
                self._next += 1
                item = Item(
                    content_hash=f"{self._next:064x}",
                    path=f"photos/{self._next}.jpg",
                    media_type=MediaType.PHOTO,
                    file_size_bytes=1,
                    file_mtime_ns=0,
                    first_seen_at=START,
                    last_seen_at=START,
                )
                session.add(item)
                session.flush()
                if embedded:
                    vector = (
                        self._rng.normal(size=512) if vectors is None else vectors[index]
                    ).astype(np.float32)
                    vector /= np.linalg.norm(vector)
                    session.add(Embedding(item_id=item.id, model=model, vector=to_blob(vector)))
                ids.append(item.id)
        return ids

    def fit(self) -> layout.Fit:
        return layout.fit(self.sessions, MODEL)

    def job(self, stage: Stage, status: JobStatus = JobStatus.PENDING) -> int:
        with self.sessions.begin() as session:
            job = Job(
                stage=stage,
                priority=PRIORITY[stage],
                status=status,
                attempts=0,
                enqueued_at=START,
            )
            session.add(job)
            session.flush()
            return job.id

    def set_status(self, item_id: int, status: ItemStatus) -> None:
        with self.sessions.begin() as session:
            session.get_one(Item, item_id).status = status

    def queue_if_due(self, *, check_growth: bool = True) -> bool:
        with self.sessions.begin() as session:
            return layout_schedule.queue_if_due(
                session, self.queue, MODEL, check_growth=check_growth
            )

    def layout(self) -> dict[int, tuple[float, float]]:
        with self.sessions() as session:
            return {row.item_id: (row.x, row.y) for row in session.scalars(sa.select(Layout))}

    def clear_layout(self) -> None:
        with self.sessions.begin() as session:
            session.execute(sa.delete(Layout))

    def restore_layout(self, points: Mapping[int, tuple[float, float]]) -> None:
        with self.sessions.begin() as session:
            session.execute(sa.delete(Layout))
            session.execute(
                sa.insert(Layout),
                [
                    {"layout_version": 1, "item_id": item_id, "x": x, "y": y}
                    for item_id, (x, y) in points.items()
                ],
            )

    def versions(self) -> set[int]:
        with self.sessions() as session:
            return set(session.scalars(sa.select(Layout.layout_version).distinct()))

    def layout_jobs(self, status: JobStatus = JobStatus.PENDING) -> int:
        with self.sessions() as session:
            return len(
                session.scalars(
                    sa.select(Job.id).where(Job.stage == Stage.LAYOUT, Job.status == status)
                ).all()
            )


@pytest.fixture
def lib(migrated: Engine) -> Library:
    return Library(migrated)


@pytest.fixture
def small_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    """A growth minimum of 4 items, so the tests needn't make thousands."""
    monkeypatch.setattr(layout_schedule, "GROWTH_MIN_ITEMS", 4)


# When a fit is due


def test_nothing_embedded_is_not_due(lib: Library) -> None:
    lib.add(3, embedded=False)
    assert not lib.queue_if_due()
    assert lib.layout_jobs() == 0


def test_the_first_embeddings_are_due_once_the_pipeline_drains(lib: Library) -> None:
    lib.add(5)
    assert lib.queue_if_due()
    assert lib.layout_jobs() == 1


def test_nothing_changed_since_the_last_fit_is_not_due(lib: Library) -> None:
    lib.add(5)
    lib.fit()
    assert not lib.queue_if_due()


@pytest.mark.parametrize("stage", layout_schedule.EMBEDDING_PIPELINE)
def test_an_open_job_in_the_pipeline_holds_a_small_change_back(lib: Library, stage: Stage) -> None:
    lib.add(5)
    lib.fit()
    lib.add(1)
    lib.job(stage)
    assert not lib.queue_if_due()


def test_a_job_waiting_to_retry_counts_as_open(lib: Library) -> None:
    lib.add(5)
    lib.fit()
    lib.add(1)
    lib.job(Stage.CLIP, JobStatus.ERROR)
    assert not lib.queue_if_due()


def test_a_parked_pipeline_job_doesnt_hold_it_back(lib: Library) -> None:
    lib.add(5)
    lib.fit()
    lib.add(1)
    lib.job(Stage.CLIP, JobStatus.PARKED)
    assert lib.queue_if_due()


@pytest.mark.usefixtures("small_growth")
def test_growth_below_the_minimum_waits_for_the_pipeline(lib: Library) -> None:
    lib.add(8)
    lib.fit()
    lib.job(Stage.CLIP)
    lib.add(3)
    assert not lib.queue_if_due()
    lib.add(1)  # 4 new: the minimum, and over 25% of 8
    assert lib.queue_if_due()


@pytest.mark.usefixtures("small_growth")
def test_growth_needs_a_quarter_of_the_layout_too(lib: Library) -> None:
    lib.add(40)
    lib.fit()
    lib.job(Stage.CLIP)
    lib.add(9)
    assert not lib.queue_if_due()
    lib.add(1)  # 10 new: 25% of 40
    assert lib.queue_if_due()


def test_the_first_fit_during_a_backfill_waits_for_the_minimum(lib: Library) -> None:
    lib.job(Stage.CLIP)
    lib.add(layout_schedule.GROWTH_MIN_ITEMS - 1)
    assert not lib.queue_if_due()
    lib.add(1)
    assert lib.queue_if_due()


@pytest.mark.usefixtures("small_growth")
def test_growth_is_checked_only_when_asked(lib: Library) -> None:
    lib.job(Stage.CLIP)
    lib.add(10)
    assert not lib.queue_if_due(check_growth=False)
    assert lib.queue_if_due(check_growth=True)


def test_an_item_gone_missing_is_due(lib: Library) -> None:
    ids = lib.add(5)
    lib.fit()
    lib.set_status(ids[0], ItemStatus.MISSING)
    assert lib.queue_if_due()


def test_embeddings_from_another_model_are_ignored(lib: Library) -> None:
    lib.add(5, model="ViT-L-14")
    assert not lib.queue_if_due()
    lib.add(5)
    lib.fit()
    assert set(lib.layout()) == set(range(6, 11))


def test_a_queued_fit_isnt_queued_again(lib: Library) -> None:
    lib.add(5)
    assert lib.queue_if_due()
    assert not lib.queue_if_due()
    assert lib.layout_jobs() == 1


def test_a_parked_fit_isnt_queued_again(lib: Library) -> None:
    lib.add(5)
    lib.job(Stage.LAYOUT, JobStatus.PARKED)
    assert not lib.queue_if_due()
    assert lib.layout_jobs() == 0


# What a fit writes


def test_a_fit_lays_out_every_embedded_active_item_and_nothing_else(lib: Library) -> None:
    embedded = lib.add(20)
    lib.add(2, embedded=False)
    lib.set_status(embedded[0], ItemStatus.MISSING)
    lib.set_status(embedded[1], ItemStatus.TRASHED)

    fit = lib.fit()

    assert fit.version == 1
    assert fit.items == 18
    assert set(lib.layout()) == set(embedded[2:])


def test_coordinates_span_0_to_1_on_the_longer_axis(lib: Library) -> None:
    lib.add(30)
    lib.fit()
    points = np.array(list(lib.layout().values()))
    assert points.min(axis=0) == pytest.approx([0, 0])
    assert points.max() == pytest.approx(1)
    assert points.max(axis=0).min() <= 1


def test_each_fit_replaces_the_last_as_the_next_version(lib: Library) -> None:
    lib.add(10)
    assert lib.fit().version == 1
    lib.add(2)
    assert lib.fit().version == 2
    assert lib.versions() == {2}
    assert len(lib.layout()) == 12


def test_the_same_embeddings_give_the_same_layout(lib: Library) -> None:
    lib.add(30)
    lib.fit()
    first = lib.layout()
    lib.clear_layout()
    lib.fit()
    assert lib.layout() == first


def test_the_same_embeddings_and_starting_layout_give_the_same_refit(lib: Library) -> None:
    lib.add(30)
    lib.fit()
    start = lib.layout()
    lib.add(3)
    lib.fit()
    refit = lib.layout()
    lib.restore_layout(start)
    lib.fit()
    assert lib.layout() == refit


def _surface(rng: np.random.Generator, basis: np.ndarray, count: int) -> np.ndarray:
    """`count` vectors near a curved 2D surface in 512 dimensions, like a library
    whose photos shade into each other rather than forming separate clusters."""
    t = rng.uniform(0, 1, size=(count, 2))
    return (
        basis[0]
        + np.cos(3 * t[:, :1]) * basis[1]
        + np.sin(3 * t[:, 1:]) * basis[2]
        + 0.02 * rng.normal(size=(count, basis.shape[1]))
    )


def test_a_refit_keeps_the_laid_out_items_where_they_were(lib: Library) -> None:
    rng = np.random.default_rng(2)
    basis = rng.normal(size=(3, 512))
    old_vectors, new_vectors = _surface(rng, basis, 1000), _surface(rng, basis, 10)
    first = lib.add(1000, vectors=old_vectors)
    lib.fit()
    before = lib.layout()

    lib.add(10, vectors=new_vectors)
    lib.fit()
    after = lib.layout()

    assert len(after) == 1010
    warm_moved = np.median([np.hypot(*np.subtract(after[i], before[i])) for i in first])
    # A fit from scratch moves the same items much further.
    every = np.concatenate([old_vectors, new_vectors]).astype(np.float32)
    every /= np.linalg.norm(every, axis=1, keepdims=True)
    cold = layout.normalize(layout.project(every))[:1000]
    cold_moved = np.median(np.hypot(*(cold - np.array([before[i] for i in first])).T))
    assert warm_moved < 0.1
    assert warm_moved < cold_moved / 2


def test_a_new_item_starts_among_its_nearest_laid_out_items() -> None:
    laid_out = np.eye(8, 4, dtype=np.float32)[[0, 0, 0, 0, 0, 1, 1, 1, 1, 1]]
    laid_out += np.linspace(0, 0.01, 10, dtype=np.float32)[:, None]
    places = np.array([[0, 0]] * 5 + [[10, 10]] * 5, dtype=np.float64)
    new = np.array([[0, 1, 0, 0], [1, 0, 0, 0]], dtype=np.float32)

    assert layout.neighbour_places(laid_out, places, new) == pytest.approx(
        np.array([[10, 10], [0, 0]])
    )


def test_align_undoes_a_rotation_reflection_scale_and_shift() -> None:
    rng = np.random.default_rng(2)
    fixed = rng.normal(size=(50, 2))
    angle = 0.7
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    moving = (fixed @ rotation * [1, -1]) * 3.5 + [4, -2]

    assert layout.align(moving, fixed)(moving) == pytest.approx(fixed)


def test_a_fit_with_nothing_embedded_removes_the_old_layout(lib: Library) -> None:
    ids = lib.add(5)
    lib.fit()
    for item_id in ids:
        lib.set_status(item_id, ItemStatus.MISSING)

    fit = lib.fit()

    assert (fit.version, fit.items) == (None, 0)
    assert lib.layout() == {}


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_a_handful_of_items_is_laid_out(lib: Library, count: int) -> None:
    lib.add(count)
    lib.fit()
    points = lib.layout()
    assert len(points) == count
    assert all(0 <= v <= 1 for point in points.values() for v in point)


# The worker


class FakeTimer:
    def __init__(self) -> None:
        self.now_s = 1000.0

    def __call__(self) -> float:
        return self.now_s


def _worker(
    lib: Library,
    runners: Mapping[str, Runner],
    max_attempts: int = 5,
    timer: FakeTimer | None = None,
) -> Worker:
    queue = JobQueue(lambda: START, RetryPolicy(max_attempts=max_attempts))
    return Worker(
        lib.engine,
        runners,
        queue=queue,
        clock=lambda: START,
        timer=timer or FakeTimer(),
        layout_model=MODEL,
    )


def test_the_worker_queues_a_fit_after_the_last_clip_job(lib: Library) -> None:
    lib.add(5)
    for _ in range(3):
        lib.job(Stage.CLIP)
    ran: list[str] = []
    worker = _worker(lib, {Stage.CLIP: lambda job: ran.append(job.stage)})

    worker.drain()

    assert ran == [Stage.CLIP] * 3
    assert lib.layout_jobs() == 1


def test_the_worker_queues_a_fit_when_the_last_clip_job_is_parked(lib: Library) -> None:
    lib.add(5)
    lib.job(Stage.CLIP)

    def fail(job: Job) -> None:
        raise OSError("unreadable")

    _worker(lib, {Stage.CLIP: fail}, max_attempts=1).drain()

    assert lib.layout_jobs() == 1


def test_the_worker_runs_a_fit_ahead_of_the_clip_backlog(lib: Library, small_growth: None) -> None:
    lib.add(5)
    for _ in range(3):
        lib.job(Stage.CLIP)
    ran: list[str] = []

    def fit(job: Job) -> None:
        ran.append(job.stage)
        lib.fit()

    worker = _worker(lib, {Stage.CLIP: lambda job: ran.append(job.stage), Stage.LAYOUT: fit})

    worker.drain()

    # The first CLIP job's growth check queues a fit, which runs before the rest.
    # Nothing changes after it, so the pipeline draining queues no other.
    assert ran == [Stage.CLIP, Stage.LAYOUT, Stage.CLIP, Stage.CLIP]


def test_the_worker_checks_growth_at_most_once_a_minute(lib: Library, small_growth: None) -> None:
    for _ in range(4):
        lib.job(Stage.CLIP)
    timer = FakeTimer()

    def embed_two(job: Job) -> None:
        lib.add(2)

    worker = _worker(lib, {Stage.CLIP: embed_two}, timer=timer)

    worker.run_one()  # checked: 2 embedded, under the minimum of 4
    assert lib.layout_jobs() == 0
    timer.now_s += 59
    worker.run_one()  # 4 embedded, but the last check was under a minute ago
    assert lib.layout_jobs() == 0
    timer.now_s += 1
    worker.run_one()  # checked again: 6 embedded
    assert lib.layout_jobs() == 1


def test_a_reconcile_queues_a_fit_when_one_is_due(lib: Library) -> None:
    lib.add(5)
    worker = _worker(lib, {})
    assert worker.reconcile().get(Stage.LAYOUT) == 1
    assert lib.layout_jobs() == 1


def test_a_worker_without_a_layout_model_queues_no_fit(lib: Library) -> None:
    lib.add(5)
    lib.job(Stage.CLIP)
    Worker(lib.engine, {Stage.CLIP: lambda job: None}, queue=lib.queue).drain()
    assert lib.layout_jobs() == 0


def test_worker_once_lays_out_the_tier_1_fixtures(
    migrated: Engine,
    settings: Settings,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "PHOTO_DIR": settings.photo_dir,
        "TRASH_DIR": settings.trash_dir,
        "DATA_DIR": settings.data_dir,
        "MODELS_DIR": settings.models_dir,
        "CLIP_MODEL": MODEL,
    }.items():
        monkeypatch.setenv(name, str(value))
    lib = Library(migrated)
    # Its scan fails and waits to retry, which holds every fit back until it's
    # parked: minutes the --once run doesn't wait for.
    (library / "broken" / "truncated.jpg").unlink()

    assert main(["scan"]) == 0
    assert main(["--once"], clip=FakeClip()) == 0

    with lib.sessions() as session:
        embedded = set(session.scalars(sa.select(Embedding.item_id)))
        items = set(session.scalars(sa.select(Item.id)))
    assert embedded  # the fixtures were embedded
    assert lib.versions() == {1}
    assert set(lib.layout()) == embedded == items

    # Nothing new: a second run fits nothing.
    assert main(["--once"], clip=FakeClip()) == 0
    assert lib.versions() == {1}
    assert lib.layout_jobs(JobStatus.DONE) == 1

    # A file gone missing is left out of the next fit.
    with lib.sessions() as session:
        missing, path = session.execute(
            sa.select(Item.id, Item.path).where(Item.id.in_(embedded)).order_by(Item.id).limit(1)
        ).one()
    (library / path).unlink()
    assert main(["scan"]) == 0
    assert main(["--once"], clip=FakeClip()) == 0
    with lib.sessions() as session:
        assert session.get_one(Item, missing).status == ItemStatus.MISSING
    assert lib.versions() == {2}
    assert set(lib.layout()) == embedded - {missing}


def test_reading_a_layout_version_uses_its_primary_key(lib: Library) -> None:
    query = sa.select(Layout.item_id, Layout.x, Layout.y).where(Layout.layout_version == 1)
    with lib.sessions() as session:
        sql = str(query.compile(lib.engine, compile_kwargs={"literal_binds": True}))
        plan = [row[-1] for row in session.execute(sa.text(f"EXPLAIN QUERY PLAN {sql}"))]
    # The primary key's index, searched by version; never a scan of the table.
    assert plan == ["SEARCH layout USING INDEX sqlite_autoindex_layout_1 (layout_version=?)"]
