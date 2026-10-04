"""The worker's two sources of time.

- A **clock** says what time it is: aware UTC, for `*_at` columns, backoff and
  (later) quiet hours. Tests inject one, and `FAKE_NOW` sets one in dev.
- A **timer** measures how long something took, in seconds. It's monotonic and
  never faked by `FAKE_NOW`, since throughput and ETA need real durations.
"""

import datetime as dt
import time
from collections.abc import Callable

Clock = Callable[[], dt.datetime]
Timer = Callable[[], float]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def running_from(start_at: dt.datetime, timer: Timer = time.monotonic) -> Clock:
    """A clock that reads `start_at` now and then advances in real time (`FAKE_NOW`)."""
    if start_at.tzinfo is None:
        raise ValueError(f"naive datetime {start_at!r}; use an aware datetime")
    started_s = timer()
    start_at = start_at.astimezone(dt.UTC)
    return lambda: start_at + dt.timedelta(seconds=timer() - started_s)
