"""Tests for analysis error and cancel event paths."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from pa_agent.data.base import KlineFrame, KlineBar, IndicatorBundle
from pa_agent.web.server import create_app
from pa_agent.web.service.analysis_service import AnalysisService
from pa_agent.util.threading import OrchestratorEvent


def _make_frame() -> KlineFrame:
    """Build a minimal KlineFrame for testing."""
    bars = (
        KlineBar(
            seq=1,
            ts_open=1000,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1000.0,
            closed=True,
        ),
        KlineBar(
            seq=2,
            ts_open=2000,
            open=100.5,
            high=102.0,
            low=100.0,
            close=101.5,
            volume=2000.0,
            closed=True,
        ),
    )
    indicators = IndicatorBundle(
        ema10=(100.0, 101.0),
        ema20=(100.0, 101.0),
        ema60=(100.0, 101.0),
        atr14=(1.0, 1.0),
    )
    return KlineFrame(symbol="TEST", timeframe="1h", bars=bars, indicators=indicators, snapshot_ts_local_ms=0)


class TestAnalysisErrorEvents:
    @pytest.mark.anyio
    async def test_submit_orchestrator_exception_yields_error(self) -> None:
        """When orchestrator.submit raises, an error event should be yielded."""
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        mock_orch = MagicMock()
        mock_orch.submit.side_effect = RuntimeError("Orchestrator crashed")

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]

        event_names = [e["event"] for e in events]
        assert "error" in event_names
        error_events = [e for e in events if e["event"] == "error"]
        assert any("Orchestrator crashed" in e.get("message", "") for e in error_events)

    @pytest.mark.anyio
    async def test_submit_record_with_exception_yields_error(self) -> None:
        """When record.exception is set, an error event should be yielded."""
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        mock_orch = MagicMock()

        def _mock_submit(frame, cancel_token, on_event, on_stage1_reasoning,
                         on_stage1_content, on_stage2_reasoning, on_stage2_content, **kwargs):
            mock_record = MagicMock()
            mock_record.stage1_diagnosis = None
            mock_record.stage2_decision = None
            mock_record.exception = {"type": "stage1_failed", "message": "Stage 1 failed"}
            return mock_record

        mock_orch.submit = _mock_submit

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]

        event_names = [e["event"] for e in events]
        assert "error" in event_names
        error_events = [e for e in events if e["event"] == "error"]
        assert any("Stage 1 failed" in e.get("message", "") for e in error_events)

    @pytest.mark.anyio
    async def test_submit_cancelled_event(self) -> None:
        """When cancel_token is set during analysis, a cancelled event should be yielded."""
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        mock_orch = MagicMock()

        def _mock_submit(frame, cancel_token, on_event, on_stage1_reasoning,
                         on_stage1_content, on_stage2_reasoning, on_stage2_content, **kwargs):
            # Simulate cancellation
            cancel_token.set()
            on_event(OrchestratorEvent.Cancelled)
            mock_record = MagicMock()
            mock_record.stage1_diagnosis = None
            mock_record.stage2_decision = None
            mock_record.exception = None
            return mock_record

        mock_orch.submit = _mock_submit

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]

        event_names = [e["event"] for e in events]
        assert "cancelled" in event_names

    @pytest.mark.anyio
    async def test_submit_stage1_failed_event(self) -> None:
        """Stage1Failed event from orchestrator should be yielded as stage1_failed."""
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        mock_orch = MagicMock()

        def _mock_submit(frame, cancel_token, on_event, on_stage1_reasoning,
                         on_stage1_content, on_stage2_reasoning, on_stage2_content, **kwargs):
            on_event(OrchestratorEvent.Stage1Failed)
            mock_record = MagicMock()
            mock_record.stage1_diagnosis = None
            mock_record.stage2_decision = None
            mock_record.exception = None
            return mock_record

        mock_orch.submit = _mock_submit

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]

        event_names = [e["event"] for e in events]
        assert "stage1_failed" in event_names

    @pytest.mark.anyio
    async def test_submit_stage2_failed_event(self) -> None:
        """Stage2Failed event from orchestrator should be yielded as stage2_failed."""
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        mock_orch = MagicMock()

        def _mock_submit(frame, cancel_token, on_event, on_stage1_reasoning,
                         on_stage1_content, on_stage2_reasoning, on_stage2_content, **kwargs):
            on_event(OrchestratorEvent.Stage1Started)
            on_event(OrchestratorEvent.Stage1Done)
            on_event(OrchestratorEvent.Stage2Failed)
            mock_record = MagicMock()
            mock_record.stage1_diagnosis = {"direction": "bullish"}
            mock_record.stage2_decision = None
            mock_record.exception = None
            return mock_record

        mock_orch.submit = _mock_submit

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]

        event_names = [e["event"] for e in events]
        assert "stage2_failed" in event_names

    @pytest.mark.anyio
    async def test_submit_record_saved_event(self) -> None:
        """RecordSaved event from orchestrator should be yielded as record_saved."""
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        mock_orch = MagicMock()

        def _mock_submit(frame, cancel_token, on_event, on_stage1_reasoning,
                         on_stage1_content, on_stage2_reasoning, on_stage2_content, **kwargs):
            on_event(OrchestratorEvent.RecordSaved)
            mock_record = MagicMock()
            mock_record.stage1_diagnosis = None
            mock_record.stage2_decision = None
            mock_record.exception = None
            return mock_record

        mock_orch.submit = _mock_submit

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]

        event_names = [e["event"] for e in events]
        assert "record_saved" in event_names

    @pytest.mark.anyio
    async def test_submit_data_service_none_yields_error(self) -> None:
        """When data_service is None but orchestrator is set, yield error."""
        mock_orch = MagicMock()
        svc = AnalysisService(orchestrator=mock_orch, data_service=None)
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]

        assert len(events) == 1
        assert events[0]["event"] == "error"
        assert "Data service not configured" in events[0]["message"]
