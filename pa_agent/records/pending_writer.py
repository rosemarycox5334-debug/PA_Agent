"""PendingWriter — persists AnalysisRecord and FollowupTurn to disk.

File naming convention:
    {YYYY-MM-DD_HH-mm-ss}_{symbol}_{timeframe}.json
    {YYYY-MM-DD_HH-mm-ss}_{symbol}_{timeframe}.followups.jsonl

Disk failures are logged and emitted to the event bus but never propagated.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pa_agent.records.schema import AnalysisRecord, FollowupTurn
from pa_agent.util.mask_secret import mask_secret


def _default_logger() -> logging.Logger:
    return logging.getLogger(__name__)


def _ms_to_local_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to local datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()


def _build_basename(record: AnalysisRecord) -> str:
    """Build the filename stem (without extension) for a record."""
    dt = _ms_to_local_datetime(record.meta.timestamp_local_ms)
    ts_str = dt.strftime("%Y-%m-%d_%H-%M-%S")
    symbol = record.meta.symbol
    timeframe = record.meta.timeframe
    return f"{ts_str}_{symbol}_{timeframe}"


class PendingWriter:
    """Writes analysis records and followup turns to the pending directory."""

    def __init__(
        self,
        pending_dir: Optional[Path] = None,
        event_bus=None,
        logger: Optional[logging.Logger] = None,
        api_key: str = "",
    ) -> None:
        if pending_dir is None:
            from pa_agent.config.paths import RECORDS_PENDING_DIR
            pending_dir = RECORDS_PENDING_DIR

        self._pending_dir = pending_dir
        self._event_bus = event_bus
        self._logger = logger or _default_logger()
        self._api_key = api_key

        try:
            self._pending_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._logger.error(
                "PendingWriter: failed to create pending directory %s: %s",
                self._pending_dir,
                exc,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_full(self, record: AnalysisRecord) -> Path:
        """Serialize and save a complete analysis record.

        Returns the path written to, or a best-effort path on failure.
        """
        basename = _build_basename(record)
        path = self._pending_dir / f"{basename}.json"
        data = record.model_dump()
        data = self._sanitize(data, self._api_key)
        self._write_json(path, data)
        try:
            from pa_agent.records.analysis_history import invalidate_latest_record_cache

            invalidate_latest_record_cache()
        except Exception:  # noqa: BLE001
            pass
        return path

    def save_partial(self, record: AnalysisRecord, reason: str) -> Path:
        """Serialize and save a partial analysis record with a reason field.

        The ``_partial_reason`` key is injected into the serialized dict
        (it is not part of the Pydantic model). When ``record.exception`` is
        set, ``partial_reason`` is also copied into that dict for easier
        filtering without reading ``_partial_reason``.

        Returns the path written to, or a best-effort path on failure.
        """
        basename = _build_basename(record)
        path = self._pending_dir / f"{basename}.json"
        data = record.model_dump()
        data["_partial_reason"] = reason
        if isinstance(data.get("exception"), dict):
            data["exception"] = {**data["exception"], "partial_reason": reason}
        data = self._sanitize(data, self._api_key)
        self._write_json(path, data)
        try:
            from pa_agent.records.analysis_history import invalidate_latest_record_cache

            invalidate_latest_record_cache()
        except Exception:  # noqa: BLE001
            pass
        return path

    def append_followup(self, record_id: str, turn: FollowupTurn) -> None:
        """Append a single followup turn to the JSONL sidecar file.

        ``record_id`` is the basename (without extension) of the record file,
        e.g. ``"2026-05-18_14-00-13_XAUUSD_1h"``.
        """
        path = self._pending_dir / f"{record_id}.followups.jsonl"
        line = json.dumps(turn.model_dump(), ensure_ascii=False)
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            self._handle_disk_error(exc, path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize(data: dict, api_key: str) -> dict:
        """Recursively replace any occurrence of *api_key* in string values.

        If *api_key* is empty, returns *data* unchanged.
        Handles nested dicts, lists, and plain string values at any depth.
        """
        if not api_key:
            return data

        masked = mask_secret(api_key)

        def _walk(node):
            if isinstance(node, str):
                return node.replace(api_key, masked)
            if isinstance(node, dict):
                return {k: _walk(v) for k, v in node.items()}
            if isinstance(node, list):
                return [_walk(item) for item in node]
            return node

        return _walk(data)

    def _write_json(self, path: Path, data: dict) -> None:
        """Write *data* as pretty-printed JSON to *path*, handling errors."""
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self._handle_disk_error(exc, path)

    def _handle_disk_error(self, exc: OSError, path: Path) -> None:
        """Log the error and optionally emit to the event bus."""
        self._logger.error(
            "PendingWriter: disk error writing %s: %s", path, exc
        )
        if self._event_bus is not None:
            try:
                self._event_bus.emit("disk_error", {"path": str(path), "error": str(exc)})
            except Exception as bus_exc:  # noqa: BLE001
                self._logger.error(
                    "PendingWriter: event_bus emit failed: %s", bus_exc
                )
