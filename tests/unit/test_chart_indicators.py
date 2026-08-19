"""Tests for moving-average and Bollinger overlays on the K-line chart."""
from __future__ import annotations

import math

import pytest

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.gui.chart_widget import ChartWidget, _bollinger_bands
from pa_agent.indicators.ema import ema_full


def _frame(n: int = 80) -> KlineFrame:
    closes_oldest_first = [100.0 + index * 0.25 for index in range(n)]
    ema20_oldest_first = ema_full(closes_oldest_first, 20)
    bars_oldest_first = [
        KlineBar(
            seq=n - index,
            ts_open=1_700_000_000_000 + index * 60_000,
            open=close - 0.1,
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            volume=100.0,
        )
        for index, close in enumerate(closes_oldest_first)
    ]
    return KlineFrame(
        symbol="TEST",
        timeframe="1m",
        bars=tuple(reversed(bars_oldest_first)),
        indicators=IndicatorBundle(
            ema20=tuple(reversed(ema20_oldest_first)),
            atr14=tuple([1.0] * n),
        ),
        snapshot_ts_local_ms=1_700_000_000_000,
    )


def test_bollinger_bands_use_sma20_and_two_population_stddevs() -> None:
    values = [float(value) for value in range(1, 21)]

    middle, upper, lower = _bollinger_bands(values)

    expected_stddev = math.sqrt(33.25)
    assert all(math.isnan(value) for value in middle[:19])
    assert middle[-1] == pytest.approx(10.5)
    assert upper[-1] == pytest.approx(10.5 + 2 * expected_stddev)
    assert lower[-1] == pytest.approx(10.5 - 2 * expected_stddev)


def test_chart_renders_ema_lines_and_complete_bollinger_channel(qtbot) -> None:
    chart = ChartWidget()
    qtbot.addWidget(chart)

    chart.set_frame_now(_frame())

    assert set(chart._ema_lines) == {10, 20, 60}
    assert set(chart._bollinger_lines) == {"middle", "upper", "lower"}
    assert chart._bollinger_fill is not None


def test_auto_fit_includes_visible_bollinger_bands(qtbot) -> None:
    chart = ChartWidget()
    qtbot.addWidget(chart)
    frame = _frame()
    closes = [bar.close for bar in reversed(frame.bars)]
    _, upper, lower = _bollinger_bands(closes)

    _, y_range = chart._view_ranges_for_frame(frame)

    assert y_range[0] < min(lower[-20:])
    assert y_range[1] > max(upper[-20:])


def test_custom_bollinger_params_redraw_the_chart(qtbot) -> None:
    chart = ChartWidget()
    qtbot.addWidget(chart)
    frame = _frame()
    chart.set_frame_now(frame)
    original_upper = chart._bollinger_lines["upper"].getData()[1][-1]

    chart.set_bollinger_params(10, 1.5)
    chart._on_timer()

    assert chart.bollinger_params() == (10, 1.5)
    updated_upper = chart._bollinger_lines["upper"].getData()[1][-1]
    assert updated_upper != pytest.approx(original_upper)


def test_reset_removes_all_indicator_overlays(qtbot) -> None:
    chart = ChartWidget()
    qtbot.addWidget(chart)
    chart.set_frame_now(_frame())

    chart.reset()

    assert chart._ema_lines == {}
    assert chart._bollinger_lines == {}
    assert chart._bollinger_fill is None
