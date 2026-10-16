"""Construct :class:`DataSource` implementations by kind id."""

from __future__ import annotations

from typing import Literal

from pa_agent.data.base import DataSource
from pa_agent.data.market_defaults import (
    A_SHARE_DEFAULT_SYMBOL,
    CRYPTO_DEFAULT_SYMBOL_BINANCE,
    CRYPTO_DEFAULT_SYMBOL_OKX,
    GOLD_MT5_SYMBOL,
    GOLD_TV_SYMBOL,
)

DataSourceKind = Literal[
    "mt5",
    "tradingview",
    "binance",
    "okx",
    "akshare",
    "eastmoney",
    "eastmoney_futures",
    "tushare",
    "yfinance",
]

# UI-visible sources — 可在界面下拉框直接选择。
# MT5 为默认数据源，排在首位；eastmoney 等仍可通过隐藏 kind 创建。
# 币安 / OKX 使用公共行情接口（无需 API Key），中国大陆需配置 HTTPS_PROXY。
DATA_SOURCE_CHOICES: tuple[tuple[DataSourceKind, str], ...] = (
    ("mt5", "MT5"),
    ("tradingview", "TradingView"),
    ("binance", "币安 Binance"),
    ("okx", "OKX 欧易"),
)

_HIDDEN_KINDS: frozenset[DataSourceKind] = frozenset(
    {"akshare", "tushare", "yfinance", "eastmoney", "eastmoney_futures"}
)

_DEFAULT_SYMBOLS: dict[DataSourceKind, str] = {
    "mt5": GOLD_MT5_SYMBOL,
    "tradingview": GOLD_TV_SYMBOL,
    "binance": CRYPTO_DEFAULT_SYMBOL_BINANCE,
    "okx": CRYPTO_DEFAULT_SYMBOL_OKX,
    "akshare": A_SHARE_DEFAULT_SYMBOL,
    "eastmoney": A_SHARE_DEFAULT_SYMBOL,
    "eastmoney_futures": "RB0 螺纹钢",
    "tushare": A_SHARE_DEFAULT_SYMBOL,
    "yfinance": "GC=F",
}

#: Which kinds read public crypto REST APIs (used by refresh_policy + GUI).
CRYPTO_KINDS: frozenset[DataSourceKind] = frozenset({"binance", "okx"})


def default_tradingview_exchange() -> str:
    """Empty string = UI «（自动）» — probe all TV preset venues."""
    return ""


def normalize_data_source_kind(kind: str | None) -> DataSourceKind:
    """Return a supported data-source kind, defaulting to MT5."""
    supported = {k for k, _ in DATA_SOURCE_CHOICES} | _HIDDEN_KINDS
    if kind in supported:
        return kind  # type: ignore[return-value]
    return "mt5"


def data_source_label(kind: str | None) -> str:
    """Human-readable label for *kind*."""
    normalized = normalize_data_source_kind(kind)
    for key, label in DATA_SOURCE_CHOICES:
        if key == normalized:
            return label
    if normalized == "eastmoney":
        return "东方财富"
    if normalized == "eastmoney_futures":
        return "东方财富期货"
    if normalized == "tushare":
        return "Tushare(A股)"
    if normalized == "akshare":
        return "AkShare"
    if normalized == "yfinance":
        return "YFinance"
    if normalized == "binance":
        return "币安 Binance"
    if normalized == "okx":
        return "OKX 欧易"
    return "MT5"


def default_symbol_for_kind(kind: str | None) -> str:
    return _DEFAULT_SYMBOLS[normalize_data_source_kind(kind)]


def create_data_source(kind: str | None) -> DataSource:
    """Instantiate a fresh data source for *kind* (not connected)."""
    normalized = normalize_data_source_kind(kind)
    if normalized == "tradingview":
        from pa_agent.data.tradingview import TradingViewSource

        return TradingViewSource()
    if normalized == "binance":
        from pa_agent.data.binance_source import BinanceSource

        return BinanceSource()
    if normalized == "okx":
        from pa_agent.data.okx_source import OkxSource

        return OkxSource()
    if normalized == "eastmoney":
        from pa_agent.data.eastmoney_source import EastMoneySource

        return EastMoneySource()
    if normalized == "eastmoney_futures":
        from pa_agent.data.eastmoney_futures_source import EastMoneyFuturesSource

        return EastMoneyFuturesSource()
    if normalized == "tushare":
        from pa_agent.config.paths import SETTINGS_JSON_PATH
        from pa_agent.config.settings import load_settings
        from pa_agent.data.tushare_source import TushareSource

        return TushareSource(settings=load_settings(SETTINGS_JSON_PATH))
    if normalized == "akshare":
        from pa_agent.data.akshare_source import AkShareSource

        return AkShareSource()
    if normalized == "yfinance":
        from pa_agent.data.yfinance_source import YFinanceSource

        return YFinanceSource()
    from pa_agent.data.mt5 import MT5Source

    return MT5Source()


def subscribe_with_fallback(
    data_source: DataSource,
    kind: str,
    symbol: str,
    timeframe: str,
) -> tuple[str, str]:
    """Subscribe *data_source*, degrading to the venue defaults.

    Switching data sources routinely carries over a symbol or timeframe the new
    venue does not know — MT5's ``XAUUSDm`` on a crypto endpoint, ``1w`` on a
    source that only offers intraday bars. Crashing the switch (or leaving the
    source unsubscribed so every refresh tick fails) is worse than falling back
    to the venue default, so this helper does the degradation and reports back
    which pair was actually used.

    Returns:
        ``(symbol_used, timeframe_used)``.

    Raises:
        ValueError: when neither the requested nor the default symbol is
            accepted by the venue.
    """
    supported: list[str] = []
    try:
        supported = list(data_source.supported_timeframes())
    except Exception:  # noqa: BLE001 — discovery is advisory only
        supported = []

    tf = timeframe if (not supported or timeframe in supported) else ""
    if not tf:
        for preferred in ("15m", "5m", "1h", "1d"):
            if preferred in supported:
                tf = preferred
                break
        if not tf:
            tf = supported[0] if supported else timeframe

    candidates = [symbol]
    default_symbol = default_symbol_for_kind(kind)
    if default_symbol and default_symbol != symbol:
        candidates.append(default_symbol)

    last_error: ValueError | None = None
    for candidate in candidates:
        try:
            data_source.subscribe(candidate, tf)
        except ValueError as exc:
            last_error = exc
            continue
        return candidate, tf

    if last_error is not None:
        raise last_error
    raise ValueError(f"subscribe failed for {symbol!r} {tf!r}")
