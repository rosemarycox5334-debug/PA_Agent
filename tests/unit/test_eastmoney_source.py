"""Unit tests for the built-in East Money data source."""
from __future__ import annotations

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
