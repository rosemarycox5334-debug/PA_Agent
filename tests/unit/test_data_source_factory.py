"""Tests for data source factory and settings."""
from __future__ import annotations

from pa_agent.config.settings import GeneralSettings
from pa_agent.data.factory import (
    create_data_source,
    default_symbol_for_kind,
    default_tradingview_exchange,
    normalize_data_source_kind,
)
from pa_agent.data.mt5 import MT5Source
from pa_agent.data.tradingview import TradingViewSource
from pa_agent.data.yfinance_source import YFinanceSource


def test_normalize_data_source_kind_defaults_unknown():
    assert normalize_data_source_kind("invalid") == "mt5"
    assert normalize_data_source_kind(None) == "mt5"
    assert normalize_data_source_kind("yfinance") == "yfinance"


def test_create_data_source_returns_expected_types():
    assert isinstance(create_data_source("mt5"), MT5Source)
    assert isinstance(create_data_source("tradingview"), TradingViewSource)
    assert isinstance(create_data_source("yfinance"), YFinanceSource)


def test_default_symbols_per_kind():
    assert default_symbol_for_kind("mt5") == "XAUUSDm"
    assert default_symbol_for_kind("tradingview") == "XAUUSD"
    assert default_symbol_for_kind("yfinance") == "GC=F"


def test_default_tradingview_exchange_is_auto():
    assert default_tradingview_exchange() == ""


def test_general_settings_last_data_source_default():
    g = GeneralSettings()
    assert g.last_data_source == "mt5"


def test_normalize_data_source_kind_downgrades_akshare() -> None:
    assert normalize_data_source_kind("akshare") == "mt5"
