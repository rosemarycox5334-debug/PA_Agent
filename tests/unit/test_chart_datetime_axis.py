"""Tests for date/time labels on the K-line chart X axis.

The chart renders bar-open timestamps in the host's *local* timezone (they are
stored as real UTC epoch by every data source). Expected labels are therefore
derived with the same ``datetime.fromtimestamp`` call so the tests are stable
regardless of the machine's timezone, while still exercising the
x-index → bar → label mapping and the daily/intraday slicing.
"""
from __future__ import annotations

from datetime import datetime

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.data.datetime_ts import datetime_to_ts_ms
from pa_agent.gui.chart_widget import ChartWidget, DateTimeAxisItem


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return float(datetime_to_ts_ms(datetime(year, month, day, hour, minute)))


def _local_intraday(ts_ms: float) -> str:
    """Mirror the chart's local-timezone intraday label (MM-DD HH:MM)."""
    return datetime.fromtimestamp(ts_ms / 1000.0).strftime("%m-%d %H:%M")


def _local_daily(ts_ms: float) -> str:
    """Mirror the chart's local-timezone daily label (YYYY-MM-DD)."""
    return datetime.fromtimestamp(ts_ms / 1000.0).strftime("%Y-%m-%d")


def test_intraday_axis_displays_date_and_time(qtbot) -> None:
    axis = DateTimeAxisItem(orientation="bottom")
    t0 = _ts(2025, 6, 7, 9, 30)
    t1 = _ts(2025, 6, 7, 9, 45)
    t2 = _ts(2025, 7, 1, 15, 0)
    axis.set_bar_times((t0, t1, t2), "15m")

    assert axis.tickStrings([0.0, 1.0, 2.0], 1.0, 1.0) == [
        _local_intraday(t0),
        _local_intraday(t1),
        _local_intraday(t2),
    ]


def test_daily_axis_displays_full_date(qtbot) -> None:
    axis = DateTimeAxisItem(orientation="bottom")
    t0 = _ts(2025, 6, 7)
    t1 = _ts(2025, 7, 1)
    axis.set_bar_times((t0, t1), "1d")

    assert axis.tickStrings([0.0, 1.0], 1.0, 1.0) == [_local_daily(t0), _local_daily(t1)]
    # Out-of-range / fractional indices render blank.
    assert axis.tickStrings([-1.0, 1.5, 3.0], 1.0, 1.0) == ["", "", ""]


def test_chart_maps_oldest_bar_to_left_axis(qtbot) -> None:
    newest = KlineBar(
        seq=1,
        ts_open=_ts(2025, 7, 1),
        open=11.0,
        high=12.0,
        low=10.0,
        close=11.5,
        volume=100.0,
    )
    oldest = KlineBar(
        seq=2,
        ts_open=_ts(2025, 6, 7),
        open=10.0,
        high=11.0,
        low=9.0,
        close=10.5,
        volume=90.0,
    )
    frame = KlineFrame(
        symbol="600519",
        timeframe="1d",
        bars=(newest, oldest),
        indicators=IndicatorBundle(
            ema20=(float("nan"), float("nan")),
            atr14=(float("nan"), float("nan")),
        ),
        snapshot_ts_local_ms=int(_ts(2025, 7, 1)),
    )
    chart = ChartWidget()
    qtbot.addWidget(chart)

    chart.set_frame_now(frame)

    assert chart._time_axis.tickStrings([0.0, 1.0], 1.0, 1.0) == [
        _local_daily(oldest.ts_open),
        _local_daily(newest.ts_open),
    ]
