"""Unit tests for RQData timezone handling in _row_ts_ms.

RQData returns China market time as naive pandas Timestamp (local Beijing/UTC+8).
_row_ts_ms must convert it to UTC before epoch conversion, or all timestamps
will be off by 8 hours.
"""

import pytest


class FakeRow:
    """Minimal namedtuple-like object for _row_ts_ms attribute probing."""

    def __init__(self, **kwargs):
        self._fields = list(kwargs.keys())
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_row(**kwargs):
    return FakeRow(**kwargs)


class TestRowTsMsTimezone:
    """Tests for _row_ts_ms timezone conversion logic."""

    def test_naive_pandas_timestamp_shanghai(self):
        """RQData returns China market time as naive Timestamp — must convert to UTC."""
        import pandas as pd

        # Beijing time 2026-06-01 10:30:00 (UTC+8)
        ts = pd.Timestamp("2026-06-01 10:30:00")
        row = _make_row(datetime=ts)

        from pa_agent.data.rqdata import _row_ts_ms

        result = _row_ts_ms(row)
        # Beijing 10:30 -> UTC 02:30 -> epoch ms
        expected = 1780281000000
        assert result == expected

    def test_aware_pandas_timestamp_preserved(self):
        """If Timestamp already has tzinfo, use it directly."""
        import pandas as pd

        ts = pd.Timestamp("2026-06-01 02:30:00", tz="UTC")
        row = _make_row(datetime=ts)

        from pa_agent.data.rqdata import _row_ts_ms

        result = _row_ts_ms(row)
        expected = 1780281000000
        assert result == expected

    def test_naive_python_datetime(self):
        """Naive Python datetime is treated as UTC by datetime_to_ts_ms."""
        from datetime import datetime

        dt = datetime(2026, 6, 1, 2, 30, 0)
        row = _make_row(datetime=dt)

        from pa_agent.data.rqdata import _row_ts_ms

        result = _row_ts_ms(row)
        expected = 1780281000000
        assert result == expected

    def test_none_datetime(self):
        """If no datetime/date attribute found, return None."""
        row = _make_row(open=100.0, close=101.0)

        from pa_agent.data.rqdata import _row_ts_ms

        result = _row_ts_ms(row)
        assert result is None

    def test_date_field_fallback(self):
        """If 'date' attribute exists, use it directly."""
        import pandas as pd

        ts = pd.Timestamp("2026-06-01 15:00:00")
        row = _make_row(date=ts, datetime="ignored")

        from pa_agent.data.rqdata import _row_ts_ms

        result = _row_ts_ms(row)
        # 15:00 Beijing = 07:00 UTC
        expected = 1780297200000
        assert result == expected

    def test_shanghai_night_session(self):
        """Night session 21:00 Beijing = 13:00 UTC same day."""
        import pandas as pd

        ts = pd.Timestamp("2026-06-01 21:00:00")
        row = _make_row(datetime=ts)

        from pa_agent.data.rqdata import _row_ts_ms

        result = _row_ts_ms(row)
        # 21:00 Beijing = 13:00 UTC
        expected = 1780318800000
        assert result == expected

    def test_shanghai_cross_date(self):
        """Night session after midnight: 00:30 Beijing = 16:30 UTC prev day."""
        import pandas as pd

        ts = pd.Timestamp("2026-06-02 00:30:00")
        row = _make_row(datetime=ts)

        from pa_agent.data.rqdata import _row_ts_ms

        result = _row_ts_ms(row)
        # 00:30 Beijing = 16:30 UTC on 2026-06-01
        expected = 1780331400000
        assert result == expected
