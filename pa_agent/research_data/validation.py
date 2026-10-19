from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from pa_agent.research_data.models import GapInterval, Kline

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
    incomplete_intervals: Iterable[GapInterval] = (),
) -> AggregationValidationReport:
    if version != AGGREGATION_VALIDATION_VERSION:
        raise ValueError(f"Unsupported aggregation validation version: {version}")
    aggregated_list = sorted(
        aggregated_bars, key=lambda bar: (bar.symbol, bar.open_time_utc_ms)
    )
    native_list = sorted(native_bars, key=lambda bar: (bar.symbol, bar.open_time_utc_ms))
    aggregated: dict[tuple[str, int], Kline] = {}
    native: dict[tuple[str, int], Kline] = {}
    issues: list[ValidationIssue] = []
    for item in aggregated_list:
        key = (item.symbol, item.open_time_utc_ms)
        if key in aggregated:
            issues.append(
                ValidationIssue(item.symbol, item.open_time_utc_ms, "duplicate_aggregated_bar", "duplicate", "n/a")
            )
        else:
            aggregated[key] = item
    for item in native_list:
        key = (item.symbol, item.open_time_utc_ms)
        if key in native:
            issues.append(
                ValidationIssue(item.symbol, item.open_time_utc_ms, "duplicate_native_bar", "n/a", "duplicate")
            )
        else:
            native[key] = item
    aggregated_keys = set(aggregated)
    native_keys = set(native)
    for symbol, open_time in sorted(aggregated_keys - native_keys):
        issues.append(ValidationIssue(symbol, open_time, "native_bar", "present", "missing"))
    for symbol, open_time in sorted(native_keys - aggregated_keys):
        issues.append(
            ValidationIssue(symbol, open_time, "extra_native_bar", "missing", "present")
        )
    incomplete = tuple(incomplete_intervals)
    fallback_symbol = next(
        (item.symbol for item in aggregated_list or native_list), ""
    )
    for interval in incomplete:
        issues.append(
            ValidationIssue(
                fallback_symbol,
                interval.start_utc_ms,
                "partial_edge_bucket",
                f"{interval.start_utc_ms}:{interval.end_utc_ms}",
                "not_comparable",
            )
        )
    common_keys = sorted(aggregated_keys & native_keys)
    if not common_keys and not issues:
        issues.append(ValidationIssue("", -1, "no_comparable_bars", "0", "0"))
    exact_fields = ("open", "high", "low", "close", "close_time_utc_ms", "trade_count")
    for key in common_keys:
        item = aggregated[key]
        reference = native[key]
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
        compared_bars=len(common_keys),
        issues=tuple(issues),
    )
