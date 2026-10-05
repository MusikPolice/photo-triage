"""Whether the worker process is running, for the Activity page (plan §7).

While the worker runs, a thread writes `worker_heartbeat.seen_at` every
`INTERVAL_S`, including while a long job runs. A clean stop sets `stopped_at`. The
API reports the worker as stopped when it stopped cleanly, or when the heartbeat is
older than `STALE_AFTER`, which means it crashed or was killed.

Heartbeats use real time, even when `FAKE_NOW` sets the worker's clock: the API
and the worker start their fake clocks at different moments, so their fake times
disagree.
"""

import datetime as dt
import logging
import threading
from types import TracebackType

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import WorkerHeartbeat
from photo_triage.worker.clock import Clock, utc_now

logger = logging.getLogger(__name__)

INTERVAL_S = 5.0
"""How often the worker writes a heartbeat."""

STALE_AFTER = dt.timedelta(seconds=30)
"""How old a heartbeat may be before the worker counts as stopped. Several missed
beats, so a moment of database contention doesn't show the worker as stopped."""

_ROW_ID = 1


def beat(session: Session, started_at: dt.datetime, now: dt.datetime) -> None:
    """Record that the worker, started at `started_at`, is running at `now`."""
    row = insert(WorkerHeartbeat).values(
        id=_ROW_ID, started_at=started_at, seen_at=now, stopped_at=None
    )
    session.execute(
        row.on_conflict_do_update(
            index_elements=[WorkerHeartbeat.id],
            set_={
                WorkerHeartbeat.started_at: row.excluded.started_at,
                WorkerHeartbeat.seen_at: row.excluded.seen_at,
                WorkerHeartbeat.stopped_at: row.excluded.stopped_at,
            },
        )
    )


def stopped(session: Session, now: dt.datetime) -> None:
    """Record a clean stop at `now`."""
    heartbeat = session.get(WorkerHeartbeat, _ROW_ID)
    if heartbeat is not None:
        heartbeat.seen_at = now
        heartbeat.stopped_at = now


def last(session: Session) -> WorkerHeartbeat | None:
    """The latest heartbeat, or None if the worker has never run."""
    return session.get(WorkerHeartbeat, _ROW_ID)


def is_alive(heartbeat: WorkerHeartbeat | None, now: dt.datetime) -> bool:
    return (
        heartbeat is not None
        and heartbeat.stopped_at is None
        and now - heartbeat.seen_at < STALE_AFTER
    )


class Heartbeat:
    """Writes heartbeats from a background thread while in a `with` block, and
    records a clean stop when the block ends."""

    def __init__(
        self, engine: Engine, clock: Clock = utc_now, interval_s: float = INTERVAL_S
    ) -> None:
        self._sessions = sessionmaker(engine)
        self._clock = clock
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._started_at = clock()

    def __enter__(self) -> "Heartbeat":
        self._beat()  # at once, so the API sees the worker before the first interval
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()
        with self._sessions.begin() as session:
            stopped(session, self._clock())

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._beat()
            except Exception:
                # The next beat may succeed. Enough misses and the API shows the
                # worker as stopped, which is the truth if the database is unusable.
                logger.warning("Couldn't write the worker heartbeat", exc_info=True)

    def _beat(self) -> None:
        with self._sessions.begin() as session:
            beat(session, self._started_at, self._clock())
