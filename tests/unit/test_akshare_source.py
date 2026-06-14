"""Unit tests for AkShare data source helpers (no network)."""
from __future__ import annotations

import sys
from types import SimpleNamespace

from pa_agent.data.akshare_source import (
    AkShareSource,
    _fetch_history_eastmoney_browser,
    _resample_rows_to_4h,
    is_futures_symbol,
    is_index_symbol,
    normalize_ashare_symbol,
    normalize_akshare_symbol,
    normalize_futures_symbol,
)


def test_normalize_ashare_symbol_stock():
    assert normalize_ashare_symbol("600519") == "600519"
    assert normalize_ashare_symbol("sh600519") == "600519"
    assert normalize_ashare_symbol("JD2607") == ""
    assert normalize_ashare_symbol("2607") == ""


def test_normalize_akshare_symbol_futures():
    assert normalize_futures_symbol("jd2607") == "JD2607"
    assert normalize_akshare_symbol("JD2607") == "JD2607"
    assert is_futures_symbol("AG2607") is True
    assert is_futures_symbol("2607") is False


def test_normalize_ashare_symbol_index():
    assert normalize_ashare_symbol("sh000300") == "sh000300"
    assert normalize_ashare_symbol("000300") == "000300"


def test_is_index_symbol():
    assert is_index_symbol("000300") is True
    assert is_index_symbol("600519") is False
    assert is_index_symbol("000001") is False


def test_resample_60m_to_4h():
    rows = [
        {"ts_open": i, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1.0}
        for i in range(8)
    ]
    out = _resample_rows_to_4h(rows)
    assert len(out) == 2
    assert out[0]["open"] == 10.0
    assert out[0]["close"] == rows[3]["close"]
    assert out[0]["volume"] == 4.0


def test_subscribe_rejects_non_ashare_symbol():
    source = AkShareSource()

    try:
        source.subscribe("2607", "1h")
    except ValueError as exc:
        assert "6" in str(exc)
    else:
        raise AssertionError("subscribe should reject non-A-share symbols")


def test_subscribe_accepts_futures_symbol():
    source = AkShareSource()

    source.subscribe("jd2607", "1h")

    assert source._symbol == "JD2607"


def test_symbol_available_accepts_futures_symbol():
    source = AkShareSource()

    assert source.is_symbol_available("JD2607") is True
    assert source.is_symbol_available("2607") is False


def test_eastmoney_browser_fallback_parses_klines(monkeypatch):
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "klines": [
                        "2026-06-01 10:30,10,11,12,9,100",
                        "2026-06-01 11:30,11,12,13,10,200",
                    ]
                }
            }

    calls: list[dict] = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    fake_curl = SimpleNamespace(requests=SimpleNamespace(get=fake_get))
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_curl)

    rows = _fetch_history_eastmoney_browser("600519", "1h", 2)

    assert len(rows) == 2
    assert rows[0]["open"] == 10.0
    assert rows[1]["close"] == 12.0
    assert calls[0]["params"]["secid"] == "1.600519"
    assert calls[0]["proxies"] == {}
