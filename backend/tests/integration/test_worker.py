"""The worker loop and `python -m photo_triage.worker` against a migrated database."""

import datetime as dt
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.db.engine import open_database
from photo_triage.db.models import Item, Job, JobStats, JobStatus, MediaType, WorkerControl
from photo_triage.quiet_hours import parse
from photo_triage.settings import Settings
from photo_triage.worker import controls, stages
from photo_triage.worker.__main__ import THREAD_ENV_VARS, main
from photo_triage.worker.loop import Worker, WorkerState, WorkerStatus
from photo_triage.worker.queue import JobQueue, RetryPolicy, Stage

START = dt.datetime(2026, 10, 3, 12, tzinfo=dt.UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, by: dt.timedelta) -> None:
        self.now += by


class FakeTimer:
    """A monotonic timer that moves only when told to."""

    def __init__(self) -> None:
        self.now_s = 1000.0

    def __call__(self) -> float:
        return self.now_s


class Recorder:
    """A runner that records the jobs it ran, taking `duration_s` of fake time each."""

    def __init__(self, timer: FakeTimer | None = None, duration_s: float = 0.0) -> None:
        self.ran: list[tuple[str, int | None]] = []
        self._timer = timer
        self._duration_s = duration_s

    def __call__(self, job: Job) -> None:
        self.ran.append((job.stage, job.item_id))
        if self._timer is not None:
            self._timer.now_s += self._duration_s


class Failing:
    def __init__(self, timer: FakeTimer | None = None, duration_s: float = 0.0) -> None:
        self._timer = timer
        self._duration_s = duration_s

    def __call__(self, job: Job) -> None:
        if self._timer is not None:
            self._timer.now_s += self._duration_s
        raise OSError(f"cannot read item {job.item_id}\nsecond line")


@pytest.fixture(autouse=True)
def restore_thread_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` sets the thread variables in `os.environ`; undo that after each test."""
    for name in THREAD_ENV_VARS:
        monkeypatch.setenv(name, "unset")


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Settings:
    """The environment `main` reads, matching the `settings` fixture."""
    monkeypatch.setenv("PHOTO_DIR", str(settings.photo_dir))
    monkeypatch.setenv("TRASH_DIR", str(settings.trash_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    return settings


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def timer() -> FakeTimer:
    return FakeTimer()


@pytest.fixture
def queue(clock: FakeClock) -> JobQueue:
    return JobQueue(clock=clock, retry_policy=RetryPolicy(max_attempts=3))


def _items(engine: Engine, count: int) -> list[int]:
    with Session(engine) as session:
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
        session.commit()
        return [item.id for item in items]


def _enqueue(engine: Engine, *jobs: tuple[Stage, int | None]) -> list[int]:
    with Session(engine) as session:
        ids = [JobQueue(clock=lambda: START).enqueue(session, stage, item) for stage, item in jobs]
        session.commit()
        return ids


def _statuses(engine: Engine) -> dict[int, JobStatus]:
    with Session(engine) as session:
        return {job_id: status for job_id, status in session.execute(sa.select(Job.id, Job.status))}


def _stats(engine: Engine) -> list[tuple[dt.datetime, str, int, int, float]]:
    with Session(engine) as session:
        return list(
            session.execute(
                sa.select(
                    JobStats.hour_start_at,
                    JobStats.stage,
                    JobStats.processed,
                    JobStats.errors,
                    JobStats.busy_seconds,
                ).order_by(JobStats.hour_start_at, JobStats.stage)
            ).all()
        )  # pyright: ignore[reportReturnType]


def test_once_runs_every_ready_job_in_priority_order_then_exits_0(
    migrated: Engine, worker_env: Settings
) -> None:
    a, b = _items(migrated, 2)
    _enqueue(
        migrated,
        (Stage.LLM_TAG, a),
        (Stage.CLIP, b),
        (Stage.LAYOUT, None),
        (Stage.SCAN, b),
        (Stage.CLIP, a),
        (Stage.METADATA_WRITE, a),
    )
    recorder = Recorder()

    assert main(["--once"], runners=dict.fromkeys(Stage, recorder)) == 0

    assert recorder.ran == [
        (Stage.METADATA_WRITE, a),
        (Stage.SCAN, b),
        (Stage.CLIP, b),  # same priority: oldest first
        (Stage.CLIP, a),
        (Stage.LAYOUT, None),
        (Stage.LLM_TAG, a),
    ]
    assert set(_statuses(migrated).values()) == {JobStatus.DONE}


def test_jobs_for_stages_without_a_runner_are_left_in_the_queue(
    migrated: Engine, queue: JobQueue
) -> None:
    clip, layout = _enqueue(migrated, (Stage.CLIP, None), (Stage.LAYOUT, None))
    recorder = Recorder()

    assert Worker(migrated, {Stage.LAYOUT: recorder}, queue=queue).drain() == 1

    assert recorder.ran == [(Stage.LAYOUT, None)]
    assert _statuses(migrated) == {clip: JobStatus.PENDING, layout: JobStatus.DONE}


KILLED_WORKER = """
import sys, time
from pathlib import Path
from photo_triage.db.engine import open_database
from photo_triage.worker.loop import Worker, WorkerState, WorkerStatus

def hang(job):
    Path(sys.argv[2]).touch()
    time.sleep(60)

Worker(open_database(Path(sys.argv[1])), {"layout": hang}).drain()
"""


def test_a_job_running_when_the_worker_is_killed_is_completed_after_restart(
    migrated: Engine, tmp_path: Path, queue: JobQueue
) -> None:
    (job_id,) = _enqueue(migrated, (Stage.LAYOUT, None))
    started = tmp_path / "started"
    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(KILLED_WORKER), str(tmp_path / "data"), str(started)]
    )
    try:
        deadline_s = time.monotonic() + 30
        while not started.exists():
            assert process.poll() is None, "the worker exited before running the job"
            assert time.monotonic() < deadline_s, "the worker never started the job"
            time.sleep(0.05)
    finally:
        process.kill()
        process.wait()
    assert _statuses(migrated) == {job_id: JobStatus.RUNNING}

    recorder = Recorder()
    worker = Worker(migrated, {Stage.LAYOUT: recorder}, queue=queue)
    assert worker.recover() == 1
    assert worker.drain() == 1

    assert recorder.ran == [(Stage.LAYOUT, None)]
    assert _statuses(migrated) == {job_id: JobStatus.DONE}


def test_a_failing_runner_is_retried_then_parked_and_the_worker_carries_on(
    migrated: Engine, queue: JobQueue, clock: FakeClock
) -> None:
    (item,) = _items(migrated, 1)
    failing, other = _enqueue(migrated, (Stage.SCAN, item), (Stage.LAYOUT, None))
    recorder = Recorder()
    worker = Worker(migrated, {Stage.SCAN: Failing(), Stage.LAYOUT: recorder}, queue=queue)

    assert worker.drain() == 2  # the failure doesn't stop the other job
    assert _statuses(migrated) == {failing: JobStatus.ERROR, other: JobStatus.DONE}
    for _ in range(2):
        clock.advance(dt.timedelta(hours=1))
        assert worker.drain() == 1

    with Session(migrated) as session:
        job = session.get_one(Job, failing)
        assert (job.status, job.attempts) == (JobStatus.PARKED, 3)
        assert job.last_error is not None
        assert "OSError: cannot read item" in job.last_error
    clock.advance(dt.timedelta(days=1))
    assert worker.drain() == 0


def test_another_connection_can_write_while_a_job_runs(
    migrated: Engine, queue: JobQueue, tmp_path: Path
) -> None:
    """The claim is committed before the job runs, and the result straight after."""
    (job_id,) = _enqueue(migrated, (Stage.LAYOUT, None))
    other = open_database(tmp_path / "data")
    # Fail at once, rather than after the busy timeout, if the worker holds the lock.
    event.listen(other, "connect", lambda conn, _: conn.execute("PRAGMA busy_timeout = 0"))
    seen: list[str] = []

    def enqueue_elsewhere(job: Job) -> None:
        with Session(other) as session:
            seen.append(session.get_one(Job, job.id).status)
            JobQueue(clock=lambda: START).enqueue(session, Stage.ATLASES)
            session.commit()

    try:
        assert Worker(migrated, {Stage.LAYOUT: enqueue_elsewhere}, queue=queue).run_one()
        with Session(other) as session:
            assert session.get_one(Job, job_id).status == JobStatus.DONE
            JobQueue(clock=lambda: START).enqueue(session, Stage.DUPLICATES)
            session.commit()
    finally:
        other.dispose()
    assert seen == [JobStatus.RUNNING]
    assert sorted(_statuses(migrated).values()) == [
        JobStatus.DONE,
        JobStatus.PENDING,
        JobStatus.PENDING,
    ]


def test_job_stats_count_runs_errors_and_busy_time_per_stage_and_hour(
    migrated: Engine, queue: JobQueue, clock: FakeClock, timer: FakeTimer
) -> None:
    a, b, c = _items(migrated, 3)
    _enqueue(migrated, (Stage.CLIP, a), (Stage.CLIP, b), (Stage.SCAN, c))
    worker = Worker(
        migrated,
        {Stage.CLIP: Recorder(timer, 2.5), Stage.SCAN: Failing(timer, 0.5)},
        queue=queue,
        timer=timer,
    )

    assert worker.drain() == 3
    clock.advance(dt.timedelta(hours=1))  # the scan job's backoff passes, in the next hour
    assert worker.drain() == 1

    assert _stats(migrated) == [
        (START, "clip", 2, 0, 5.0),
        (START, "scan", 0, 1, 0.5),
        (START + dt.timedelta(hours=1), "scan", 0, 1, 0.5),
    ]


def test_each_job_is_logged_at_debug(
    migrated: Engine, queue: JobQueue, timer: FakeTimer, caplog: pytest.LogCaptureFixture
) -> None:
    (item,) = _items(migrated, 1)
    clip, layout = _enqueue(migrated, (Stage.CLIP, item), (Stage.LAYOUT, None))
    caplog.set_level(logging.DEBUG, logger="photo_triage.worker")

    Worker(migrated, dict.fromkeys(Stage, Recorder(timer, 1.25)), queue=queue, timer=timer).drain()

    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug == [
        f"Running clip job {clip} (item {item})",
        f"Finished clip job {clip} (item {item}) in 1.250 s",
        f"Running layout job {layout} (batch)",
        f"Finished layout job {layout} (batch) in 1.250 s",
    ]


def test_a_failure_that_will_retry_is_one_warning_line(
    migrated: Engine, queue: JobQueue, caplog: pytest.LogCaptureFixture
) -> None:
    (item,) = _items(migrated, 1)
    (job_id,) = _enqueue(migrated, (Stage.SCAN, item))

    Worker(migrated, {Stage.SCAN: Failing()}, queue=queue).drain()

    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == (
        f"Scan job {job_id} (item {item}) failed (attempt 1 of 3), retrying after "
        f"2026-10-03T12:01:00Z: OSError: cannot read item {item} second line"
    )
    assert warning.exc_info is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_parked_job_is_an_error_with_its_traceback(
    migrated: Engine, queue: JobQueue, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    (job_id,) = _enqueue(migrated, (Stage.LAYOUT, None))
    worker = Worker(migrated, {Stage.LAYOUT: Failing()}, queue=queue)
    for _ in range(3):
        worker.drain()
        clock.advance(dt.timedelta(hours=1))

    (error,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error.getMessage() == f"Parked layout job {job_id} (batch) after 3 failed attempts"
    assert error.exc_info is not None
    assert isinstance(error.exc_info[1], OSError)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_progress_is_summarized_at_info_every_few_minutes(
    migrated: Engine, queue: JobQueue, timer: FakeTimer, caplog: pytest.LogCaptureFixture
) -> None:
    items = _items(migrated, 8)
    _enqueue(migrated, *[(Stage.CLIP, item) for item in items[:6]])
    _enqueue(migrated, (Stage.SCAN, items[6]), (Stage.THUMBNAIL, items[7]))
    worker = Worker(
        migrated,
        {Stage.CLIP: Recorder(timer, 60.0), Stage.SCAN: Failing(timer, 1.0)},
        queue=queue,
        timer=timer,
        summary_interval_s=300.0,
    )

    caplog.set_level(logging.INFO)
    worker.drain()

    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert info == [
        # Scan runs first (1 s), then clip jobs of 60 s: due after the 5th.
        "Progress in the last 301 s: clip 5 done, 0 failed; scan 0 done, 1 failed. "
        "3 job(s) waiting.",
        # The end of the drain summarizes the rest. The thumbnail job has no
        # runner, and the scan job waits out its backoff.
        "Progress in the last 60 s: clip 1 done, 0 failed. 2 job(s) waiting.",
    ]


def test_nothing_is_summarized_when_nothing_ran(
    migrated: Engine, queue: JobQueue, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    Worker(migrated, {}, queue=queue).drain()
    assert not caplog.records


def test_run_keeps_going_until_stopped(migrated: Engine, queue: JobQueue) -> None:
    recorder = Recorder()
    worker = Worker(migrated, {Stage.LAYOUT: recorder}, queue=queue)
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop, 0.01))
    thread.start()
    try:
        for _ in range(2):  # each one arrives after the worker found the queue empty
            count = len(recorder.ran)
            time.sleep(0.05)
            _enqueue(migrated, (Stage.LAYOUT, None))
            deadline_s = time.monotonic() + 10
            while len(recorder.ran) == count:
                assert time.monotonic() < deadline_s
                time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(recorder.ran) == 2


def test_sigterm_stops_the_worker_after_the_current_job(
    migrated: Engine, worker_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    (job_id,) = _enqueue(migrated, (Stage.LAYOUT, None))

    def signal_mid_job(job: Job) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.05)  # the handler runs here; the job still finishes

    handler = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr("photo_triage.worker.loop.IDLE_POLL_S", 60.0)

    assert main([], runners={Stage.LAYOUT: signal_mid_job}) == 0

    assert _statuses(migrated) == {job_id: JobStatus.DONE}
    assert signal.getsignal(signal.SIGTERM) == handler
    log = (worker_env.data_dir / "logs/worker.log").read_text()
    assert "Stopping after the current job (SIGTERM)" in log
    assert "Worker stopped" in log


def test_main_recovers_interrupted_jobs_and_logs_to_worker_log(
    migrated: Engine, worker_env: Settings
) -> None:
    (job_id,) = _enqueue(migrated, (Stage.LAYOUT, None))
    with Session(migrated) as session:
        JobQueue(clock=lambda: START).claim(session)
        session.commit()

    assert main(["--once"], runners={Stage.LAYOUT: Recorder()}) == 0

    assert _statuses(migrated) == {job_id: JobStatus.DONE}
    log = (worker_env.data_dir / "logs/worker.log").read_text()
    assert "INFO photo_triage.worker Worker starting: PHOTO_DIR=" in log
    assert (
        f"WARNING photo_triage.worker.loop Layout job {job_id} (batch) was interrupted when "
        "the worker stopped (attempt 1 of 5), queued again"
    ) in log
    assert "Progress in the last" in log


def test_main_applies_worker_threads(
    migrated: Engine, worker_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKER_THREADS", "3")

    assert main(["--once"], runners={}) == 0

    assert {os.environ[name] for name in THREAD_ENV_VARS} == {"3"}


def test_bad_settings_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--once"]) == 2
    assert "PHOTO_DIR" in capsys.readouterr().err


@pytest.fixture
def fast_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stages, "NOOP_RUN_S", 0.0)


def test_noop_jobs_can_be_queued_and_run_when_enabled(
    migrated: Engine, worker_env: Settings, monkeypatch: pytest.MonkeyPatch, fast_noop: None
) -> None:
    monkeypatch.setenv("WORKER_NOOP_STAGE", "true")

    assert main(["noop", "3"]) == 0
    assert list(_statuses(migrated).values()) == [JobStatus.PENDING] * 3
    assert not (worker_env.data_dir / "logs/worker.log").exists()  # stderr only

    assert main(["--once"]) == 0
    # Only the latest done job per stage and item is kept; job_stats has the count.
    assert list(_statuses(migrated).values()) == [JobStatus.DONE]
    assert [row[2] for row in _stats(migrated)] == [3]


def test_the_noop_stage_is_off_unless_enabled(
    migrated: Engine,
    worker_env: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fast_noop: None,
) -> None:
    assert main(["noop", "3"]) == 2
    assert "WORKER_NOOP_STAGE=true" in capsys.readouterr().err
    assert _statuses(migrated) == {}

    # Queued while it was on, the jobs wait while it's off.
    monkeypatch.setenv("WORKER_NOOP_STAGE", "true")
    assert main(["noop", "2"]) == 0
    monkeypatch.delenv("WORKER_NOOP_STAGE")
    assert main(["--once"]) == 0
    assert list(_statuses(migrated).values()) == [JobStatus.PENDING] * 2


def test_fake_now_sets_the_clock(
    migrated: Engine, worker_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKER_NOOP_STAGE", "true")
    monkeypatch.setenv("FAKE_NOW", "2030-01-01T23:30:00-05:00")

    assert main(["noop", "1"]) == 0

    with Session(migrated) as session:
        enqueued_at = session.scalars(sa.select(Job.enqueued_at)).one()
    expected = dt.datetime(2030, 1, 2, 4, 30, tzinfo=dt.UTC)
    assert expected <= enqueued_at < expected + dt.timedelta(seconds=5)


def test_a_job_that_keeps_killing_the_worker_is_parked_with_an_error(
    migrated: Engine, queue: JobQueue, caplog: pytest.LogCaptureFixture
) -> None:
    (job_id,) = _enqueue(migrated, (Stage.LAYOUT, None))
    worker = Worker(migrated, {Stage.LAYOUT: Recorder()}, queue=queue)
    for _ in range(3):  # each time, the worker dies mid-job and restarts
        with Session(migrated) as session:
            assert queue.claim(session) is not None
            session.commit()
        assert worker.recover() == 1

    assert _statuses(migrated) == {job_id: JobStatus.PARKED}
    assert worker.drain() == 0
    levels = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert levels == [
        (
            logging.WARNING,
            f"Layout job {job_id} (batch) was interrupted when the worker stopped "
            f"(attempt {n} of 3), queued again",
        )
        for n in (1, 2)
    ] + [
        (
            logging.ERROR,
            f"Parked layout job {job_id} (batch) after 3 failed attempts, the last "
            "interrupted when the worker stopped",
        )
    ]


def _pause(engine: Engine, scope: str) -> None:
    with Session(engine) as session, session.begin():
        controls.pause(session, scope, "anonymous", START)


def _resume(engine: Engine, scope: str) -> None:
    with Session(engine) as session, session.begin():
        controls.resume(session, scope, "anonymous", START)


def test_pausing_a_stage_stops_its_jobs_being_claimed_and_the_running_one_finishes(
    migrated: Engine, queue: JobQueue, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    a, b = _items(migrated, 2)
    clip_a, clip_b, layout = _enqueue(
        migrated, (Stage.CLIP, a), (Stage.CLIP, b), (Stage.LAYOUT, None)
    )
    ran: list[int] = []

    def pause_clip_mid_job(job: Job) -> None:
        ran.append(job.id)
        if job.id == clip_a:
            _pause(migrated, Stage.CLIP)

    worker = Worker(migrated, dict.fromkeys(Stage, pause_clip_mid_job), queue=queue)

    assert worker.drain() == 2
    assert ran == [clip_a, layout]
    assert _statuses(migrated) == {
        clip_a: JobStatus.DONE,
        clip_b: JobStatus.PENDING,
        layout: JobStatus.DONE,
    }
    assert worker.state == WorkerState(paused_stages=frozenset({"clip"}))
    assert "Worker running; paused stages: clip" in caplog.messages

    _resume(migrated, Stage.CLIP)
    assert worker.drain() == 1
    assert _statuses(migrated)[clip_b] == JobStatus.DONE
    assert worker.state == WorkerState()


def test_a_global_pause_stops_all_claiming_and_the_running_job_finishes(
    migrated: Engine, queue: JobQueue, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    first, second = _enqueue(migrated, (Stage.SCAN, None), (Stage.LAYOUT, None))

    def pause_mid_job(job: Job) -> None:
        _pause(migrated, controls.GLOBAL)

    worker = Worker(migrated, dict.fromkeys(Stage, pause_mid_job), queue=queue)

    assert worker.drain() == 1
    assert _statuses(migrated) == {first: JobStatus.DONE, second: JobStatus.PENDING}
    assert worker.state.status == WorkerStatus.PAUSED
    assert "Worker paused" in caplog.messages


def test_a_pause_survives_a_worker_restart(migrated: Engine, worker_env: Settings) -> None:
    clip, layout = _enqueue(migrated, (Stage.CLIP, None), (Stage.LAYOUT, None))
    _pause(migrated, controls.GLOBAL)
    _pause(migrated, Stage.CLIP)
    recorder = Recorder()

    assert main(["--once"], runners=dict.fromkeys(Stage, recorder)) == 0
    assert recorder.ran == []

    _resume(migrated, controls.GLOBAL)
    assert main(["--once"], runners=dict.fromkeys(Stage, recorder)) == 0
    assert recorder.ran == [(Stage.LAYOUT, None)]
    assert _statuses(migrated) == {clip: JobStatus.PENDING, layout: JobStatus.DONE}
    log = (worker_env.data_dir / "logs/worker.log").read_text()
    assert "INFO photo_triage.worker.loop Worker paused; paused stages: clip" in log


def test_controls_keep_who_changed_them_and_when(migrated: Engine) -> None:
    later = START + dt.timedelta(hours=1)
    with Session(migrated) as session, session.begin():
        controls.pause(session, Stage.CLIP, "alice", START)
        controls.resume(session, Stage.CLIP, "bob", later)
        controls.pause(session, controls.GLOBAL, "alice", later)
        assert controls.paused_scopes(session) == {controls.GLOBAL}

    with Session(migrated) as session:
        clip = session.get_one(WorkerControl, "clip")
        assert (clip.paused, clip.actor, clip.changed_at) == (False, "bob", later)


def test_only_global_or_a_stage_can_be_paused(migrated: Engine) -> None:
    with Session(migrated) as session, pytest.raises(ValueError, match="neither"):
        controls.pause(session, "everything", "alice", START)


def test_no_job_is_claimed_during_quiet_hours_and_a_running_job_finishes(
    migrated: Engine, queue: JobQueue, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    first, second = _enqueue(migrated, (Stage.LAYOUT, None), (Stage.SCAN, None))

    def run_into_quiet_hours(job: Job) -> None:
        clock.advance(dt.timedelta(minutes=40))  # START is Saturday 12:00 UTC

    worker = Worker(
        migrated,
        dict.fromkeys(Stage, run_into_quiet_hours),
        queue=queue,
        clock=clock,
        quiet_hours=parse("Sat 12:30-14:00"),
    )

    assert worker.drain() == 1
    assert _statuses(migrated) == {first: JobStatus.PENDING, second: JobStatus.DONE}
    until = dt.datetime(2026, 10, 3, 14, tzinfo=dt.UTC)
    assert worker.state == WorkerState(WorkerStatus.QUIET, until)
    assert "Quiet hours until Sat 14:00 UTC (2026-10-03T14:00:00Z)" in caplog.messages

    clock.now = until
    assert worker.drain() == 1
    assert _statuses(migrated)[first] == JobStatus.DONE
    assert worker.state == WorkerState()
    assert "Worker running" in caplog.messages


def test_quiet_hours_are_read_in_tz(
    migrated: Engine, worker_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enqueue(migrated, (Stage.LAYOUT, None))
    monkeypatch.setenv("FAKE_NOW", "2026-10-05T18:00:00-04:00")  # a Monday
    monkeypatch.setenv("TZ", "America/Toronto")
    monkeypatch.setenv("WORKER_QUIET_HOURS", "Mon-Fri 17:00-23:00")
    recorder = Recorder()

    assert main(["--once"], runners={Stage.LAYOUT: recorder}) == 0

    assert recorder.ran == []
    log = (worker_env.data_dir / "logs/worker.log").read_text()
    assert "Quiet hours until Mon 23:00 EDT (2026-10-06T03:00:00Z)" in log
