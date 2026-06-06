"""Service layer for data access."""
from __future__ import annotations

from typing import Any

from pa_agent.data.base import DataSource, DataSourceTransientError, KlineFrame
from pa_agent.data.factory import (
    configure_data_source,
    create_data_source,
    normalize_data_source_kind,
)
from pa_agent.data.snapshot import build_live_frame


class DataService:
    """Provides formatted snapshots from a DataSource for web consumers."""

    def __init__(
        self,
        data_source: DataSource,
        symbol: str,
        timeframe: str,
        settings: Any = None,
    ) -> None:
        self._source = data_source
        self._symbol = symbol
        self._timeframe = timeframe
        self._settings = settings
        self._source_kind = self._settings_kind()
        self.last_error: str | None = None

    def _settings_general(self) -> Any | None:
        if self._settings is None:
            return None
        return getattr(self._settings, "general", None)

    def _settings_kind(self) -> str | None:
        general = self._settings_general()
        if general is None:
            return None
        return normalize_data_source_kind(getattr(general, "last_data_source", None))

    def _configure_source(self) -> None:
        """Apply provider-specific settings to the current source."""
        configure_data_source(self._source, self._source_kind, self._settings)

    def _apply_source_kind_from_settings(self) -> None:
        """Replace the source if settings selected a different provider."""
        kind = self._settings_kind()
        if kind is None or kind == self._source_kind:
            return
        try:
            self._source.disconnect()
        except Exception:
            pass
        self._source = create_data_source(kind)
        self._source_kind = kind

    def _connect_and_subscribe(self) -> None:
        """Connect the current source and subscribe to the current symbol."""
        self._configure_source()
        self._source.connect()
        self._source.subscribe(self._symbol, self._timeframe)

    def _ensure_subscription(self) -> None:
        """Re-subscribe if settings symbol/timeframe changed since last call."""
        if self._settings is None:
            return
        g = getattr(self._settings, "general", None)
        if g is None:
            return
        new_symbol = getattr(g, "last_symbol", None) or self._symbol
        new_timeframe = getattr(g, "last_timeframe", None) or self._timeframe
        if new_symbol != self._symbol or new_timeframe != self._timeframe:
            self._source.subscribe(new_symbol, new_timeframe)
            self._symbol = new_symbol
            self._timeframe = new_timeframe

    def update_subscription(self, symbol: str, timeframe: str) -> None:
        """Re-subscribe the data source to a new symbol/timeframe."""
        if symbol != self._symbol or timeframe != self._timeframe:
            self._source.subscribe(symbol, timeframe)
            self._symbol = symbol
            self._timeframe = timeframe

    def apply_settings(self, settings: Any | None = None) -> None:
        """Apply settings changes and reconnect the current data source.

        This is used after the Settings API persists data-source credentials or
        symbol/timeframe changes. It deliberately reconnects, because startup
        may have continued after a transient or credential-related connect
        failure.
        """
        if settings is not None:
            self._settings = settings
        general = self._settings_general()
        if general is not None:
            self._symbol = getattr(general, "last_symbol", None) or self._symbol
            self._timeframe = getattr(general, "last_timeframe", None) or self._timeframe
        self._apply_source_kind_from_settings()
        self._ensure_subscription()
        self._connect_and_subscribe()

    def get_frame(self, n: int = 80) -> KlineFrame | None:
        """Return the latest *n* bars as a KlineFrame, or None if unavailable."""
        self._ensure_subscription()
        # build_live_frame needs n+1 bars when the newest bar is still forming,
        # so fetch one extra to ensure we can build the frame.
        self.last_error = None
        try:
            bars = self._source.latest_snapshot(n + 1)
        except DataSourceTransientError as exc:
            if "Not connected" in str(exc):
                try:
                    self._connect_and_subscribe()
                    bars = self._source.latest_snapshot(n + 1)
                except Exception as retry_exc:  # noqa: BLE001
                    self.last_error = str(retry_exc)
                    return None
            else:
                self.last_error = str(exc)
                return None
        except Exception as exc:
            self.last_error = str(exc)
            return None
        if not bars:
            return None
        frame = build_live_frame(bars, n, self._symbol, self._timeframe)
        if frame is None:
            return None
        return frame

    def get_snapshot(self, n: int = 80) -> dict | None:
        """Return the latest *n* bars as a JSON-friendly dict, or None if unavailable."""
        frame = self.get_frame(n)
        if frame is None:
            return None
        return {
            "symbol": frame.symbol,
            "timeframe": frame.timeframe,
            "bars": [
                {
                    "seq": b.seq,
                    "ts_open": b.ts_open,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                    "closed": b.closed,
                }
                for b in frame.bars
            ],
            "indicators": {
                "ema10": list(frame.indicators.ema10),
                "ema20": list(frame.indicators.ema20),
                "ema60": list(frame.indicators.ema60),
                "atr14": list(frame.indicators.atr14),
            },
        }
