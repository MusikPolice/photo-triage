import datetime as dt

import pytest
from sqlalchemy.dialects import sqlite

from photo_triage.db.columns import UTCDateTime

DIALECT = sqlite.dialect()


def test_aware_datetimes_are_stored_as_naive_utc() -> None:
    plus_two = dt.timezone(dt.timedelta(hours=2))
    stored = UTCDateTime().process_bind_param(dt.datetime(2026, 1, 1, 12, tzinfo=plus_two), DIALECT)
    assert stored == dt.datetime(2026, 1, 1, 10)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        UTCDateTime().process_bind_param(dt.datetime(2026, 1, 1), DIALECT)


def test_stored_values_come_back_as_utc() -> None:
    loaded = UTCDateTime().process_result_value(dt.datetime(2026, 1, 1, 10), DIALECT)
    assert loaded == dt.datetime(2026, 1, 1, 10, tzinfo=dt.UTC)


def test_null_passes_through() -> None:
    assert UTCDateTime().process_bind_param(None, DIALECT) is None
    assert UTCDateTime().process_result_value(None, DIALECT) is None
