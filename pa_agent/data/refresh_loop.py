"""1 Hz data refresh loop running on a dedicated QThread."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from pa_agent.data.base import DataSource, DataSourceTransientError
from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS

if TYPE_CHECKING:
    from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)


class RefreshLoop(QThread):
    """Fetches the latest K-line snapshot every *interval_ms* milliseconds.

    Signals
    -------
    frame_ready(list[KlineBar])
        Emitted after each successful fetch with the raw bar list (newest-first).
    status_changed(str)
        Emitted with a human-readable status string (e.g. "数据延迟").
    """

    frame_ready = pyqtSignal(list)
    status_changed = pyqtSignal(str)

    # Backoff constants
    _MAX_BACKOFF_S = 10.0  # cap exponential backoff at 10 seconds
    _BACKOFF_BASE_S = 0.5  # initial backoff = 0.5s, doubles each failure

    def __init__(
        self,
        data_source: DataSource,
        n_bars: int,
        interval_ms: int = 1000,
        cancel_token: CancelToken | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._source = data_source
        self._n_bars = n_bars
        self._interval_ms = interval_ms
        self._cancel_token = cancel_token
        self._consecutive_failures = 0
        self._failure_threshold_s = 5.0
        self._last_unexpected_message = ""

    def run(self) -> None:
        """Main loop — runs on the worker thread.

        Note: there is deliberately no "already fetching" guard. The fetch and
        the sleep both happen here on this single thread, so ticks can never
        overlap; the old ``_in_flight`` flag checked at the top of the loop was
        always False (2026-10 code review).
        """
        failure_start: float | None = None

        while True:
            if self._cancelled():
                logger.debug("RefreshLoop cancelled")
                break

            t0 = time.monotonic()
            try:
                bars = self._source.latest_snapshot(self._n_bars + INDICATOR_WARMUP_BARS + 5)
                if self._consecutive_failures > 0:
                    # Clear any previous error message from the status bar.
                    self.status_changed.emit("")
                self._consecutive_failures = 0
                self._last_unexpected_message = ""
                failure_start = None
                if bars:
                    self.frame_ready.emit(bars)

            except DataSourceTransientError as exc:
                logger.debug("RefreshLoop transient error: %s", exc)
                self._consecutive_failures += 1
                if failure_start is None:
                    failure_start = time.monotonic()
                user_msg = str(exc).strip()
                if user_msg:
                    self.status_changed.emit(user_msg)
                elapsed = time.monotonic() - failure_start
                if elapsed >= self._failure_threshold_s and not user_msg:
                    self.status_changed.emit("数据延迟")
            except Exception as exc:
                # A parse bug or AttributeError used to be logged and swallowed,
                # leaving the UI silently "stale" (2026-10 code review). Count it
                # as a failure (so backoff applies) and tell the user once per
                # distinct message.
                logger.exception("RefreshLoop unexpected error")
                self._consecutive_failures += 1
                if failure_start is None:
                    failure_start = time.monotonic()
                message = f"数据源异常：{type(exc).__name__}: {exc}"
                if message != self._last_unexpected_message:
                    self._last_unexpected_message = message
                    self.status_changed.emit(message)

            # Exponential backoff on repeated failures to avoid hammering the
            # upstream endpoint.
            if self._consecutive_failures > 0:
                backoff_s = min(
                    self._BACKOFF_BASE_S * (2 ** (self._consecutive_failures - 1)),
                    self._MAX_BACKOFF_S,
                )
                logger.debug(
                    "RefreshLoop backoff %.1fs after %d consecutive failure(s)",
                    backoff_s,
                    self._consecutive_failures,
                )
                if self._sleep_cancellable(backoff_s):
                    break
                continue

            elapsed_ms = (time.monotonic() - t0) * 1000
            if self._sleep_cancellable(max(0.0, self._interval_ms - elapsed_ms) / 1000.0):
                break

    # ── Cancellation helpers ──────────────────────────────────────────────────

    def _cancelled(self) -> bool:
        return self._cancel_token is not None and self._cancel_token.is_set()

    def _sleep_cancellable(self, seconds: float) -> bool:
        """Sleep *seconds*, waking immediately when cancelled.

        Returns True when the token was set. The previous implementation used a
        bare ``time.sleep``, so cancelling a loop parked in the 10 s backoff
        waited out the full delay before the thread could exit (2026-10 review).
        """
        if seconds <= 0:
            return self._cancelled()
        if self._cancel_token is None:
            time.sleep(seconds)
            return False
        return bool(self._cancel_token.wait(timeout=seconds))
