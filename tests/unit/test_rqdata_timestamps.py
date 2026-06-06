from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

from pa_agent.data.datetime_ts import datetime_to_ts_ms
from pa_agent.data.rqdata import RQDataSource, _is_bar_forming


def test_rqdata_preserves_index_timestamp_columns(monkeypatch) -> None:
    pd = pytest.importorskip("pandas")

    older = pd.Timestamp("2026-01-01 09:30:00")
    newer = pd.Timestamp("2026-01-01 09:35:00")
    df = pd.DataFrame(
        {
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
            "volume": [100.0, 200.0],
        },
        index=pd.Index([older, newer], name="date"),
    )

    fake_rqdatac = types.SimpleNamespace(get_price=lambda *args, **kwargs: df)
    monkeypatch.setitem(sys.modules, "rqdatac", fake_rqdatac)

    source = RQDataSource()
    source.subscribe("000001.XSHG", "5m")
    source._connected = True

    bars = source.latest_snapshot(2)

    # _row_ts_ms treats naive pandas Timestamps as Beijing time (UTC+8)
    # and converts them to UTC before epoch conversion.
    older_utc = older.tz_localize("Asia/Shanghai").tz_convert("UTC")
    newer_utc = newer.tz_localize("Asia/Shanghai").tz_convert("UTC")
    assert [bar.ts_open for bar in bars] == [
        datetime_to_ts_ms(newer_utc),
        datetime_to_ts_ms(older_utc),
    ]
    assert [bar.closed for bar in bars] == [True, True]


def test_rqdata_forming_detection_uses_bar_close_time() -> None:
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    now_ms = datetime_to_ts_ms(now)

    assert _is_bar_forming(now_ms - 30_000, "1m", now_ms=now_ms) is True
    assert _is_bar_forming(now_ms - 60_000, "1m", now_ms=now_ms) is False
    assert _is_bar_forming(now_ms - 5 * 60_000, "5m", now_ms=now_ms) is False
