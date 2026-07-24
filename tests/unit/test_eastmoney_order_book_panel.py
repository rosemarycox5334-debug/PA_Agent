from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from pa_agent.data.base import IndicatorBundle, KlineFrame
from pa_agent.data.eastmoney_quote import OrderBookLevel, StockOrderBook, TickTrade
from pa_agent.gui.main_window import MainWindow
from pa_agent.gui.widgets.eastmoney_order_book import EastMoneyOrderBookPanel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _book() -> StockOrderBook:
    return StockOrderBook(
        code="600519",
        name="贵州茅台",
        price=1450.5,
        pct_chg=1.25,
        open=1435.0,
        high=1460.0,
        low=1430.0,
        prev_close=1432.59,
        volume=100_000,
        amount=145_000_000.0,
        bids=[
            OrderBookLevel(1450.4, 120),
            OrderBookLevel(1450.3, 80),
        ],
        asks=[
            OrderBookLevel(1450.6, 50),
            OrderBookLevel(1450.7, 30),
        ],
    )


def test_order_book_panel_renders_both_sides(qapp):
    panel = EastMoneyOrderBookPanel()

    panel.set_order_book(_book())

    assert "贵州茅台 600519" in panel._quote_label.text()
    assert panel._bid_rows[0][1].text() == "1450.4"
    assert panel._bid_rows[0][2].text() == "120手"
    assert panel._ask_rows[0][1].text() == "1450.6"
    assert panel._ask_rows[0][2].text() == "50手"
    assert "买盘 200手" in panel._summary_label.text()
    assert "卖盘 80手" in panel._summary_label.text()
    assert "委比 +42.9%" in panel._summary_label.text()


def test_order_book_panel_clears_missing_depth(qapp):
    panel = EastMoneyOrderBookPanel()
    panel.set_order_book(_book())

    panel.set_order_book(None)

    assert panel._quote_label.text() == "暂无盘口数据"
    assert panel._bid_rows[0][1].text() == "—"
    assert panel._ask_rows[0][1].text() == "—"


def test_order_book_panel_renders_recent_trades_newest_first(qapp):
    panel = EastMoneyOrderBookPanel()
    trades = [
        TickTrade("14:59:57", 1450.4, 12, "卖"),
        TickTrade("14:59:58", 1450.5, 20, "买"),
    ]

    panel.set_market_data(_book(), trades)

    assert panel._trade_count_label.text() == "最近 2 笔"
    assert panel._trade_table.item(0, 0).text() == "14:59:58"
    assert panel._trade_table.item(0, 1).text() == "1450.5"
    assert panel._trade_table.item(0, 2).text() == "20"
    assert panel._trade_table.item(0, 3).text() == "主买"
    assert panel._trade_table.item(1, 3).text() == "主卖"

    panel.clear()
    assert panel._trade_table.rowCount() == 0
    assert panel._trade_count_label.text() == "暂无数据"


def test_order_book_visibility_is_limited_to_eastmoney():
    class _Panel:
        visible = False
        cleared = False

        def setVisible(self, visible):
            self.visible = visible

        def clear(self):
            self.cleared = True

    class _Toggle:
        def __init__(self, checked=True):
            self.checked = checked
            self.visible = False
            self.enabled = False
            self.text = ""

        def isChecked(self):
            return self.checked

        def setVisible(self, visible):
            self.visible = visible

        def setEnabled(self, enabled):
            self.enabled = enabled

        def setText(self, text):
            self.text = text

    class _Host:
        def __init__(self, kind, *, checked=True):
            self.kind = kind
            self._demo_mode = False
            self._eastmoney_order_book_panel = _Panel()
            self._eastmoney_market_panel_toggle = _Toggle(checked)

        def _current_data_source_kind(self):
            return self.kind

    eastmoney = _Host("eastmoney")
    MainWindow._sync_eastmoney_order_book_visibility(eastmoney)
    assert eastmoney._eastmoney_order_book_panel.visible is True
    assert eastmoney._eastmoney_market_panel_toggle.visible is True
    assert eastmoney._eastmoney_market_panel_toggle.enabled is True
    assert eastmoney._eastmoney_market_panel_toggle.text == "隐藏盘口/成交"

    hidden = _Host("eastmoney", checked=False)
    MainWindow._sync_eastmoney_order_book_visibility(hidden)
    assert hidden._eastmoney_order_book_panel.visible is False
    assert hidden._eastmoney_order_book_panel.cleared is False
    assert hidden._eastmoney_market_panel_toggle.text == "显示盘口/成交"

    akshare = _Host("akshare")
    MainWindow._sync_eastmoney_order_book_visibility(akshare)
    assert akshare._eastmoney_order_book_panel.visible is False
    assert akshare._eastmoney_order_book_panel.cleared is True
    assert akshare._eastmoney_market_panel_toggle.visible is False
    assert akshare._eastmoney_market_panel_toggle.enabled is False


def test_market_panel_toggle_resyncs_visibility():
    class _Host:
        calls = 0

        def _sync_eastmoney_order_book_visibility(self):
            self.calls += 1

    host = _Host()
    MainWindow._on_eastmoney_market_panel_toggled(host, False)
    assert host.calls == 1


def test_analysis_snapshot_attaches_context_only_for_eastmoney():
    context = {"provider": "eastmoney", "bids": [{"price": 10, "volume_lots": 1}]}
    frame = KlineFrame(
        symbol="600519",
        timeframe="15m",
        bars=(),
        indicators=IndicatorBundle(ema20=(), atr14=()),
        snapshot_ts_local_ms=1,
    )

    class _Host:
        def __init__(self, kind):
            self.kind = kind
            self._ctx = SimpleNamespace(
                data_source=SimpleNamespace(
                    latest_market_context=lambda: context,
                )
            )

        def _current_data_source_kind(self):
            return self.kind

    eastmoney_frame = MainWindow._attach_analysis_market_context(
        _Host("eastmoney"),
        frame,
    )
    assert eastmoney_frame.market_context == context
    assert eastmoney_frame is not frame

    akshare_frame = MainWindow._attach_analysis_market_context(
        _Host("akshare"),
        frame,
    )
    assert akshare_frame is frame
    assert akshare_frame.market_context is None
