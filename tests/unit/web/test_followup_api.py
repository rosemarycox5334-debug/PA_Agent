"""Tests for follow-up chat API endpoint."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from pa_agent.web.server import create_app
from pa_agent.web.service.analysis_service import AnalysisService


def _sse_data(content: str, event_name: str) -> str | None:
    """Extract the data payload from an SSE response line for a given event.

    json.dumps escapes non-ASCII by default, so we compare against JSON-encoded
    payloads rather than raw Unicode strings.
    """
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"event: {event_name}"):
            # The data payload follows the event line (possibly separated by a blank line).
            for j in range(i + 1, len(lines)):
                candidate = lines[j]
                if candidate.startswith("data: "):
                    return candidate[len("data: "):]
                if candidate and not candidate.startswith(":"):
                    # Skip blank lines / comments between event and data.
                    continue
            return None
    return None


class TestFollowupApi:
    def test_followup_returns_sse_events(self) -> None:
        """POST /api/analysis/followup returns an SSE stream with followup events."""
        mock_chat_session = MagicMock()
        mock_chat_session.send.return_value = MagicMock(content="测试回复")

        svc = AnalysisService(orchestrator=None)
        svc._chat_session = mock_chat_session

        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/followup", json={"text": "为什么做多？"}
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        content = response.text
        assert "event: followup_reply" in content
        data = _sse_data(content, "followup_reply")
        assert data is not None
        parsed = json.loads(data)
        assert parsed["content"] == "测试回复"

    def test_followup_503_when_uninitialized(self) -> None:
        """POST /api/analysis/followup returns 503 when service is not set."""
        app = create_app(analysis_service=None)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/followup", json={"text": "test"}
        )
        assert response.status_code == 503

    def test_followup_yields_reasoning_events(self) -> None:
        """POST /api/analysis/followup yields followup_reasoning events from callback."""
        mock_chat_session = MagicMock()

        def _mock_send(text, cancel_token=None, on_reasoning_token=None, on_content_token=None):
            if on_reasoning_token:
                on_reasoning_token("思考中...")
            if on_content_token:
                on_content_token("回复内容")
            mock_reply = MagicMock()
            mock_reply.content = "最终回复"
            return mock_reply

        mock_chat_session.send = _mock_send

        svc = AnalysisService(orchestrator=None)
        svc._chat_session = mock_chat_session

        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/followup", json={"text": "分析原因"}
        )
        assert response.status_code == 200
        content = response.text
        assert "event: followup_reasoning" in content
        reasoning_data = _sse_data(content, "followup_reasoning")
        assert reasoning_data is not None
        assert json.loads(reasoning_data)["text"] == "思考中..."
        assert "event: followup_content" in content
        content_data = _sse_data(content, "followup_content")
        assert content_data is not None
        assert json.loads(content_data)["text"] == "回复内容"
        assert "event: followup_reply" in content
        assert "event: done" in content

    def test_followup_error_when_no_chat_session(self) -> None:
        """POST /api/analysis/followup yields error when no chat session exists."""
        svc = AnalysisService(orchestrator=None)
        # _chat_session is None by default

        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/followup", json={"text": "test"}
        )
        assert response.status_code == 200
        content = response.text
        assert "event: error" in content

    def test_followup_error_when_chat_session_send_raises(self) -> None:
        """POST /api/analysis/followup yields error event when chat session raises."""
        mock_chat_session = MagicMock()
        mock_chat_session.send.side_effect = RuntimeError("Chat failed")

        svc = AnalysisService(orchestrator=None)
        svc._chat_session = mock_chat_session

        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/followup", json={"text": "test"}
        )
        assert response.status_code == 200
        content = response.text
        assert "event: error" in content
        data = _sse_data(content, "error")
        assert data is not None
        parsed = json.loads(data)
        assert "Chat failed" in parsed["message"]

    def test_create_app_auto_wires_followup_service_from_analysis_session(self):
        """Lightweight app factory callers can still use follow-up chat."""
        mock_chat_session = MagicMock()
        mock_chat_session._turn = 0
        mock_chat_session.send.return_value = MagicMock(content="reply")

        svc = AnalysisService(orchestrator=None)
        svc._chat_session = mock_chat_session

        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/followup",
            json={"text": "why wait?"},
        )

        assert response.status_code == 200
        assert "event: followup_reply" in response.text
        data = _sse_data(response.text, "followup_reply")
        assert data is not None
        assert json.loads(data)["content"] == "reply"
