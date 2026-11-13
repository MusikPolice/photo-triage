"""The worker queues walks on `SCAN_SCHEDULE` (`photo_triage.worker.scan_schedule`)."""

import datetime as dt
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.db.models import Job, JobStatus
from photo_triage.quiet_hours import parse as parse_quiet_hours
from photo_triage.schedule import parse
from photo_triage.worker import controls
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import PRIORITY, JobQueue, Stage
from photo_triage.worker.scan_schedule import queue_walk

TORONTO = ZoneInfo("America/Toronto")
NIGHTLY = parse("0 2 * * *")
# Saturday 2026-10-03, 08:00 EDT. The schedule's 02:00 EDT is 06:00 UTC.
START = dt.datetime(2026, 10, 3, 12, tzinfo=dt.UTC)
SUNDAY_2AM = dt.datetime(2026, 10, 4, 6, tzinfo=dt.UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> dt.datetime:
        return self.now


def _walked(walks: list[int]):
    def run(job: Job) -> None:
        walks.append(job.id)

    return run


def _worker(engine: Engine, clock: FakeClock, walks: list[int], **kwargs: object) -> Worker:
    return Worker(
        engine,
        {Stage.WALK: _walked(walks)},
        queue=JobQueue(clock),
        clock=clock,
        zone=TORONTO,
        scan_schedule=NIGHTLY,
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


def _walk(engine: Engine, status: JobStatus, at: dt.datetime) -> None:
    """A walk queued (and, if done, finished) at `at`."""
    with Session(engine) as session, session.begin():
        session.add(
            Job(
                stage=Stage.WALK,
                priority=PRIORITY[Stage.WALK],
                status=status,
                attempts=0,
                enqueued_at=at,
                finished_at=at if status == JobStatus.DONE else None,
            )
        )


def _walks(engine: Engine) -> list[tuple[JobStatus, str | None]]:
    with Session(engine) as session:
        rows = session.execute(
            sa.select(Job.status, Job.actor).where(Job.stage == Stage.WALK).order_by(Job.id)
        )
        return [(status, actor) for status, actor in rows]


def test_a_new_library_is_walked_when_the_worker_first_starts(migrated: Engine) -> None:
    clock, walks = FakeClock(), []

    _worker(migrated, clock, walks).drain()

    assert len(walks) == 1
    assert _walks(migrated) == [(JobStatus.DONE, None)]


def test_one_walk_is_queued_when_the_scheduled_time_passes(migrated: Engine) -> None:
    _walk(migrated, JobStatus.DONE, START)
    clock, walks = FakeClock(), []
    worker = _worker(migrated, clock, walks)

    worker.drain()
    clock.now = SUNDAY_2AM - dt.timedelta(seconds=1)
    worker.drain()
    assert walks == []

    clock.now = SUNDAY_2AM
    worker.drain()
    clock.now = SUNDAY_2AM + dt.timedelta(hours=1)
    worker.drain()

    assert len(walks) == 1


def test_times_missed_while_the_worker_was_stopped_get_one_walk(migrated: Engine) -> None:
    _walk(migrated, JobStatus.DONE, START)
    clock, walks = FakeClock(), []
    clock.now = START + dt.timedelta(days=5)  # five 02:00s passed while stopped

    _worker(migrated, clock, walks).drain()

    assert len(walks) == 1


def test_no_walk_is_added_while_one_is_waiting(migrated: Engine) -> None:
    # Queued by hand on Saturday, and still waiting (no runner) at Sunday 02:00.
    _walk(migrated, JobStatus.PENDING, START)
    clock = FakeClock()
    worker = Worker(
        migrated, {}, queue=JobQueue(clock), clock=clock, zone=TORONTO, scan_schedule=NIGHTLY
    )
    clock.now = SUNDAY_2AM + dt.timedelta(minutes=5)

    worker.drain()

    assert _walks(migrated) == [(JobStatus.PENDING, None)]


def test_a_walk_running_at_the_scheduled_time_counts_for_it(migrated: Engine) -> None:
    _walk(migrated, JobStatus.RUNNING, SUNDAY_2AM - dt.timedelta(minutes=30))
    clock, walks = FakeClock(), []
    clock.now = SUNDAY_2AM + dt.timedelta(minutes=5)
    worker = _worker(migrated, clock, walks)
    worker.drain()
    assert walks == []

    # It finishes after 02:00, so the 02:00 walk has been done.
    with Session(migrated) as session, session.begin():
        job = session.scalars(sa.select(Job).where(Job.stage == Stage.WALK)).one()
        job.status, job.finished_at = JobStatus.DONE, clock.now
    clock.now += dt.timedelta(hours=1)
    worker = _worker(migrated, clock, walks)
    worker.drain()

    assert walks == []


def test_the_walk_is_queued_during_quiet_hours_and_runs_after(migrated: Engine) -> None:
    _walk(migrated, JobStatus.DONE, START)
    clock, walks = FakeClock(), []
    worker = _worker(migrated, clock, walks, quiet_hours=parse_quiet_hours("Sat-Sun 22:00-07:00"))
    clock.now = SUNDAY_2AM

    worker.drain()
    assert walks == []
    assert _walks(migrated)[-1] == (JobStatus.PENDING, None)

    clock.now = SUNDAY_2AM + dt.timedelta(hours=6)  # 08:00 EDT
    worker.drain()
    assert len(walks) == 1


def test_the_walk_is_queued_while_paused_and_runs_on_resume(migrated: Engine) -> None:
    _walk(migrated, JobStatus.DONE, START)
    with Session(migrated) as session, session.begin():
        controls.pause(session, controls.GLOBAL, "someone", START)
    clock, walks = FakeClock(), []
    worker = _worker(migrated, clock, walks)
    clock.now = SUNDAY_2AM

    worker.drain()
    assert walks == []
    assert _walks(migrated)[-1] == (JobStatus.PENDING, None)

    with Session(migrated) as session, session.begin():
        controls.resume(session, controls.GLOBAL, "someone", clock.now)
    worker.drain()
    assert len(walks) == 1


def test_a_walk_by_hand_records_who_asked_and_is_not_doubled(migrated: Engine) -> None:
    queue = JobQueue(lambda: START)
    with Session(migrated) as session, session.begin():
        first = queue_walk(session, queue, "alice")
        second = queue_walk(session, queue, "bob")

    assert first is not None
    assert second is None
    assert _walks(migrated) == [(JobStatus.PENDING, "alice")]


def test_a_walk_by_hand_is_not_added_while_one_runs(migrated: Engine) -> None:
    _walk(migrated, JobStatus.RUNNING, START)
    with Session(migrated) as session, session.begin():
        assert queue_walk(session, JobQueue(lambda: START), "alice") is None
