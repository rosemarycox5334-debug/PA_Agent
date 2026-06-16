"""Shared A-share symbol normalization and OHLCV helpers.

Used by East Money, Baostock, and optional AkShare paths — not tied to AkShareSource.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pa_agent.data.base import KlineBar, normalize_kline_bar
from pa_agent.data.datetime_ts import datetime_to_ts_ms

_CN_TZ = ZoneInfo("Asia/Shanghai")

_STOCK_CODE_RE = re.compile(r"^\d{6}$")
_INDEX_PREFIX_RE = re.compile(r"^(sh|sz)(\d{6})$", re.IGNORECASE)

PRESET_SYMBOLS: tuple[str, ...] = (
    "000001",
    "600519",
    "000300",
    "399006",
)


def normalize_ashare_symbol(symbol: str) -> str:
    """Normalize user input to 6-digit code or index id (sh000300)."""
    raw = (symbol or "").strip()
    if not raw:
        return ""
    m = _INDEX_PREFIX_RE.match(raw)
    if m:
        prefix, digits = m.group(1).lower(), m.group(2)
        if _is_index_digits(digits):
            return f"{prefix}{digits}"
        return digits
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 6:
        return digits[-6:]
    return digits


def _is_index_digits(digits: str) -> bool:
    return digits in {
        "000300",
        "000016",
        "000905",
        "000852",
        "399001",
        "399006",
        "399300",
    }


def is_index_symbol(symbol: str) -> bool:
    """True for sh/sz-prefixed index codes or common CSI/ChiNext codes."""
    sym = normalize_ashare_symbol(symbol)
    if sym.startswith(("sh", "sz")) and len(sym) >= 8:
        return True
    if _STOCK_CODE_RE.match(sym):
        return _is_index_digits(sym)
    return False


def index_symbol_for_api(symbol: str) -> str:
    sym = normalize_ashare_symbol(symbol)
    if sym.startswith(("sh", "sz")):
        return sym
    if sym.startswith("399"):
        return f"sz{sym}"
    return f"sh{sym}"


def cn_now() -> datetime:
    return datetime.now(tz=_CN_TZ)


def ashare_session_open(now: datetime | None = None) -> bool:
    """True during A-share cash session (Mon–Fri, 09:30–11:30 & 13:00–15:00 CN)."""
    now = now or cn_now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    morning = 9 * 60 + 30 <= t < 11 * 60 + 30
    afternoon = 13 * 60 <= t < 15 * 60
    return morning or afternoon


def ashare_trading_day(now: datetime | None = None) -> bool:
    """True on A-share cash days within 09:30–15:00 CN, including the lunch break."""
    now = now or cn_now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= t < 15 * 60


def ashare_head_bar_live(timeframe: str, now: datetime | None = None) -> bool:
    """Whether snapshot index-0 should be marked unclosed (forming/live).

    Intraday bars only update during continuous sessions; daily bars stay live
    through lunch until 15:00.
    """
    tf = str(timeframe or "").strip().lower()
    if tf == "1d":
        return ashare_trading_day(now)
    return ashare_session_open(now)


def bar_trade_date(ts_open_ms: int) -> datetime.date:
    return datetime.fromtimestamp(int(ts_open_ms) / 1000, tz=_CN_TZ).date()


def quote_volume_lots_to_shares(lots: float, *, symbol: str = "") -> float:
    """Convert East Money quote volume (手) to Baostock bar volume (股)."""
    if lots <= 0:
        return 0.0
    if symbol and is_index_symbol(symbol):
        return float(lots)
    return float(lots) * 100.0


def ensure_today_forming_daily_bar(
    rows_asc: list[dict[str, Any]],
    *,
    symbol: str,
    spot_price: float | None = None,
    session_open: float = 0.0,
    session_high: float = 0.0,
    session_low: float = 0.0,
    session_volume_lots: float = 0.0,
    session_amount: float = 0.0,
    now: datetime | None = None,
) -> bool:
    """交易时段若日线仍停在昨收，补一根「当日未收盘」K 线以便刷新现价。"""
    from datetime import time as time_cls

    if not rows_asc:
        return False
    now = now or cn_now()
    if not ashare_trading_day(now):
        return False
    today = now.date()
    if bar_trade_date(int(rows_asc[-1]["ts_open"])) >= today:
        return False

    prev_close = float(rows_asc[-1]["close"])
    price = float(spot_price) if spot_price and spot_price > 0 else prev_close
    open_ = float(session_open) if session_open > 0 else prev_close
    high = float(session_high) if session_high > 0 else max(open_, price)
    low = float(session_low) if session_low > 0 else min(open_, price)
    high = max(high, open_, price)
    low = min(low, open_, price)
    vol = quote_volume_lots_to_shares(session_volume_lots, symbol=symbol)
    today_ms = int(datetime.combine(today, time_cls(0, 0), tzinfo=_CN_TZ).timestamp() * 1000)
    rows_asc.append(
        {
            "ts_open": today_ms,
            "open": open_,
            "high": high,
            "low": low,
            "close": price,
            "volume": vol,
            "amount": float(session_amount) if session_amount > 0 else 0.0,
            "pct_chg": ((price - prev_close) / prev_close * 100.0) if prev_close > 0 else None,
        }
    )
    return True


def apply_session_quote_to_forming_row(
    row: dict[str, Any],
    *,
    price: float,
    open_: float = 0.0,
    high: float = 0.0,
    low: float = 0.0,
    volume: float = 0.0,
    amount: float = 0.0,
    prev_close: float = 0.0,
    daily: bool = False,
    volume_lots: bool = False,
    symbol: str = "",
) -> None:
    """Refresh the newest (forming) bar from a live quote.

    For daily charts, use exchange session open/high/low so upper/lower shadows
    are visible. For intraday bars, only expand this bar's range with *price*.
    """
    price = float(price)
    row["close"] = price

    if daily:
        if open_ > 0:
            row["open"] = float(open_)
        if high > 0:
            row["high"] = float(high)
        if low > 0:
            row["low"] = float(low)
    else:
        o = float(row.get("open", price))
        row["high"] = max(float(row.get("high", price)), price, o)
        row["low"] = min(float(row.get("low", price)), price, o)

    o = float(row.get("open", price))
    c = float(row["close"])
    row["high"] = max(float(row.get("high", c)), o, c)
    row["low"] = min(float(row.get("low", c)), o, c)

    if volume > 0:
        row["volume"] = (
            quote_volume_lots_to_shares(volume, symbol=symbol)
            if volume_lots
            else float(volume)
        )
    if amount > 0:
        row["amount"] = float(amount)
    if prev_close > 0:
        row["pct_chg"] = (c - prev_close) / prev_close * 100.0


def row_time_to_ts_ms(value: Any) -> int:
    if value is None:
        return int(cn_now().timestamp() * 1000)
    try:
        import pandas as pd

        if isinstance(value, pd.Timestamp):
            ts = value
            if ts.tz is None:
                ts = ts.tz_localize(_CN_TZ)
            else:
                ts = ts.tz_convert(_CN_TZ)
            return int(ts.timestamp() * 1000)
    except ImportError:
        pass
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_CN_TZ)
        return int(value.timestamp() * 1000)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text[: len(fmt)], fmt).replace(tzinfo=_CN_TZ)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return datetime_to_ts_ms(text)


def df_to_bars_asc(df: Any, *, time_col: str) -> list[dict[str, Any]]:
    """Convert normalized ascending OHLCV rows to dicts."""
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        ts = row_time_to_ts_ms(row[time_col])
        o = float(row["open"])
        h = float(row["high"])
        lo = float(row["low"])
        c = float(row["close"])
        vol = float(row.get("volume", 0.0) or 0.0)
        amt = float(row.get("amount", 0.0) or 0.0)
        pct_raw = row.get("pct_chg")
        pct = None
        if pct_raw is not None:
            try:
                pct = float(pct_raw)
            except (ValueError, TypeError):
                pct = None
        rows.append(
            {
                "ts_open": ts,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": vol,
                "pct_chg": pct,
                "amount": amt,
            }
        )
    for i in range(1, len(rows)):
        prev_c = float(rows[i - 1]["close"])
        if prev_c > 0:
            rows[i]["pct_chg"] = (float(rows[i]["close"]) - prev_c) / prev_c * 100.0
    return rows


def normalize_ohlcv_df(df: Any, *, time_col: str) -> Any:
    import pandas as pd

    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    rename: dict[Any, str] = {}
    time_mapped = False
    for col in out.columns:
        c = str(col).strip()
        if c in ("时间", "日期", "date", "datetime", "time"):
            if not time_mapped and str(col) != time_col:
                rename[col] = time_col
                time_mapped = True
        elif c in ("开盘", "open", "Open"):
            rename[col] = "open"
        elif c in ("收盘", "close", "Close"):
            rename[col] = "close"
        elif c in ("最高", "high", "High"):
            rename[col] = "high"
        elif c in ("最低", "low", "Low"):
            rename[col] = "low"
        elif c in ("成交量", "volume", "Volume", "vol", "Vol"):
            rename[col] = "volume"
        elif c in ("成交额", "成交金额", "amount", "Amount"):
            rename[col] = "amount"
        elif c in ("涨跌幅", "pct_chg", "pctChg"):
            rename[col] = "pct_chg"
    out = out.rename(columns=rename)
    drop_cols = [
        c
        for c in out.columns
        if str(c).strip() in ("时间", "日期", "date", "datetime", "time")
        and c != time_col
    ]
    if drop_cols:
        out = out.drop(columns=drop_cols, errors="ignore")
    if time_col not in out.columns:
        return pd.DataFrame()
    for req in ("open", "high", "low", "close"):
        if req not in out.columns:
            return pd.DataFrame()
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out = out.sort_values(time_col).reset_index(drop=True)
    return out


def merge_ohlcv(chunk: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ts_open": chunk[0]["ts_open"],
        "open": chunk[0]["open"],
        "high": max(r["high"] for r in chunk),
        "low": min(r["low"] for r in chunk),
        "close": chunk[-1]["close"],
        "volume": sum(r["volume"] for r in chunk),
    }


def resample_rows_to_4h(rows_asc: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows_asc:
        return []
    buckets: list[dict[str, Any]] = []
    chunk: list[dict[str, Any]] = []
    for row in rows_asc:
        chunk.append(row)
        if len(chunk) == 4:
            buckets.append(merge_ohlcv(chunk))
            chunk = []
    if chunk:
        buckets.append(merge_ohlcv(chunk))
    return buckets


def rows_to_kline_bars(rows_newest_first: list[dict[str, Any]], n: int) -> list[KlineBar]:
    from pa_agent.data.ashare_limits import effective_pct_chg, prev_trading_day_close

    bars: list[KlineBar] = []
    for i, row in enumerate(rows_newest_first[:n]):
        ts_ms = int(row["ts_open"])
        bars.append(
            normalize_kline_bar(
                KlineBar(
                    seq=i + 1,
                    ts_open=float(ts_ms),
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                    amount=float(row.get("amount", 0.0) or 0.0),
                    pct_chg=row.get("pct_chg"),
                    closed=row.get("closed", i != 0),
                )
            )
        )
    filled: list[KlineBar] = []
    for i, bar in enumerate(bars):
        if bar.pct_chg is not None:
            filled.append(bar)
            continue
        prev = prev_trading_day_close(bars, i)
        pct = effective_pct_chg(bar, prev)
        if pct is None:
            filled.append(bar)
        else:
            filled.append(
                KlineBar(
                    seq=bar.seq,
                    ts_open=bar.ts_open,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    amount=bar.amount,
                    pct_chg=pct,
                    closed=bar.closed,
                )
            )
    return filled


# Backward-compatible aliases (legacy private names)
_cn_now = cn_now
_ashare_session_open = ashare_session_open
_ashare_trading_day = ashare_trading_day
_ashare_head_bar_live = ashare_head_bar_live
_row_time_to_ts_ms = row_time_to_ts_ms
_df_to_bars_asc = df_to_bars_asc
_normalize_ohlcv_df = normalize_ohlcv_df
_merge_ohlcv = merge_ohlcv
_resample_rows_to_4h = resample_rows_to_4h
_rows_to_kline_bars = rows_to_kline_bars
_index_symbol_for_api = index_symbol_for_api
_PRESET_SYMBOLS = PRESET_SYMBOLS
