"""Unit tests for the built-in East Money data source."""
from __future__ import annotations

from types import SimpleNamespace

import pa_agent.data.eastmoney_source as eastmoney_source
from pa_agent.data.eastmoney_source import EastMoneySource


def test_intraday_spot_refresh_uses_module_level_fetch(monkeypatch):
    source = EastMoneySource()
    source._symbol = "600519"
    source._timeframe = "15m"
    rows = [
        {
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 100.0,
        }
    ]

    monkeypatch.setattr(eastmoney_source, "_ashare_session_open", lambda: True)
    monkeypatch.setattr(eastmoney_source, "fetch_spot_price", lambda _symbol: 12.0)

    source._apply_spot_to_forming(rows)

    assert rows[-1]["close"] == 12.0
    assert rows[-1]["high"] == 12.0


def test_intraday_spot_refresh_prefers_cached_order_book(monkeypatch):
    source = EastMoneySource()
    source._symbol = "600519"
    source._timeframe = "15m"
    source._latest_order_book = SimpleNamespace(price=12.5)
    rows = [
        {
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 100.0,
        }
    ]

    monkeypatch.setattr(eastmoney_source, "_ashare_session_open", lambda: True)

    def _unexpected_fetch(_symbol):
        raise AssertionError("cached order book price should be reused")

    monkeypatch.setattr(eastmoney_source, "fetch_spot_price", _unexpected_fetch)

    source._apply_spot_to_forming(rows)

    assert rows[-1]["close"] == 12.5
    assert source.latest_order_book().price == 12.5


def test_subscribe_clears_order_book_when_symbol_changes():
    source = EastMoneySource()
    source._symbol = "600519"
    source._timeframe = "15m"
    source._latest_order_book = object()
    source._latest_trades = [object()]

    source.subscribe("000001", "15m")

    assert source.latest_order_book() is None
    assert source.latest_trades() == []


def test_latest_market_context_serializes_both_sides():
    source = EastMoneySource()
    source._symbol = "600519"
    source._latest_order_book_ts_ms = 123456
    source._latest_trades_ts_ms = 123457
    source._latest_order_book = SimpleNamespace(
        code="600519",
        name="贵州茅台",
        price=1450.5,
        pct_chg=1.25,
        bids=[
            SimpleNamespace(price=1450.4, volume=120),
            SimpleNamespace(price=1450.3, volume=80),
        ],
        asks=[
            SimpleNamespace(price=1450.6, volume=50),
            SimpleNamespace(price=1450.7, volume=30),
        ],
        depth_levels=5,
        depth_source="push2_free",
    )
    source._latest_trades = [
        SimpleNamespace(
            time="14:59:57",
            price=1450.5,
            volume=12,
            side_hint="买",
        ),
        SimpleNamespace(
            time="14:59:58",
            price=1450.4,
            volume=7,
            side_hint="卖",
        ),
        SimpleNamespace(
            time="14:59:59",
            price=1450.5,
            volume=3,
            side_hint="中性",
        ),
    ]

    context = source.latest_market_context()

    assert context is not None
    assert context["provider"] == "eastmoney"
    assert context["snapshot_ts_ms"] == 123457
    assert context["trades_snapshot_ts_ms"] == 123457
    assert context["bids"][0] == {
        "level": 1,
        "price": 1450.4,
        "volume_lots": 120,
    }
    assert context["asks"][0]["price"] == 1450.6
    assert context["bid_total_lots"] == 200
    assert context["ask_total_lots"] == 80
    assert context["order_imbalance_pct"] == 42.86
    assert context["recent_trades"][0] == {
        "time": "14:59:57",
        "price": 1450.5,
        "volume_lots": 12,
        "side": "买",
    }
    assert context["trade_count"] == 3
    assert context["active_buy_lots"] == 12
    assert context["active_sell_lots"] == 7
    assert context["neutral_trade_lots"] == 3
    assert context["active_net_lots"] == 5
