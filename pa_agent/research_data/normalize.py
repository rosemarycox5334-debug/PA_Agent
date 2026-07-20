from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from pa_agent.research_data.models import (
    KLINE_SCHEMA_VERSION,
    ContractRuleSnapshot,
    ContractRuleValidationSnapshot,
    FundingRate,
    Kline,
)


class DataSchemaError(ValueError):
    pass


class ContractRuleValidationFailure(DataSchemaError):
    def __init__(self, snapshot: ContractRuleValidationSnapshot) -> None:
        self.snapshot = snapshot
        self.missing_symbols = snapshot.missing_symbols
        super().__init__(
            "ContractRuleValidationFailure: missing requested symbols: "
            + ", ".join(snapshot.missing_symbols)
        )


INTERVAL_MS = {
    "1m": 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DataSchemaError(f"Invalid Decimal field: {field}") from exc
    if not result.is_finite():
        raise DataSchemaError(f"Non-finite Decimal field: {field}")
    return result


def _integer(value: Any, field: str) -> int:
    number = _decimal(value, field)
    if number != number.to_integral_value():
        raise DataSchemaError(f"Non-integral integer field: {field}")
    return int(number)


def _nonnegative(value: Decimal, field: str) -> Decimal:
    if value < 0:
        raise DataSchemaError(f"Negative field: {field}")
    return value


def _validate_kline_row(row: Sequence[Any], *, interval: str) -> None:
    if isinstance(row, (str, bytes)) or len(row) != 12:
        raise DataSchemaError(
            f"Binance kline {KLINE_SCHEMA_VERSION} must contain exactly 12 fields"
        )
    if interval not in INTERVAL_MS:
        raise DataSchemaError(f"Unsupported kline interval: {interval}")


def _validated_ohlc(row: Sequence[Any], *, interval: str) -> tuple[int, int, Decimal, Decimal, Decimal, Decimal]:
    open_time = _integer(row[0], "open_time")
    close_time = _integer(row[6], "close_time")
    if open_time < 0 or close_time != open_time + INTERVAL_MS[interval] - 1:
        raise DataSchemaError("Invalid kline open/close time or interval boundary")
    open_price = _decimal(row[1], "open")
    high = _decimal(row[2], "high")
    low = _decimal(row[3], "low")
    close = _decimal(row[4], "close")
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        raise DataSchemaError("Invalid OHLC relationship")
    return open_time, close_time, open_price, high, low, close


def normalize_trade_kline(
    row: Sequence[Any],
    *,
    symbol: str,
    interval: str,
    source_server_time_utc_ms: int,
) -> Kline:
    _validate_kline_row(row, interval=interval)
    try:
        open_time, close_time, open_price, high, low, close = _validated_ohlc(
            row, interval=interval
        )
        if close_time >= source_server_time_utc_ms:
            raise DataSchemaError(
                "UNCLOSED_BAR: kline close boundary has not passed exchange server time"
            )
        trade_count = _integer(row[8], "trade_count")
        if trade_count < 0:
            raise DataSchemaError("Negative field: trade_count")
        return Kline(
            source="binance_fapi",
            stream="trade",
            symbol=symbol,
            interval=interval,
            open_time_utc_ms=open_time,
            close_time_utc_ms=close_time,
            open=open_price,
            high=high,
            low=low,
            close=close,
            base_volume=_nonnegative(_decimal(row[5], "base_volume"), "base_volume"),
            quote_volume=_nonnegative(_decimal(row[7], "quote_volume"), "quote_volume"),
            trade_count=trade_count,
            taker_buy_base_volume=_nonnegative(
                _decimal(row[9], "taker_buy_base_volume"), "taker_buy_base_volume"
            ),
            taker_buy_quote_volume=_nonnegative(
                _decimal(row[10], "taker_buy_quote_volume"), "taker_buy_quote_volume"
            ),
            is_closed=True,
        )
    except (IndexError, TypeError, ValueError) as exc:
        if isinstance(exc, DataSchemaError):
            raise
        raise DataSchemaError("Invalid Binance trade kline row") from exc


def normalize_price_kline(
    row: Sequence[Any],
    *,
    stream: str,
    symbol: str,
    interval: str,
    source_server_time_utc_ms: int,
) -> Kline:
    _validate_kline_row(row, interval=interval)
    if stream not in {"mark", "index"}:
        raise DataSchemaError("Price kline stream must be mark or index")
    try:
        open_time, close_time, open_price, high, low, close = _validated_ohlc(
            row, interval=interval
        )
        if close_time >= source_server_time_utc_ms:
            raise DataSchemaError(
                "UNCLOSED_BAR: kline close boundary has not passed exchange server time"
            )
        return Kline(
            source="binance_fapi",
            stream=stream,
            symbol=symbol,
            interval=interval,
            open_time_utc_ms=open_time,
            close_time_utc_ms=close_time,
            open=open_price,
            high=high,
            low=low,
            close=close,
            base_volume=Decimal("0"),
            quote_volume=Decimal("0"),
            trade_count=0,
            taker_buy_base_volume=Decimal("0"),
            taker_buy_quote_volume=Decimal("0"),
            is_closed=True,
        )
    except (IndexError, TypeError, ValueError) as exc:
        if isinstance(exc, DataSchemaError):
            raise
        raise DataSchemaError("Invalid Binance price kline row") from exc


def normalize_funding_rate(
    item: Mapping[str, Any], *, expected_symbol: str
) -> FundingRate:
    required = {"symbol", "fundingTime", "fundingRate", "markPrice"}
    if not required.issubset(item):
        raise DataSchemaError(f"Funding rate missing fields: {sorted(required - set(item))}")
    try:
        returned_symbol = str(item["symbol"])
        if returned_symbol != expected_symbol:
            raise DataSchemaError(
                f"Funding symbol mismatch: expected {expected_symbol}, got {returned_symbol}"
            )
        funding_time = _integer(item["fundingTime"], "funding_time")
        if funding_time < 0:
            raise DataSchemaError("Invalid funding_time: must be nonnegative")
        funding_rate = _decimal(item["fundingRate"], "funding_rate")
        mark_price = _decimal(item["markPrice"], "mark_price")
        if mark_price <= 0:
            raise DataSchemaError("Invalid mark_price: must be positive")
        return FundingRate(
            source="binance_fapi",
            symbol=returned_symbol,
            funding_time_utc_ms=funding_time,
            funding_rate=funding_rate,
            mark_price=mark_price,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, DataSchemaError):
            raise
        raise DataSchemaError("Invalid funding rate record") from exc


def normalize_contract_rules(
    payload: Mapping[str, Any],
    *,
    symbols: Sequence[str],
    acquired_at_utc_ms: int,
    source_hash: str,
) -> tuple[ContractRuleSnapshot, ...]:
    validation = contract_rule_validation_snapshot(
        payload,
        symbols=symbols,
        acquired_at_utc_ms=acquired_at_utc_ms,
        source_hash=source_hash,
    )
    if validation.missing_symbols:
        raise ContractRuleValidationFailure(validation)
    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, list):
        raise DataSchemaError("exchangeInfo symbols must be a list")
    wanted = set(symbols)
    rules: list[ContractRuleSnapshot] = []
    for raw in raw_symbols:
        if not isinstance(raw, dict) or raw.get("symbol") not in wanted:
            continue
        filters = {
            entry.get("filterType"): entry
            for entry in raw.get("filters", [])
            if isinstance(entry, dict)
        }
        try:
            price = filters["PRICE_FILTER"]
            lot = filters["LOT_SIZE"]
            notional = filters.get("MIN_NOTIONAL") or filters["NOTIONAL"]
            rules.append(
                ContractRuleSnapshot(
                    source="binance_fapi_exchange_info",
                    symbol=str(raw["symbol"]),
                    status=str(raw["status"]),
                    price_tick=_decimal(price["tickSize"], "tick_size"),
                    quantity_step=_decimal(lot["stepSize"], "step_size"),
                    min_quantity=_decimal(lot["minQty"], "min_quantity"),
                    min_notional=_decimal(notional["notional"], "min_notional"),
                    acquired_at_utc_ms=acquired_at_utc_ms,
                    source_hash=source_hash,
                    validity="CURRENT_SNAPSHOT_ONLY",
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, DataSchemaError):
                raise
            raise DataSchemaError(f"Invalid contract filters for {raw.get('symbol')}") from exc
    return tuple(sorted(rules, key=lambda rule: rule.symbol))


def contract_rule_validation_snapshot(
    payload: Mapping[str, Any],
    *,
    symbols: Sequence[str],
    acquired_at_utc_ms: int,
    source_hash: str,
) -> ContractRuleValidationSnapshot:
    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, list):
        raise DataSchemaError("exchangeInfo symbols must be a list")
    requested = tuple(sorted(set(symbols)))
    returned = tuple(
        sorted(
            {
                str(item["symbol"])
                for item in raw_symbols
                if isinstance(item, dict) and item.get("symbol") in requested
            }
        )
    )
    missing = tuple(sorted(set(requested) - set(returned)))
    return ContractRuleValidationSnapshot(
        requested_symbols=requested,
        returned_symbols=returned,
        missing_symbols=missing,
        source_hash=source_hash,
        acquired_at_utc_ms=acquired_at_utc_ms,
        validity="CURRENT_SNAPSHOT_ONLY",
        review_status="VALIDATION_FAILED" if missing else "REVIEW_REQUIRED",
    )
