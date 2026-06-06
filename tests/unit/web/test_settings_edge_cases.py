"""Edge-case tests for settings API."""
from __future__ import annotations

from fastapi.testclient import TestClient

from pa_agent.web.server import create_app


class TestSettingsEdgeCases:
    def test_update_settings_empty_payload(self) -> None:
        """POST /api/settings with empty payload returns ok without errors."""
        app = create_app()
        client = TestClient(app)
        response = client.post("/api/settings", json={})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_update_settings_both_general_and_provider(self) -> None:
        """POST /api/settings can update both general and provider sections at once."""
        app = create_app()
        client = TestClient(app)
        response = client.post(
            "/api/settings",
            json={
                "general": {"analysis_bar_count": 120},
                "provider": {"model": "gpt-4o"},
            },
        )
        assert response.status_code == 200
        data = client.get("/api/settings").json()
        assert data["general"]["analysis_bar_count"] == 120
        assert data["provider"]["model"] == "gpt-4o"

    def test_update_settings_api_key_sentinel_preserves_existing(self) -> None:
        """POST /api/settings with provider.api_key='***' preserves the existing key."""
        app = create_app()
        client = TestClient(app)

        # First, set a placeholder API key value
        client.post(
            "/api/settings",
            json={"provider": {"api_key": "TEST_API_KEY_VALUE"}},
        )

        # Now send *** sentinel — should NOT overwrite
        response = client.post(
            "/api/settings",
            json={"provider": {"api_key": "***"}},
        )
        assert response.status_code == 200

        # Verify the original key is preserved (masked in response)
        data = client.get("/api/settings").json()
        assert data["provider"]["api_key"] == "***"

    def test_update_settings_rqdata_license_key_sentinel_preserves_existing(self) -> None:
        """POST /api/settings with general.rqdata_license_key='***' preserves the existing key."""
        app = create_app()
        client = TestClient(app)

        # First, set a placeholder license value
        client.post(
            "/api/settings",
            json={"general": {"rqdata_license_key": "TEST_RQDATA_LICENSE_VALUE"}},
        )

        # Now send *** sentinel — should NOT overwrite
        response = client.post(
            "/api/settings",
            json={"general": {"rqdata_license_key": "***"}},
        )
        assert response.status_code == 200

        # Verify the original key is preserved (masked in response)
        data = client.get("/api/settings").json()
        assert data["general"]["rqdata_license_key"] == "***"

    def test_update_settings_unknown_general_field_ignored(self) -> None:
        """Unknown fields in general section are silently ignored."""
        app = create_app()
        client = TestClient(app)
        response = client.post(
            "/api/settings",
            json={"general": {"nonexistent_field_xyz": "abc"}},
        )
        assert response.status_code == 200
        data = client.get("/api/settings").json()
        assert "nonexistent_field_xyz" not in data["general"]

    def test_update_settings_unknown_provider_field_ignored(self) -> None:
        """Unknown fields in provider section are silently ignored."""
        app = create_app()
        client = TestClient(app)
        response = client.post(
            "/api/settings",
            json={"provider": {"nonexistent_field_xyz": "abc"}},
        )
        assert response.status_code == 200
        data = client.get("/api/settings").json()
        assert "nonexistent_field_xyz" not in data["provider"]

    def test_get_settings_masks_api_key(self) -> None:
        """GET /api/settings always masks api_key and rqdata_license_key."""
        app = create_app()
        client = TestClient(app)
        response = client.get("/api/settings")
        assert response.status_code == 200
        data = response.json()
        api_key = data["provider"]["api_key"]
        rqdata_key = data["general"]["rqdata_license_key"]
        # Should be masked — never a real key
        assert api_key in ("", "***")
        assert rqdata_key in ("", "***")

    def test_get_settings_returns_all_general_fields(self) -> None:
        """GET /api/settings returns all expected general fields."""
        app = create_app()
        client = TestClient(app)
        response = client.get("/api/settings")
        assert response.status_code == 200
        general = response.json()["general"]
        expected_fields = [
            "analysis_bar_count",
            "refresh_interval_ms",
            "context_warning_threshold_pct",
            "last_data_source",
            "last_symbol",
            "last_timeframe",
            "decision_stance",
            "rqdata_license_key",
            "auto_resume_chart_after_analysis",
            "stream_pane_font_pt",
            "chart_seq_label_font_pt",
            "incremental_max_new_bars",
            "decision_flow_auto_play",
            "decision_flow_play_seconds",
            "decision_flow_default_zoom_pct",
        ]
        for field in expected_fields:
            assert field in general, f"Missing general field: {field}"

    def test_get_settings_returns_all_provider_fields(self) -> None:
        """GET /api/settings returns all expected provider fields."""
        app = create_app()
        client = TestClient(app)
        response = client.get("/api/settings")
        assert response.status_code == 200
        provider = response.json()["provider"]
        expected_fields = [
            "model",
            "base_url",
            "api_key",
            "thinking",
            "reasoning_effort",
            "context_window",
        ]
        for field in expected_fields:
            assert field in provider, f"Missing provider field: {field}"
