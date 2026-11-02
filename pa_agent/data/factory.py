"""Construct :class:`DataSource` implementations by kind id."""
from __future__ import annotations

from typing import Literal

from pa_agent.data.base import DataSource
from pa_agent.data.market_defaults import (
    A_SHARE_DEFAULT_SYMBOL,
    GOLD_MT5_SYMBOL,
    GOLD_TV_SYMBOL,
)

DataSourceKind = Literal[
    "mt5",
    "tradingview",
    "ashare",
    "akshare",
    "eastmoney_futures",
    "tushare",
    "yfinance",
]

#: 是否在界面「数据来源」下拉框里显示 A股（专属数据源）。
#:
#: ⚠️ 只能改代码，UI 设置里不暴露（用户明确要求）：默认 False = 不显示，
#: 界面只有 MT5 / TradingView。需要时把这里改成 True 并重启程序即可。
#: 关闭显示不影响该 kind 本身——它始终是合法 kind，脚本/后端仍可用
#: ``create_data_source("ashare")`` 创建；已经保存过 ashare 的配置会被界面
#: 回退到 MT5（沿用「保存的源不在下拉列表就回退默认」的既有行为）。
SHOW_ASHARE_DATA_SOURCE: bool = False

#: A股专属数据源在界面/状态栏的统一文案。
ASHARE_SOURCE_LABEL = "A股（专属数据源）"

#: 出厂下拉框内容：MT5 为默认数据源，排在首位。
_BASE_SOURCE_CHOICES: tuple[tuple[DataSourceKind, str], ...] = (
    ("mt5", "MT5"),
    ("tradingview", "TradingView"),
)


def data_source_choices(
    show_ashare: bool | None = None,
) -> tuple[tuple[DataSourceKind, str], ...]:
    """界面可选数据源列表；*show_ashare* 缺省时取 :data:`SHOW_ASHARE_DATA_SOURCE`。"""
    show = SHOW_ASHARE_DATA_SOURCE if show_ashare is None else bool(show_ashare)
    if not show:
        return _BASE_SOURCE_CHOICES
    return (*_BASE_SOURCE_CHOICES, ("ashare", ASHARE_SOURCE_LABEL))


#: 供界面/测试直接使用的可选列表（导入时求值 → 改开关需重启程序生效）。
DATA_SOURCE_CHOICES: tuple[tuple[DataSourceKind, str], ...] = data_source_choices()

#: 不在下拉框里显示、但仍可通过 kind 创建的源（含未开启显示的 ashare）。
_HIDDEN_KINDS: frozenset[DataSourceKind] = frozenset(
    {"ashare", "akshare", "tushare", "yfinance", "eastmoney_futures"}
)

_DEFAULT_SYMBOLS: dict[DataSourceKind, str] = {
    "mt5": GOLD_MT5_SYMBOL,
    "tradingview": GOLD_TV_SYMBOL,
    "ashare": A_SHARE_DEFAULT_SYMBOL,
    "akshare": A_SHARE_DEFAULT_SYMBOL,
    "eastmoney_futures": "RB0 螺纹钢",
    "tushare": A_SHARE_DEFAULT_SYMBOL,
    "yfinance": "GC=F",
}


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
    if normalized == "ashare":
        return ASHARE_SOURCE_LABEL
    if normalized == "eastmoney_futures":
        return "东方财富期货"
    if normalized == "tushare":
        return "Tushare(A股)"
    if normalized == "akshare":
        return "AkShare"
    if normalized == "yfinance":
        return "YFinance"
    return "MT5"


def default_symbol_for_kind(kind: str | None) -> str:
    return _DEFAULT_SYMBOLS[normalize_data_source_kind(kind)]


def create_data_source(kind: str | None) -> DataSource:
    """Instantiate a fresh data source for *kind* (not connected)."""
    normalized = normalize_data_source_kind(kind)
    if normalized == "tradingview":
        from pa_agent.data.tradingview import TradingViewSource

        return TradingViewSource()
    if normalized == "ashare":
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
