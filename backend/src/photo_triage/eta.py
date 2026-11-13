"""When the work left will be done, counting only the hours the worker runs (plan §7).

With quiet hours (plan §6.11), the remaining work is laid over the time outside
them, so an estimate reads like "~23 working windows" rather than a misleading
"~8 days" of wall-clock time. Without them, it's plain wall-clock time.
"""

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from photo_triage.quiet_hours import QuietHours

MAX_WINDOWS = 10_000
"""More windows than this (decades of nightly runs) is no estimate worth showing."""


@dataclass(frozen=True)
class Eta:
    done_at: dt.datetime
    """Aware UTC."""
    windows: int | None
    """With quiet hours, the working windows the work spans: 1 if it finishes in the
    current window (or the next, during quiet hours). None without quiet hours."""


def finish_at(
    now: dt.datetime, work: dt.timedelta, quiet_hours: QuietHours | None, zone: ZoneInfo
) -> Eta | None:
    """When `work` of running time, starting at `now`, is done. None if it would
    take more than `MAX_WINDOWS` working windows."""
    if now.tzinfo is None:
        raise ValueError(f"naive datetime {now!r}; use an aware datetime")
    if work < dt.timedelta(0):
        raise ValueError(f"negative work {work}")
    at, left = now.astimezone(dt.UTC), work
    if quiet_hours is None:
        return Eta(at + left, None)
    for windows in range(1, MAX_WINDOWS + 1):
        if (until := quiet_hours.quiet_until(at, zone)) is not None:
            at = until
        quiet_at = quiet_hours.next_start(at, zone)
        if quiet_at is None or at + left <= quiet_at:
            return Eta(at + left, windows)
        left -= quiet_at - at
        at = quiet_at
    return None
