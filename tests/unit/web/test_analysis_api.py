"""Tests for analysis API.

P0 baseline contract:
    * POST /api/analysis/submit returns an SSE stream whose every event
      carries a server-generated ``run_id`` (UUID4 hex).
    * Lifecycle event names are aligned with the PyQt ``AIStreamPanel``
      (snake_case: ``stage1_started``, ``stage1_done``, ``stage2_started``,
      ``stage2_done``, ``record_saved``, ``done``, ...).
    * Submitting twice yields two distinct ``run_id`` values (no
      server-side aliasing).
"""
from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from pa_agent.web.server import create_app
from pa_agent.web.service.analysis_service import (
    AnalysisService,
    generate_run_id,
)


def _sse_data_lines(content: str, event_name: str) -> list[str]:
    """Return all ``data:`` payloads that follow an ``event: <event_name>`` line."""
    out: list[str] = []
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"event: {event_name}"):
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("data: "):
                    out.append(lines[j][len("data: "):])
                elif lines[j].startswith("event: "):
                    break
    return out


_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class TestAnalysisApi:
    def test_submit_returns_sse_events(self) -> None:
        """POST /api/analysis/submit returns an SSE stream with analysis events."""
        svc = AnalysisService(orchestrator=None)
        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/submit", json={"bar_count": 80, "stance": "balanced"}
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = "".join(response.iter_text())
        # The P0 baseline mock emits the full PyQt-aligned lifecycle:
        assert "event: stage1_started" in body
        assert "event: stage1_reasoning" in body
        assert "event: stage1_done" in body
        assert "event: stage2_started" in body
        assert "event: stage2_decision" in body
        assert "event: stage2_done" in body
        assert "event: record_saved" in body
        assert "event: done" in body

    def test_submit_503_when_uninitialized(self) -> None:
        """POST /api/analysis/submit returns 503 when the analysis service is not set."""
        app = create_app(analysis_service=None)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/submit", json={"bar_count": 80, "stance": "balanced"}
        )
        assert response.status_code == 503

    def test_submit_injects_run_id_into_every_event(self) -> None:
        """Every emitted event payload must carry a server-generated run_id."""
        svc = AnalysisService(orchestrator=None)
        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/submit", json={"bar_count": 80, "stance": "balanced"}
        )
        body = "".join(response.iter_text())

        # Collect every ``data:`` payload and verify each one has a run_id.
        run_ids: set[str] = set()
        for line in body.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                assert "run_id" in payload, f"missing run_id in {payload!r}"
                rid = payload["run_id"]
                assert _RUN_ID_RE.match(rid), f"malformed run_id {rid!r}"
                run_ids.add(rid)
        # All events in a single submission share the same run_id.
        assert len(run_ids) == 1, f"expected one run_id, got {run_ids!r}"

    def test_submit_two_runs_get_distinct_run_ids(self) -> None:
        """Two consecutive submissions must each receive a fresh run_id."""
        svc = AnalysisService(orchestrator=None)
        app = create_app(analysis_service=svc)
        client = TestClient(app)

        rids: list[str] = []
        for _ in range(2):
            response = client.post(
                "/api/analysis/submit", json={"bar_count": 80, "stance": "balanced"}
            )
            body = "".join(response.iter_text())
            for line in body.splitlines():
                if line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                    if "run_id" in payload:
                        rids.append(payload["run_id"])
                        break
        assert len(rids) == 2
        assert rids[0] != rids[1], f"run_id collision across submissions: {rids!r}"

    def test_submit_accepts_client_supplied_run_id(self) -> None:
        """If the client supplies a run_id, the server echoes it in every event."""
        svc = AnalysisService(orchestrator=None)
        app = create_app(analysis_service=svc)
        client = TestClient(app)
        client_rid = generate_run_id()

        response = client.post(
            "/api/analysis/submit",
            json={
                "bar_count": 80,
                "stance": "balanced",
                "run_id": client_rid,
            },
        )
        body = "".join(response.iter_text())
        run_ids_seen: set[str] = set()
        for line in body.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                if "run_id" in payload:
                    run_ids_seen.add(payload["run_id"])
        assert run_ids_seen == {client_rid}

    def test_submit_stage1_result_payload_includes_run_id(self) -> None:
        """The ``stage1_result`` event payload must include a run_id field."""
        svc = AnalysisService(orchestrator=None)
        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/submit", json={"bar_count": 80, "stance": "balanced"}
        )
        body = "".join(response.iter_text())
        payloads = _sse_data_lines(body, "stage1_result")
        assert payloads, "stage1_result event missing"
        parsed = json.loads(payloads[0])
        assert "run_id" in parsed
        assert parsed["diagnosis"] == "Bullish trend"

    def test_submit_incremental_requires_previous_record(self) -> None:
        """Incremental submission without a prior record must return HTTP 400."""
        svc = AnalysisService(orchestrator=None)
        app = create_app(analysis_service=svc)
        client = TestClient(app)

        response = client.post(
            "/api/analysis/submit",
            json={
                "bar_count": 80,
                "stance": "balanced",
                "incremental": True,
                "incremental_new_bars": 3,
            },
        )
        assert response.status_code == 400
