"""Parse East Money push2 quote payloads (五档/十档 + 逐笔)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pa_agent.data.eastmoney_quote_api import (
    ASK_FIELD_PAIRS,
    BID_FIELD_PAIRS,
    FREE_DEPTH_LEVELS,
    L2_ASK_EXTENDED,
    L2_BID_EXTENDED,
    L2_DEPTH_LEVELS,
)


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    volume: int  # 手（与东财网页一致）


@dataclass
class StockOrderBook:
    code: str
    name: str
    price: float
    pct_chg: float
    open: float
    high: float
    low: float
    prev_close: float
    volume: int
    amount: float
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    depth_levels: int = FREE_DEPTH_LEVELS
    depth_source: str = "push2_free"  # push2_free | push2_l2


@dataclass(frozen=True)
class TickTrade:
    time: str
    price: float
    volume: int
    side_hint: str


def _to_float(raw: Any) -> float | None:
    if raw is None or raw == "-":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _to_int(raw: Any) -> int:
    val = _to_float(raw)
    if val is None:
        return 0
    return int(val)


def _parse_side(levels: list[str]) -> str:
    if len(levels) < 5:
        return "—"
    flag = (levels[4] or "").strip()
    return {
        "1": "买",
        "2": "卖",
        "0": "中性",
        "4": "竞价",
    }.get(flag, flag or "—")


def _infer_fltt(payload: dict[str, Any]) -> int:
    """Detect fltt=1 (分) vs fltt=2 (元)."""
    price = _to_float(payload.get("f43"))
    if price is None:
        return 2
    prev = _to_float(payload.get("f60"))
    if prev is not None and prev > 500 and price > 500:
        return 1
    if price > 500:
        return 1
    return 2


def _price_decimals(payload: dict[str, Any], fltt: int) -> int:
    """Return the price precision advertised by East Money for ``fltt=1``."""
    if fltt != 1:
        return 0
    raw = _to_float(payload.get("f59"))
    if raw is None:
        return 2
    decimals = int(raw)
    return decimals if 0 <= decimals <= 6 else 2


def _scale_price(
    val: float | None,
    fltt: int,
    *,
    decimals: int = 2,
) -> float | None:
    if val is None:
        return None
    if fltt == 1:
        return val / (10.0**decimals)
    return val


def _scale_pct(val: float | None, fltt: int) -> float:
    if val is None:
        return 0.0
    if fltt == 1:
        return val / 100.0
    return val


def _levels_from_pairs(
    payload: dict[str, Any],
    pairs: tuple[tuple[str, str], ...],
    *,
    fltt: int,
    decimals: int,
) -> list[OrderBookLevel]:
    out: list[OrderBookLevel] = []
    for px_key, vol_key in pairs:
        px = _scale_price(
            _to_float(payload.get(px_key)),
            fltt,
            decimals=decimals,
        )
        vol = _to_int(payload.get(vol_key))
        if px is not None and px > 0:
            out.append(OrderBookLevel(px, vol))
    return out


def parse_order_book_payload(
    payload: dict[str, Any],
    *,
    fltt: int | None = None,
) -> StockOrderBook | None:
    """Parse ``/api/qt/stock/get`` — 五档 + 可选 L2 六~十档（f21–f30 / f1–f10）。"""
    if not payload:
        return None
    flt = fltt or _infer_fltt(payload)
    price_decimals = _price_decimals(payload, flt)
    code = str(payload.get("f57") or "").strip()
    name = str(payload.get("f58") or code).strip()
    price = _scale_price(
        _to_float(payload.get("f43")),
        flt,
        decimals=price_decimals,
    )
    if price is None:
        return None

    asks = _levels_from_pairs(
        payload,
        ASK_FIELD_PAIRS,
        fltt=flt,
        decimals=price_decimals,
    )
    bids = _levels_from_pairs(
        payload,
        BID_FIELD_PAIRS,
        fltt=flt,
        decimals=price_decimals,
    )

    l2_asks = _levels_from_pairs(
        payload,
        L2_ASK_EXTENDED,
        fltt=flt,
        decimals=price_decimals,
    )
    l2_bids = _levels_from_pairs(
        payload,
        L2_BID_EXTENDED,
        fltt=flt,
        decimals=price_decimals,
    )
    if l2_asks or l2_bids:
        asks = asks + l2_asks
        bids = bids + l2_bids
        depth = L2_DEPTH_LEVELS
        source = "push2_l2"
    else:
        depth = FREE_DEPTH_LEVELS
        source = "push2_free"

    return StockOrderBook(
        code=code,
        name=name,
        price=price,
        pct_chg=_scale_pct(_to_float(payload.get("f170")), flt),
        open=_scale_price(
            _to_float(payload.get("f46")),
            flt,
            decimals=price_decimals,
        )
        or 0.0,
        high=_scale_price(
            _to_float(payload.get("f44")),
            flt,
            decimals=price_decimals,
        )
        or 0.0,
        low=_scale_price(
            _to_float(payload.get("f45")),
            flt,
            decimals=price_decimals,
        )
        or 0.0,
        prev_close=_scale_price(
            _to_float(payload.get("f60")),
            flt,
            decimals=price_decimals,
        )
        or 0.0,
        volume=_to_int(payload.get("f47")),
        amount=_to_float(payload.get("f48")) or 0.0,
        bids=bids,
        asks=asks,
        depth_levels=depth,
        depth_source=source,
    )


def parse_tick_lines(lines: list[str], *, tail: int = 30) -> list[TickTrade]:
    """Parse ``details`` strings: ``HH:MM:SS,price,volume,?,side``."""
    out: list[TickTrade] = []
    for raw in lines:
        text = (raw or "").strip()
        if not text:
            continue
        parts = text.split(",")
        if len(parts) < 3:
            continue
        price = _to_float(parts[1])
        if price is None:
            continue
        out.append(
            TickTrade(
                time=parts[0],
                price=price,
                volume=_to_int(parts[2]),
                side_hint=_parse_side(parts),
            )
        )
    if tail > 0 and len(out) > tail:
        return out[-tail:]
    return out
