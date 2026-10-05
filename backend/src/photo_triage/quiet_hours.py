"""Quiet hours: when the worker stays idle so the host can serve media (plan §6.11).

`WORKER_QUIET_HOURS` lists periods separated by `;`. Each is a day or a day range,
then a time range, in the household's local time (`TZ`):

    Mon-Fri 17:00-02:00; Sat-Sun 07:00-02:00

- Days are `Mon` … `Sun`, in any case. A range runs forwards within the week
  (`Mon-Fri`, not `Fri-Mon`).
- The days say when a period starts. An end at or before the start is on the next
  day: `Fri 17:00-02:00` ends at 02:00 on Saturday, and an end of `00:00` is midnight.
- Periods that overlap or touch merge into one quiet stretch.

Times are wall-clock times in `TZ`, so a period on a day the clocks change is an
hour longer or shorter.
"""

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY

UTC_ZONE = ZoneInfo("UTC")

EXAMPLE = "Mon-Fri 17:00-23:00; Sat-Sun 08:00-23:00"

_PERIOD = re.compile(
    r"(?P<first>[a-z]{3})(?:-(?P<last>[a-z]{3}))?\s+(?P<start>\d\d:\d\d)-(?P<end>\d\d:\d\d)",
    re.IGNORECASE,
)

_LOOKAHEAD_DAYS = 15
"""How far ahead `quiet_until` looks for the end of a quiet stretch. A stretch ends
within a week, since a value covering the whole week is rejected. The second week
covers a gap that falls inside the hour skipped when the clocks go forward."""


class QuietHoursError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    weekday: int
    """The day it starts, 0 = Monday."""
    start: dt.time
    end: dt.time
    """On the next day if at or before `start`."""

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    def week_minutes(self) -> tuple[int, int]:
        """Start and end in minutes from Monday 00:00. The end may pass the week's end."""
        start = self.weekday * MINUTES_PER_DAY + _minutes(self.start)
        end = self.weekday * MINUTES_PER_DAY + _minutes(self.end)
        if self.crosses_midnight:
            end += MINUTES_PER_DAY
        return start, end


@dataclass(frozen=True)
class QuietHours:
    text: str
    """The value as configured."""
    periods: tuple[Period, ...]

    def __str__(self) -> str:
        return self.text

    def quiet_until(self, now: dt.datetime, zone: ZoneInfo) -> dt.datetime | None:
        """When the quiet stretch around `now` ends (aware UTC), or None if `now`
        isn't in quiet hours."""
        if now.tzinfo is None:
            raise ValueError(f"naive datetime {now!r}; use an aware datetime")
        now = now.astimezone(dt.UTC)
        intervals = self._intervals(now.astimezone(zone).date(), zone)
        until = None
        at = now
        while ends := [end for start, end in intervals if start <= at < end]:
            at = until = max(ends)
        return until

    def is_quiet(self, now: dt.datetime, zone: ZoneInfo) -> bool:
        return self.quiet_until(now, zone) is not None

    def _intervals(self, today: dt.date, zone: ZoneInfo) -> list[tuple[dt.datetime, dt.datetime]]:
        """Each period from yesterday on, as UTC start and end.

        Local times are compared in UTC: Python compares two datetimes in the same
        zone by wall-clock time, which is wrong around a change of the clocks. A time
        the clocks skip is read with the offset from before the change.
        """
        intervals: list[tuple[dt.datetime, dt.datetime]] = []
        for offset in range(-1, _LOOKAHEAD_DAYS):
            day = today + dt.timedelta(days=offset)
            for period in self.periods:
                if period.weekday != day.weekday():
                    continue
                end_day = day + dt.timedelta(days=1) if period.crosses_midnight else day
                start = dt.datetime.combine(day, period.start, zone).astimezone(dt.UTC)
                end = dt.datetime.combine(end_day, period.end, zone).astimezone(dt.UTC)
                if start < end:
                    intervals.append((start, end))
        return intervals


def parse(value: str) -> QuietHours:
    """Read a `WORKER_QUIET_HOURS` value, or raise `QuietHoursError` saying what's wrong."""
    periods: list[Period] = []
    for part in (p.strip() for p in value.split(";")):
        if not part:
            continue
        match = _PERIOD.fullmatch(part)
        if match is None:
            raise QuietHoursError(
                f'"{part}" isn\'t a day or day range and a time range, like "{EXAMPLE}"'
            )
        first = _day(match["first"], part)
        last = first if match["last"] is None else _day(match["last"], part)
        if last < first:
            raise QuietHoursError(
                f'"{part}": a day range must run forwards from Mon to Sun; '
                "split it into two periods"
            )
        start, end = _time(match["start"], part), _time(match["end"], part)
        if start == end:
            raise QuietHoursError(f'"{part}": the period starts and ends at the same time')
        periods.extend(Period(weekday, start, end) for weekday in range(first, last + 1))
    if not periods:
        raise QuietHoursError(f'no quiet periods given; leave it unset or write e.g. "{EXAMPLE}"')
    if _covers_whole_week(periods):
        raise QuietHoursError("the quiet periods cover the whole week, so nothing would ever run")
    return QuietHours(value.strip(), tuple(periods))


def _day(name: str, part: str) -> int:
    try:
        return DAYS.index(name.lower())
    except ValueError:
        raise QuietHoursError(f'"{part}": "{name}" isn\'t a day (Mon, Tue, … Sun)') from None


def _time(text: str, part: str) -> dt.time:
    hours, minutes = (int(n) for n in text.split(":"))
    if hours > 23 or minutes > 59:
        raise QuietHoursError(f'"{part}": "{text}" isn\'t a time from 00:00 to 23:59')
    return dt.time(hours, minutes)


def _minutes(time: dt.time) -> int:
    return time.hour * 60 + time.minute


def _covers_whole_week(periods: list[Period]) -> bool:
    spans: list[tuple[int, int]] = []
    for period in periods:
        start, end = period.week_minutes()
        if end > MINUTES_PER_WEEK:  # Sunday night into Monday
            spans += [(start, MINUTES_PER_WEEK), (0, end - MINUTES_PER_WEEK)]
        else:
            spans.append((start, end))
    covered_to = 0
    for start, end in sorted(spans):
        if start > covered_to:
            return False
        covered_to = max(covered_to, end)
    return covered_to >= MINUTES_PER_WEEK
