"""Core data types and DataSource abstract base class."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence


# ── KlineBar ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KlineBar:
    """A single OHLCV bar with sequence number and closed flag."""
    seq: int           # 1 = newest (including forming bar), N = oldest
    ts_open: float     # Unix timestamp (seconds) of bar open
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool       # False for the currently-forming bar


# ── IndicatorBundle ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IndicatorBundle:
    """Per-bar indicator values aligned to a KlineFrame's bars list."""
    ema20: tuple[float, ...]   # len == len(bars); nan for warm-up period
    atr14: tuple[float, ...]   # len == len(bars); nan for warm-up period


# ── KlineFrame ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KlineFrame:
    """Immutable snapshot of N bars plus computed indicators.

    bars[0] is the newest bar (seq=1, closed=False).
    bars[-1] is the oldest bar (seq=N, closed=True).
    snapshot_ts_local_ms is the local machine time when the snapshot was taken.
    """
    symbol: str
    timeframe: str
    bars: tuple[KlineBar, ...]
    indicators: IndicatorBundle
    snapshot_ts_local_ms: int   # milliseconds since epoch, local time


# ── DataSource ABC ────────────────────────────────────────────────────────────

class DataSourceError(Exception):
    """Base class for data source errors."""


class DataSourceTransientError(DataSourceError):
    """Transient (retryable) error from a data source."""


class DataSource(ABC):
    """Abstract interface for K-line data providers.

    Implementations: TradingViewSource (active), MT5Source (stub).
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish connection / authenticate."""

    @abstractmethod
    def disconnect(self) -> None:
        """Tear down connection cleanly."""

    @abstractmethod
    def list_symbols(self) -> list[str]:
        """Return available symbol names."""

    @abstractmethod
    def supported_timeframes(self) -> list[str]:
        """Return supported timeframe strings, e.g. ['1m','5m','1h','1d']."""

    @abstractmethod
    def subscribe(self, symbol: str, timeframe: str) -> None:
        """Subscribe to live updates for *symbol* at *timeframe*."""

    @abstractmethod
    def unsubscribe(self) -> None:
        """Cancel the current subscription."""

    @abstractmethod
    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """Return the *n* most recent bars (index 0 = newest, including forming bar).

        Raises DataSourceTransientError on recoverable network issues.
        """

    @abstractmethod
    def load_history_range(
        self,
        symbol: str,
        timeframe: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[KlineBar]:
        """Load historical K-line data for the given symbol/timeframe/date range.

        Returns bars in **ascending** (oldest-first) time order.
        All bars have ``closed=True``.
        Raises DataSourceTransientError on recoverable failures.
        """
