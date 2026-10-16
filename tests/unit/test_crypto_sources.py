"""Unit tests for the Binance / OKX data sources and their factory wiring.

All tests are offline: HTTP is replaced by an injected transport on
``CryptoRestClient``. The Binance kline payload is a *recorded real response*
(``tests/fixtures/binance_klines_btcusdt_15m.json``); the OKX payloads are
synthetic (okx.com is unreachable from this machine) and follow the documented
schema.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from pa_agent.data.base import DataSourceError, DataSourceTransientError
from pa_agent.data.binance_source import BinanceSource
from pa_agent.data.crypto_rest import CryptoRestClient
from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    create_data_source,
    default_symbol_for_kind,
    subscribe_with_fallback,
)
from pa_agent.data.market_defaults import normalize_gold_symbol_for_kind
from pa_agent.data.okx_source import OkxSource
from pa_agent.data.refresh_policy import effective_refresh_interval_ms

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

#: Wall-clock used by the recorded Binance fixture's "now" (see the fixture file).
FIXTURE_NOW_MS = 1_792_065_899_088

HOST_A = "https://fake-primary.test"
HOST_B = "https://fake-secondary.test"


# ── Fake transports ───────────────────────────────────────────────────────────


class _RouteTransport:
    """Answers from a ``path fragment → payload`` table; can simulate dead hosts."""

    def __init__(self, routes: dict[str, object], *, dead_hosts: tuple[str, ...] = ()) -> None:
        self.routes = routes
        self.dead_hosts = set(dead_hosts)
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float, headers: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append(url)
        if any(url.startswith(host) for host in self.dead_hosts):
            raise ConnectionError("simulated network failure")
        for fragment, payload in self.routes.items():
            if fragment not in url:
                continue
            if isinstance(payload, tuple):
                status, body = payload
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return status, raw
            return 200, json.dumps(payload).encode()
        return 400, b'{"code":-1121,"msg":"Invalid symbol."}'


class _PagedTransport:
    """Serves Binance-style klines pages honouring ``limit`` and ``endTime``."""

    def __init__(self, rows_oldest_first: list[list]) -> None:
        self.rows = rows_oldest_first
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float, headers: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        limit = int(query.get("limit", ["500"])[0])
        end_time = query.get("endTime")
        rows = [r for r in self.rows if end_time is None or int(r[0]) <= int(end_time[0])]
        return 200, json.dumps(rows[-limit:]).encode()


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _binance_source(transport, hosts: tuple[str, ...] = (HOST_A,)) -> BinanceSource:
    """BinanceSource wired to a fake transport (both market host sets faked)."""
    client = CryptoRestClient(hosts, transport=transport, timeout_s=5.0)
    return BinanceSource(client=client, spot_hosts=hosts, perp_hosts=hosts)


# ── Binance: recorded real response ───────────────────────────────────────────


@pytest.mark.unit
def test_binance_snapshot_parses_recorded_response(monkeypatch: pytest.MonkeyPatch) -> None:
    klines = _load("binance_klines_btcusdt_15m.json")
    transport = _RouteTransport(
        {"/api/v3/klines": klines, "/api/v3/time": {"serverTime": FIXTURE_NOW_MS - 300}}
    )
    source = _binance_source(transport)
    source.connect()
    source.subscribe("BTC/USDT", "15m")  # slashed input must normalise
    monkeypatch.setattr(source, "_now_ms", lambda: FIXTURE_NOW_MS)

    bars = source.latest_snapshot(6)

    assert len(bars) == 6
    # Newest first, all timestamps from the recorded payload.
    assert [b.ts_open for b in bars] == sorted([b.ts_open for b in bars], reverse=True)
    assert bars[0].ts_open == 1_792_065_600_000
    assert bars[-1].ts_open == 1_792_061_100_000
    # Head bar is still forming → seq 0; the rest are numbered 1..N.
    assert bars[0].closed is False and bars[0].seq == 0
    assert bars[1].closed is True and bars[1].seq == 1
    assert bars[-1].seq == 5
    # Volume = base asset, amount = quote turnover (Binance field 7).
    assert bars[0].volume == pytest.approx(103.52741)
    assert bars[0].amount == pytest.approx(8373928.53360960)
    # Invariants enforced by normalize_kline_bar.
    assert all(b.high >= b.low for b in bars)
    assert all(b.low <= b.close <= b.high for b in bars)


@pytest.mark.unit
def test_binance_bar_closed_flag_follows_close_time() -> None:
    """A quiet market can return an already-closed head bar; it must be numbered."""
    klines = _load("binance_klines_btcusdt_15m.json")
    transport = _RouteTransport({"/api/v3/klines": klines, "/api/v3/time": {"serverTime": 1}})
    source = _binance_source(transport)
    source.connect()
    source.subscribe("BTCUSDT", "15m")
    # "now" far after the newest closeTime → nothing is forming.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(source, "_now_ms", lambda: FIXTURE_NOW_MS + 10_000_000)
    try:
        bars = source.latest_snapshot(3)
    finally:
        monkeypatch.undo()
    assert all(b.closed for b in bars)
    assert [b.seq for b in bars] == [1, 2, 3]


@pytest.mark.unit
def test_binance_ignores_implausible_server_time() -> None:
    """A bogus serverTime must not make every bar look unclosed."""
    transport = _RouteTransport(
        {
            "/api/v3/klines": _load("binance_klines_btcusdt_15m.json"),
            "/api/v3/time": {"serverTime": 1},
        }
    )
    source = _binance_source(transport)
    source.connect()

    assert source.server_time_ms() is None  # offset rejected as implausible


@pytest.mark.unit
def test_binance_paginates_deep_history() -> None:
    rows = [
        [
            1_700_000_000_000 + i * 60_000,
            "1.0",
            "2.0",
            "0.5",
            "1.5",
            "10.0",
            1_700_000_059_999 + i * 60_000,
            "15.0",
            5,
            "4.0",
            "6.0",
            "0",
        ]
        for i in range(1200)
    ]
    transport = _PagedTransport(rows)
    source = _binance_source(transport)
    source._connected = True
    source.subscribe("BTCUSDT", "1m")

    bars = source.latest_snapshot(2500)  # more than a single page (1000 cap)

    kline_calls = [call for call in transport.calls if "klines" in call]
    assert len(bars) == 1200
    assert len(kline_calls) == 2, transport.calls
    assert "endTime=" in kline_calls[1]
    assert bars[0].ts_open == rows[-1][0]


@pytest.mark.unit
def test_binance_perp_uses_futures_endpoint() -> None:
    transport = _RouteTransport(
        {
            "/fapi/v1/klines": _load("binance_klines_btcusdt_15m.json"),
            "/fapi/v1/time": {"serverTime": FIXTURE_NOW_MS},
        }
    )
    source = _binance_source(transport)
    source._connected = True
    source.subscribe("BTCUSDT.perp", "15m")

    source.latest_snapshot(2)

    kline_calls = [call for call in transport.calls if "klines" in call]
    assert kline_calls and all("/fapi/v1/klines" in call for call in kline_calls)
    assert not any("/api/v3/klines" in call for call in transport.calls)


@pytest.mark.unit
def test_binance_rejects_unsupported_timeframe_and_symbol() -> None:
    source = _binance_source(_RouteTransport({}))
    with pytest.raises(ValueError):
        source.subscribe("BTCUSDT", "7m")
    with pytest.raises(ValueError):
        source.subscribe("垃圾代码", "15m")


@pytest.mark.unit
def test_binance_requires_connect_and_subscribe() -> None:
    source = _binance_source(_RouteTransport({}))
    with pytest.raises(DataSourceTransientError):
        source.latest_snapshot(5)  # not connected
    source._connected = True
    with pytest.raises(DataSourceTransientError):
        source.latest_snapshot(5)  # not subscribed


# ── Host failover / error taxonomy ────────────────────────────────────────────


@pytest.mark.unit
def test_host_failover_parks_failing_host(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _RouteTransport(
        {
            "/api/v3/klines": _load("binance_klines_btcusdt_15m.json"),
            "/api/v3/time": {"serverTime": FIXTURE_NOW_MS},
        },
        dead_hosts=(HOST_A,),
    )
    source = _binance_source(transport, hosts=(HOST_A, HOST_B))
    source.connect()
    source.subscribe("BTCUSDT", "15m")
    monkeypatch.setattr(source, "_now_ms", lambda: FIXTURE_NOW_MS)

    assert len(source.latest_snapshot(2)) == 2
    assert any(call.startswith(HOST_A) for call in transport.calls)
    assert any(call.startswith(HOST_B) for call in transport.calls)

    # Second fetch must not pay the dead host again (cooldown park).
    before = len(transport.calls)
    source.latest_snapshot(2)
    second_batch = [call for call in transport.calls[before:] if "klines" in call]
    assert second_batch, "expected a klines call on the second fetch"
    assert not any(call.startswith(HOST_A) for call in second_batch)


@pytest.mark.unit
def test_rate_limit_is_transient_invalid_symbol_is_permanent() -> None:
    rate_limited = _binance_source(_RouteTransport({"/api/v3/klines": (418, b"")}))
    rate_limited._connected = True
    rate_limited.subscribe("BTCUSDT", "15m")
    with pytest.raises(DataSourceTransientError):
        rate_limited.latest_snapshot(2)

    bad_symbol = _binance_source(
        _RouteTransport({"/api/v3/klines": (400, b'{"code":-1121,"msg":"Invalid symbol."}')})
    )
    bad_symbol._connected = True
    bad_symbol.subscribe("FOOUSDT", "15m")  # normalises fine, venue rejects it
    with pytest.raises(DataSourceError) as excinfo:
        bad_symbol.latest_snapshot(2)
    assert "Invalid symbol." in str(excinfo.value)


@pytest.mark.unit
def test_invalid_json_body_is_transient() -> None:
    source = _binance_source(_RouteTransport({"/api/v3/klines": (200, b"<html>nope</html>")}))
    source._connected = True
    source.subscribe("BTCUSDT", "15m")
    with pytest.raises(DataSourceTransientError):
        source.latest_snapshot(2)


@pytest.mark.unit
def test_all_hosts_down_raises_transient_with_diagnostics() -> None:
    transport = _RouteTransport({}, dead_hosts=(HOST_A, HOST_B))
    source = _binance_source(transport, hosts=(HOST_A, HOST_B))
    source._connected = True
    source.subscribe("BTCUSDT", "15m")
    with pytest.raises(DataSourceTransientError) as excinfo:
        source.latest_snapshot(2)
    message = str(excinfo.value)
    assert "2 tried" in message
    assert HOST_B in message


@pytest.mark.unit
def test_connect_reports_unreachable_venue() -> None:
    source = _binance_source(_RouteTransport({}, dead_hosts=(HOST_A,)))
    with pytest.raises(DataSourceTransientError) as excinfo:
        source.connect()
    assert "HTTPS_PROXY" in str(excinfo.value)


# ── OKX ───────────────────────────────────────────────────────────────────────


def _okx_source(transport, hosts: tuple[str, ...] = (HOST_A,)) -> OkxSource:
    client = CryptoRestClient(hosts, transport=transport, timeout_s=5.0)
    return OkxSource(client=client)


_OKX_SPOT_ROUTES = {
    "/api/v5/market/candles": _load("okx_candles_btc_usdt_15m.json"),
    "/api/v5/public/time": {"code": "0", "msg": "", "data": [{"ts": str(FIXTURE_NOW_MS)}]},
}


@pytest.mark.unit
def test_okx_spot_snapshot_parses_documented_schema() -> None:
    transport = _RouteTransport(_OKX_SPOT_ROUTES)
    source = _okx_source(transport)
    source.connect()
    source.subscribe("BTCUSDT", "15m")  # normalises to BTC-USDT

    bars = source.latest_snapshot(6)

    assert source._inst_id == "BTC-USDT"
    assert len(bars) == 6
    assert bars[0].ts_open == 1_792_065_600_000
    # confirm="0" on the newest row → forming.
    assert bars[0].closed is False and bars[0].seq == 0
    assert bars[1].closed is True and bars[1].seq == 1
    # Spot: vol is base-currency size, volCcyQuote is quote turnover.
    assert bars[0].volume == pytest.approx(22.38694)
    assert bars[0].amount == pytest.approx(1812104.22)
    # The request carried the venue's bar spelling and instId.
    assert "bar=15m" in transport.calls[-1]
    assert "instId=BTC-USDT" in transport.calls[-1]


@pytest.mark.unit
def test_okx_swap_volume_comes_from_volccy() -> None:
    routes = {
        "/api/v5/market/candles": _load("okx_candles_btc_usdt_swap_15m.json"),
        "/api/v5/public/time": {"code": "0", "data": [{"ts": str(FIXTURE_NOW_MS)}]},
    }
    source = _okx_source(_RouteTransport(routes))
    source.connect()
    source.subscribe("BTC-USDT-SWAP", "15m")

    bars = source.latest_snapshot(6)

    assert source._swap is True
    # vol counts contracts (2238); the base-currency size is volCcy (22.38694).
    assert bars[0].volume == pytest.approx(22.38694)
    assert bars[0].amount == pytest.approx(1812104.22)


@pytest.mark.unit
def test_okx_timeframe_spelling() -> None:
    from pa_agent.data.okx_source import TF_MAP

    assert TF_MAP["1h"] == "1H"
    assert TF_MAP["4h"] == "4H"
    assert TF_MAP["1d"] == "1D"
    assert TF_MAP["1m"] == "1m"  # minute stays lowercase
    assert TF_MAP["1M"] == "1M"  # month stays uppercase


@pytest.mark.unit
def test_okx_paginates_via_history_candles() -> None:
    base_ts = 1_792_000_000_000
    rows = [
        [
            str(base_ts - i * 900_000),
            "1.0",
            "2.0",
            "0.5",
            "1.5",
            "10.0",
            "10.0",
            "15.0",
            "1",
        ]
        for i in range(350)
    ]
    calls: list[str] = []

    def transport(url: str, timeout: float, headers: dict[str, str]) -> tuple[int, bytes]:
        calls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        limit = int(query.get("limit", ["300"])[0])
        after = query.get("after")
        pool = [r for r in rows if after is None or int(r[0]) < int(after[0])]
        return 200, json.dumps({"code": "0", "data": pool[:limit]}).encode()

    source = _okx_source(transport)
    source.connect()
    source.subscribe("BTC-USDT", "15m")

    bars = source.latest_snapshot(350)

    candle_calls = [call for call in calls if "candles" in call]
    assert len(bars) == 350
    assert "/api/v5/market/candles" in candle_calls[0]
    assert any("/api/v5/market/history-candles" in call for call in candle_calls)
    assert any("after=" in call for call in candle_calls)


@pytest.mark.unit
def test_okx_business_error_codes() -> None:
    permanent = _okx_source(
        _RouteTransport(
            {
                "/api/v5/market/candles": {"code": "51001", "msg": "Instrument ID does not exist"},
                "/api/v5/public/time": {"code": "0", "data": [{"ts": "1"}]},
            }
        )
    )
    permanent._connected = True
    permanent.subscribe("BTC-USDT", "15m")
    with pytest.raises(DataSourceError) as excinfo:
        permanent.latest_snapshot(2)
    assert "51001" in str(excinfo.value)

    transient = _okx_source(
        _RouteTransport(
            {
                "/api/v5/market/candles": {"code": "50011", "msg": "Too many requests"},
                "/api/v5/public/time": {"code": "0", "data": [{"ts": "1"}]},
            }
        )
    )
    transient._connected = True
    transient.subscribe("BTC-USDT", "15m")
    with pytest.raises(DataSourceTransientError):
        transient.latest_snapshot(2)


@pytest.mark.unit
def test_okx_rejects_bad_instrument_and_timeframe() -> None:
    source = _okx_source(_RouteTransport({}))
    with pytest.raises(ValueError):
        source.subscribe("000001", "15m")
    with pytest.raises(ValueError):
        source.subscribe("BTC-USDT", "7m")


# ── Factory / policy wiring ───────────────────────────────────────────────────


@pytest.mark.unit
def test_crypto_kinds_are_creatable_and_ui_selectable() -> None:
    assert isinstance(create_data_source("binance"), BinanceSource)
    assert isinstance(create_data_source("okx"), OkxSource)

    kinds = [kind for kind, _ in DATA_SOURCE_CHOICES]
    assert kinds[0] == "mt5"  # MT5 stays the default/first entry
    assert "binance" in kinds and "okx" in kinds
    labels = dict(DATA_SOURCE_CHOICES)
    assert "币安" in labels["binance"] and "OKX" in labels["okx"]


@pytest.mark.unit
def test_crypto_default_symbols() -> None:
    assert default_symbol_for_kind("binance") == "BTCUSDT"
    assert default_symbol_for_kind("okx") == "BTC-USDT"


@pytest.mark.unit
def test_gold_symbol_does_not_leak_into_crypto_kinds() -> None:
    assert normalize_gold_symbol_for_kind("binance", "XAUUSDm") == "BTCUSDT"
    assert normalize_gold_symbol_for_kind("okx", "XAUUSDm") == "BTC-USDT"
    # A genuine crypto pair is preserved as typed.
    assert normalize_gold_symbol_for_kind("binance", "ETHUSDT") == "ETHUSDT"
    assert normalize_gold_symbol_for_kind("okx", "BTC-USDT-SWAP") == "BTC-USDT-SWAP"


@pytest.mark.unit
def test_subscribe_with_fallback_degrades_symbol_and_timeframe() -> None:
    source = BinanceSource(client=CryptoRestClient((HOST_A,), transport=_RouteTransport({})))

    symbol, tf = subscribe_with_fallback(source, "binance", "000001", "7m")

    assert symbol == "BTCUSDT"
    assert tf == "15m"

    symbol2, tf2 = subscribe_with_fallback(source, "binance", "ETHUSDT", "4h")
    assert (symbol2, tf2) == ("ETHUSDT", "4h")


@pytest.mark.unit
def test_refresh_policy_covers_crypto_sources() -> None:
    assert effective_refresh_interval_ms("binance", 1000) == 2500
    assert effective_refresh_interval_ms("okx", 1000, timeframe="1d") == 3000
    assert effective_refresh_interval_ms("mt5", 1000) == 1000
    assert effective_refresh_interval_ms("binance", 5000) == 5000
