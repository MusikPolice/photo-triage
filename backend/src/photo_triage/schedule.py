"""Cron schedules in the household's timezone, for `SCAN_SCHEDULE` (plan §6.1, §9).

A schedule is a five-field cron expression (`0 2 * * *`, every night at 02:00)
read in `TZ`, so it follows daylight saving time. When the clocks go forward
past a scheduled time, it fires at the first minute that exists after it, as
cron does. When they go back, it fires once.
"""

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from cronsim import CronSim, CronSimError


@dataclass(frozen=True)
class Schedule:
    expression: str

    def __post_init__(self) -> None:
        if len(self.expression.split()) != 5:
            raise ValueError(
                f"{self.expression!r} isn't a cron expression: it needs five fields "
                "(minute hour day-of-month month day-of-week), e.g. '0 2 * * *'"
            )
        try:
            CronSim(self.expression, dt.datetime(2000, 1, 1, tzinfo=dt.UTC))
        except CronSimError as exc:
            raise ValueError(f"{self.expression!r} isn't a valid cron expression: {exc}") from None

    def __str__(self) -> str:
        return self.expression

    def previous_at(self, now: dt.datetime, zone: ZoneInfo) -> dt.datetime:
        """The latest scheduled time at or before `now`, in UTC."""
        # Going back, CronSim skips its start when that's on a whole minute, so
        # start just after `now` to include a time exactly at it.
        return self._first(now + dt.timedelta(microseconds=1), zone, reverse=True)

    def next_at(self, now: dt.datetime, zone: ZoneInfo) -> dt.datetime:
        """The first scheduled time after `now`, in UTC."""
        return self._first(now, zone, reverse=False)

    def _first(self, start: dt.datetime, zone: ZoneInfo, *, reverse: bool) -> dt.datetime:
        if start.tzinfo is None:
            raise ValueError(f"naive datetime {start!r}; use an aware datetime")
        fire = next(CronSim(self.expression, start.astimezone(zone), reverse=reverse))
        return fire.astimezone(dt.UTC)


def parse(text: str) -> Schedule:
    """`text` as a schedule, or ValueError saying what's wrong with it."""
    return Schedule(" ".join(text.split()))
