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

    verified = detect_funding_gap_intervals(
        [0, eight_hours, 2 * eight_hours],
        expected_start_ms=0,
        expected_end_ms=2 * eight_hours,
        schedule_version=FUNDING_SCHEDULE_VERSION,
    )
    unverified = detect_funding_gap_intervals(
        [0, four_hours := eight_hours // 2, eight_hours],
        expected_start_ms=0,
        expected_end_ms=eight_hours,
        schedule_version=FUNDING_SCHEDULE_VERSION,
    )

    assert verified.schedule_version == FUNDING_SCHEDULE_VERSION
    assert verified.status == "COMPLETE"
    assert unverified.status == "FUNDING_SCHEDULE_UNVERIFIED"
    assert unverified.intervals == ()
    assert unverified.observed_steps_ms == (four_hours,)


def test_funding_zero_or_one_record_cannot_be_complete():
    eight_hours = 8 * 60 * 60 * 1_000
    for timestamps in ([], [0]):
        report = detect_funding_gap_intervals(
            timestamps,
            expected_start_ms=0,
            expected_end_ms=eight_hours,
            schedule_version=FUNDING_SCHEDULE_VERSION,
        )
        assert report.status != "COMPLETE"
        assert report.schedule_status == "INSUFFICIENT_OBSERVATIONS"
        assert report.coverage_status == "GAPS_DETECTED"


def test_funding_coverage_reports_leading_and_trailing_missing_slots():
    eight_hours = 8 * 60 * 60 * 1_000
    report = detect_funding_gap_intervals(
        [eight_hours, 2 * eight_hours],
        expected_start_ms=0,
        expected_end_ms=3 * eight_hours,
        schedule_version=FUNDING_SCHEDULE_VERSION,
    )
    assert report.schedule_status == "VERIFIED"
    assert report.coverage_status == "GAPS_DETECTED"
    assert [(gap.start_utc_ms, gap.end_utc_ms) for gap in report.gap_intervals] == [
        (0, eight_hours - 1),
        (3 * eight_hours, 3 * eight_hours),
    ]


def test_funding_millisecond_jitter_is_versioned_and_tolerated():
    eight_hours = 8 * 60 * 60 * 1_000
    report = detect_funding_gap_intervals(
        [7, eight_hours, 2 * eight_hours - 7],
        expected_start_ms=0,
        expected_end_ms=2 * eight_hours,
        schedule_version=FUNDING_SCHEDULE_VERSION,
    )
    assert report.status == "COMPLETE"
    assert report.schedule_status == "VERIFIED"
    assert report.coverage_status == "COMPLETE"
