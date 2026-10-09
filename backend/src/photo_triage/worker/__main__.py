"""Run the worker: `python -m photo_triage.worker [--once]` (`just worker`). Only
one runs per `DATA_DIR`: a second exits 1 at startup.

`python -m photo_triage.worker noop N` queues N jobs for the `noop` stage, to
exercise the worker by hand (needs `WORKER_NOOP_STAGE=true`).

`python -m photo_triage.worker pause [STAGE]` and `resume [STAGE]` pause and resume
the worker, or one stage, the same way the Activity page does.
"""

import argparse
import getpass
import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping, Sequence
from types import FrameType

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage import logs
from photo_triage.db.engine import open_database
from photo_triage.db.migrate import SchemaError, check_schema
from photo_triage.files import LockHeldError, exclusive_lock
from photo_triage.settings import (
    Settings,
    SettingsError,
    load_settings,
    trash_filesystem_warning,
)
from photo_triage.worker import controls
from photo_triage.worker.clock import Clock, running_from, utc_now
from photo_triage.worker.heartbeat import Heartbeat
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import JobQueue, Stage
from photo_triage.worker.stages import Runner, build_runners
from photo_triage.worker.state import observed_state

# Named, since `__name__` is "__main__" when run with `python -m`.
logger = logging.getLogger("photo_triage.worker")

LOCK_FILENAME = "worker.lock"
"""In `DATA_DIR`. A running worker holds it, so that only one runs (plan §6.11)."""

THREAD_ENV_VARS = [
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMBA_NUM_THREADS",
]
"""Read by OpenMP (which ONNX Runtime and PyTorch use), the BLAS libraries behind
NumPy, and numba (UMAP) when they load. ONNX Runtime sessions also take the count
directly."""


def main(argv: Sequence[str] | None = None, *, runners: Mapping[str, Runner] | None = None) -> int:
    """Runs the worker. `runners` replaces the stages from `build_runners` (tests)."""
    parser = argparse.ArgumentParser(prog="python -m photo_triage.worker")
    parser.add_argument("--once", action="store_true", help="run every ready job, then exit")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    noop = commands.add_parser(
        "noop", help="queue N noop jobs and exit (needs WORKER_NOOP_STAGE=true)"
    )
    noop.add_argument("count", type=int, metavar="N")
    for name, verb in [("pause", "pause"), ("resume", "resume")]:
        control = commands.add_parser(
            name, help=f"{verb} the worker, or one stage, and exit (a running job finishes)"
        )
        control.add_argument(
            "stage", nargs="?", choices=list(Stage), metavar="STAGE", help="default: every stage"
        )
        control.add_argument(
            "--actor", default=getpass.getuser(), help="who to record (default: your user name)"
        )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except SettingsError as e:
        print(e, file=sys.stderr)
        return 2
    # The migrate step runs before the worker starts; the worker never migrates.
    try:
        check_schema(settings.data_dir)
    except SchemaError as e:
        print(e, file=sys.stderr)
        return 1

    limit_threads(settings.worker_threads)  # before any ML library loads
    clock = utc_now if settings.fake_now is None else running_from(settings.fake_now)
    queue = JobQueue(clock)
    engine = open_database(settings.data_dir)
    try:
        if args.command == "noop":
            # The worker may be running and writing worker.log, and two processes
            # can't share a rotating file.
            logs.configure(settings, "worker", to_file=False)
            return enqueue_noop(settings, engine, queue, args.count)
        if args.command in ("pause", "resume"):
            logs.configure(settings, "worker", to_file=False)  # as for noop
            return set_paused(
                settings, engine, clock, args.command == "pause", args.stage, args.actor
            )

        # Before anything else, since a second worker would requeue the first one's
        # running job in `recover`, and write to its worker.log.
        lock_path = settings.data_dir / LOCK_FILENAME
        try:
            with exclusive_lock(lock_path):
                return run(settings, engine, queue, clock, runners, once=args.once)
        except LockHeldError:
            print(
                f"A worker is already running for {settings.data_dir} (it holds "
                f"{lock_path}). Only one worker may run at a time; stop that one first.",
                file=sys.stderr,
            )
            return 1
    finally:
        engine.dispose()


def run(
    settings: Settings,
    engine: Engine,
    queue: JobQueue,
    clock: Clock,
    runners: Mapping[str, Runner] | None,
    *,
    once: bool,
) -> int:
    logs.configure(settings, "worker")
    logger.info("Worker starting: %s", settings.summary())
    if (warning := trash_filesystem_warning(settings)) is not None:
        logger.warning(warning)
    worker = Worker(
        engine,
        build_runners(settings) if runners is None else runners,
        queue=queue,
        clock=clock,
        quiet_hours=settings.worker_quiet_hours,
        zone=settings.tz,
    )
    # Real time, not `clock`: see `photo_triage.worker.heartbeat`.
    with Heartbeat(engine, utc_now):
        worker.recover()
        if once:
            worker.drain()
        else:
            run_until_signalled(worker)
    logger.info("Worker stopped")
    return 0


def limit_threads(count: int) -> None:
    """Apply `WORKER_THREADS`. Must run before NumPy or ONNX Runtime is imported."""
    for name in THREAD_ENV_VARS:
        os.environ[name] = str(count)


def enqueue_noop(settings: Settings, engine: Engine, queue: JobQueue, count: int) -> int:
    if not settings.worker_noop_stage:
        print("The noop stage is off. Set WORKER_NOOP_STAGE=true to use it.", file=sys.stderr)
        return 2
    with Session(engine) as session, session.begin():
        for _ in range(count):
            queue.enqueue(session, Stage.NOOP)
    logger.info("Queued %d noop job(s)", count)
    return 0


def set_paused(
    settings: Settings,
    engine: Engine,
    clock: Clock,
    paused: bool,
    stage: str | None,
    actor: str,
) -> int:
    """Pause or resume `stage`, or every stage when it's None, and log the new state."""
    set_control = controls.pause if paused else controls.resume
    scope = controls.GLOBAL if stage is None else stage
    now = clock()
    with Session(engine) as session, session.begin():
        set_control(session, scope, actor, now)
        state = observed_state(
            session, settings.worker_quiet_hours, settings.tz, now, real_now=utc_now()
        )
    logger.info(
        "%s %s. %s",
        "Paused" if paused else "Resumed",
        "the worker" if stage is None else f"stage {stage}",
        state.describe(settings.tz),
    )
    return 0


def run_until_signalled(worker: Worker) -> None:
    """Run until SIGINT or SIGTERM, finishing the job in progress. A second signal
    stops at once, leaving that job for `recover` at the next start."""
    stop = threading.Event()
    stop_signals = (signal.SIGINT, signal.SIGTERM)

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("Stopping after the current job (%s)", signal.Signals(signum).name)
        stop.set()
        for s, handler in previous.items():
            signal.signal(s, handler)

    previous = {s: signal.signal(s, request_stop) for s in stop_signals}
    try:
        worker.run(stop)
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


if __name__ == "__main__":
    sys.exit(main())
