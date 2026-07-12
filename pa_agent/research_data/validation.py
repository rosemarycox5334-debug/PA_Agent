from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from pa_agent.research_data.models import Kline

AGGREGATION_VALIDATION_VERSION = "AGG_VALIDATION_V1"
RELATIVE_TOLERANCE = Decimal("0.000000000001")
ABSOLUTE_TOLERANCES = {
    "base_volume": Decimal("0.00000001"),
    "quote_volume": Decimal("0.000001"),
    "taker_buy_base_volume": Decimal("0.00000001"),
    "taker_buy_quote_volume": Decimal("0.000001"),
}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    symbol: str
    open_time_utc_ms: int
    field: str
    aggregated: str
    native: str


@dataclass(frozen=True, slots=True)
class AggregationValidationReport:
    version: str
    valid: bool
    compared_bars: int
    issues: tuple[ValidationIssue, ...]


def _volume_matches(aggregated: Decimal, native: Decimal, absolute: Decimal) -> bool:
    difference = abs(aggregated - native)
    if difference <= absolute:
        return True
    denominator = max(abs(aggregated), abs(native))
    return denominator != 0 and difference / denominator <= RELATIVE_TOLERANCE


def validate_native_bars(
    aggregated_bars: Iterable[Kline],
    native_bars: Iterable[Kline],
    *,
    version: str = AGGREGATION_VALIDATION_VERSION,
) -> AggregationValidationReport:
    if version != AGGREGATION_VALIDATION_VERSION:
        raise ValueError(f"Unsupported aggregation validation version: {version}")
    aggregated = sorted(aggregated_bars, key=lambda bar: (bar.symbol, bar.open_time_utc_ms))
    native = {(bar.symbol, bar.open_time_utc_ms): bar for bar in native_bars}
    issues: list[ValidationIssue] = []
    exact_fields = ("open", "high", "low", "close", "close_time_utc_ms", "trade_count")
    for item in aggregated:
        key = (item.symbol, item.open_time_utc_ms)
        reference = native.get(key)
        if reference is None:
            issues.append(
                ValidationIssue(item.symbol, item.open_time_utc_ms, "native_bar", "present", "missing")
            )
            continue
        for field in exact_fields:
            left = getattr(item, field)
            right = getattr(reference, field)
            if left != right:
                issues.append(
                    ValidationIssue(item.symbol, item.open_time_utc_ms, field, str(left), str(right))
                )
        for field, tolerance in ABSOLUTE_TOLERANCES.items():
            left = getattr(item, field)
            right = getattr(reference, field)
            if not _volume_matches(left, right, tolerance):
                issues.append(
                    ValidationIssue(item.symbol, item.open_time_utc_ms, field, str(left), str(right))
                )
    return AggregationValidationReport(
        version=version,
        valid=not issues,
        compared_bars=len(aggregated),
        issues=tuple(issues),
    )
