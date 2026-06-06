"""ChartWidget auto-fit view range after symbol/timeframe or demo load."""
from __future__ import annotations

import math

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")


def _sample_frame(*, n: int = 5):
    from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame

    bars = tuple(
        KlineBar(
            seq=i + 1,
            ts_open=1_700_000_000_000 - i * 3_600_000,
            open=2000.0 + i,
            high=2010.0 + i,
            low=1990.0 + i,
            close=2005.0 + i,
            volume=100.0,
            closed=True,
        )
        for i in range(n)
    )
    return KlineFrame(
        symbol="XAUUSD",
        timeframe="15m",
        bars=bars,
        snapshot_ts_local_ms=1_700_000_000_000,
        indicators=IndicatorBundle(
            ema10=tuple(2000.0 + i for i in range(n)), ema20=tuple(2000.0 + i for i in range(n)), ema60=tuple(2000.0 + i for i in range(n)),
            atr14=tuple([10.0] * n),
        ),
    )


@pytest.fixture
def chart_widget(qtbot):
    from pa_agent.gui.chart_widget import ChartWidget

    widget = ChartWidget()
    qtbot.addWidget(widget)
    return widget


class TestChartFitView:
    def test_view_ranges_show_newest_twenty_when_many_bars(self, chart_widget):
        from pa_agent.gui.chart_widget import _FIT_VISIBLE_BARS, _X_MARGIN_BARS

        frame = _sample_frame(n=50)
        x_range, _ = chart_widget._view_ranges_for_frame(frame)
        assert x_range[0] == pytest.approx(50 - _FIT_VISIBLE_BARS - _X_MARGIN_BARS)
        assert x_range[1] == pytest.approx(49 + _X_MARGIN_BARS)

    def test_view_ranges_include_all_bars_when_fewer_than_twenty(self, chart_widget):
        frame = _sample_frame(n=8)
        x_range, y_range = chart_widget._view_ranges_for_frame(frame)
        assert x_range[0] < 0
        assert x_range[1] > 7
        assert y_range[0] < min(b.low for b in frame.bars)
        assert y_range[1] > max(b.high for b in frame.bars)

    def test_fit_view_after_set_frame_now(self, chart_widget, qtbot):
        frame = _sample_frame(n=6)
        chart_widget.set_frame_now(frame, fit_view=True)
        x_range, y_range = chart_widget.getViewBox().viewRange()
        exp_x, exp_y = chart_widget._view_ranges_for_frame(frame)
        assert math.isclose(x_range[0], exp_x[0], rel_tol=1e-6)
        assert math.isclose(x_range[1], exp_x[1], rel_tol=1e-6)
        assert math.isclose(y_range[0], exp_y[0], rel_tol=1e-6)
        assert math.isclose(y_range[1], exp_y[1], rel_tol=1e-6)

    def test_request_fit_on_next_render_via_timer(self, chart_widget, qtbot):
        frame = _sample_frame(n=4)
        chart_widget.set_frame_now(frame, fit_view=False)
        chart_widget.getViewBox().setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
        chart_widget.request_fit_on_next_render()
        qtbot.wait(80)
        x_range, y_range = chart_widget.getViewBox().viewRange()
        exp_x, exp_y = chart_widget._view_ranges_for_frame(frame)
        assert x_range[1] > 2.5
        assert y_range[1] > exp_y[0]

    def test_fit_includes_trade_overlay_prices(self, chart_widget):
        frame = _sample_frame(n=3)
        chart_widget._pending_decision = {
            "order_type": "限价单",
            "entry_price": 2100.0,
            "take_profit_price": 2150.0,
            "stop_loss_price": 1950.0,
        }
        _, y_range = chart_widget._view_ranges_for_frame(frame)
        assert y_range[0] <= 1950.0
        assert y_range[1] >= 2150.0


class TestFirstFrameAutoFit:
    """On first data load the chart should auto-fit even without fit_view=True."""

    def test_first_set_frame_triggers_fit(self, chart_widget, qtbot):
        assert not chart_widget._first_frame_fitted
        frame = _sample_frame(n=4)
        # set_frame without fit_view=True — should still fit on first data
        chart_widget.set_frame(frame)
        assert chart_widget._fit_on_next_render is True
        qtbot.wait(80)
        x_range, y_range = chart_widget.getViewBox().viewRange()
        exp_x, exp_y = chart_widget._view_ranges_for_frame(frame)
        assert math.isclose(x_range[0], exp_x[0], rel_tol=1e-6)
        assert math.isclose(y_range[0], exp_y[0], rel_tol=1e-6)
        assert chart_widget._first_frame_fitted is True

    def test_second_set_frame_does_not_auto_fit(self, chart_widget, qtbot):
        frame1 = _sample_frame(n=4)
        chart_widget.set_frame(frame1)
        qtbot.wait(80)
        assert chart_widget._first_frame_fitted is True

        # Manually override view range (simulates user pan/zoom)
        chart_widget.getViewBox().setRange(xRange=(0, 1), yRange=(0, 1), padding=0)

        frame2 = _sample_frame(n=10)
        chart_widget.set_frame(frame2)  # no fit_view=True
        assert chart_widget._fit_on_next_render is False

    def test_reset_clears_first_frame_fitted(self, chart_widget, qtbot):
        frame = _sample_frame(n=4)
        chart_widget.set_frame(frame)
        qtbot.wait(80)
        assert chart_widget._first_frame_fitted is True

        chart_widget.reset()
        assert chart_widget._first_frame_fitted is False


class TestResizableAxisItem:
    """viewportEvent-based axis resize."""

    def test_axis_resize_via_viewport_event(self, chart_widget, qtbot):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtWidgets import QApplication

        vp = chart_widget.viewport()
        edge_wx = chart_widget._axis_right_edge_wx()
        initial_w = chart_widget.getPlotItem().getAxis("left").width()

        # Press near axis right edge → should start resize
        me_press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(edge_wx - 2, 300),
                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(vp, me_press)
        assert chart_widget._axis_resizing is True

        # Drag right by 30px
        me_drag = QMouseEvent(QEvent.Type.MouseMove, QPointF(edge_wx + 28, 300),
                              Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                              Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(vp, me_drag)
        qtbot.wait(60)  # let layout engine apply setWidth

        # Release
        me_rel = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(edge_wx + 28, 300),
                              Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                              Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(vp, me_rel)

        final_w = chart_widget.getPlotItem().getAxis("left").width()
        assert final_w == pytest.approx(initial_w + 30, abs=1.0)
        assert chart_widget._axis_resizing is False

    def test_axis_resize_minimum_width(self, chart_widget, qtbot):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtWidgets import QApplication
        from pa_agent.gui.chart_widget import _AXIS_RESIZE_MIN_WIDTH

        vp = chart_widget.viewport()
        edge_wx = chart_widget._axis_right_edge_wx()

        # Press near axis edge
        me_press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(edge_wx - 2, 300),
                                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(vp, me_press)

        # Drag far left
        me_drag = QMouseEvent(QEvent.Type.MouseMove, QPointF(0, 300),
                               Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(vp, me_drag)
        qtbot.wait(60)

        # Release
        me_rel = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(0, 300),
                              Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                              Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(vp, me_rel)

        final_w = chart_widget.getPlotItem().getAxis("left").width()
        assert final_w >= _AXIS_RESIZE_MIN_WIDTH

    def test_viewbox_still_receives_normal_click(self, chart_widget, qtbot):
        """Click in the ViewBox area should NOT be consumed by axis resize."""
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtWidgets import QApplication

        vp = chart_widget.viewport()
        # Click in the center of the chart (far from axis edge)
        me = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(400, 300),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(vp, me)
        # viewportEvent passes through to super(), not consumed by us
        assert chart_widget._axis_resizing is False
