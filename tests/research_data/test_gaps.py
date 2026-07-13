from pa_agent.research_data.gaps import (
    FUNDING_SCHEDULE_VERSION,
    detect_funding_gap_intervals,
    detect_gap_intervals,
)


def test_gap_detection_reports_missing_interval_without_global_invalidity():
    report = detect_gap_intervals(
        stream="mark",
        timestamps=[0, 60_000, 180_000],
        expected_step_ms=60_000,
    )

    assert report.status == "GAPS_DETECTED"
    assert [(gap.start_utc_ms, gap.end_utc_ms) for gap in report.intervals] == [
        (120_000, 179_999)
    ]
    assert not hasattr(report, "experiment_invalid")


def test_each_stream_has_an_independent_report():
    trade = detect_gap_intervals(stream="trade", timestamps=[0, 60], expected_step_ms=60)
    mark = detect_gap_intervals(stream="mark", timestamps=[0, 120], expected_step_ms=60)
    funding = detect_gap_intervals(stream="funding", timestamps=[], expected_step_ms=480)
    index = detect_gap_intervals(stream="index", timestamps=[0], expected_step_ms=60)

    assert trade.status == "COMPLETE"
    assert mark.status == "GAPS_DETECTED"
    assert funding.status == "EMPTY"
    assert index.status == "COMPLETE"


def test_leading_and_trailing_expected_range_gaps_are_reported():
    report = detect_gap_intervals(
        stream="trade",
        timestamps=[60, 120],
        expected_step_ms=60,
        expected_start_ms=0,
        expected_end_ms=180,
    )

    assert [(gap.start_utc_ms, gap.end_utc_ms) for gap in report.intervals] == [
        (0, 59),
        (180, 239),
    ]


def test_funding_schedule_is_versioned_and_non_8h_interval_is_unverified():
    eight_hours = 8 * 60 * 60 * 1_000

    verified = detect_funding_gap_intervals([0, eight_hours, 2 * eight_hours])
    unverified = detect_funding_gap_intervals([0, four_hours := eight_hours // 2, eight_hours])

    assert verified.schedule_version == FUNDING_SCHEDULE_VERSION
    assert verified.status == "COMPLETE"
    assert unverified.status == "FUNDING_SCHEDULE_UNVERIFIED"
    assert unverified.intervals == ()
    assert unverified.observed_steps_ms == (four_hours,)
