"""Verify debug turns are collected after analysis submission."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from pa_agent.web.service.analysis_service import AnalysisService
from pa_agent.web.api.debug import _turns, clear_turns
from pa_agent.data.base import KlineFrame, KlineBar, IndicatorBundle
from pa_agent.records.schema import AnalysisRecord, RecordMeta


def _make_frame() -> KlineFrame:
    bars = (
        KlineBar(seq=1, ts_open=1000, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0, closed=True),
        KlineBar(seq=2, ts_open=2000, open=100.5, high=102.0, low=100.0, close=101.5, volume=2000.0, closed=True),
    )
    indicators = IndicatorBundle(ema10=(100.0, 101.0), ema20=(100.0, 101.0), ema60=(100.0, 101.0), atr14=(1.0, 1.0))
    return KlineFrame(symbol="TEST", timeframe="1h", bars=bars, indicators=indicators, snapshot_ts_local_ms=0)


def _make_record(**overrides) -> AnalysisRecord:
    meta = RecordMeta(
        timestamp_local_iso="2024-01-01T00:00:00",
        timestamp_local_ms=0,
        symbol="TEST",
        timeframe="1h",
        bar_count=2,
        ai_provider={},
    )
    defaults = dict(
        meta=meta,
        kline_data=[],
        htf_text="",
        stage1_messages=[{"role": "system", "content": "s1-sys"}, {"role": "user", "content": "s1-user"}],
        stage1_response={"content": "s1-raw"},
        stage1_diagnosis={"direction": "bullish"},
        stage2_messages=[{"role": "system", "content": "s2-sys"}, {"role": "user", "content": "s2-user"}],
        stage2_response={"content": "s2-raw"},
        stage2_decision={"order_type": "限价单"},
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )
    defaults.update(overrides)
    return AnalysisRecord(**defaults)


async def _run_analysis(record: AnalysisRecord) -> list:
    frame = _make_frame()
    data_svc = MagicMock()
    data_svc.get_frame.return_value = frame
    mock_orch = MagicMock()
    mock_orch.submit.return_value = record
    svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
    return [e async for e in svc.submit(bar_count=2, stance="balanced")]


def test_debug_turns_collected_for_successful_analysis():
    clear_turns()
    record = _make_record()
    asyncio.run(_run_analysis(record))
    assert len(_turns) == 2
    assert _turns[0]["label"] == "阶段一 · 市场诊断"
    assert _turns[0]["system_prompt"] == "s1-sys"
    assert _turns[0]["user_prompt"] == "s1-user"
    assert _turns[0]["raw_response"] == {"content": "s1-raw"}
    assert _turns[0]["validation_info"]["status"] == "ok"
    assert _turns[1]["label"] == "阶段二 · 交易决策"
    assert _turns[1]["system_prompt"] == "s2-sys"
    assert _turns[1]["user_prompt"] == "s2-user"
    assert _turns[1]["validation_info"]["status"] == "ok"


def test_debug_turns_for_stage1_validation_error():
    clear_turns()
    record = _make_record(
        stage1_diagnosis=None,
        stage2_messages=[],
        stage2_response=None,
        stage2_decision=None,
        exception={"type": "validation_error", "stage": "stage1", "category": "a", "message": "bad json"},
    )
    asyncio.run(_run_analysis(record))
    assert len(_turns) == 1
    assert _turns[0]["label"] == "阶段一 · 市场诊断"
    assert _turns[0]["validation_info"]["status"] == "error"
    assert _turns[0]["validation_info"]["category"] == "a"


def test_debug_turns_for_gate_shortcircuit():
    clear_turns()
    record = _make_record(
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={"gate_shortcircuited": True},
    )
    asyncio.run(_run_analysis(record))
    assert len(_turns) == 2
    assert _turns[1]["label"] == "阶段二 · 交易决策"
    assert _turns[1]["validation_info"]["status"] == "skipped"
    assert _turns[1]["validation_info"]["reason"] == "gate_shortcircuited"


def test_debug_turns_not_pushed_for_mock_record():
    """MagicMock records should be silently ignored (no crash)."""
    clear_turns()
    frame = _make_frame()
    data_svc = MagicMock()
    data_svc.get_frame.return_value = frame
    mock_orch = MagicMock()
    mock_record = MagicMock()
    mock_record.stage1_diagnosis = {"direction": "bullish"}
    mock_record.stage2_decision = {"order_type": "limit"}
    mock_record.exception = None
    mock_orch.submit.return_value = mock_record
    svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
    asyncio.run(_collect(svc))
    assert len(_turns) == 0


async def _collect(svc):
    [e async for e in svc.submit(bar_count=2, stance="balanced")]


if __name__ == "__main__":
    test_debug_turns_collected_for_successful_analysis()
    print("test_debug_turns_collected_for_successful_analysis PASSED")
    test_debug_turns_for_stage1_validation_error()
    print("test_debug_turns_for_stage1_validation_error PASSED")
    test_debug_turns_for_gate_shortcircuit()
    print("test_debug_turns_for_gate_shortcircuit PASSED")
    test_debug_turns_not_pushed_for_mock_record()
    print("test_debug_turns_not_pushed_for_mock_record PASSED")
    print("\nAll debug-turns integration tests passed!")
