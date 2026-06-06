"""Tests for settings API."""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from pa_agent.web.server import create_app


class TestSettingsApi:
    def test_get_settings(self) -> None:
        """GET /api/settings returns settings with sensitive fields masked."""
        app = create_app()
        client = TestClient(app)
        response = client.get("/api/settings")
        assert response.status_code == 200
        data = response.json()
        assert "provider" in data
        assert "general" in data
        # api_key is masked (empty or ***)
        assert data["provider"]["api_key"] in ("", "***")
        assert "rqdata_license_key" in data["general"]

    def test_update_settings(self) -> None:
        """POST /api/settings updates general settings and persists them."""
        app = create_app()
        client = TestClient(app)
        response = client.post("/api/settings", json={"general": {"analysis_bar_count": 100}})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

        # Verify the change was persisted
        response = client.get("/api/settings")
        assert response.json()["general"]["analysis_bar_count"] == 100

    def test_update_settings_ignores_unknown_fields(self) -> None:
        """POST /api/settings ignores unknown fields in the payload."""
        app = create_app()
        client = TestClient(app)
        response = client.post(
            "/api/settings",
            json={"general": {"unknown_field_xyz": 42, "analysis_bar_count": 60}},
        )
        assert response.status_code == 200
        data = client.get("/api/settings").json()
        assert "unknown_field_xyz" not in data["general"]
        assert data["general"]["analysis_bar_count"] == 60

    def test_update_provider_settings(self) -> None:
        """POST /api/settings updates provider settings."""
        app = create_app()
        client = TestClient(app)
        response = client.post(
            "/api/settings",
            json={"provider": {"model": "test-model", "thinking": False}},
        )
        assert response.status_code == 200
        data = client.get("/api/settings").json()
        assert data["provider"]["model"] == "test-model"
        assert data["provider"]["thinking"] is False

    def test_update_settings_applies_data_service_settings(self) -> None:
        """POST /api/settings reconnects the data service with new settings."""
        data_service = MagicMock()
        app = create_app(data_service=data_service)
        client = TestClient(app)

        response = client.post(
            "/api/settings",
            json={"general": {"analysis_bar_count": 100}},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        data_service.apply_settings.assert_called_once()

    def test_update_settings_returns_data_source_error_without_losing_save(self) -> None:
        """Invalid data-source credentials should be visible but not block saving."""
        data_service = MagicMock()
        data_service.apply_settings.side_effect = RuntimeError("RQData init failed")
        app = create_app(data_service=data_service)
        client = TestClient(app)

        response = client.post(
            "/api/settings",
            json={"general": {"analysis_bar_count": 100}},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["data_source_error"] == "RQData init failed"
        assert data_service.last_error == "RQData init failed"
