"""Independent regression checks for the exact-URL fast-fail boundary.

These complement tests/unit/test_sensenova_fallback.py:
- the SDK/backoff retry bypass must apply to the SenseNova token URL only
- a 429 on that URL must fail fast instead of delaying the free-model fallback
- a non-429 rejection must still allow the stream_options downgrade retry
"""
from unittest.mock import MagicMock, patch

import pytest

from pa_agent.ai import deepseek_client
from pa_agent.ai.deepseek_client import DeepSeekClient
from pa_agent.config.settings import AIProviderSettings
from tests.unit.test_deepseek_client import _make_mock_response

SENSENOVA = "https://token.sensenova.cn/v1"


def _client(base: str) -> DeepSeekClient:
    return DeepSeekClient(AIProviderSettings(base_url=base, api_key="synthetic"))


def _recording_backoff(seen: dict):
    real = deepseek_client.call_with_rate_limit_backoff

    def _wrapper(fn, **kwargs):
        seen.clear()
        seen.update(kwargs)
        return real(fn, **kwargs)

    return _wrapper


@pytest.mark.parametrize(
    "base,enabled",
    [
        (SENSENOVA, True),
        ("https://token.sensenova.cn/v1/", True),
        ("https://token.sensenova.cn/v2", False),
        ("https://other.sensenova.cn/v1", False),
        ("https://sensenova.cn.evil.invalid/v1", False),
        ("https://api.deepseek.com", False),
    ],
)
def test_exact_url_alone_disables_sdk_retry(base, enabled):
    sdk = MagicMock()
    sdk.return_value.chat.completions.create.return_value = _make_mock_response()
    with patch("pa_agent.ai.deepseek_client._OpenAI", sdk):
        _client(base).chat([{"role": "user", "content": "test"}])
    kwargs = sdk.call_args.kwargs
    assert ("max_retries" in kwargs) is enabled
    if enabled:
        assert kwargs["max_retries"] == 0


@pytest.mark.parametrize(
    "base,expected_retries",
    [(SENSENOVA, 0), ("https://api.deepseek.com", None)],
)
def test_backoff_retry_bypass_only_for_sensenova(base, expected_retries):
    seen: dict = {}
    sdk = MagicMock()
    sdk.return_value.chat.completions.create.return_value = _make_mock_response()
    with patch("pa_agent.ai.deepseek_client._OpenAI", sdk), patch.object(
        deepseek_client, "call_with_rate_limit_backoff", _recording_backoff(seen)
    ):
        _client(base).chat([{"role": "user", "content": "test"}])
    if expected_retries is None:
        assert "max_retries" not in seen
    else:
        assert seen["max_retries"] == expected_retries


def test_sensenova_429_fails_fast_without_repeating_request():
    """429 must reach the orchestrator immediately so it can rotate free models."""
    sdk = MagicMock()
    sdk.return_value.chat.completions.create.side_effect = RuntimeError(
        "Error code: 429 - rate limit reached"
    )
    seen: dict = {}
    with patch("pa_agent.ai.deepseek_client._OpenAI", sdk), patch.object(
        deepseek_client, "call_with_rate_limit_backoff", _recording_backoff(seen)
    ), pytest.raises(RuntimeError, match="429"):
        _client(SENSENOVA).stream_chat([{"role": "user", "content": "test"}])
    assert seen["max_retries"] == 0
    assert sdk.return_value.chat.completions.create.call_count == 1


def test_non_rate_limit_rejection_still_downgrades_stream_options():
    """A provider rejecting stream_options must keep its non-429 retry path."""
    ok_chunk = MagicMock()
    ok_chunk.choices = []
    ok_chunk.usage = MagicMock(
        prompt_tokens=1, completion_tokens=1, total_tokens=2,
        prompt_tokens_details=MagicMock(cached_tokens=0),
    )
    ok_chunk.id = "id-1"
    ok_chunk.model = "m"
    sdk = MagicMock()
    sdk.return_value.chat.completions.create.side_effect = [
        RuntimeError("stream_options is not supported"),
        iter([ok_chunk]),
    ]
    with patch("pa_agent.ai.deepseek_client._OpenAI", sdk):
        _client(SENSENOVA).stream_chat([{"role": "user", "content": "test"}])
    assert sdk.return_value.chat.completions.create.call_count == 2
    assert "stream_options" not in sdk.return_value.chat.completions.create.call_args.kwargs


def test_thinking_route_is_pinned_to_lowest_effort():
    """Every spare-channel request pins thinking to its smallest budget, and only
    a route that rejects that shape is retried at the lowest effort tier."""
    from pa_agent.ai.sensenova_fallback import KILO_BASE, FreeCandidate, call_free_candidate

    attempts: list[dict] = []

    def post(url, *, headers, body, timeout):  # noqa: ANN001
        attempts.append(dict(body))
        if body.get("reasoning") == {"max_tokens": 512}:
            return 400, {
                "error": {"message": "reasoning.max_tokens is not supported for this model"}
            }
        return 200, {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}

    call_free_candidate(
        FreeCandidate("kilo", "stepfun/step-5-preview-free", KILO_BASE),
        [{"role": "user", "content": "synthetic"}],
        http_post=post,
    )

    assert attempts[0]["reasoning"] == {"max_tokens": 512}
    assert attempts[-1]["reasoning"] == {"effort": "minimal"}
    assert all(a.get("reasoning") for a in attempts)


# ── Progress hints: the UI must show "still working, not frozen" ──────────────

def _free_ok(content_obj: dict) -> dict:
    import json as _json

    return {
        "id": "x",
        "choices": [
            {
                "message": {"role": "assistant", "content": _json.dumps(content_obj, ensure_ascii=False)},
                "finish_reason": "stop",
            }
        ],
    }


def test_run_free_stage_fallback_reports_each_candidate():
    """Rotation reports waiting and success, but keeps candidate failures internal."""
    import json as _json

    from pa_agent.ai.json_validator import Ok
    from pa_agent.ai.sensenova_fallback import (
        FreeCandidate,
        run_free_stage_fallback,
    )
    from tests.unit.test_sensenova_fallback import VALID_STAGE1

    lines: list[str] = []
    calls = {"n": 0}

    def post(url, *, headers, body, timeout):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 rate limited")
        return 200, _free_ok(VALID_STAGE1)

    outcome = run_free_stage_fallback(
        messages=[{"role": "user", "content": "stage1"}],
        validate=lambda reply: Ok(obj=_json.loads(reply.content)),
        primary_api_key="sk-SECRET",
        candidates=[
            FreeCandidate("kilo", "first/free", "https://api.kilo.ai/api/gateway"),
            FreeCandidate("kilo", "second/free", "https://api.kilo.ai/api/gateway"),
        ],
        http_post=post,
        on_progress=lines.append,
        on_content_token=lines.append,
    )

    joined = "".join(lines)
    assert "第 1/2 次尝试" in joined
    assert "第 2/2 次尝试" in joined
    assert "当前尝试未成功" not in joined
    assert "当前结果未通过检查" not in joined
    assert "正在继续处理" not in joined
    assert "校验通过" in joined
    assert "sk-SECRET" not in joined
    assert outcome.candidate.model == "second/free"

    # The success hint must precede the answer it introduces.
    hint_idx = next(i for i, c in enumerate(lines) if "校验通过" in c)
    answer_idx = next(i for i, c in enumerate(lines) if '"cycle_position"' in c)
    assert hint_idx < answer_idx


def test_progress_lines_never_advertise_the_free_lane():
    """User-facing text must stay neutral: no upstream model names, no "免费".

    The user explicitly asked for this (it reads as cheap); provider/model stay
    in the log and the saved record for diagnosis.
    """
    import json as _json

    from pa_agent.ai.json_validator import Ok
    from pa_agent.ai.sensenova_fallback import FreeCandidate, run_free_stage_fallback
    from tests.unit.test_sensenova_fallback import VALID_STAGE1

    lines: list[str] = []
    calls = {"n": 0}

    def post(url, *, headers, body, timeout):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kilo/inclusionai/ling-3.1-flash: HTTP 429 rate limited")
        return 200, _free_ok(VALID_STAGE1)

    run_free_stage_fallback(
        messages=[{"role": "user", "content": "stage1"}],
        validate=lambda reply: Ok(obj=_json.loads(reply.content)),
        primary_api_key=None,
        candidates=[
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway"),
            FreeCandidate("kilo", "nvidia/nemotron-3.5-lightning:free", "https://api.kilo.ai/api/gateway"),
        ],
        http_post=post,
        on_progress=lines.append,
    )

    joined = "".join(lines)
    for forbidden in (
        "ling-3.1-flash",
        "nemotron-3.5-lightning",
        "api.kilo.ai",
        "kilo/",
        "免费",
        "兜底",
        "[兜底]",
    ):
        assert forbidden not in joined, f"user-visible hint leaked {forbidden!r}: {joined}"
    # No technical failure cause is exposed; the UI only reports progress.
    assert "当前尝试未成功" not in joined
    assert "当前结果未通过检查" not in joined
    assert "正在继续处理" not in joined
    assert "429" not in joined
    assert "限流" not in joined


def test_validation_failure_stays_internal_and_rotation_continues():
    from pa_agent.ai.json_validator import Ok, ValidationError
    from pa_agent.ai.sensenova_fallback import FreeCandidate, run_free_stage_fallback
    from tests.unit.test_sensenova_fallback import VALID_STAGE1

    lines: list[str] = []
    validations = iter([
        ValidationError(category="a", stage="stage1", raw_text="bad", message="invalid result"),
        Ok(obj=VALID_STAGE1),
    ])
    outcome = run_free_stage_fallback(
        messages=[{"role": "user", "content": "stage1"}],
        validate=lambda reply: next(validations),
        primary_api_key=None,
        candidates=[
            FreeCandidate("kilo", "first/free", "https://api.kilo.ai/api/gateway"),
            FreeCandidate("kilo", "second/free", "https://api.kilo.ai/api/gateway"),
        ],
        http_post=lambda url, **kwargs: (200, _free_ok(VALID_STAGE1)),
        on_progress=lines.append,
    )
    joined = "".join(lines)
    for unwanted in ("当前尝试未成功", "当前结果未通过检查", "正在继续处理", "invalid result"):
        assert unwanted not in joined
    assert "第 2/2 次尝试" in joined
    assert "校验通过" in joined
    assert outcome.candidate.model == "second/free"
    assert not outcome.attempts[0].ok
    assert "invalid result" in outcome.attempts[0].error


def test_run_free_stage_fallback_reports_total_exhaustion():
    from pa_agent.ai.sensenova_fallback import FreeCandidate, run_free_stage_fallback

    lines: list[str] = []

    def post(url, *, headers, body, timeout):  # noqa: ANN001
        raise RuntimeError("upstream down")

    outcome = run_free_stage_fallback(
        messages=[{"role": "user", "content": "stage1"}],
        validate=lambda reply: None,
        primary_api_key=None,
        candidates=[FreeCandidate("kilo", "only/free", "https://api.kilo.ai/api/gateway")],
        http_post=post,
        on_progress=lines.append,
    )
    joined = "".join(lines)
    assert "均未成功" in joined
    assert outcome.attempts and not outcome.attempts[0].ok


def test_orchestrator_shows_progress_in_ui_during_fallback():
    """End-to-end: a frozen-looking wait must still emit visible progress."""
    import openai
    from unittest.mock import patch

    from pa_agent.ai.sensenova_fallback import FreeCandidate
    from pa_agent.config.settings import Settings
    from pa_agent.util.threading import CancelToken
    from tests.integration.conftest import make_frame
    from tests.unit.test_sensenova_fallback import (
        VALID_STAGE1,
        VALID_STAGE2,
        _http_post_stage,
        _orchestrator,
        make_reply,
    )

    settings = Settings()
    settings.provider.base_url = "https://token.sensenova.cn/v1"
    client = MagicMock()
    client.stream_chat.side_effect = [
        openai.APIConnectionError(request=MagicMock(), message="primary down"),
        make_reply(VALID_STAGE2),
    ]
    orch = _orchestrator(settings, client)

    chunks: list[str] = []
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE1),
    ):
        record = orch.submit(
            make_frame(),
            CancelToken(),
            lambda _e: None,
            on_stage1_content=chunks.append,
        )

    joined = "".join(chunks)
    assert record.exception is None
    assert "[提示]" in joined
    assert "正在等待备用通道" in joined
    assert "界面会显示当前处理进度，请稍候" in joined
    for unwanted in ("备用通道可能较慢", "最长需要几分钟", "120秒", "上游限流", "限流"):
        assert unwanted not in joined
    # The real answer must still be delivered after the hints.
    assert "cycle_position" in joined
    # Neutral wording only: never expose the lane or the upstream model.
    for forbidden in ("ling-3.1-flash", "kilo", "api.kilo.ai", "免费", "兜底"):
        assert forbidden not in joined, f"UI progress leaked {forbidden!r}"

