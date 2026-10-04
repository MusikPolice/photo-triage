"""The worker's clocks (dev-environment §5: `FAKE_NOW`)."""

import datetime as dt

import pytest

from photo_triage.worker.clock import running_from, utc_now


def test_utc_now_is_aware_utc() -> None:
    assert utc_now().tzinfo == dt.UTC


def test_running_from_starts_at_the_given_time_and_advances_in_real_time() -> None:
    elapsed_s = [100.0]
    eastern = dt.timezone(dt.timedelta(hours=-5))
    clock = running_from(dt.datetime(2030, 1, 1, 21, 59, tzinfo=eastern), lambda: elapsed_s[0])

    assert clock() == dt.datetime(2030, 1, 2, 2, 59, tzinfo=dt.UTC)
    assert clock().tzinfo == dt.UTC
    elapsed_s[0] += 90.5
    assert clock() == dt.datetime(2030, 1, 2, 3, 0, 30, 500000, tzinfo=dt.UTC)


def test_running_from_refuses_a_naive_time() -> None:
    with pytest.raises(ValueError, match="naive"):
        running_from(dt.datetime(2030, 1, 1))
