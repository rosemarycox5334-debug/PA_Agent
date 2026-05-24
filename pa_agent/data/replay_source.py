"""Replay data source — loads historical bars and advances one by one."""
from __future__ import annotations

import logging
from typing import Any

from pa_agent.data.base import KlineBar, KlineFrame, IndicatorBundle
from pa_agent.data.snapshot import compute_indicators
from pa_agent.util.timefmt import now_local_ms

logger = logging.getLogger(__name__)


class ReplaySource:
    """Manages a list of historical KlineBar objects for step-by-step replay.

    Parameters
    ----------
    bars:
        Historical KlineBar list in **ascending** (oldest-first) order.
    symbol:
        Trading symbol (e.g. "XAUUSD").
    timeframe:
        Timeframe string (e.g. "15m").
    """

    def __init__(
        self,
        bars: list[KlineBar],
        symbol: str,
        timeframe: str,
    ) -> None:
        if not bars:
            raise ValueError("ReplaySource requires at least one bar")

        self._all_bars: list[KlineBar] = bars  # oldest-first
        self._symbol = symbol
        self._timeframe = timeframe
        self._current_index: int = 0  # 0-based index into _all_bars
        self._total_count = len(bars)

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def timeframe(self) -> str:
        return self._timeframe

    @property
    def current_index(self) -> int:
        """0-based index of the current bar being displayed."""
        return self._current_index

    @property
    def total_count(self) -> int:
        return self._total_count

    @property
    def has_next(self) -> bool:
        """True if there is at least one more bar to advance to."""
        return self._current_index < self._total_count - 1

    @property
    def progress_text(self) -> str:
        """Human-readable progress, e.g. 'K线 5/100'."""
        return f"K线 {self._current_index + 1}/{self._total_count}"

    # ── Navigation ───────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset to the first bar."""
        self._current_index = 0

    def advance(self) -> bool:
        """Advance to the next bar. Returns False if already at the end."""
        if not self.has_next:
            return False
        self._current_index += 1
        return True

    # ── Frame building ───────────────────────────────────────────────────────

    def current_frame(self) -> KlineFrame:
        """Build a KlineFrame from bars[0..current_index] (newest-first).

        The frame includes all bars from the start up to the current position.
        The newest bar is at index 0 (seq=1, closed=True).
        """
        # Slice bars[0..current_index] and reverse to newest-first
        visible = list(reversed(self._all_bars[: self._current_index + 1]))

        # Re-assign seq numbers
        rebased: list[KlineBar] = [
            KlineBar(
                seq=i + 1,
                ts_open=b.ts_open,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                closed=True,  # All historical bars are closed
            )
            for i, b in enumerate(visible)
        ]

        indicators = compute_indicators(rebased)

        return KlineFrame(
            symbol=self._symbol,
            timeframe=self._timeframe,
            bars=tuple(rebased),
            indicators=indicators,
            snapshot_ts_local_ms=now_local_ms(),
        )

    def analysis_frame(self, n: int | None = None) -> KlineFrame:
        """Build a KlineFrame suitable for AI analysis.

        If *n* is None, uses all bars up to current_index.
        If *n* is specified, uses the most recent *n* bars.
        """
        if n is not None and n > 0:
            start = max(0, self._current_index + 1 - n)
            visible = list(reversed(self._all_bars[start : self._current_index + 1]))
        else:
            visible = list(reversed(self._all_bars[: self._current_index + 1]))

        rebased: list[KlineBar] = [
            KlineBar(
                seq=i + 1,
                ts_open=b.ts_open,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                closed=True,
            )
            for i, b in enumerate(visible)
        ]

        indicators = compute_indicators(rebased)

        return KlineFrame(
            symbol=self._symbol,
            timeframe=self._timeframe,
            bars=tuple(rebased),
            indicators=indicators,
            snapshot_ts_local_ms=now_local_ms(),
        )