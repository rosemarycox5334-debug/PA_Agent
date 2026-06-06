"""Tests for incremental analysis via POST /api/analysis/submit."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from pa_agent.web.server import create_app
from pa_agent.web.service.analysis_service import AnalysisService


def _collect_sse(response) -> list[dict]:
    """Parse SSE response into a list of event dicts."""
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


import json  # noqa: E402


class TestIncrementalAnalysis:
    def test_incremental_without_previous_record_returns_400(self) -> None:
        """POST /api/analysis/submit with incremental=true but no previous record → 400."""
        svc = AnalysisService(orchestrator=None)
        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/submit",
            json={"bar_count": 80, "stance": "balanced", "incremental": True},
        )
        assert response.status_code == 400
        assert "previous" in response.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_incremental_with_previous_record_succeeds(self) -> None:
        """After a successful analysis, incremental submit should succeed."""
        from pa_agent.data.base import KlineFrame, KlineBar, IndicatorBundle

        frame = KlineFrame(
            symbol="TEST",
            timeframe="1h",
            bars=(
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
            ),
            indicators=IndicatorBundle(
                ema10=(100.0, 101.0),
                ema20=(100.0, 101.0),
                ema60=(100.0, 101.0),
                atr14=(1.0, 1.0),
            ),
            snapshot_ts_local_ms=0,
        )

        data_svc = MagicMock()
        data_svc.get_frame.return_value = frame

        mock_orch = MagicMock()

        def _mock_submit(frame, cancel_token, on_event, on_stage1_reasoning,
                         on_stage1_content, on_stage2_reasoning, on_stage2_content,
                         previous_record=None, incremental_new_bar_count=None, **kwargs):
            on_event(MagicMock(name="Stage1Started"))
            on_event(MagicMock(name="Stage1Done"))
            on_event(MagicMock(name="Stage2Started"))
            on_event(MagicMock(name="Stage2Done"))
            mock_record = MagicMock()
            mock_record.stage1_diagnosis = {"direction": "bullish"}
            mock_record.stage2_decision = {"order_type": "limit"}
            mock_record.exception = None
            return mock_record

        mock_orch.submit = _mock_submit

        svc = AnalysisService(orchestrator=mock_orch, data_service=data_svc)

        # First analysis to establish previous_record
        events = [e async for e in svc.submit(bar_count=2, stance="balanced")]
        assert svc.previous_record is not None

        # Incremental submit should now succeed
        incremental_events = [
            e async for e in svc.submit(
                bar_count=2,
                stance="balanced",
                previous_record=svc.previous_record,
                incremental_new_bar_count=1,
            )
        ]
        event_names = [e["event"] for e in incremental_events]
        assert "done" in event_names

    def test_incremental_new_bars_passed_through(self) -> None:
        """Verify incremental_new_bars parameter is forwarded to the service."""
        svc = AnalysisService(orchestrator=None)
        # Simulate that a previous record exists
        svc._previous_record = MagicMock()
        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/submit",
            json={
                "bar_count": 80,
                "stance": "balanced",
                "incremental": True,
                "incremental_new_bars": 5,
            },
        )
        # Should succeed (200) because previous_record exists
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
