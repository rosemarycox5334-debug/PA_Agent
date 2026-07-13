from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Kline:
    source: str
    stream: str
    symbol: str
    interval: str
    open_time_utc_ms: int
    close_time_utc_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal
    is_closed: bool


@dataclass(frozen=True, slots=True)
class FundingRate:
    source: str
    symbol: str
    funding_time_utc_ms: int
    funding_rate: Decimal
    mark_price: Decimal


@dataclass(frozen=True, slots=True)
class GapInterval:
    start_utc_ms: int
    end_utc_ms: int


@dataclass(frozen=True, slots=True)
class StreamGapReport:
    stream: str
    status: str
    intervals: tuple[GapInterval, ...]
    schedule_version: str | None = None
    observed_steps_ms: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ContractRuleSnapshot:
    source: str
    symbol: str
    status: str
    price_tick: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    acquired_at_utc_ms: int
    source_hash: str
    validity: str
