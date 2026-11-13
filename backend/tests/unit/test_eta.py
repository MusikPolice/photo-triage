"""The ETA counts only the hours outside quiet hours (`photo_triage.eta`, plan §7)."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from photo_triage.eta import finish_at
from photo_triage.quiet_hours import parse

TORONTO = ZoneInfo("America/Toronto")
# The household's: the worker runs 02:00-17:00 on weekdays, 02:00-07:00 at weekends.
HOUSEHOLD = parse("Mon-Fri 17:00-02:00; Sat-Sun 07:00-02:00")
# Monday 2026-10-05, 10:00 EDT.
MONDAY_10AM = dt.datetime(2026, 10, 5, 14, tzinfo=dt.UTC)


def _local(at: dt.datetime) -> str:
    return f"{at.astimezone(TORONTO):%a %H:%M}"


def test_without_quiet_hours_it_is_wall_clock_time() -> None:
    eta = finish_at(MONDAY_10AM, dt.timedelta(hours=30), None, TORONTO)

    assert eta is not None
    assert eta.done_at == MONDAY_10AM + dt.timedelta(hours=30)
    assert eta.windows is None


def test_work_that_fits_finishes_in_this_window() -> None:
    eta = finish_at(MONDAY_10AM, dt.timedelta(hours=3), HOUSEHOLD, TORONTO)

    assert eta is not None
    assert (_local(eta.done_at), eta.windows) == ("Mon 13:00", 1)


def test_work_carries_over_quiet_hours_into_the_next_window() -> None:
    # 7 h left today (10:00-17:00), then 2 h from 02:00 on Tuesday.
    eta = finish_at(MONDAY_10AM, dt.timedelta(hours=9), HOUSEHOLD, TORONTO)

    assert eta is not None
    assert (_local(eta.done_at), eta.windows) == ("Tue 04:00", 2)


def test_during_quiet_hours_it_starts_when_they_end() -> None:
    monday_8pm = MONDAY_10AM + dt.timedelta(hours=10)

    eta = finish_at(monday_8pm, dt.timedelta(hours=1), HOUSEHOLD, TORONTO)

    assert eta is not None
    assert (_local(eta.done_at), eta.windows) == ("Tue 03:00", 1)


def test_a_long_job_counts_windows_over_weekends() -> None:
    # Weekdays give 15 h, weekends 5 h: Mon 7 h + Tue-Fri 4 x 15 h = 67 h by Friday
    # 17:00, then Sat 5 h = 72 h by Saturday 07:00.
    eta = finish_at(MONDAY_10AM, dt.timedelta(hours=72), HOUSEHOLD, TORONTO)

    assert eta is not None
    assert (_local(eta.done_at), eta.windows) == ("Sat 07:00", 6)


def test_a_change_of_the_clocks_is_counted_in_real_hours() -> None:
    # Sunday 2026-11-01: 01:00-02:00 happens twice, so the window from 02:00 to
    # 07:00 EST starts five real hours before it ends, as on any other day.
    quiet = parse("Sat-Sun 07:00-02:00")
    saturday_noon = dt.datetime(2026, 10, 31, 16, tzinfo=dt.UTC)

    eta = finish_at(saturday_noon, dt.timedelta(hours=5), quiet, TORONTO)

    assert eta is not None
    assert eta.done_at.astimezone(TORONTO).strftime("%a %H:%M %Z") == "Sun 07:00 EST"
    assert eta.windows == 1


def test_negative_work_and_naive_times_are_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        finish_at(MONDAY_10AM, dt.timedelta(seconds=-1), None, TORONTO)
    with pytest.raises(ValueError, match="naive"):
        finish_at(dt.datetime(2026, 1, 1), dt.timedelta(1), None, TORONTO)


_NOW = st.datetimes(
    min_value=dt.datetime(2026, 1, 1),
    max_value=dt.datetime(2028, 1, 1),
    timezones=st.just(dt.UTC),
)
_WORK = st.timedeltas(min_value=dt.timedelta(0), max_value=dt.timedelta(days=60))


@given(now=_NOW, work=_WORK)
def test_it_matches_wall_clock_time_without_quiet_hours(
    now: dt.datetime, work: dt.timedelta
) -> None:
    eta = finish_at(now, work, None, TORONTO)
    assert eta is not None and eta.done_at == now + work


@given(now=_NOW, less=_WORK, more=_WORK)
def test_it_is_monotonic_in_the_work_left(
    now: dt.datetime, less: dt.timedelta, more: dt.timedelta
) -> None:
    less, more = sorted((less, more))
    a = finish_at(now, less, HOUSEHOLD, TORONTO)
    b = finish_at(now, more, HOUSEHOLD, TORONTO)
    assert a is not None and b is not None
    assert a.done_at <= b.done_at
    assert a.windows is not None and b.windows is not None and a.windows <= b.windows


@given(now=_NOW, work=_WORK)
def test_it_is_never_sooner_than_wall_clock_time_and_never_ends_in_quiet_hours(
    now: dt.datetime, work: dt.timedelta
) -> None:
    eta = finish_at(now, work, HOUSEHOLD, TORONTO)
    assert eta is not None
    assert eta.done_at >= now + work
    # It may end exactly as quiet hours start, but not inside them.
    assert not HOUSEHOLD.is_quiet(eta.done_at - dt.timedelta(microseconds=1), TORONTO) or (
        work == dt.timedelta(0)
    )
