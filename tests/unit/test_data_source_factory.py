"""Tests for data source factory and settings."""
from __future__ import annotations

from pa_agent.config.settings import GeneralSettings
from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    create_data_source,
    default_symbol_for_kind,
    default_tradingview_exchange,
    normalize_data_source_kind,
)
from pa_agent.data.eastmoney_source import EastMoneySource
from pa_agent.data.mt5 import MT5Source
from pa_agent.data.tushare_source import TushareSource
from pa_agent.data.tradingview import TradingViewSource


def test_normalize_data_source_kind_defaults_unknown():
    assert normalize_data_source_kind("invalid") == "mt5"
    assert normalize_data_source_kind(None) == "mt5"


def test_normalize_data_source_kind_hidden_sources():
    assert normalize_data_source_kind("akshare") == "akshare"
    assert normalize_data_source_kind("tushare") == "tushare"
    assert normalize_data_source_kind("yfinance") == "yfinance"
    # eastmoney 已并入 UI 可见的 ashare，不再是独立 kind
    assert normalize_data_source_kind("eastmoney") == "mt5"


def test_mt5_in_ui_choices():
    """MT5 为默认数据源, 必须在 UI 可选列表中且排首位。"""
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "mt5" in ui_kinds
    assert DATA_SOURCE_CHOICES[0][0] == "mt5"
    assert "akshare" not in ui_kinds


def test_ashare_hidden_in_ui_by_default():
    """A股数据源默认不在界面显示：开关是代码常量，UI 里不暴露。"""
    import pa_agent.data.factory as factory

    assert factory.SHOW_ASHARE_DATA_SOURCE is False
    assert "ashare" not in {k for k, _ in DATA_SOURCE_CHOICES}
    assert [k for k, _ in DATA_SOURCE_CHOICES] == ["mt5", "tradingview"]
    # 只是不显示，kind 本身仍然合法：脚本/后端照样能创建
    assert normalize_data_source_kind("ashare") == "ashare"
    assert isinstance(create_data_source("ashare"), EastMoneySource)


def test_ashare_shown_when_code_switch_is_on():
    from pa_agent.data.factory import ASHARE_SOURCE_LABEL, data_source_choices

    choices = data_source_choices(show_ashare=True)
    assert choices[-1] == ("ashare", ASHARE_SOURCE_LABEL)
    assert [k for k, _ in choices] == ["mt5", "tradingview", "ashare"]
    assert [k for k, _ in data_source_choices(show_ashare=False)] == ["mt5", "tradingview"]


def test_tushare_not_in_ui_choices():
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "tushare" not in ui_kinds


def test_ashare_label_uses_shared_constant():
    """状态栏文案与下拉文案共用 ASHARE_SOURCE_LABEL，避免两处写死不同名。"""
    from pa_agent.data.factory import (
        ASHARE_SOURCE_LABEL,
        data_source_choices,
        data_source_label,
    )

    assert ASHARE_SOURCE_LABEL == "A股（专属数据源）"
    assert data_source_label("ashare") == ASHARE_SOURCE_LABEL
    assert dict(data_source_choices(show_ashare=True))["ashare"] == ASHARE_SOURCE_LABEL


def test_create_data_source_returns_expected_types():
    assert isinstance(create_data_source("mt5"), MT5Source)
    assert isinstance(create_data_source("tradingview"), TradingViewSource)
    assert isinstance(create_data_source("ashare"), EastMoneySource)
    assert isinstance(create_data_source("tushare"), TushareSource)


def test_default_symbols_per_kind():
    assert default_symbol_for_kind("mt5") == "XAUUSDm"
    assert default_symbol_for_kind("tradingview") == "XAUUSD"
    assert default_symbol_for_kind("ashare") == "000001"
    assert default_symbol_for_kind("tushare") == "000001"


def test_default_tradingview_exchange_is_auto():
    assert default_tradingview_exchange() == ""


def test_general_settings_last_data_source_default():
    g = GeneralSettings()
    assert g.last_data_source == "mt5"
