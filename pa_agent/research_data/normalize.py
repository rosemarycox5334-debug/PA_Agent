from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from pa_agent.research_data.models import ContractRuleSnapshot, FundingRate, Kline


class DataSchemaError(ValueError):
    pass


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DataSchemaError(f"Invalid Decimal field: {field}") from exc
    if not result.is_finite():
        raise DataSchemaError(f"Non-finite Decimal field: {field}")
    return result


def _validate_kline_row(row: Sequence[Any]) -> None:
    if isinstance(row, (str, bytes)) or len(row) < 12:
        raise DataSchemaError("Binance kline row must contain at least 12 fields")


def normalize_trade_kline(
    row: Sequence[Any], *, symbol: str, interval: str, now_ms: int
) -> Kline:
    _validate_kline_row(row)
    try:
        open_time = int(row[0])
        close_time = int(row[6])
        trade_count = int(row[8])
        return Kline(
            source="binance_fapi",
            stream="trade",
            symbol=symbol,
            interval=interval,
            open_time_utc_ms=open_time,
            close_time_utc_ms=close_time,
            open=_decimal(row[1], "open"),
            high=_decimal(row[2], "high"),
            low=_decimal(row[3], "low"),
            close=_decimal(row[4], "close"),
            base_volume=_decimal(row[5], "base_volume"),
            quote_volume=_decimal(row[7], "quote_volume"),
            trade_count=trade_count,
            taker_buy_base_volume=_decimal(row[9], "taker_buy_base_volume"),
            taker_buy_quote_volume=_decimal(row[10], "taker_buy_quote_volume"),
            is_closed=close_time < now_ms,
        )
    except (IndexError, TypeError, ValueError) as exc:
        if isinstance(exc, DataSchemaError):
            raise
        raise DataSchemaError("Invalid Binance trade kline row") from exc


def normalize_price_kline(
    row: Sequence[Any], *, stream: str, symbol: str, interval: str, now_ms: int
) -> Kline:
    _validate_kline_row(row)
    if stream not in {"mark", "index"}:
        raise DataSchemaError("Price kline stream must be mark or index")
    try:
        open_time = int(row[0])
        close_time = int(row[6])
        return Kline(
            source="binance_fapi",
            stream=stream,
            symbol=symbol,
            interval=interval,
            open_time_utc_ms=open_time,
            close_time_utc_ms=close_time,
            open=_decimal(row[1], "open"),
            high=_decimal(row[2], "high"),
            low=_decimal(row[3], "low"),
            close=_decimal(row[4], "close"),
            base_volume=Decimal("0"),
            quote_volume=Decimal("0"),
            trade_count=0,
            taker_buy_base_volume=Decimal("0"),
            taker_buy_quote_volume=Decimal("0"),
            is_closed=close_time < now_ms,
        )
    except (IndexError, TypeError, ValueError) as exc:
        if isinstance(exc, DataSchemaError):
            raise
        raise DataSchemaError("Invalid Binance price kline row") from exc


def normalize_funding_rate(item: Mapping[str, Any]) -> FundingRate:
    required = {"symbol", "fundingTime", "fundingRate", "markPrice"}
    if not required.issubset(item):
        raise DataSchemaError(f"Funding rate missing fields: {sorted(required - set(item))}")
    try:
        return FundingRate(
            source="binance_fapi",
            symbol=str(item["symbol"]),
            funding_time_utc_ms=int(item["fundingTime"]),
            funding_rate=_decimal(item["fundingRate"], "funding_rate"),
            mark_price=_decimal(item["markPrice"], "mark_price"),
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
