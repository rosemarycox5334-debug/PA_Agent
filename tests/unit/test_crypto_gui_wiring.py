"""GUI wiring for the Binance / OKX sources (placeholder + symbol combo).

These are the copies of the data-source switch path that used to be hard-coded
to MT5/TradingView/gold: the symbol placeholder and the symbol-combo population
must follow the crypto kinds, otherwise the UI offers `XAUUSDm` for a crypto
endpoint.
"""

from __future__ import annotations

import pytest

from pa_agent.gui.main_window import MainWindow


class _FakeCryptoSource:
    """Minimal stand-in exposing what the GUI reads from a data source."""

    _connected = True
    _symbol = "BTCUSDT"
    _timeframe = "15m"

    def list_symbols(self) -> list[str]:
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def supported_timeframes(self) -> list[str]:
        return ["1m", "15m", "1h", "4h", "1d"]


@pytest.fixture
def window(qtbot, tmp_path):
    from tests.e2e.test_smoke_happy_path import _make_ctx

    ctx, _pending = _make_ctx(tmp_path)
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    return win


@pytest.mark.unit
def test_symbol_placeholder_follows_crypto_kind(window) -> None:
    window._active_data_source_kind = "binance"
    window._apply_data_source_symbol_placeholder()
    binance_hint = window._symbol_combo.lineEdit().placeholderText()
    assert "BTCUSDT" in binance_hint
    assert ".perp" in binance_hint

    window._active_data_source_kind = "okx"
    window._apply_data_source_symbol_placeholder()
    okx_hint = window._symbol_combo.lineEdit().placeholderText()
    assert "BTC-USDT" in okx_hint
    assert "SWAP" in okx_hint

    # Regression: the MT5 default must not leak into the crypto hint.
    assert "XAUUSD" not in binance_hint


@pytest.mark.unit
def test_switch_to_crypto_replaces_previous_symbol(window) -> None:
    """Mirror the real switch sequence: normalise for the new kind, then fill."""
    window._ctx.data_source = _FakeCryptoSource()
    window._active_data_source_kind = "binance"

    # _switch_data_source() does exactly this pair, in this order.
    window._apply_gold_defaults_for_data_source("binance")
    window._populate_symbol_combo_for_source()

    items = [window._symbol_combo.itemText(i) for i in range(window._symbol_combo.count())]
    assert "BTCUSDT" in items
    assert window._symbol_combo.currentText() == "BTCUSDT"
