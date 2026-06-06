"""Tests for SSE stream."""
from __future__ import annotations

import json
from typing import AsyncIterator
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.web.api.stream import _event_stream
from pa_agent.web.server import create_app
from pa_agent.web.service.data_service import DataService


def _make_frame() -> KlineFrame:
    bars = (
        KlineBar(
            seq=1,
            ts_open=1_700_000_000_000,
            open=100.0,
            high=105.0,
            low=99.0,
            close=102.0,
            volume=1000.0,
            closed=True,
        ),
    )
    return KlineFrame(
        symbol="TEST",
        timeframe="1m",
        bars=bars,
        indicators=IndicatorBundle(
            ema10=(100.0,),
            ema20=(100.0,),
            ema60=(100.0,),
            atr14=(5.0,),
        ),
        snapshot_ts_local_ms=1_700_000_000_000,
    )


class TestStreamApi:
    @patch("pa_agent.web.service.data_service.build_live_frame")
    def test_stream_returns_sse(self, mock_build: MagicMock) -> None:
        """GET /api/stream returns an SSE stream with kline_frame events."""
        frame = _make_frame()
        mock_build.return_value = frame
        mock_source = MagicMock()
        mock_source.latest_snapshot.return_value = frame.bars

        svc = DataService(mock_source, symbol="TEST", timeframe="1m")
        app = create_app(data_service=svc)
        client = TestClient(app)

        # Patch _event_stream to a finite generator so TestClient can
        # consume the response without hanging on the infinite loop.
        async def _finite_event_stream(
            data_service: DataService,
        ) -> AsyncIterator[str]:
            snapshot = data_service.get_snapshot()
            if snapshot:
                yield f"event: kline_frame\ndata: {json.dumps(snapshot)}\n\n"

        with patch(
            "pa_agent.web.api.stream._event_stream",
            _finite_event_stream,
        ):
            response = client.get("/api/stream")
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            assert "event: kline_frame" in response.text

    def test_stream_503_when_uninitialized(self) -> None:
        """GET /api/stream returns 503 when the data service is not set."""
        app = create_app(data_service=None)
        client = TestClient(app)

        response = client.get("/api/stream")
        assert response.status_code == 503
