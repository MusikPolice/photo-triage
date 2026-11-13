"""Cron schedules in `TZ` (`photo_triage.schedule`), for `SCAN_SCHEDULE`."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from photo_triage.schedule import parse

TORONTO = ZoneInfo("America/Toronto")
NIGHTLY = parse("0 2 * * *")


def _utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).astimezone(dt.UTC)


def test_the_times_are_local_and_returned_in_utc() -> None:
    now = _utc("2026-11-12T12:00:00Z")  # 07:00 EST

    assert NIGHTLY.previous_at(now, TORONTO) == _utc("2026-11-12T07:00:00Z")
    assert NIGHTLY.next_at(now, TORONTO) == _utc("2026-11-13T07:00:00Z")


def test_a_time_exactly_now_is_the_previous_one_not_the_next() -> None:
    now = _utc("2026-11-12T07:00:00Z")

    assert NIGHTLY.previous_at(now, TORONTO) == now
    assert NIGHTLY.next_at(now, TORONTO) == _utc("2026-11-13T07:00:00Z")
    just_before = now - dt.timedelta(microseconds=1)
    assert NIGHTLY.previous_at(just_before, TORONTO) == _utc("2026-11-11T07:00:00Z")


def test_the_hour_follows_daylight_saving_time() -> None:
    # 02:00 is 06:00 UTC in summer (EDT) and 07:00 in winter (EST).
    assert NIGHTLY.next_at(_utc("2026-07-01T12:00:00Z"), TORONTO) == _utc("2026-07-02T06:00:00Z")
    assert NIGHTLY.next_at(_utc("2026-12-01T12:00:00Z"), TORONTO) == _utc("2026-12-02T07:00:00Z")


def test_a_time_skipped_when_the_clocks_go_forward_fires_after_the_gap() -> None:
    # 2026-03-08: 02:00 EST jumps to 03:00 EDT, so 02:30 never happens.
    fire = parse("30 2 * * *").next_at(_utc("2026-03-07T12:00:00Z"), TORONTO)

    assert fire.astimezone(TORONTO).date() == dt.date(2026, 3, 8)
    assert fire == _utc("2026-03-08T03:00:00-04:00")


def test_a_time_repeated_when_the_clocks_go_back_fires_once() -> None:
    # 2026-11-01: 01:30 happens twice, at 05:30 and 06:30 UTC.
    schedule = parse("30 1 * * *")
    first = schedule.next_at(_utc("2026-10-31T12:00:00Z"), TORONTO)
    after = schedule.next_at(first, TORONTO)

    assert first.astimezone(TORONTO).date() == dt.date(2026, 11, 1)
    assert after.astimezone(TORONTO).date() == dt.date(2026, 11, 2)


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        NIGHTLY.next_at(dt.datetime(2026, 1, 1), TORONTO)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("0 2 * *", "needs five fields"),
        ("0 2 * * * *", "needs five fields"),
        ("@daily", "needs five fields"),
        ("61 2 * * *", "Bad minute"),
        ("0 2 31 2 *", "Bad day-of-month"),
        ("0 2 * * Funday", "Bad day-of-week"),
    ],
)
def test_bad_expressions_say_what_is_wrong(text: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse(text)


def test_whitespace_is_normalised() -> None:
    assert str(parse("  30   3 * * 1-5 ")) == "30 3 * * 1-5"


@given(
    now=st.datetimes(
        min_value=dt.datetime(2020, 1, 1),
        max_value=dt.datetime(2035, 1, 1),
        timezones=st.just(dt.UTC),
    ),
    expression=st.sampled_from(["0 2 * * *", "30 1 * * *", "*/15 * * * *", "0 3 * * 1-5"]),
)
def test_now_lies_between_the_previous_and_next_times(now: dt.datetime, expression: str) -> None:
    schedule = parse(expression)
    previous_at = schedule.previous_at(now, TORONTO)
    next_at = schedule.next_at(now, TORONTO)

    assert previous_at <= now < next_at
    # Nothing is scheduled between them.
    assert schedule.next_at(previous_at, TORONTO) == next_at
