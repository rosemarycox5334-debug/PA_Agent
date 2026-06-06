"""Quick tests for new APIs: records, debug, decision-tree."""
from __future__ import annotations

from fastapi.testclient import TestClient

from pa_agent.web.server import create_app


class TestRecordsAPI:
    def test_list_records_returns_list(self) -> None:
        app = create_app()
        client = TestClient(app)
        response = client.get("/api/records")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_load_record_404(self) -> None:
        app = create_app()
        client = TestClient(app)
        response = client.get("/api/records/nonexistent.json")
        assert response.status_code == 404


class TestDebugAPI:
    def test_get_turns_empty(self) -> None:
        app = create_app()
        client = TestClient(app)
        response = client.get("/api/debug/turns")
        assert response.status_code == 200
        assert response.json() == {"turns": []}

    def test_clear_turns(self) -> None:
        app = create_app()
        client = TestClient(app)
        response = client.post("/api/debug/turns/clear")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestDecisionTreeAPI:
    def test_get_tree(self) -> None:
        app = create_app()
        client = TestClient(app)
        response = client.get("/api/decision-tree")
        assert response.status_code == 200
        data = response.json()
        assert "sections" in data
        assert "node_index" in data
