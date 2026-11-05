"""Free-lane retry policy: retry the same candidate once, then rotate."""
from __future__ import annotations

import json

import pytest

from pa_agent.ai.json_validator import Ok, ValidationError
from pa_agent.ai.sensenova_fallback import (
    FreeCandidate,
    FreeStageFailure,
    FreeStageSuccess,
    reset_fallback_state_for_tests,
    run_free_stage_fallback,
)

BASE = "https://api.kilo.ai/api/gateway"
MESSAGES = [{"role": "user", "content": "stage2"}]


@pytest.fixture(autouse=True)
def _clear_fallback_state() -> None:
    reset_fallback_state_for_tests()
    yield
    reset_fallback_state_for_tests()


def _cand(name: str) -> FreeCandidate:
    return FreeCandidate("kilo", name, BASE)


def _payload(model: str, content: str, *, reasoning: str = "") -> tuple[int, dict]:
    return 200, {
        "id": "x",
        "model": model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning": reasoning,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }


def test_empty_body_retries_same_candidate() -> None:
    calls: list[str] = []

    def post(url, *, headers, body, timeout):  # noqa: ANN001
        calls.append(body["model"])
        if len(calls) == 1:
            return _payload(body["model"], "", reasoning="only thinking")
        return _payload(body["model"], '{"order_type": "不下单"}')

    outcome = run_free_stage_fallback(
        messages=MESSAGES,
        validate=lambda reply: Ok(obj=json.loads(reply.content)),
        primary_api_key=None,
        candidates=[_cand("a/free")],
        http_post=post,
        attempts_per_candidate=2,
    )

    assert isinstance(outcome, FreeStageSuccess)
    assert calls == ["a/free", "a/free"]
    assert [a.ok for a in outcome.attempts] == [False, True]


def test_429_is_not_retried_in_place() -> None:
    calls: list[str] = []

    def post(url, *, headers, body, timeout):  # noqa: ANN001
        calls.append(body["model"])
        if body["model"] == "a/free":
            return 429, {"error": {"message": "rate limited"}}
        return _payload(body["model"], '{"ok": 1}')

    outcome = run_free_stage_fallback(
        messages=MESSAGES,
        validate=lambda reply: Ok(obj=json.loads(reply.content)),
        primary_api_key=None,
        candidates=[_cand("a/free"), _cand("b/free")],
        http_post=post,
        attempts_per_candidate=2,
    )

    assert isinstance(outcome, FreeStageSuccess)
    assert outcome.candidate.model == "b/free"
    # The rate-limited candidate is cooled down, not hammered twice.
    assert calls == ["a/free", "b/free"]


def test_validation_miss_retries_same_candidate_then_rotates() -> None:
    calls: list[str] = []
    misses = [
        ValidationError(category="c", stage="stage2", raw_text="bad", message="quality gate")
    ]

    def post(url, *, headers, body, timeout):  # noqa: ANN001
        calls.append(body["model"])
        return _payload(body["model"], '{"ok": 1}')

    def validate(_reply):  # noqa: ANN001
        if misses:
            return misses.pop()
        return Ok(obj={})

    outcome = run_free_stage_fallback(
        messages=MESSAGES,
        validate=validate,
        primary_api_key=None,
        candidates=[_cand("a/free"), _cand("b/free")],
        http_post=post,
        attempts_per_candidate=2,
    )

    assert isinstance(outcome, FreeStageSuccess)
    assert outcome.candidate.model == "a/free"
    assert calls == ["a/free", "a/free"]


def test_failure_keeps_last_reply_for_later_salvage() -> None:
    def post(url, *, headers, body, timeout):  # noqa: ANN001
        return _payload(body["model"], "prose, not json")

    outcome = run_free_stage_fallback(
        messages=MESSAGES,
        validate=lambda _reply: ValidationError(
            category="c", stage="stage2", raw_text="x", message="quality gate"
        ),
        primary_api_key=None,
        candidates=[_cand("a/free")],
        http_post=post,
        attempts_per_candidate=2,
    )

    assert isinstance(outcome, FreeStageFailure)
    assert len(outcome.attempts) == 2
    assert outcome.last_reply is not None
    assert outcome.last_reply.content == "prose, not json"
