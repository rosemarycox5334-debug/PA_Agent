"""Curl-style integration tests for new APIs (uses TestClient, no real server needed)."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from pa_agent.web.server import create_app


def _curl(method: str, path: str, payload: dict | None = None) -> None:
    app = create_app()
    client = TestClient(app)
    if method == "GET":
        r = client.get(path)
    elif method == "POST":
        r = client.post(path, json=payload)
    else:
        raise ValueError(method)
    print(f">>> curl -X {method} http://localhost:8080{path}")
    if payload:
        print(f"    -d '{json.dumps(payload, ensure_ascii=False)}'")
    print(f"<<< HTTP {r.status_code}")
    try:
        body = r.json()
        print(json.dumps(body, ensure_ascii=False, indent=2))
    except Exception:
        print(r.text)
    print()


def test_records_list() -> None:
    _curl("GET", "/api/records")


def test_records_load_404() -> None:
    _curl("GET", "/api/records/nonexistent.json")


def test_debug_turns_empty() -> None:
    _curl("GET", "/api/debug/turns")


def test_debug_turns_clear() -> None:
    _curl("POST", "/api/debug/turns/clear")


def test_decision_tree() -> None:
    _curl("GET", "/api/decision-tree")


if __name__ == "__main__":
    test_records_list()
    test_records_load_404()
    test_debug_turns_empty()
    test_debug_turns_clear()
    test_decision_tree()
