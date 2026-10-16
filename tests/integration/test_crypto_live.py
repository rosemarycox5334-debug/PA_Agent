"""Live smoke tests for the Binance / OKX public market-data sources.

Opt-in: ``pytest -m live tests/integration/test_crypto_live.py``.

Binance is reachable from most networks via ``data-api.binance.vision``; okx.com
is blocked in some regions (mainland China), so the OKX tests skip instead of
failing when the venue cannot be reached. Set ``HTTPS_PROXY`` to route through a
proxy (httpx honours it).
"""

from __future__ import annotations

import pytest

from pa_agent.data.base import DataSourceError, DataSourceTransientError
from pa_agent.data.binance_source import BinanceSource
from pa_agent.data.okx_source import OkxSource

pytestmark = [pytest.mark.live, pytest.mark.integration]


def _connected(source):
    try:
        source.connect()
    except DataSourceTransientError as exc:
        pytest.skip(f"venue unreachable from this machine: {exc}")
    return source


def test_live_binance_btcusdt_15m() -> None:
    source = _connected(BinanceSource())
    source.subscribe("BTCUSDT", "15m")

    bars = source.latest_snapshot(30)

    assert len(bars) >= 20
    assert all(b.close > 0 for b in bars)
    assert all(b.high >= b.low for b in bars)
    assert all(b.low <= b.close <= b.high for b in bars)
    # Newest first, strictly decreasing timestamps.
    stamps = [b.ts_open for b in bars]
    assert stamps == sorted(stamps, reverse=True)
    # The head bar is the in-progress candle → seq 0.
    assert bars[0].seq == 0


def test_live_binance_perp_btcusdt_15m() -> None:
    source = _connected(BinanceSource())
    source.subscribe("BTCUSDT.perp", "15m")

    bars = source.latest_snapshot(20)

    assert len(bars) >= 10
    assert all(b.close > 0 for b in bars)


def test_live_binance_deep_history_paginates() -> None:
    source = _connected(BinanceSource())
    source.subscribe("BTCUSDT", "1h")

    bars = source.latest_snapshot(1200)  # forces a second page (>1000 cap)

    assert len(bars) >= 1000
    stamps = [b.ts_open for b in bars]
    assert stamps == sorted(stamps, reverse=True)


def test_live_binance_invalid_symbol_is_permanent_error() -> None:
    source = _connected(BinanceSource())
    source.subscribe("NOTAREALPAIR", "15m")

    with pytest.raises(DataSourceError):
        source.latest_snapshot(5)


def test_live_okx_btc_usdt_15m() -> None:
    source = _connected(OkxSource())
    source.subscribe("BTC-USDT", "15m")

    bars = source.latest_snapshot(30)

    assert len(bars) >= 20
    assert all(b.high >= b.low for b in bars)
    assert bars[0].ts_open == max(b.ts_open for b in bars)
