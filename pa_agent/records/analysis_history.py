"""Helpers for locating prior analysis records for incremental runs."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pa_agent.config.paths import RECORDS_PENDING_DIR
from pa_agent.data.base import KlineFrame
from pa_agent.records.schema import AnalysisRecord

_TS_EPS = 1e-3  # seconds tolerance for bar open time matching


@dataclass(frozen=True)
class IncrementalBarDelta:
    """How many closed bars appeared since a previous record."""

    new_count: int
    anchor_ts_open: float
    new_bar_ts_opens: tuple[float, ...]


def _normalize_ts(value: object) -> float:
    ts = float(value)
    return ts / 1000.0 if ts > 1e12 else ts


def format_bar_ts(ts_open: float) -> str:
    """Format bar open time for logs/UI (local time)."""
    sec = _normalize_ts(ts_open)
    _EPOCH = datetime(1970, 1, 1)
    return (_EPOCH + timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S")


def list_record_paths(directory: Path | None = None) -> list[Path]:
    """Return saved analysis record paths, newest modified first."""
    root = directory or RECORDS_PENDING_DIR
    if not root.is_dir():
        return []
    paths = [p for p in root.glob("*.json") if p.is_file()]
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths


def load_record(path: Path) -> AnalysisRecord | None:
    """Load one AnalysisRecord, returning None for unreadable legacy files."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AnalysisRecord.model_validate(raw)
    except Exception:
        return None


def find_latest_successful_record(
    *,
    symbol: str,
    timeframe: str,
    directory: Path | None = None,
) -> AnalysisRecord | None:
    """Find the newest full successful record for a symbol/timeframe."""
    for path in list_record_paths(directory):
        record = load_record(path)
        if record is None:
            continue
        if record.meta.symbol != symbol or record.meta.timeframe != timeframe:
            continue
        if record.exception is not None:
            continue
        if not record.stage1_diagnosis or not record.stage2_decision:
            continue
        if not record.kline_data:
            continue
        return record
    return None


def compute_incremental_bar_delta(
    frame: KlineFrame,
    previous_record: AnalysisRecord,
) -> IncrementalBarDelta | None:
    """Return bars newer than the previous record's latest closed bar.

    ``frame.bars`` and ``previous_record.kline_data`` are newest-first. The anchor
    is ``kline_data[0]`` (K1 at the time of the previous analysis). New bars are
    those with ``ts_open`` strictly greater than the anchor — not merely bars
    appearing before the anchor index in the current window.
    """
    if not previous_record.kline_data:
        return None

    anchor_raw = previous_record.kline_data[0]["ts_open"]
    anchor = _normalize_ts(anchor_raw)

    anchor_seen = False
    new_ts: list[float] = []
    for bar in frame.bars:
        ts = _normalize_ts(bar.ts_open)
        if abs(ts - anchor) <= _TS_EPS:
            anchor_seen = True
            continue
        if ts > anchor + _TS_EPS:
            new_ts.append(bar.ts_open)

    if not anchor_seen:
        return None

    return IncrementalBarDelta(
        new_count=len(new_ts),
        anchor_ts_open=float(anchor_raw),
        new_bar_ts_opens=tuple(new_ts),
    )


def count_new_bars_since_record(
    frame: KlineFrame,
    previous_record: AnalysisRecord,
) -> int | None:
    """Backward-compatible wrapper returning only the new bar count."""
    delta = compute_incremental_bar_delta(frame, previous_record)
    if delta is None:
        return None
    return delta.new_count
