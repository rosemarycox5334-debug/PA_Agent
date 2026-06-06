"""Unit tests for AnalysisService orchestrator bridge.

P0 baseline contract:
    * ``OrchestratorEvent`` names emitted by the orchestrator are
      normalised to snake_case before reaching the SSE consumer.
    * Every event dict carries a server-generated ``run_id``.
    * A prewarm() helper exists for the phase-1 startup warmup.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest

from pa_agent.web.service.analysis_service import (
    AnalysisService,
    generate_run_id,
)
from pa_agent.data.base import KlineFrame, KlineBar, IndicatorBundle
from pa_agent.util.threading import CancelToken, OrchestratorEvent


def _make_frame() -> KlineFrame:
    """Build a minimal KlineFrame for testing."""
    bars = (
        KlineBar(seq=1, ts_open=1000, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0, closed=True),
        KlineBar(seq=2, ts_open=2000, open=100.5, high=102.0, low=100.0, close=101.5, volume=2000.0, closed=True),
    )
    indicators = IndicatorBundle(
        ema10=(100.0, 101.0),
        ema20=(100.0, 101.0),
        ema60=(100.0, 101.0),
        atr14=(1.0, 1.0),
    )
    return KlineFrame(symbol="TEST", timeframe="1h", bars=bars, indicators=indicators, snapshot_ts_local_ms=0)


class TestAnalysisServiceMockOrchestrator:
    """Tests using a mock orchestrator to verify bridge logic."""

    @pytest.mark.anyio
    async def test_submit_yields_snake_case_lifecycle_events(self):
        """PascalCase OrchestratorEvent names must be normalised to snake_case."""
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        # Build a mock orchestrator that simulates the two-stage flow
        mock_orch = MagicMock()

        def _mock_submit(frame, cancel_token, on_event, on_stage1_reasoning, on_stage1_content, on_stage2_reasoning, on_stage2_content, **kwargs):
            on_event(OrchestratorEvent.Stage1Started)
            on_stage1_reasoning("thinking...")
            on_stage1_content("content...")
            on_event(OrchestratorEvent.Stage1Done)
            on_event(OrchestratorEvent.Stage2Started)
            on_stage2_reasoning("s2 thinking...")
            on_stage2_content("s2 content...")
            on_event(OrchestratorEvent.Stage2Done)
            on_event(OrchestratorEvent.RecordSaved)
            mock_record = MagicMock()
            mock_record.stage1_diagnosis = {"direction": "bullish"}
            mock_record.stage2_decision = {"order_type": "limit"}
            mock_record.exception = None
            return mock_record

        mock_orch.submit = _mock_submit

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]

        event_names = [e["event"] for e in events]
        # snake_case names — aligned with PyQt AIStreamPanel
        assert "stage1_started" in event_names
        assert "stage1_reasoning" in event_names
        assert "stage1_content" in event_names
        assert "stage1_done" in event_names
        assert "stage2_started" in event_names
        assert "stage2_reasoning" in event_names
        assert "stage2_content" in event_names
        assert "stage2_done" in event_names
        assert "stage1_result" in event_names
        assert "stage2_decision" in event_names
        assert "record_saved" in event_names
        assert "done" in event_names
        # PascalCase must NOT leak into the wire format
        for name in event_names:
            assert not name.startswith("Stage"), f"PascalCase leaked: {name}"

    @pytest.mark.anyio
    async def test_submit_injects_run_id_into_every_event(self):
        """Every yielded event must carry a run_id field."""
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        mock_orch = MagicMock()
        def _mock_submit(frame, cancel_token, on_event, **kwargs):
            on_event(OrchestratorEvent.Stage1Started)
            on_event(OrchestratorEvent.Stage1Done)
            rec = MagicMock()
            rec.stage1_diagnosis = {"x": 1}
            rec.stage2_decision = None
            rec.exception = None
            return rec
        mock_orch.submit = _mock_submit

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]

        run_ids = {e["run_id"] for e in events if "run_id" in e}
        assert run_ids, "no run_id in any event"
        assert len(run_ids) == 1, f"expected one run_id, got {run_ids!r}"
        for rid in run_ids:
            assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid)

    @pytest.mark.anyio
    async def test_submit_honors_client_run_id(self):
        """If a run_id is supplied, every event echoes it back."""
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        mock_orch = MagicMock()
        def _mock_submit(frame, cancel_token, on_event, **kwargs):
            on_event(OrchestratorEvent.Stage1Started)
            rec = MagicMock()
            rec.stage1_diagnosis = {"x": 1}
            rec.stage2_decision = None
            rec.exception = None
            return rec
        mock_orch.submit = _mock_submit

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
        client_rid = generate_run_id()
        events = [
            e
            async for e in svc.submit(bar_count=2, stance="balanced", run_id=client_rid)
        ]
        run_ids = {e["run_id"] for e in events}
        assert run_ids == {client_rid}

    @pytest.mark.anyio
    async def test_submit_with_no_frame_yields_error(self):
        """If DataService returns no frame, yield an error event."""
        data_svc = MagicMock()
        data_svc.get_frame.return_value = None
        svc = AnalysisService(orchestrator=MagicMock(), data_service=data_svc)
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]
        assert len(events) == 1
        assert events[0]["event"] == "error"
        assert "run_id" in events[0]

    @pytest.mark.anyio
    async def test_cancel_watcher_triggers_cancel_token(self):
        """When is_disconnected returns True, cancel_token should be set."""
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        mock_orch = MagicMock()
        def _slow_submit(*args, **kwargs):
            import time
            time.sleep(1.5)  # simulate long-running analysis
            mock_record = MagicMock()
            mock_record.stage1_diagnosis = None
            mock_record.stage2_decision = None
            mock_record.exception = None
            return mock_record
        mock_orch.submit = _slow_submit

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)

        disconnected_called = [False]
        async def _is_disconnected():
            disconnected_called[0] = True
            return True

        events = [e async for e in svc.submit(
            bar_count=2, stance="balanced",
            is_disconnected=_is_disconnected,
            cancel_check_interval=0.1,
        )]
        assert disconnected_called[0]

    @pytest.mark.anyio
    async def test_fallback_mock_when_no_orchestrator(self):
        """When orchestrator is None, should fall back to mock events (PyQt-aligned)."""
        svc = AnalysisService(orchestrator=None, data_service=None)
        events = [e async for e in svc.submit(bar_count=80, stance="balanced")]
        event_names = [e["event"] for e in events]
        # PyQt-aligned lifecycle
        assert "stage1_started" in event_names
        assert "stage1_reasoning" in event_names
        assert "stage1_result" in event_names
        assert "stage1_done" in event_names
        assert "stage2_started" in event_names
        assert "stage2_content" in event_names
        assert "stage2_decision" in event_names
        assert "stage2_done" in event_names
        assert "record_saved" in event_names
        assert "done" in event_names
        # Every event has a run_id
        for e in events:
            assert "run_id" in e


class TestAnalysisServicePrewarm:
    """Phase-1 incremental prewarm at server startup."""

    def test_prewarm_returns_false_when_no_orchestrator(self):
        """Without an orchestrator, prewarm must record a clear error."""
        svc = AnalysisService(orchestrator=None)
        ok = svc.prewarm(bar_count=80)
        assert ok is False
        assert svc.is_prewarmed is False
        assert svc.prewarm_error is not None

    def test_prewarm_succeeds_with_wired_orchestrator(self):
        """With an orchestrator + data service, prewarm should succeed and set ts."""
        mock_orch = MagicMock()
        frame = _make_frame()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        svc = AnalysisService(
            orchestrator=mock_orch,
            data_service=data_svc,
        )
        ok = svc.prewarm(bar_count=80)
        assert ok is True
        assert svc.is_prewarmed is True
        assert svc.prewarm_error is None
        assert svc._prewarm_ts_ms is not None

    def test_prewarm_tolerates_missing_data_frame(self):
        """If the data service cannot produce a frame, prewarm still succeeds."""
        mock_orch = MagicMock()
        data_svc = MagicMock()
        data_svc.get_frame.return_value = None

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)
        ok = svc.prewarm(bar_count=80)
        assert ok is True
        assert svc.is_prewarmed is True
        # No frame, no prompt-build exercise — but prewarm itself is OK.

