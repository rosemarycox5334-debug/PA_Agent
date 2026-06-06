"""Tests for data API."""
from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.web.server import create_app
from pa_agent.web.service.data_service import DataService


def _mock_frame() -> KlineFrame:
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


class TestDataApi:
    @patch("pa_agent.web.service.data_service.build_live_frame")
    def test_snapshot_returns_frame(self, mock_build: MagicMock) -> None:
        """GET /api/data/snapshot returns the current K-line frame."""
        frame = _mock_frame()
        mock_build.return_value = frame
        mock_source = MagicMock()
        mock_source.latest_snapshot.return_value = frame.bars

        svc = DataService(mock_source, symbol="TEST", timeframe="1m")
        app = create_app(data_service=svc)
        client = TestClient(app)

        response = client.get("/api/data/snapshot")
        assert response.status_code == 200
        data = response.json()
        assert data["symbol"] == "TEST"
        assert len(data["bars"]) == 1
        assert data["bars"][0]["close"] == 102.0

    def test_snapshot_503_when_no_data(self) -> None:
        """GET /api/data/snapshot returns 503 when the data source has no bars."""
        mock_source = MagicMock()
        mock_source.latest_snapshot.return_value = []
        svc = DataService(mock_source, symbol="TEST", timeframe="1m")
        app = create_app(data_service=svc)
        client = TestClient(app)

        response = client.get("/api/data/snapshot")
        assert response.status_code == 503

    def test_snapshot_503_includes_data_source_error(self) -> None:
        """Data source failures are reported as readable 503 errors, not 500s."""
        mock_source = MagicMock()
        mock_source.latest_snapshot.side_effect = RuntimeError("RQData license key not configured")
        svc = DataService(mock_source, symbol="TEST", timeframe="1m")
        app = create_app(data_service=svc)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/data/snapshot")

        assert response.status_code == 503
        assert "RQData license key not configured" in response.json()["detail"]

    def test_snapshot_503_when_service_uninitialized(self) -> None:
        """GET /api/data/snapshot returns 503 when the data service is not set."""
        app = create_app(data_service=None)
        client = TestClient(app)

        response = client.get("/api/data/snapshot")
        assert response.status_code == 503

    @patch("pa_agent.web.service.data_service.build_live_frame")
    def test_snapshot_serializes_indicator_nan_as_null(self, mock_build: MagicMock) -> None:
        """Warm-up indicator NaN values must not crash JSON serialization."""
        frame = _mock_frame()
        mock_build.return_value = KlineFrame(
            symbol=frame.symbol,
            timeframe=frame.timeframe,
            bars=frame.bars,
            indicators=IndicatorBundle(
                ema10=(math.nan,),
                ema20=(100.0,),
                ema60=(math.nan,),
                atr14=(math.nan,),
            ),
            snapshot_ts_local_ms=frame.snapshot_ts_local_ms,
        )
        mock_source = MagicMock()
        mock_source.latest_snapshot.return_value = frame.bars
        svc = DataService(mock_source, symbol="TEST", timeframe="1m")
        app = create_app(data_service=svc)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/data/snapshot")

        assert response.status_code == 200
        data = response.json()
        assert data["indicators"]["ema10"] == [None]
        assert data["indicators"]["ema20"] == [100.0]
        assert data["indicators"]["ema60"] == [None]
        assert data["indicators"]["atr14"] == [None]
