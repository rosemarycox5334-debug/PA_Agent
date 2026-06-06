"""Tests for ledger API."""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from pa_agent.web.server import create_app


class TestLedgerApi:
    def test_get_ledger_returns_breakdown(self) -> None:
        """GET /api/ledger returns token usage breakdown when ledger is initialized."""
        mock_ledger = MagicMock()
        mock_ledger.breakdown.return_value = {
            "total_input": 1000,
            "total_cached_input": 500,
            "total_output": 200,
            "context_used": 1200,
            "context_window": 100000,
            "context_pct": 1.2,
        }
        app = create_app(ledger=mock_ledger)
        client = TestClient(app)
        response = client.get("/api/ledger")
        assert response.status_code == 200
        data = response.json()
        assert data["total_input"] == 1000
        assert data["total_cached_input"] == 500
        assert data["total_output"] == 200
        assert data["context_used"] == 1200
        assert data["context_window"] == 100000
        assert data["context_pct"] == 1.2

    def test_get_ledger_503_when_uninitialized(self) -> None:
        """GET /api/ledger returns 503 when ledger is not initialized."""
        app = create_app(ledger=None)
        client = TestClient(app)
        response = client.get("/api/ledger")
        assert response.status_code == 503

    def test_get_ledger_breakdown_has_required_keys(self) -> None:
        """GET /api/ledger breakdown contains all required keys."""
        mock_ledger = MagicMock()
        mock_ledger.breakdown.return_value = {
            "total_input": 0,
            "total_cached_input": 0,
            "total_output": 0,
            "context_used": 0,
            "context_window": 131072,
            "context_pct": 0.0,
        }
        app = create_app(ledger=mock_ledger)
        client = TestClient(app)
        response = client.get("/api/ledger")
        assert response.status_code == 200
        data = response.json()
        required_keys = [
            "total_input",
            "total_cached_input",
            "total_output",
            "context_used",
            "context_window",
            "context_pct",
        ]
        for key in required_keys:
            assert key in data, f"Missing key: {key}"

    def test_get_ledger_with_usage(self) -> None:
        """GET /api/ledger returns correct values after usage has been recorded."""
        mock_ledger = MagicMock()
        mock_ledger.breakdown.return_value = {
            "total_input": 50000,
            "total_cached_input": 20000,
            "total_output": 5000,
            "context_used": 55000,
            "context_window": 100000,
            "context_pct": 55.0,
        }
        app = create_app(ledger=mock_ledger)
        client = TestClient(app)
        response = client.get("/api/ledger")
        assert response.status_code == 200
        data = response.json()
        assert data["context_pct"] == 55.0
        assert data["context_used"] == 55000
