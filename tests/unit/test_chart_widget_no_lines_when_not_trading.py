"""Test that ChartWidget does not draw InfiniteLine items when order_type == '不下单'.

Task 14.7 — pytest-qt test.

Validates: Requirements R9.4, R10.2
"""
from __future__ import annotations

import pytest

# Guard: skip the whole module if PyQt6 / pyqtgraph are not available
pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")


@pytest.fixture
def chart_widget(qtbot):
    """Create a ChartWidget and register it with qtbot."""
    from pa_agent.gui.chart_widget import ChartWidget

    widget = ChartWidget()
    qtbot.addWidget(widget)
    return widget


def _count_infinite_lines(plot_widget) -> int:
    """Count the number of InfiniteLine items currently in the plot.

    Excludes crosshair lines (v_crosshair / h_crosshair) which are always
    present as UI helpers regardless of trading decisions.
    """
    import pyqtgraph as pg

    return sum(
        1
        for item in plot_widget.getPlotItem().items
        if isinstance(item, pg.InfiniteLine)
        and item not in (plot_widget._v_crosshair, plot_widget._h_crosshair)
    )


def _count_structure_lines(plot_widget) -> int:
    """Count support/resistance InfiniteLine items managed by ChartWidget."""
    import pyqtgraph as pg

    return sum(
        1 for item in plot_widget._structure_items if isinstance(item, pg.InfiniteLine)
    )


class TestNoLinesWhenNotTrading:
    """ChartWidget must not show InfiniteLine items for '不下单' decisions."""

    def test_no_infinite_lines_after_no_order_decision(self, chart_widget):
        """set_decision with order_type='不下单' must leave zero InfiniteLine items."""
        decision = {
            "order_type": "不下单",
            "entry_price": None,
            "take_profit_price": None,
            "stop_loss_price": None,
            "reasoning": "市场结构不明朗，暂不入场。",
        }
        chart_widget.set_decision(decision)

        assert _count_infinite_lines(chart_widget) == 0, (
            "Expected no InfiniteLine items after a '不下单' decision, "
            f"but found {_count_infinite_lines(chart_widget)}."
        )

    def test_lines_cleared_when_switching_to_no_order(self, chart_widget):
        """Lines drawn for a trading decision must be cleared when '不下单' is set."""
        # First set a trading decision (should draw 3 lines)
        trading_decision = {
            "order_type": "限价单",
            "order_direction": "做多",
            "entry_price": 1900.0,
            "take_profit_price": 1920.0,
            "stop_loss_price": 1880.0,
            "reasoning": "上升趋势明确。",
        }
        chart_widget.set_decision(trading_decision)

        # Sanity check: lines should exist now
        assert _count_infinite_lines(chart_widget) > 0, (
            "Expected InfiniteLine items after a trading decision."
        )

        # Now switch to 不下单
        no_order_decision = {
            "order_type": "不下单",
            "entry_price": None,
            "take_profit_price": None,
            "stop_loss_price": None,
            "reasoning": "风险过高。",
        }
        chart_widget.set_decision(no_order_decision)

        assert _count_infinite_lines(chart_widget) == 0, (
            "Expected no InfiniteLine items after switching to '不下单', "
            f"but found {_count_infinite_lines(chart_widget)}."
        )

    def test_short_decision_shows_down_arrow(self, chart_widget, qtbot):
        """做空 decision draws a ▼ marker at the newest bar."""
        from pa_agent.data.base import KlineBar, KlineFrame, IndicatorBundle

        bars = tuple(
            KlineBar(
                seq=i + 1,
                ts_open=1_700_000_000_000 - i * 3_600_000,
                open=2000.0,
                high=2010.0,
                low=1990.0,
                close=2005.0,
                volume=100.0,
                closed=True,
            )
            for i in range(5)
        )
        frame = KlineFrame(
            symbol="XAUUSD",
            timeframe="1h",
            bars=bars,
            snapshot_ts_local_ms=1_700_000_000_000,
            indicators=IndicatorBundle(ema10=tuple([2000.0] * 5), ema20=tuple([2000.0] * 5), ema60=tuple([2000.0] * 5), atr14=tuple([10.0] * 5),),
        )
        chart_widget.set_frame(frame)
        qtbot.wait(100)

        chart_widget.set_decision({
            "order_type": "限价单",
            "order_direction": "做空",
            "entry_price": 2000.0,
            "take_profit_price": 1980.0,
            "stop_loss_price": 2010.0,
        })
        qtbot.wait(100)

        assert len(chart_widget._direction_items) >= 1

    def test_reset_clears_all_lines(self, chart_widget):
        """reset() must remove all InfiniteLine items."""
        trading_decision = {
            "order_type": "市价单",
            "order_direction": "做空",
            "entry_price": 1850.0,
            "take_profit_price": 1830.0,
            "stop_loss_price": 1870.0,
            "reasoning": "下降趋势。",
        }
        chart_widget.set_decision(trading_decision)
        chart_widget.reset()

        assert _count_infinite_lines(chart_widget) == 0, (
            "Expected no InfiniteLine items after reset(), "
            f"but found {_count_infinite_lines(chart_widget)}."
        )

    def test_clear_decision_overlay_keeps_lines_gone(self, chart_widget):
        """clear_decision_overlay() removes trade lines without requiring reset()."""
        chart_widget.set_decision({
            "order_type": "限价单",
            "order_direction": "做多",
            "entry_price": 1900.0,
            "take_profit_price": 1920.0,
            "stop_loss_price": 1880.0,
        })
        chart_widget.clear_decision_overlay()
        assert _count_infinite_lines(chart_widget) == 0
        assert chart_widget._pending_decision is None

    def test_support_resistance_levels_render_in_price_plot(self, chart_widget):
        """Support/resistance structure levels are drawn as price-pane lines."""
        from pa_agent.gui.support_resistance import StructureLevel

        chart_widget.set_structure_levels(
            [
                StructureLevel("support", 4220.0, 4230.0),
                StructureLevel("resistance", 4306.0, 4306.0),
            ]
        )

        assert _count_structure_lines(chart_widget) == 3
        assert all(
            item in chart_widget._price_plot.items
            for item in chart_widget._structure_items
        )
        assert not any(
            item in chart_widget._volume_plot.items
            for item in chart_widget._structure_items
        )

    def test_reset_clears_support_resistance_levels(self, chart_widget):
        """reset() must remove support/resistance overlays."""
        from pa_agent.gui.support_resistance import StructureLevel

        chart_widget.set_structure_levels(
            [StructureLevel("support", 4220.0, 4230.0)]
        )
        chart_widget.reset()

        assert chart_widget._structure_items == []
        assert chart_widget._structure_levels == []

    def test_support_resistance_levels_are_filtered_by_current_price(self, chart_widget, qtbot):
        """Out-of-scale text numbers must not become chart structure lines."""
        from pa_agent.data.base import KlineBar, KlineFrame, IndicatorBundle
        from pa_agent.gui.support_resistance import StructureLevel

        bars = tuple(
            KlineBar(
                seq=i + 1,
                ts_open=1_700_000_000_000 - i * 3_600_000,
                open=1205.0,
                high=1220.0,
                low=1198.0,
                close=1208.0,
                volume=100.0,
                closed=True,
            )
            for i in range(5)
        )
        frame = KlineFrame(
            symbol="SA2609",
            timeframe="1h",
            bars=bars,
            snapshot_ts_local_ms=1_700_000_000_000,
            indicators=IndicatorBundle(
                ema10=tuple([1205.0] * 5),
                ema20=tuple([1205.0] * 5),
                ema60=tuple([1205.0] * 5),
                atr14=tuple([10.0] * 5),
            ),
        )
        chart_widget.set_frame_now(frame)

        chart_widget.set_structure_levels(
            [
                StructureLevel("support", 1205.0, 1205.0),
                StructureLevel("support", 55.0, 60.0),
                StructureLevel("resistance", 20.0, 20.0),
            ]
        )

        assert [
            (level.kind, level.low, level.high)
            for level in chart_widget._structure_levels
        ] == [("support", 1205.0, 1205.0)]
