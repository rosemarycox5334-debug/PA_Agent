from __future__ import annotations

from collections.abc import Iterable
from itertools import pairwise

from pa_agent.research_data.models import GapInterval, StreamGapReport

FUNDING_SCHEDULE_VERSION = "FUNDING_SCHEDULE_ASSUMED_8H_V1"
ASSUMED_FUNDING_STEP_MS = 8 * 60 * 60 * 1_000
FUNDING_SETTLEMENT_TOLERANCE_MS = 1_000


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


def _funding_slots(expected_start_ms: int, expected_end_ms: int) -> tuple[int, ...]:
    first = (
        (expected_start_ms + ASSUMED_FUNDING_STEP_MS - 1) // ASSUMED_FUNDING_STEP_MS
    ) * ASSUMED_FUNDING_STEP_MS
    return tuple(range(first, expected_end_ms + 1, ASSUMED_FUNDING_STEP_MS))


def _missing_slot_intervals(
    missing_slots: list[int], *, expected_end_ms: int
) -> tuple[GapInterval, ...]:
    if not missing_slots:
        return ()
    intervals: list[GapInterval] = []
    start = previous = missing_slots[0]
    for slot in missing_slots[1:]:
        if slot != previous + ASSUMED_FUNDING_STEP_MS:
            intervals.append(
                GapInterval(start, min(previous + ASSUMED_FUNDING_STEP_MS - 1, expected_end_ms))
            )
            start = slot
        previous = slot
    intervals.append(
        GapInterval(start, min(previous + ASSUMED_FUNDING_STEP_MS - 1, expected_end_ms))
    )
    return tuple(intervals)


def detect_funding_gap_intervals(
    timestamps: Iterable[int],
    *,
    expected_start_ms: int,
    expected_end_ms: int,
    schedule_version: str,
) -> StreamGapReport:
    if schedule_version != FUNDING_SCHEDULE_VERSION:
        raise ValueError(f"Unsupported funding schedule version: {schedule_version}")
    if expected_start_ms > expected_end_ms:
        raise ValueError("Invalid funding coverage range")
    ordered = sorted(set(timestamps))
    observed_steps = tuple(sorted({current - previous for previous, current in pairwise(ordered)}))
    if len(ordered) < 2:
        schedule_status = "INSUFFICIENT_OBSERVATIONS"
    else:
        aligned = all(
            abs(
                timestamp
                - ((timestamp + ASSUMED_FUNDING_STEP_MS // 2) // ASSUMED_FUNDING_STEP_MS)
                * ASSUMED_FUNDING_STEP_MS
            )
            <= FUNDING_SETTLEMENT_TOLERANCE_MS
            for timestamp in ordered
        )
        schedule_status = "VERIFIED" if aligned else "UNVERIFIED"
    slots = _funding_slots(expected_start_ms, expected_end_ms)
    missing_slots = [
        slot
        for slot in slots
        if not any(abs(timestamp - slot) <= FUNDING_SETTLEMENT_TOLERANCE_MS for timestamp in ordered)
    ]
    coverage_status = "GAPS_DETECTED" if missing_slots else "COMPLETE"
    intervals = _missing_slot_intervals(missing_slots, expected_end_ms=expected_end_ms)
    if schedule_status != "VERIFIED":
        status = "FUNDING_SCHEDULE_UNVERIFIED"
    elif coverage_status != "COMPLETE":
        status = "GAPS_DETECTED"
    else:
        status = "COMPLETE"
    return StreamGapReport(
        stream="funding",
        status=status,
        intervals=intervals,
        schedule_version=schedule_version,
        observed_steps_ms=observed_steps,
        schedule_status=schedule_status,
        coverage_status=coverage_status,
    )
