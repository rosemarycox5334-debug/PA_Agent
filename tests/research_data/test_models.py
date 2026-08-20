from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from pa_agent.research_data.models import (
    ContractRuleSnapshot,
    FundingRate,
    GapInterval,
    Kline,
    StreamGapReport,
)


def test_kline_is_immutable_and_preserves_decimal_values():
    bar = Kline(
        source="binance_fapi",
        stream="trade",
        symbol="BTCUSDT",
        interval="1m",
        open_time_utc_ms=0,
        close_time_utc_ms=59_999,
        open=Decimal("100.10"),
        high=Decimal("101.20"),
        low=Decimal("99.90"),
        close=Decimal("100.50"),
        base_volume=Decimal("3"),
        quote_volume=Decimal("301.5"),
        trade_count=2,
        taker_buy_base_volume=Decimal("1"),
        taker_buy_quote_volume=Decimal("100.5"),
        is_closed=True,
    )

    assert bar.close == Decimal("100.50")
    with pytest.raises(FrozenInstanceError):
        bar.close = Decimal("0")


def test_gap_report_keeps_streams_independent():
    report = StreamGapReport(
        stream="mark",
        status="GAPS_DETECTED",
        intervals=(GapInterval(start_utc_ms=60_000, end_utc_ms=119_999),),
    )

    assert report.stream == "mark"
    assert report.intervals[0].start_utc_ms == 60_000


def test_funding_and_contract_snapshots_are_data_only_models():
    funding = FundingRate(
        source="binance_fapi",
        symbol="BTCUSDT",
        funding_time_utc_ms=1,
        funding_rate=Decimal("0.0001"),
        mark_price=Decimal("100"),
    )
    rule = ContractRuleSnapshot(
        source="binance_fapi_exchange_info",
        symbol="BTCUSDT",
        status="TRADING",
        price_tick=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        acquired_at_utc_ms=2,
        source_hash="abc",
        validity="CURRENT_SNAPSHOT_ONLY",
    )

    assert funding.mark_price == Decimal("100")
    assert rule.validity == "CURRENT_SNAPSHOT_ONLY"
