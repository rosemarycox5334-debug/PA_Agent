"""RQData (RiceQuant) data source for China A-shares, indices and futures.

Requires: pip install rqdatac
License Key configured via settings.json → general.rqdata_license_key

Supported symbols:
  - A-shares: 000001.XSHE, 600519.XSHG (6-digit + exchange suffix)
  - Indices:  000001.XSHG (上证指数), 000300.XSHG (沪深300)
  - Futures:  IF2506, RB2506, CU2506, AU2506 (code + 4-digit expiry, no suffix)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from pa_agent.data.base import (
    DataSource,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)
from pa_agent.data.datetime_ts import datetime_to_ts_ms

logger = logging.getLogger(__name__)

# Map PA Agent timeframe strings → rqdatac frequency strings
_TF_MAP: dict[str, str] = {
    "1m":  "1m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "60m",
    "60m": "60m",
    "1d":  "1d",
    "1w":  "1w",
}

# Common RQData order_book_ids for UI hints
_RQ_SYMBOL_HINTS: list[str] = [
    # Indices
    "000001.XSHG",   # 上证指数
    "399001.XSHE",   # 深证成指
    "000300.XSHG",   # 沪深300
    "000905.XSHG",   # 中证500
    "000016.XSHG",   # 上证50
    # A-shares
    "000001.XSHE",   # 平安银行
    "600519.XSHG",   # 贵州茅台
    "300750.XSHE",   # 宁德时代
    "600036.XSHG",   # 招商银行
    "000858.XSHE",   # 五粮液
    # Futures (CFFEX / SHFE / DCE / ZCE) — 请替换为当前主力合约
    "IF2606",        # 沪深300股指期货
    "IC2606",        # 中证500股指期货
    "IH2606",        # 上证50股指期货
    "RB2606",        # 螺纹钢
    "CU2606",        # 沪铜
    "AU2606",        # 沪金
    "AG2606",        # 沪银
    "AL2606",        # 沪铝
    "FU2606",        # 燃料油
    "M2606",         # 豆粕
    "TA2606",        # PTA
    "SR2606",        # 白糖
]


import re

_FUTURES_RE = re.compile(r"^[A-Za-z]{1,2}\d{4}$")


def _normalize_rq_symbol(symbol: str) -> str:
    """Normalize user input to RQData order_book_id format.

    Supports:
      - Futures code (1-2 letters + 4 digits): IF2506, RB2506 → kept as-is
      - 6-digit A-share code → auto-detect exchange (.XSHE / .XSHG)
      - Full order_book_id like 000001.XSHE
      - Index codes like 000001 (→ 000001.XSHG)
    """
    s = (symbol or "").strip()
    if not s:
        return "000001.XSHG"

    # Already full order_book_id
    if ".XSHG" in s.upper() or ".XSHE" in s.upper():
        return s

    # Futures code (e.g. IF2506, RB2506, m2506) — must stay suffix-free
    if _FUTURES_RE.match(s):
        return s.upper()

    # 6-digit numeric code — infer exchange
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) == 6:
        # SSE indices / 600-603 / 688-689 / 900
        if digits.startswith(("000", "600", "601", "603", "605", "688", "689", "900")):
            return f"{digits}.XSHG"
        return f"{digits}.XSHE"

    return s


class RQDataSource(DataSource):
    """K-line data from RiceQuant RQData (China A-shares, indices & futures).

    Supports equities (with exchange suffix) and futures (suffix-free code).
    Minute-bar availability depends on your RQData license tier.
    """

    def __init__(self) -> None:
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._license_key: str = ""

    def set_license(self, license_key: str) -> None:
        """Set the RQData license key before connect()."""
        self._license_key = (license_key or "").strip()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        try:
            import rqdatac  # type: ignore[import]
        except ImportError as exc:
            raise DataSourceTransientError(
                "rqdatac not installed — run: pip install rqdatac"
            ) from exc

        if not self._license_key:
            raise DataSourceTransientError(
                "RQData license key not configured. "
                "Set it in Settings → API Key (saved to config/settings.json)."
            )

        try:
            rqdatac.init(username="license", password=self._license_key, lazy=True)
            self._connected = True
            logger.info("RQDataSource connected")
        except Exception as exc:
            self._connected = False
            raise DataSourceTransientError(f"RQData init failed: {exc}") from exc

    def disconnect(self) -> None:
        self._connected = False
        logger.info("RQDataSource disconnected")

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        return list(_RQ_SYMBOL_HINTS)

    def supported_timeframes(self) -> list[str]:
        return list(_TF_MAP.keys())

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _TF_MAP:
            raise ValueError(
                f"Unsupported timeframe: {timeframe!r}. "
                f"Use one of {list(_TF_MAP)}"
            )
        self._symbol = _normalize_rq_symbol(symbol)
        self._timeframe = timeframe
        logger.info("RQDataSource subscribed: %s %s", self._symbol, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        logger.info("RQDataSource unsubscribed")

    # ── Data fetch ────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("Not connected — call connect() first")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("Not subscribed — call subscribe() first")

        try:
            import rqdatac  # type: ignore[import]
        except ImportError as exc:
            raise DataSourceTransientError("rqdatac not installed") from exc

        freq = _TF_MAP[self._timeframe]

        # Calculate date range: fetch enough calendar days to cover n bars
        # For daily: n days; for minute: ~n * bar_minutes / 240 trading minutes/day
        bar_minutes = _bar_minutes(self._timeframe)
        if bar_minutes:
            trading_days_needed = max(1, (n * bar_minutes) // 240 + 2)
        else:
            trading_days_needed = n + 5

        # RQData get_price with end_date=today only returns data up to the day
        # session close (15:00 Beijing); night session (21:00-23:00) is excluded.
        # Use tomorrow as end_date so night-session bars are included.
        end_date = datetime.now(timezone.utc).date() + timedelta(days=1)
        start_date = end_date - timedelta(days=trading_days_needed * 2)

        try:
            df = rqdatac.get_price(
                self._symbol,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                frequency=freq,
            )
        except Exception as exc:
            raise DataSourceTransientError(f"RQData get_price failed: {exc}") from exc

        if df is None or df.empty:
            raise DataSourceTransientError(
                f"RQData returned no data for {self._symbol} {self._timeframe}. "
                f"Check symbol code and license permissions."
            )

        # rqdatac returns oldest-first data; reverse to newest-first while
        # preserving index date/time values as columns for timestamp extraction.
        df = df.iloc[::-1].reset_index()

        bars: list[KlineBar] = []
        for i, row in enumerate(df.itertuples(index=False)):
            # Handle MultiIndex column names like ('open', '000001.XSHG')
            open_val = _get_col(row, "open")
            high_val = _get_col(row, "high")
            low_val = _get_col(row, "low")
            close_val = _get_col(row, "close")
            volume_val = _get_col(row, "volume")

            # Determine timestamp from index or column
            ts_ms = _row_ts_ms(row)
            if ts_ms is None:
                # Fallback: use end_date minus i bars
                ts_ms = _fallback_ts(i, self._timeframe)

            is_forming = _is_bar_forming(ts_ms, self._timeframe)

            bars.append(
                normalize_kline_bar(
                    KlineBar(
                        seq=i + 1,
                        ts_open=ts_ms,
                        open=float(open_val),
                        high=float(high_val),
                        low=float(low_val),
                        close=float(close_val),
                        volume=float(volume_val) if volume_val is not None else 0.0,
                        closed=not is_forming,
                    )
                )
            )
            if len(bars) >= n:
                break

        return bars


# ── Helpers ──────────────────────────────────────────────────────────────────

def _bar_minutes(tf: str) -> int | None:
    """Approximate minutes per bar for trading-day estimation."""
    mapping = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "60m": 60,
        "1d": None,   # daily: use calendar-day logic
        "1w": None,
    }
    return mapping.get(tf)


def _get_col(row, name: str):
    """Extract a column value from a namedtuple row, handling MultiIndex."""
    # Try direct attribute
    val = getattr(row, name, None)
    if val is not None:
        return val
    # Try first attribute that contains the name (MultiIndex fallback)
    for attr in row._fields:
        if name in attr.lower():
            return getattr(row, attr)
    return None


def _row_ts_ms(row) -> int | None:
    """Extract bar timestamp from rqdatac DataFrame row.

    RQData returns China market time (UTC+8) as naive datetime.
    We must localize to Asia/Shanghai before converting to UTC epoch.
    """
    dt = getattr(row, "date", None)
    if dt is None:
        # Try datetime or index fields
        for attr in row._fields:
            if "date" in attr.lower() or "time" in attr.lower():
                dt = getattr(row, attr)
                break
    if dt is None:
        return None

    # RQData returns China market time (UTC+8) as naive datetime.
    # Localize to Asia/Shanghai first, then convert to UTC epoch.
    try:
        import pandas as pd
        if isinstance(dt, pd.Timestamp) and dt.tz is None:
            dt = dt.tz_localize("Asia/Shanghai").tz_convert("UTC")
    except ImportError:
        pass

    return datetime_to_ts_ms(dt)


def _fallback_ts(bar_index: int, tf: str) -> int:
    """Generate a plausible timestamp when row timestamp is unavailable."""
    now = datetime.now(timezone.utc)
    minutes = _bar_minutes(tf) or 1
    delta = timedelta(minutes=minutes * bar_index)
    ts = (now - delta).timestamp() * 1000
    return int(ts)


def _bar_duration_ms(tf: str) -> int:
    minutes = _bar_minutes(tf)
    if minutes is not None:
        return minutes * 60_000
    if tf == "1w":
        return 7 * 24 * 60 * 60_000
    return 24 * 60 * 60_000


def _is_bar_forming(ts_open_ms: int, tf: str, *, now_ms: int | None = None) -> bool:
    """Return True only when the bar close time is still in the future."""
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return ts_open_ms + _bar_duration_ms(tf) > now_ms
