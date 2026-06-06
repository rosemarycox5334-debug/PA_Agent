"""Construct :class:`DataSource` implementations by kind id."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any
from typing import Literal

from pa_agent.data.base import DataSource
from pa_agent.data.market_defaults import (
    A_SHARE_DEFAULT_SYMBOL,
    GOLD_MT5_SYMBOL,
    GOLD_TV_SYMBOL,
)

DataSourceKind = Literal["mt5", "tradingview", "akshare", "rqdata"]


@dataclass(frozen=True)
class DataSourceSpec:
    """Registration metadata for a lazily-loaded data source."""

    kind: DataSourceKind
    label: str
    module: str
    class_name: str
    default_symbol: str


_SPECS: tuple[DataSourceSpec, ...] = (
    DataSourceSpec("mt5", "MT5", "pa_agent.data.mt5", "MT5Source", GOLD_MT5_SYMBOL),
    DataSourceSpec(
        "tradingview",
        "TradingView",
        "pa_agent.data.tradingview",
        "TradingViewSource",
        GOLD_TV_SYMBOL,
    ),
    DataSourceSpec(
        "akshare",
        "AkShare",
        "pa_agent.data.akshare_source",
        "AkShareSource",
        A_SHARE_DEFAULT_SYMBOL,
    ),
    DataSourceSpec(
        "rqdata",
        "RQData",
        "pa_agent.data.rqdata",
        "RQDataSource",
        "000001.XSHG",
    ),
)

_SPEC_BY_KIND: dict[DataSourceKind, DataSourceSpec] = {spec.kind: spec for spec in _SPECS}

DATA_SOURCE_CHOICES: tuple[tuple[DataSourceKind, str], ...] = tuple(
    (spec.kind, spec.label) for spec in _SPECS
)

_DEFAULT_SYMBOLS: dict[DataSourceKind, str] = {
    spec.kind: spec.default_symbol for spec in _SPECS
}


def default_tradingview_exchange() -> str:
    """Empty string = UI «（自动）» — probe all TV preset venues."""
    return ""


def normalize_data_source_kind(kind: str | None) -> DataSourceKind:
    """Return a supported data-source kind, defaulting to MT5."""
    if kind in {k for k, _ in DATA_SOURCE_CHOICES}:
        return kind  # type: ignore[return-value]
    return "mt5"


def data_source_label(kind: str | None) -> str:
    """Human-readable label for *kind*."""
    normalized = normalize_data_source_kind(kind)
    for key, label in DATA_SOURCE_CHOICES:
        if key == normalized:
            return label
    return "MT5"


def default_symbol_for_kind(kind: str | None) -> str:
    return _DEFAULT_SYMBOLS[normalize_data_source_kind(kind)]


def data_source_specs() -> tuple[DataSourceSpec, ...]:
    """Return immutable metadata for all registered data sources."""
    return _SPECS


def create_data_source(kind: str | None) -> DataSource:
    """Instantiate a fresh data source for *kind* (not connected)."""
    normalized = normalize_data_source_kind(kind)
    spec = _SPEC_BY_KIND[normalized]
    module = import_module(spec.module)
    source_cls = getattr(module, spec.class_name)
    return source_cls()


def configure_data_source(
    data_source: DataSource,
    kind: str | None,
    settings: Any = None,
    *,
    tv_exchange: str | None = None,
) -> None:
    """Apply settings to a data source without leaking provider classes outward."""
    normalized = normalize_data_source_kind(kind)
    general = getattr(settings, "general", None) if settings is not None else None

    should_set_license = normalized == "rqdata" or (
        kind is None and hasattr(data_source, "set_license")
    )
    if should_set_license and hasattr(data_source, "set_license"):
        license_key = getattr(general, "rqdata_license_key", "") if general is not None else ""
        data_source.set_license(license_key or "")

    should_set_exchange = normalized == "tradingview" or (
        tv_exchange is not None and hasattr(data_source, "set_exchange")
    )
    if should_set_exchange and hasattr(data_source, "set_exchange"):
        exchange = tv_exchange
        if exchange is None and general is not None:
            exchange = getattr(general, "last_tradingview_exchange", "") or ""
        data_source.set_exchange(exchange or "")
