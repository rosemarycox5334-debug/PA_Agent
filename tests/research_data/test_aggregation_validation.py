from dataclasses import replace
from decimal import Decimal

import pytest

from pa_agent.research_data.models import Kline
from pa_agent.research_data.validation import (
    AGGREGATION_VALIDATION_VERSION,
    validate_native_bars,
)


def bar(**changes) -> Kline:
    base = Kline(
        source="binance_fapi",
        stream="trade",
        symbol="BTCUSDT",
        interval="4h",
        open_time_utc_ms=0,
        close_time_utc_ms=14_399_999,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        base_volume=Decimal("1"),
        quote_volume=Decimal("100"),
        trade_count=10,
        taker_buy_base_volume=Decimal("0.5"),
        taker_buy_quote_volume=Decimal("50"),
        is_closed=True,
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("field", "difference"),
    [
        ("base_volume", Decimal("0.00000001")),
        ("quote_volume", Decimal("0.000001")),
        ("taker_buy_base_volume", Decimal("0.00000001")),
        ("taker_buy_quote_volume", Decimal("0.000001")),
    ],
)
def test_absolute_tolerance_boundary_passes(field, difference):
    aggregated = bar()
    native = replace(aggregated, **{field: getattr(aggregated, field) + difference})

    report = validate_native_bars([aggregated], [native])

    assert report.version == AGGREGATION_VALIDATION_VERSION == "AGG_VALIDATION_V1"
    assert report.valid is True


def test_relative_tolerance_can_pass_when_absolute_tolerance_fails():
    aggregated = bar(base_volume=Decimal("100000000"))
    native = replace(aggregated, base_volume=Decimal("100000000.00002"))

    assert validate_native_bars([aggregated], [native]).valid is True


def test_outside_both_volume_tolerances_fails_with_field_issue():
    aggregated = bar()
    native = replace(aggregated, base_volume=Decimal("1.00000002"))

    report = validate_native_bars([aggregated], [native])

    assert report.valid is False
    assert report.issues[0].field == "base_volume"


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "close_time_utc_ms", "trade_count"])
def test_ohlc_utc_and_trade_count_require_exact_match(field):
    aggregated = bar()
    current = getattr(aggregated, field)
    replacement = current + (Decimal("0.00000001") if isinstance(current, Decimal) else 1)

    report = validate_native_bars([aggregated], [replace(aggregated, **{field: replacement})])

    assert report.valid is False
    assert report.issues[0].field == field


def test_missing_native_bar_fails_validation():
    report = validate_native_bars([bar()], [])

    assert report.valid is False
    assert report.issues[0].field == "native_bar"
