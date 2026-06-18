from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from pa_agent.config.settings import Settings
from pa_agent.data.base import DataSourceTransientError
from pa_agent.data.tushare_source import (
    TushareSource,
    display_tushare_symbol,
    normalize_tushare_symbol,
)


def test_normalize_tushare_symbol() -> None:
    assert normalize_tushare_symbol("600519") == "600519.SH"
    assert normalize_tushare_symbol("000001") == "000001.SZ"
    assert normalize_tushare_symbol("300750") == "300750.SZ"
    assert normalize_tushare_symbol("688981") == "688981.SH"
    assert normalize_tushare_symbol("430047") == "430047.BJ"
    assert normalize_tushare_symbol("600519.SH") == "600519.SH"


def test_display_tushare_symbol() -> None:
    assert display_tushare_symbol("600519.SH") == "600519"


def test_supported_timeframes_include_minutes() -> None:
    assert TushareSource().supported_timeframes() == ["1m", "5m", "15m", "30m", "1h", "1d"]


def test_connect_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    source = TushareSource()

    with pytest.raises(DataSourceTransientError, match="TUSHARE_TOKEN"):
        source.connect()


def test_connect_uses_settings_token_before_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    fake_tushare = types.SimpleNamespace(set_token=lambda token: calls.append(token))
    monkeypatch.setitem(sys.modules, "tushare", fake_tushare)
    monkeypatch.setenv("TUSHARE_TOKEN", "env-token")

    source = TushareSource(settings=Settings(tushare={"token": "settings-token"}))
    source.connect()

    assert calls == ["settings-token"]


def test_latest_snapshot_fetches_daily_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_set_token(token: str) -> None:
        calls.append({"set_token": token})

    def fake_pro_bar(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "trade_date": "20240102",
                    "open": 8.0,
                    "high": 9.0,
                    "low": 7.5,
                    "close": 8.5,
                    "vol": 10,
                },
                {
                    "trade_date": "20240103",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "vol": 123.4,
                },
            ]
        )

    fake_tushare = types.SimpleNamespace(set_token=fake_set_token, pro_bar=fake_pro_bar)
    monkeypatch.setitem(sys.modules, "tushare", fake_tushare)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")

    source = TushareSource()
    source.connect()
    source.subscribe("600519", "1d")

    bars = source.latest_snapshot(2)

    assert [b.close for b in bars] == [10.5, 8.5]
    assert all(b.closed for b in bars)
    assert calls[1]["ts_code"] == "600519.SH"
    assert calls[1]["adj"] == "qfq"


def test_latest_snapshot_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch_count = 0

    def fake_pro_bar(**kwargs):
        nonlocal fetch_count
        fetch_count += 1
        return pd.DataFrame(
            [
                {
                    "trade_date": "20240103",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "vol": 123,
                },
            ]
        )

    fake_tushare = types.SimpleNamespace(set_token=lambda token: None, pro_bar=fake_pro_bar)
    monkeypatch.setitem(sys.modules, "tushare", fake_tushare)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")

    source = TushareSource()
    source.connect()
    source.subscribe("600519", "1d")

    assert source.latest_snapshot(1)[0].close == 10.5
    assert source.latest_snapshot(1)[0].close == 10.5
    assert fetch_count == 1


@pytest.mark.parametrize(
    ("timeframe", "expected_freq"),
    [("1m", "1min"), ("5m", "5min"), ("1h", "60min")],
)
def test_latest_snapshot_fetches_minute_bars(
    monkeypatch: pytest.MonkeyPatch,
    timeframe: str,
    expected_freq: str,
) -> None:
    calls: list[dict] = []

    class FakeApi:
        def stk_mins(self, **kwargs):
            calls.append(kwargs)
            return pd.DataFrame(
                [
                    {
                        "trade_time": "2024-01-03 09:31:00",
                        "open": 10.0,
                        "high": 10.8,
                        "low": 9.9,
                        "close": 10.5,
                        "vol": 100,
                    },
                    {
                        "trade_time": "2024-01-03 10:31:00",
                        "open": 11.0,
                        "high": 11.8,
                        "low": 10.9,
                        "close": 11.5,
                        "vol": 200,
                    },
                ]
            )

    fake_tushare = types.SimpleNamespace(
        set_token=lambda token: None,
        pro_api=lambda token=None: FakeApi(),
    )
    monkeypatch.setitem(sys.modules, "tushare", fake_tushare)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")

    source = TushareSource()
    source.connect()
    source.subscribe("600519", timeframe)
    bars = source.latest_snapshot(2)

    assert calls[0]["ts_code"] == "600519.SH"
    assert calls[0]["freq"] == expected_freq
    assert [b.close for b in bars] == [11.5, 10.5]
    assert bars[0].ts_open > bars[1].ts_open


def test_minute_rate_limit_error_is_user_facing(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeApi:
        def stk_mins(self, **kwargs):
            raise Exception("抱歉，您访问接口(stk_mins)频率超限(1次/小时)")

    fake_tushare = types.SimpleNamespace(
        set_token=lambda token: None,
        pro_api=lambda token=None: FakeApi(),
    )
    monkeypatch.setitem(sys.modules, "tushare", fake_tushare)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")

    source = TushareSource()
    source.connect()
    source.subscribe("600519", "5m")

    with pytest.raises(DataSourceTransientError, match="限频"):
        source.latest_snapshot(2)
