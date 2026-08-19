"""Tests for hover and selected K-line details."""
from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QPointF, Qt

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.data.datetime_ts import datetime_to_ts_ms
from pa_agent.gui.chart_widget import ChartWidget
from pa_agent.gui.widgets.chart_panel import ChartPanel


def _bar(
    day: int,
    *,
    seq: int,
    open_price: float,
    close: float,
    pct_chg: float | None = None,
) -> KlineBar:
    return KlineBar(
        seq=seq,
        ts_open=float(datetime_to_ts_ms(datetime(2025, 6, day, 9, 30))),
        open=open_price,
        high=max(open_price, close) + 1.0,
        low=min(open_price, close) - 1.0,
        close=close,
        volume=12345.0,
        amount=67890.0,
        pct_chg=pct_chg,
    )


def _frame() -> KlineFrame:
    newest = _bar(8, seq=1, open_price=11.0, close=12.0, pct_chg=2.5)
    oldest = _bar(7, seq=2, open_price=10.0, close=9.5)
    return KlineFrame(
        symbol="600519",
        timeframe="15m",
        bars=(newest, oldest),
        indicators=IndicatorBundle(
            ema20=(float("nan"), float("nan")),
            atr14=(float("nan"), float("nan")),
        ),
        snapshot_ts_local_ms=int(newest.ts_open),
    )


def test_hover_details_show_ohlcv_and_emit_footer_summary(qtbot) -> None:
    chart = ChartWidget()
    qtbot.addWidget(chart)
    summaries: list[str] = []
    chart.bar_hovered.connect(summaries.append)
    chart.set_frame_now(_frame())

    chart._show_bar_details(0, 9.75)

    assert chart._hover_detail.isVisible()
    assert chart._hover_vline.isVisible()
    assert chart._hover_hline.isVisible()
    assert chart._hover_timestamp == chart._bars_by_x[0].ts_open
    assert "06-07 09:30" in summaries[-1]
    assert "开 10" in summaries[-1]
    assert "高 11" in summaries[-1]
    assert "低 8.5" in summaries[-1]
    assert "收 9.5" in summaries[-1]
    assert "涨跌 -5.00%" in summaries[-1]
    assert "量 12,345.00" in summaries[-1]


def test_nearest_bar_hit_testing_rejects_outside_chart(qtbot) -> None:
    chart = ChartWidget()
    qtbot.addWidget(chart)
    chart.set_frame_now(_frame())

    assert chart._bar_index_at_x(0.49) == 0
    assert chart._bar_index_at_x(0.51) == 1
    assert chart._bar_index_at_x(-0.51) is None
    assert chart._bar_index_at_x(1.51) is None


def test_locked_bar_is_restored_by_timestamp_after_refresh(qtbot) -> None:
    chart = ChartWidget()
    qtbot.addWidget(chart)
    chart.set_frame_now(_frame())
    chart._hover_locked = True
    chart._show_bar_details(0, 9.75)
    selected_ts = chart._hover_timestamp

    chart.set_frame_now(_frame())

    assert chart._hover_locked is True
    assert chart._hover_timestamp == selected_ts
    assert chart._hover_index == 0
    assert chart._hover_detail.isVisible()


def test_reset_clears_hover_and_selection(qtbot) -> None:
    chart = ChartWidget()
    qtbot.addWidget(chart)
    chart.set_frame_now(_frame())
    chart._hover_locked = True
    chart._show_bar_details(1, 12.0)

    chart.reset()

    assert chart._hover_locked is False
    assert chart._hover_timestamp is None
    assert not chart._hover_detail.isVisible()


def test_click_same_bar_locks_then_unlocks_details(qtbot) -> None:
    chart = ChartWidget()
    qtbot.addWidget(chart)
    chart.resize(800, 500)
    chart.show()
    chart.set_frame_now(_frame(), fit_view=True)
    scene_pos = chart.getViewBox().mapViewToScene(QPointF(0.0, 9.5))

    class _Click:
        @staticmethod
        def button():
            return Qt.MouseButton.LeftButton

        @staticmethod
        def scenePos():  # noqa: N802
            return scene_pos

    chart._on_scene_mouse_clicked(_Click())
    assert chart._hover_locked is True
    assert chart._hover_index == 0

    chart._on_scene_mouse_clicked(_Click())
    assert chart._hover_locked is False
    assert chart._hover_index == 0


def test_chart_panel_footer_receives_hover_summary(qtbot) -> None:
    panel = ChartPanel()
    qtbot.addWidget(panel)
    chart = panel.chart_widget()
    chart.set_frame_now(_frame())

    chart._show_bar_details(1, 12.0)

    assert "06-08 09:30" in panel._footer_left.text()
    assert "收 12" in panel._footer_left.text()
