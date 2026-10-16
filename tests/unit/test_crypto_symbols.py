"""Unit tests for the shared crypto symbol/instrument normalisation."""

from __future__ import annotations

import pytest

from pa_agent.data.crypto_symbols import (
    binance_symbol_is_perp,
    looks_like_crypto_pair,
    normalize_binance_symbol,
    normalize_okx_inst_id,
    okx_inst_is_swap,
    okx_inst_to_display,
    split_pair,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTCUSDT", ("BTC", "USDT")),
        ("btc/usdt", ("BTC", "USDT")),
        ("BTC-USDT", ("BTC", "USDT")),
        ("ETHUSDC", ("ETH", "USDC")),
        ("SOLUSD", ("SOL", "USD")),
        ("", None),
        ("BTC", None),
    ],
)
def test_split_pair(raw: str, expected: tuple[str, str] | None) -> None:
    assert split_pair(raw) == expected


@pytest.mark.unit
def test_split_pair_prefers_longest_quote_asset() -> None:
    """USDT must win over USD, otherwise BTCUSDT would split as BTCUSD + T."""
    assert split_pair("BTCUSDT") == ("BTC", "USDT")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("btc/usdt", "BTCUSDT"),
        ("BTC-USDT", "BTCUSDT"),
        ("btcusdt", "BTCUSDT"),
        ("BTCUSDT.perp", "BTCUSDT.PERP"),
        ("ETH/USDT.perp", "ETHUSDT.PERP"),
        ("垃圾", ""),
        ("", ""),
    ],
)
def test_normalize_binance_symbol(raw: str, expected: str) -> None:
    assert normalize_binance_symbol(raw) == expected


@pytest.mark.unit
def test_binance_perp_marker() -> None:
    assert binance_symbol_is_perp("BTCUSDT.PERP")
    assert binance_symbol_is_perp("btcusdt.perp")
    assert not binance_symbol_is_perp("BTCUSDT")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTCUSDT", "BTC-USDT"),
        ("btc/usdt", "BTC-USDT"),
        ("BTC-USDT", "BTC-USDT"),
        ("BTC-USDT-SWAP", "BTC-USDT-SWAP"),
        ("ETHUSDT.perp", "ETH-USDT-SWAP"),
        ("000001", ""),
    ],
)
def test_normalize_okx_inst_id(raw: str, expected: str) -> None:
    assert normalize_okx_inst_id(raw) == expected


@pytest.mark.unit
def test_okx_swap_helpers() -> None:
    assert okx_inst_is_swap("BTC-USDT-SWAP")
    assert not okx_inst_is_swap("BTC-USDT")
    assert okx_inst_to_display("BTC-USDT-SWAP") == "BTC/USDT 永续"
    assert okx_inst_to_display("BTC-USDT") == "BTC/USDT"


@pytest.mark.unit
def test_looks_like_crypto_pair_accepts_pairs() -> None:
    for symbol in ("BTCUSDT", "BTC/USDT", "BTC-USDT-SWAP", "ETHUSDT.perp", "SOLUSDC"):
        assert looks_like_crypto_pair(symbol), symbol


@pytest.mark.unit
def test_looks_like_crypto_pair_rejects_metals_fx_and_codes() -> None:
    """Regression: MT5 gold and A-share codes must not be treated as crypto."""
    for symbol in ("XAUUSDm", "XAUUSD", "EURUSD", "USOIL", "000001", "", "垃圾"):
        assert not looks_like_crypto_pair(symbol), symbol
