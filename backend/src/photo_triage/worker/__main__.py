"""Run the worker: `python -m photo_triage.worker [--once]` (`just worker`).

`python -m photo_triage.worker noop N` queues N jobs for the `noop` stage, to
exercise the worker by hand (needs `WORKER_NOOP_STAGE=true`).
"""

import argparse
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
from photo_triage.settings import Settings, SettingsError, load_settings
from photo_triage.worker.clock import running_from, utc_now
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import JobQueue, Stage
from photo_triage.worker.stages import Runner, build_runners

# Named, since `__name__` is "__main__" when run with `python -m`.
logger = logging.getLogger("photo_triage.worker")

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
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except SettingsError as e:
        print(e, file=sys.stderr)
        return 2

    limit_threads(settings.worker_threads)  # before any ML library loads
    queue = JobQueue(
        clock=utc_now if settings.fake_now is None else running_from(settings.fake_now)
    )
    engine = open_database(settings.data_dir)
    try:
        if args.command == "noop":
            # The worker may be running and writing worker.log, and two processes
            # can't share a rotating file.
            logs.configure(settings, "worker", to_file=False)
            return enqueue_noop(settings, engine, queue, args.count)

        logs.configure(settings, "worker")
        logger.info("Worker starting: %s", settings.summary())
        worker = Worker(
            engine, build_runners(settings) if runners is None else runners, queue=queue
        )
        worker.recover()
        if args.once:
            worker.drain()
        else:
            run_until_signalled(worker)
        logger.info("Worker stopped")
        return 0
    finally:
        engine.dispose()


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
