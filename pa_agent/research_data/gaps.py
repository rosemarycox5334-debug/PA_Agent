from __future__ import annotations

from collections.abc import Iterable
from itertools import pairwise

from pa_agent.research_data.models import GapInterval, StreamGapReport


def detect_gap_intervals(
    *,
    stream: str,
    timestamps: Iterable[int],
    expected_step_ms: int,
    expected_start_ms: int | None = None,
    expected_end_ms: int | None = None,
) -> StreamGapReport:
    if expected_step_ms <= 0:
        raise ValueError("expected_step_ms must be positive")
    ordered = sorted(set(timestamps))
    if not ordered:
        return StreamGapReport(stream=stream, status="EMPTY", intervals=())
    gaps: list[GapInterval] = []
    if expected_start_ms is not None and ordered[0] > expected_start_ms:
        gaps.append(GapInterval(expected_start_ms, ordered[0] - 1))
    for previous, current in pairwise(ordered):
        expected = previous + expected_step_ms
        if current > expected:
            gaps.append(GapInterval(expected, current - 1))
    if expected_end_ms is not None and ordered[-1] < expected_end_ms:
        gaps.append(GapInterval(ordered[-1] + expected_step_ms, expected_end_ms + expected_step_ms - 1))
    return StreamGapReport(
        stream=stream,
        status="GAPS_DETECTED" if gaps else "COMPLETE",
        intervals=tuple(gaps),
    )
