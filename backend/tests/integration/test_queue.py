"""The job queue against a migrated SQLite database, with an injected clock."""

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from photo_triage.db.models import Item, Job, JobStatus, MediaType
from photo_triage.worker.queue import JobQueue, JobStateError, RetryPolicy, Stage

START = dt.datetime(2026, 10, 3, 12, tzinfo=dt.UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, by: dt.timedelta) -> None:
        self.now += by


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def queue(clock: FakeClock) -> JobQueue:
    return JobQueue(clock=clock, retry_policy=RetryPolicy(max_attempts=3))


@pytest.fixture
def session(migrated: Engine) -> Iterator[Session]:
    with Session(migrated) as session:
        yield session


def _items(session: Session, count: int) -> list[int]:
    items = [
        Item(
            content_hash=f"{n:064x}",
            path=f"{n}.jpg",
            media_type=MediaType.PHOTO,
            file_size_bytes=1,
            file_mtime_ns=0,
            first_seen_at=START,
            last_seen_at=START,
        )
        for n in range(count)
    ]
    session.add_all(items)
    session.flush()
    return [item.id for item in items]


def _boom() -> BaseException:
    try:
        raise OSError("file is locked")
    except OSError as exc:
        return exc


def _claim_id(queue: JobQueue, session: Session) -> int | None:
    job = queue.claim(session)
    return None if job is None else job.id


def test_claim_takes_highest_priority_then_oldest(
    queue: JobQueue, session: Session, clock: FakeClock
) -> None:
    a, b = _items(session, 2)
    llm = queue.enqueue(session, Stage.LLM_TAG, a)
    clock.advance(dt.timedelta(seconds=1))
    clip_old = queue.enqueue(session, Stage.CLIP, a)
    clock.advance(dt.timedelta(seconds=1))
    clip_new = queue.enqueue(session, Stage.CLIP, b)
    write = queue.enqueue(session, Stage.METADATA_WRITE, b)
    layout = queue.enqueue(session, Stage.LAYOUT)

    claimed = [_claim_id(queue, session) for _ in range(6)]
    assert claimed == [write, clip_old, clip_new, layout, llm, None]


def test_claim_marks_the_job_running(queue: JobQueue, session: Session, clock: FakeClock) -> None:
    (item,) = _items(session, 1)
    job_id = queue.enqueue(session, Stage.SCAN, item)
    clock.advance(dt.timedelta(minutes=5))
    session.commit()

    job = queue.claim(session)
    session.commit()

    assert job is not None
    assert job.id == job_id
    stored = session.get_one(Job, job_id, populate_existing=True)
    assert stored.status == JobStatus.RUNNING
    assert stored.started_at == START + dt.timedelta(minutes=5)


def test_claiming_uses_the_open_jobs_index(migrated: Engine) -> None:
    # Run the real statement, with its bound parameters, as EXPLAIN QUERY PLAN.
    # SQLite only uses a partial index when the query repeats its condition
    # literally, so this would catch the status filter becoming a parameter.
    def explain(*args: Any) -> tuple[str, Any]:
        statement, parameters = args[2], args[3]
        return f"EXPLAIN QUERY PLAN {statement}", parameters

    with migrated.connect() as conn:
        event.listen(conn, "before_cursor_execute", explain, retval=True)
        rows = conn.execute(JobQueue.claimable(START).limit(1)).all()
    plan = " ".join(str(row[-1]) for row in rows)  # EXPLAIN rows, despite the select's type
    assert "ix_jobs_open_by_priority" in plan
    assert "TEMP B-TREE" not in plan  # no sort step: the index supplies the order


def test_enqueueing_an_open_job_again_does_not_duplicate_it(
    queue: JobQueue, session: Session
) -> None:
    a, b = _items(session, 2)
    first = queue.enqueue(session, Stage.THUMBNAIL, a)
    assert queue.enqueue(session, Stage.THUMBNAIL, a) == first
    assert queue.enqueue(session, Stage.THUMBNAIL, b) != first
    assert queue.enqueue(session, Stage.CLIP, a) != first
    batch = queue.enqueue(session, Stage.LAYOUT)
    assert queue.enqueue(session, Stage.LAYOUT) == batch
    assert session.scalar(sa.select(sa.func.count()).select_from(Job)) == 4


def test_enqueue_returns_the_job_another_connection_just_queued(
    queue: JobQueue, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    (item,) = _items(session, 1)
    theirs = queue.enqueue(session, Stage.SCAN, item)
    # Our check runs before their insert, so it finds nothing; the unique index
    # then turns our insert into a no-op.
    real_lookup = JobQueue._open_job_id  # pyright: ignore[reportPrivateUsage]
    stale_results: list[int | None] = [None]

    def lookup(*args: Any) -> int | None:
        return stale_results.pop() if stale_results else real_lookup(*args)

    monkeypatch.setattr(JobQueue, "_open_job_id", staticmethod(lookup))
    assert queue.enqueue(session, Stage.SCAN, item) == theirs
    assert session.scalar(sa.select(sa.func.count()).select_from(Job)) == 1


def test_a_running_job_can_be_queued_again(queue: JobQueue, session: Session) -> None:
    # The file may have changed after the running job read it.
    (item,) = _items(session, 1)
    first = queue.enqueue(session, Stage.SCAN, item)
    queue.claim(session)
    assert queue.enqueue(session, Stage.SCAN, item) != first


def test_the_database_rejects_a_duplicate_open_job(queue: JobQueue, session: Session) -> None:
    (item,) = _items(session, 1)
    queue.enqueue(session, Stage.SCAN, item)
    session.commit()
    with pytest.raises(IntegrityError):
        session.add(Job(item_id=item, stage=Stage.SCAN, priority=1, enqueued_at=START))
        session.commit()


def test_a_completed_job_is_done(queue: JobQueue, session: Session, clock: FakeClock) -> None:
    job_id = queue.enqueue(session, Stage.LAYOUT)
    queue.claim(session)
    clock.advance(dt.timedelta(seconds=30))
    queue.complete(session, job_id)

    job = session.get_one(Job, job_id)
    assert job.status == JobStatus.DONE
    assert job.finished_at == START + dt.timedelta(seconds=30)
    assert queue.claim(session) is None


def test_a_failed_job_waits_out_its_backoff_then_parks(
    queue: JobQueue, session: Session, clock: FakeClock
) -> None:
    (item,) = _items(session, 1)
    job_id = queue.enqueue(session, Stage.CLIP, item)

    for attempt, backoff_min in [(1, 1), (2, 2)]:
        assert _claim_id(queue, session) == job_id
        assert queue.fail(session, job_id, _boom()) == JobStatus.ERROR
        job = session.get_one(Job, job_id)
        assert job.attempts == attempt
        assert job.last_error is not None
        assert job.last_error.endswith("OSError: file is locked")
        assert job.retry_at == clock.now + dt.timedelta(minutes=backoff_min)

        clock.advance(dt.timedelta(minutes=backoff_min) - dt.timedelta(seconds=1))
        assert queue.claim(session) is None, "claimed before its backoff passed"
        clock.advance(dt.timedelta(seconds=1))

    assert _claim_id(queue, session) == job_id
    assert queue.fail(session, job_id, _boom()) == JobStatus.PARKED
    job = session.get_one(Job, job_id)
    assert job.attempts == 3
    assert job.last_error is not None

    clock.advance(dt.timedelta(days=1))
    assert queue.claim(session) is None


def test_a_waiting_job_does_not_hold_up_others(
    queue: JobQueue, session: Session, clock: FakeClock
) -> None:
    a, b = _items(session, 2)
    failing = queue.enqueue(session, Stage.SCAN, a)
    queue.claim(session)
    queue.fail(session, failing, _boom())
    other = queue.enqueue(session, Stage.LLM_TAG, b)

    assert _claim_id(queue, session) == other


def test_a_parked_job_can_be_retried(queue: JobQueue, session: Session, clock: FakeClock) -> None:
    (item,) = _items(session, 1)
    job_id = queue.enqueue(session, Stage.FACES, item)
    for _ in range(3):
        queue.claim(session)
        queue.fail(session, job_id, _boom())
        clock.advance(dt.timedelta(hours=1))

    assert queue.retry(session, job_id) == job_id
    job = session.get_one(Job, job_id)
    assert job.status == JobStatus.PENDING
    assert job.attempts == 0
    assert job.retry_at is None
    assert job.last_error is not None  # kept until it next fails
    assert _claim_id(queue, session) == job_id


def test_retrying_a_parked_job_that_was_queued_again_keeps_one_job(
    queue: JobQueue, session: Session, clock: FakeClock
) -> None:
    (item,) = _items(session, 1)
    parked = queue.enqueue(session, Stage.FACES, item)
    for _ in range(3):
        queue.claim(session)
        queue.fail(session, parked, _boom())
        clock.advance(dt.timedelta(hours=1))
    requeued = queue.enqueue(session, Stage.FACES, item)

    assert queue.retry(session, parked) == requeued
    assert session.scalars(sa.select(Job.id)).all() == [requeued]


@pytest.mark.parametrize("action", ["complete", "fail", "retry"])
def test_transitions_from_the_wrong_state_are_refused(
    queue: JobQueue, session: Session, action: str
) -> None:
    job_id = queue.enqueue(session, Stage.LAYOUT)  # pending: not running, not parked
    with pytest.raises(JobStateError, match="is pending"):
        if action == "complete":
            queue.complete(session, job_id)
        elif action == "fail":
            queue.fail(session, job_id, _boom())
        else:
            queue.retry(session, job_id)


def test_unknown_jobs_are_refused(queue: JobQueue, session: Session) -> None:
    with pytest.raises(JobStateError, match="doesn't exist"):
        queue.complete(session, 999)


def test_queued_jobs_survive_a_restart(migrated: Engine, clock: FakeClock) -> None:
    with Session(migrated) as session:
        JobQueue(clock=clock).enqueue(session, Stage.LAYOUT)
        session.commit()
    migrated.dispose()  # every connection closed, as when the process stops

    with Session(migrated) as session:
        job = JobQueue(clock=clock).claim(session)
        assert job is not None
        assert job.stage == Stage.LAYOUT
