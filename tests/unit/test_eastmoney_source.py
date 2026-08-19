"""Unit tests for the built-in East Money data source."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pa_agent.data.eastmoney_source as eastmoney_source
from pa_agent.data.eastmoney_source import EastMoneySource


def test_date_range_is_inclusive_and_clears_snapshot_cache():
    source = EastMoneySource()
    source._snap_cache_n = 10
    source._snap_cache_bars = [object()]

    source.set_date_range("2025-06-07", "2025-07-01")

    assert source.date_range == (date(2025, 6, 7), date(2025, 7, 1))
    assert source._snap_cache_n == 0
    assert source._snap_cache_bars == []

    rows = [
        {
            "ts_open": datetime(
                2025, 6, 6, 15, tzinfo=eastmoney_source._CN_TZ
            ).timestamp()
            * 1000
        },
        {
            "ts_open": datetime(
                2025, 6, 7, 15, tzinfo=eastmoney_source._CN_TZ
            ).timestamp()
            * 1000
        },
        {
            "ts_open": datetime(
                2025, 7, 1, 15, tzinfo=eastmoney_source._CN_TZ
            ).timestamp()
            * 1000
        },
        {
            "ts_open": datetime(
                2025, 7, 2, 15, tzinfo=eastmoney_source._CN_TZ
            ).timestamp()
            * 1000
        },
    ]
    assert source._filter_rows_by_date(rows) == rows[1:3]


def test_date_range_rejects_reversed_dates():
    source = EastMoneySource()

    try:
        source.set_date_range("2025-07-01", "2025-06-07")
    except ValueError as exc:
        assert "开始日期" in str(exc)
    else:
        raise AssertionError("reversed date range should fail")


def test_daily_date_range_is_forwarded_to_eastmoney(monkeypatch):
    source = EastMoneySource()
    source.set_date_range("2025-06-07", "2025-07-01")
    captured = {}

    def _fetch(symbol, **kwargs):
        captured["symbol"] = symbol
        captured.update(kwargs)
        return []

    monkeypatch.setattr(eastmoney_source, "fetch_stock_period", _fetch)

    assert source._fetch_daily("600519", 100, timeframe="1d") == []
    assert captured == {
        "symbol": "600519",
        "timeframe": "1d",
        "start_date": "20250607",
        "end_date": "20250701",
        "adjust": "qfq",
        "is_index": False,
    }


def test_historical_date_range_marks_newest_bar_closed(monkeypatch):
    source = EastMoneySource()
    source.connect()
    source.subscribe("600519", "1d")
    source.set_date_range("2025-06-07", "2025-07-01")
    ts_ms = (
        datetime(2025, 7, 1, 15, tzinfo=eastmoney_source._CN_TZ).timestamp()
        * 1000
    )
    rows = [
        {
            "ts_open": ts_ms,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 100.0,
        }
    ]
    monkeypatch.setattr(source, "_fetch_history", lambda *_args: rows)
    monkeypatch.setattr(eastmoney_source, "_ashare_head_bar_live", lambda _tf: True)

    bars = source.latest_snapshot(1)

    assert len(bars) == 1
    assert bars[0].closed is True
    assert source.latest_order_book() is None


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
