"""yfinance-based data source for futures and equity data.

Supports symbols like GC=F (Gold), CL=F (Crude Oil), ES=F (S&P 500),
NQ=F (Nasdaq), BTC-USD, etc.

Note: yfinance data has ~15 min delay for futures. Intraday data
(< 1d interval) is only available for the last 60 days.
"""
from __future__ import annotations

import logging

from pa_agent.data.base import (
    DataSource,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)
from pa_agent.data.datetime_ts import datetime_to_ts_ms

logger = logging.getLogger(__name__)

# Map our timeframe strings → yfinance interval strings
_TF_MAP: dict[str, str] = {
    "1m":  "1m",
    "2m":  "2m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
    "4h":  "1h",   # yfinance has no 4h; use 1h and take every 4th bar
    "1d":  "1d",
    "1w":  "1wk",
    "1M":  "1mo",
}

# How many bars to request from yfinance (we need more for 4h aggregation)
_FETCH_MULTIPLIER: dict[str, int] = {
    "4h": 4,   # fetch 4x bars then downsample
}

# yfinance period strings for intraday vs daily
_INTRADAY_TF = {"1m", "2m", "5m", "15m", "30m", "1h", "4h"}


class YFinanceSource(DataSource):
    """K-line data from Yahoo Finance (yfinance).

    Suitable for futures (GC=F, CL=F, ES=F, NQ=F) and crypto (BTC-USD).
    Data has ~15 min delay for futures; intraday only available last 60 days.
    """

    def __init__(self) -> None:
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        try:
            import yfinance  # noqa: F401  — just verify it's installed
            self._connected = True
            logger.info("YFinanceSource connected (yfinance available)")
        except ImportError as exc:
            raise DataSourceTransientError(
                "yfinance not installed — run: pip install yfinance"
            ) from exc

    def disconnect(self) -> None:
        self._connected = False
        logger.info("YFinanceSource disconnected")

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        return [
            "GC=F",    # Gold futures
            "CL=F",    # Crude Oil futures
            "ES=F",    # S&P 500 futures
            "NQ=F",    # Nasdaq futures
            "SI=F",    # Silver futures
            "HG=F",    # Copper futures
            "BTC-USD", # Bitcoin
            "ETH-USD", # Ethereum
        ]

    def supported_timeframes(self) -> list[str]:
        return list(_TF_MAP.keys())

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _TF_MAP:
            raise ValueError(
                f"Unsupported timeframe: {timeframe!r}. Use one of {list(_TF_MAP)}"
            )
        self._symbol = symbol
        self._timeframe = timeframe
        logger.info("YFinanceSource subscribed: %s %s", symbol, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        logger.info("YFinanceSource unsubscribed")

    # ── Data fetch ────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """Return *n* bars newest-first; bars[0] is the forming (unclosed) bar."""
        if not self._connected:
            raise DataSourceTransientError("Not connected — call connect() first")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("Not subscribed — call subscribe() first")

        try:
            import yfinance as yf
        except ImportError as exc:
            raise DataSourceTransientError("yfinance not installed") from exc

        yf_interval = _TF_MAP[self._timeframe]
        is_4h = self._timeframe == "4h"

        # Determine how many bars to request
        fetch_n = n * _FETCH_MULTIPLIER.get(self._timeframe, 1) + 10

        # Choose period based on timeframe
        if self._timeframe in _INTRADAY_TF:
            period = "60d"   # max for intraday
        else:
            period = "2y"

        try:
            ticker = yf.Ticker(self._symbol)
            df = ticker.history(period=period, interval=yf_interval)
        except Exception as exc:
            raise DataSourceTransientError(f"yfinance fetch failed: {exc}") from exc

        if df is None or df.empty:
            raise DataSourceTransientError(
                f"yfinance returned no data for {self._symbol} {yf_interval}"
            )

        # For 4h: resample 1h → 4h
        if is_4h:
            df = _resample_4h(df)

        # df is sorted oldest-first; take the last fetch_n rows
        df = df.tail(fetch_n)

        # Reverse to newest-first
        df = df.iloc[::-1].reset_index()

        bars: list[KlineBar] = []
        for i, row in enumerate(df.itertuples(index=False)):
            ts_ms = _row_ts_ms(row)
            # bars[0] is the forming (most recent, possibly unclosed) bar
            bars.append(
                normalize_kline_bar(
                    KlineBar(
                        seq=i + 1,
                        ts_open=ts_ms,
                        open=float(row.Open),
                        high=float(row.High),
                        low=float(row.Low),
                        close=float(row.Close),
                        volume=float(getattr(row, "Volume", 0.0)),
                        closed=(i != 0),
                    )
                )
            )
            if len(bars) >= n:
                break

        return bars


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resample_4h(df):
    """Resample a 1h OHLCV DataFrame to 4h bars."""
    import pandas as pd

    df = df.copy()
    # Ensure the index is a DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    # Make timezone-aware if needed
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    resampled = df.resample("4h").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna()
    return resampled


def _row_ts_ms(row) -> int:
    """Extract bar open time in milliseconds from a yfinance DataFrame row."""
    dt = getattr(row, "Datetime", None) or getattr(row, "Date", None)
    return datetime_to_ts_ms(dt)
