"""Tests for data source factory and settings."""
from __future__ import annotations

from pa_agent.config.settings import GeneralSettings
from pa_agent.data.factory import (
    configure_data_source,
    create_data_source,
    default_symbol_for_kind,
    default_tradingview_exchange,
    normalize_data_source_kind,
)
from pa_agent.data.mt5 import MT5Source
from pa_agent.data.tradingview import TradingViewSource


def test_normalize_data_source_kind_defaults_unknown():
    assert normalize_data_source_kind("invalid") == "mt5"
    assert normalize_data_source_kind(None) == "mt5"
    assert normalize_data_source_kind("yfinance") == "mt5"


def test_create_data_source_returns_expected_types():
    assert isinstance(create_data_source("mt5"), MT5Source)
    assert isinstance(create_data_source("tradingview"), TradingViewSource)
    assert isinstance(create_data_source("yfinance"), MT5Source)
    assert create_data_source("akshare").__class__.__name__ == "AkShareSource"
    assert create_data_source("rqdata").__class__.__name__ == "RQDataSource"


def test_default_symbols_per_kind():
    assert default_symbol_for_kind("mt5") == "XAUUSDm"
    assert default_symbol_for_kind("tradingview") == "XAUUSD"
    assert default_symbol_for_kind("akshare") == "000001"
    assert default_symbol_for_kind("rqdata") == "000001.XSHG"


def test_default_tradingview_exchange_is_auto():
    assert default_tradingview_exchange() == ""


def test_general_settings_last_data_source_default():
    g = GeneralSettings()
    assert g.last_data_source == "mt5"


def test_normalize_data_source_kind_accepts_optional_market_sources() -> None:
    assert normalize_data_source_kind("akshare") == "akshare"
    assert normalize_data_source_kind("rqdata") == "rqdata"


def test_configure_data_source_applies_provider_settings() -> None:
    class Source:
        def __init__(self) -> None:
            self.license_key = ""
            self.exchange = ""

        def set_license(self, value: str) -> None:
            self.license_key = value

        def set_exchange(self, value: str) -> None:
            self.exchange = value

    class General:
        rqdata_license_key = "license-123"
        last_tradingview_exchange = "OANDA"

    class Settings:
        general = General()

    source = Source()
    configure_data_source(source, "rqdata", Settings())
    assert source.license_key == "license-123"
    assert source.exchange == ""

    configure_data_source(source, "tradingview", Settings())
    assert source.exchange == "OANDA"


def test_configure_data_source_supports_gui_capability_fallbacks() -> None:
    class Source:
        def __init__(self) -> None:
            self.license_key = ""
            self.exchange = ""

        def set_license(self, value: str) -> None:
            self.license_key = value

        def set_exchange(self, value: str) -> None:
            self.exchange = value

    class General:
        rqdata_license_key = "license-456"

    class Settings:
        general = General()

    source = Source()
    configure_data_source(source, None, Settings(), tv_exchange="TVC")

    assert source.license_key == "license-456"
    assert source.exchange == "TVC"
