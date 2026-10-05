"""Parsing `WORKER_QUIET_HOURS` and finding when quiet hours end (plan §6.11)."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from photo_triage.quiet_hours import DAYS, QuietHours, QuietHoursError, parse

UTC = ZoneInfo("UTC")
TORONTO = ZoneInfo("America/Toronto")

# 2026-10-05 is a Monday.
MONDAY = dt.date(2026, 10, 5)


def at(day: int, time: str, zone: ZoneInfo = UTC) -> dt.datetime:
    """`day` days after Monday 2026-10-05, at a wall-clock `time` in `zone`."""
    local = dt.datetime.combine(MONDAY + dt.timedelta(days=day), dt.time.fromisoformat(time))
    return local.replace(tzinfo=zone).astimezone(dt.UTC)


def test_a_day_range_applies_to_each_day_in_it() -> None:
    quiet = parse("Mon-Fri 17:00-23:00")

    for day in range(5):
        assert quiet.quiet_until(at(day, "17:00"), UTC) == at(day, "23:00")
        assert quiet.quiet_until(at(day, "22:59"), UTC) == at(day, "23:00")
        assert not quiet.is_quiet(at(day, "16:59"), UTC)
        assert not quiet.is_quiet(at(day, "23:00"), UTC)
    assert not quiet.is_quiet(at(5, "18:00"), UTC)
    assert not quiet.is_quiet(at(6, "18:00"), UTC)


def test_a_single_day() -> None:
    quiet = parse("wed 09:30-10:00")

    assert quiet.quiet_until(at(2, "09:45"), UTC) == at(2, "10:00")
    assert not quiet.is_quiet(at(1, "09:45"), UTC)
    assert not quiet.is_quiet(at(3, "09:45"), UTC)


def test_several_periods_give_different_hours_on_different_days() -> None:
    quiet = parse("Mon-Fri 17:00-00:00; Sat-Sun 07:00-00:00")

    assert quiet.quiet_until(at(0, "20:00"), UTC) == at(1, "00:00")
    assert not quiet.is_quiet(at(0, "08:00"), UTC)
    assert quiet.quiet_until(at(5, "08:00"), UTC) == at(6, "00:00")
    assert not quiet.is_quiet(at(5, "06:59"), UTC)


def test_a_period_that_crosses_midnight_ends_the_next_day() -> None:
    quiet = parse("Fri 22:00-02:00")

    assert quiet.quiet_until(at(4, "23:00"), UTC) == at(5, "02:00")
    assert quiet.quiet_until(at(5, "01:00"), UTC) == at(5, "02:00")
    assert not quiet.is_quiet(at(4, "01:00"), UTC)  # Thursday night isn't included
    assert not quiet.is_quiet(at(5, "02:00"), UTC)


def test_sunday_night_runs_into_monday() -> None:
    quiet = parse("Sun 22:00-02:00")

    assert quiet.quiet_until(at(7, "01:00"), UTC) == at(7, "02:00")
    assert quiet.quiet_until(at(6, "23:00"), UTC) == at(7, "02:00")


def test_periods_that_touch_or_overlap_merge() -> None:
    quiet = parse("Mon-Fri 17:00-02:00; Sat-Sun 07:00-02:00; Sat 01:00-08:00")

    # Friday evening to Saturday 08:00, overlapping Saturday's own period, which runs
    # to Sunday 02:00.
    assert quiet.quiet_until(at(4, "18:00"), UTC) == at(6, "02:00")
    assert not quiet.is_quiet(at(6, "03:00"), UTC)


def test_times_are_local_to_the_zone() -> None:
    quiet = parse("Mon 17:00-23:00")

    # 17:00 UTC is 13:00 in Toronto, and 17:00 there is 21:00 UTC (EDT).
    assert not quiet.is_quiet(at(0, "17:00"), TORONTO)
    assert quiet.quiet_until(at(0, "17:00", TORONTO), TORONTO) == at(0, "23:00", TORONTO)
    assert at(0, "23:00", TORONTO) == dt.datetime(2026, 10, 6, 3, tzinfo=dt.UTC)


def test_a_period_on_the_night_the_clocks_go_back_is_an_hour_longer() -> None:
    quiet = parse("Sat 22:00-06:00")
    start = dt.datetime(2026, 10, 31, 22, tzinfo=TORONTO)  # EDT, the night of 1 November

    until = quiet.quiet_until(start, TORONTO)

    assert until == dt.datetime(2026, 11, 1, 11, tzinfo=dt.UTC)  # 06:00 EST
    assert until is not None
    assert until - start == dt.timedelta(hours=9)


def test_naive_times_are_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        parse("Mon 17:00-23:00").quiet_until(dt.datetime(2026, 10, 5, 18), UTC)


def test_the_value_is_kept_as_written() -> None:
    assert str(parse(" Mon-Fri 17:00-23:00; ")) == "Mon-Fri 17:00-23:00;"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Mon-Fri 5pm-11pm", "isn't a day or day range and a time range"),
        ("17:00-23:00", "isn't a day or day range and a time range"),
        ("Monday 17:00-23:00", "isn't a day or day range and a time range"),
        ("Mon-Fri 17:00-23:00, Sat 08:00-23:00", "isn't a day or day range"),
        ("Xyz 17:00-23:00", '"Xyz" isn\'t a day'),
        ("Fri-Mon 17:00-23:00", "must run forwards"),
        ("Mon 24:00-02:00", '"24:00" isn\'t a time'),
        ("Mon 17:60-23:00", '"17:60" isn\'t a time'),
        ("Mon 17:00-17:00", "starts and ends at the same time"),
        ("", "no quiet periods"),
        (";", "no quiet periods"),
        ("Mon-Sun 00:00-12:00; Mon-Sun 12:00-00:00", "cover the whole week"),
        ("Mon-Sun 08:00-09:00; Mon-Sun 09:00-08:00", "cover the whole week"),
    ],
)
def test_malformed_values_are_rejected_with_the_reason(value: str, reason: str) -> None:
    with pytest.raises(QuietHoursError, match=reason):
        parse(value)


def test_almost_the_whole_week_is_allowed() -> None:
    quiet = parse("Mon-Sun 08:00-07:59")

    assert quiet.quiet_until(at(0, "09:00"), UTC) == at(1, "07:59")
    assert not quiet.is_quiet(at(1, "07:59"), UTC)


# Properties. Zones with daylight saving, including a 30-minute shift (Lord Howe),
# and ones without.
ZONES = [UTC, TORONTO, ZoneInfo("Europe/London"), ZoneInfo("Australia/Lord_Howe")]


@st.composite
def quiet_hours(draw: st.DrawFn) -> QuietHours:
    periods = []
    for _ in range(draw(st.integers(1, 4))):
        first = draw(st.integers(0, 6))
        last = draw(st.integers(first, 6))
        start = draw(st.integers(0, 24 * 60 - 1))
        end = draw(st.integers(0, 24 * 60 - 1).filter(lambda m, s=start: m != s))
        days = DAYS[first].title() + ("" if last == first else f"-{DAYS[last].title()}")
        periods.append(f"{days} {start // 60:02}:{start % 60:02}-{end // 60:02}:{end % 60:02}")
    try:
        return parse("; ".join(periods))
    except QuietHoursError:
        assume(False)
        raise


instants = st.datetimes(
    min_value=dt.datetime(2020, 1, 1), max_value=dt.datetime(2035, 12, 31), timezones=st.just(UTC)
)


@given(quiet_hours(), instants, st.sampled_from(ZONES), st.floats(0, 1, exclude_max=True))
def test_quiet_hours_and_their_end_agree(
    quiet: QuietHours, now: dt.datetime, zone: ZoneInfo, fraction: float
) -> None:
    until = quiet.quiet_until(now, zone)

    assert quiet.is_quiet(now, zone) == (until is not None)
    if until is None:
        return
    assert until > now
    assert until - now < dt.timedelta(days=8)
    assert not quiet.is_quiet(until, zone)
    # Every moment before the end is quiet, with the same end. (A fraction just under
    # 1 can round to the end itself, to the microsecond.)
    during = min(now + (until - now) * fraction, until - dt.timedelta(microseconds=1))
    assert quiet.quiet_until(during, zone) == until


@given(quiet_hours(), instants)
def test_without_daylight_saving_quiet_means_inside_a_period_on_the_wall_clock(
    quiet: QuietHours, now: dt.datetime
) -> None:
    second_of_week = now.weekday() * 86400 + now.hour * 3600 + now.minute * 60 + now.second
    week_s = 7 * 86400

    def covers(period_start_s: int, period_end_s: int) -> bool:
        return any(
            period_start_s <= s < period_end_s for s in (second_of_week, second_of_week + week_s)
        )

    inside = any(
        covers(start * 60, end * 60)
        for start, end in (period.week_minutes() for period in quiet.periods)
    )
    assert quiet.is_quiet(now, UTC) == inside
