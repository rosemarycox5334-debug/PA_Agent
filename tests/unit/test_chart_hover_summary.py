"""Tests for GUI chart hover summary linkage."""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")


@pytest.fixture(scope="module")
def qapp():
    """Shared QApplication for widget tests."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_chart_widget_formats_hover_summary(qapp):
    """Hover summary uses AI-facing K numbering plus OHLC context."""
    from pa_agent.data.base import KlineBar
    from pa_agent.gui.chart_widget import ChartWidget

    widget = ChartWidget()
    bar = KlineBar(
        seq=1,
        ts_open=1_700_000_000_000,
        open=1200.0,
        high=1210.0,
        low=1195.0,
        close=1208.0,
        volume=1000.0,
        closed=True,
    )

    summary = widget._format_hover_summary(bar)

    assert "K1" in summary
    assert "O 1200.00" in summary
    assert "C 1208.00" in summary
    assert "Vol 1,000" in summary


def test_chart_panel_shows_hover_summary_and_restores_hint(qapp):
    """ChartPanel footer switches to hovered bar context, then restores the hint."""
    from pa_agent.gui.widgets.chart_panel import ChartPanel

    panel = ChartPanel()

    panel._on_bar_hovered("K1 · 06-02 21:45 · O 1200.00 / C 1208.00")
    assert "K1" in panel._footer_left.text()

    panel._on_bar_hovered("")
    assert "滚轮缩放" in panel._footer_left.text()
