"""Tests for PA Agent Web server."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pa_agent.web.server import create_app

ROOT = Path(__file__).resolve().parents[3]


class TestWebServer:
    def test_root_returns_html(self) -> None:
        """The root endpoint returns the main HTML page."""
        app = create_app()
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "<!DOCTYPE html>" in response.text

    def test_static_files_served(self) -> None:
        """Static files under /static are served correctly."""
        app = create_app()
        client = TestClient(app)
        response = client.get("/static/css/app.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]
        assert "oklch" in response.text

    def test_summary_strip_uses_gui_decision_metrics(self) -> None:
        """The web decision summary mirrors the compact GUI metric strip."""
        html = (ROOT / "pa_agent" / "web" / "static" / "index.html").read_text(encoding="utf-8")

        for label in ("最终动作", "方向概率", "关键上破", "支撑区", "耗时"):
            assert f'<span class="label">{label}</span>' in html

        assert "summaryMetrics.finalAction" in html
        assert "summaryMetrics.directionProb" in html
        assert "summaryMetrics.resistance" in html
        assert "summaryMetrics.support" in html
        assert "summaryMetrics.elapsed" in html
        assert 'decision.take_profit_price || \'--\'' not in html
        assert "分析仅供参考" not in html

    def test_summary_strip_stays_on_one_row(self) -> None:
        """The web summary strip keeps five stable columns like the GUI."""
        css = (ROOT / "pa_agent" / "web" / "static" / "css" / "app.css").read_text(encoding="utf-8")

        assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in css
        assert ".metric .value" in css
        assert "overflow-wrap: anywhere;" in css

    def test_api_client_preserves_error_detail(self) -> None:
        """Fetch errors should include backend detail text for user-facing toasts."""
        js = (ROOT / "pa_agent" / "web" / "static" / "js" / "api.js").read_text(encoding="utf-8")

        assert "async function errorMessage" in js
        assert "data.detail" in js
        assert "throw new Error(await errorMessage(res))" in js
