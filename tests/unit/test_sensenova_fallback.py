"""Unit / orchestrator-wiring tests for SenseNova-only free-model fallback."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import openai
import pytest

from pa_agent.ai.json_validator import Ok
from pa_agent.ai.sensenova_fallback import (
    FreeCandidate,
    FreeStageFailure,
    FreeStageSuccess,
    build_candidates,
    call_free_candidate,
    is_sensenova_fallback_eligible,
    mark_cooldown,
    normalize_base_url,
    reset_fallback_state_for_tests,
    run_free_stage_fallback,
)
from pa_agent.config.settings import Settings
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.util.threading import CancelToken
from tests.fixtures.validators import schema_test_validator
from tests.integration.conftest import (
    VALID_STAGE1,
    VALID_STAGE2,
    make_frame,
    make_reply,
)


@pytest.fixture(autouse=True)
def _clear_fallback_state() -> None:
    reset_fallback_state_for_tests()
    yield
    reset_fallback_state_for_tests()


def _sensenova_settings() -> Settings:
    settings = Settings()
    settings.provider.base_url = "https://token.sensenova.cn/v1"
    settings.provider.api_key = "sk-PRIMARY-SECRET"
    return settings


def _assembler() -> MagicMock:
    mock = MagicMock()
    mock.build_stage1.return_value = [{"role": "user", "content": "stage1"}]
    mock.build_stage2.return_value = [{"role": "user", "content": "stage2"}]
    mock.build_stage2_continuation.return_value = [
        {"role": "user", "content": "stage2"},
        {"role": "user", "content": "cont"},
    ]
    return mock


def _orchestrator(
    settings: Settings,
    client: Any | None = None,
    *,
    validator: Any | None = None,
    pending_writer: Any | None = None,
) -> TwoStageOrchestrator:
    from pa_agent.ai.router import route_strategy_files

    return TwoStageOrchestrator(
        client=client or MagicMock(),
        assembler=_assembler(),
        router=route_strategy_files,
        validator=validator or schema_test_validator(),
        pending_writer=pending_writer or MagicMock(),
        exp_reader=MagicMock(read_top5=MagicMock(return_value=[]), read_for_stage2=MagicMock(return_value=[])),
        settings=settings,
    )


def _free_ok_payload(content_obj: dict[str, Any], *, model: str = "inclusionai/ling-3.1-flash") -> dict:
    return {
        "id": "free-ok",
        "model": model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content_obj, ensure_ascii=False),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "cost": 0,
        },
    }


def _http_post_stage(content_obj: dict[str, Any]):
    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        assert "authorization" not in {k.lower() for k in headers}
        assert headers.get("user-agent", "").startswith("PA-Agent/")
        # The spare channel prefers thinking routes but pins them to the smallest
        # thinking budget they accept; an unconstrained request would be a bug.
        reasoning = body.get("reasoning") or {}
        assert (
            "max_tokens" in reasoning or "effort" in reasoning
        ) or body.get("reasoning_effort"), (
            f"request left thinking unconstrained: {body.get('reasoning')!r}"
        )
        return 200, _free_ok_payload(content_obj, model=body["model"])

    return _post


def test_normalize_base_url_sensenova_variants() -> None:
    assert normalize_base_url("https://token.sensenova.cn/v1") == (
        "https://token.sensenova.cn/v1"
    )
    assert normalize_base_url("https://token.sensenova.cn/v1/") == (
        "https://token.sensenova.cn/v1"
    )
    assert normalize_base_url("HTTPS://TOKEN.SENSENOVA.CN/v1/") == (
        "https://token.sensenova.cn/v1"
    )
    assert is_sensenova_fallback_eligible("https://token.sensenova.cn/v1/")
    assert not is_sensenova_fallback_eligible("https://api.deepseek.com")
    assert not is_sensenova_fallback_eligible("https://token.sensenova.cn/v1/extra")


def test_submit_primary_success_does_not_call_free_fallback() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = [
        make_reply(VALID_STAGE1),
        make_reply(VALID_STAGE2),
    ]
    orch = _orchestrator(settings, client)
    with patch(
        "pa_agent.ai.sensenova_fallback.run_free_stage_fallback"
    ) as run_fb:
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    run_fb.assert_not_called()
    assert record.exception is None
    assert record.stage1_diagnosis is not None
    assert record.stage2_decision is not None


def test_submit_non_sensenova_url_does_not_call_free_fallback() -> None:
    settings = Settings()
    settings.provider.base_url = "https://api.deepseek.com"
    client = MagicMock()
    client.stream_chat.side_effect = openai.APIConnectionError(
        request=MagicMock(), message="primary down"
    )
    orch = _orchestrator(settings, client)
    with patch(
        "pa_agent.ai.sensenova_fallback.run_free_stage_fallback"
    ) as run_fb:
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    run_fb.assert_not_called()
    assert record.exception is not None
    assert record.exception.get("type") == "network_error"


def test_submit_stage1_network_error_recovers_via_free_and_validator() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = [
        openai.APIConnectionError(request=MagicMock(), message="stage1 network"),
        make_reply(VALID_STAGE2),
    ]
    pending = MagicMock()
    orch = _orchestrator(settings, client, pending_writer=pending)
    validate_spy = MagicMock(wraps=orch._validator.validate)
    orch._validator.validate = validate_spy  # type: ignore[method-assign]

    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate(
                "kilo",
                "inclusionai/ling-3.1-flash",
                "https://api.kilo.ai/api/gateway",
            )
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE1),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)

    assert record.exception is None
    assert record.stage1_diagnosis is not None
    assert record.stage2_decision is not None
    assert (record.stage1_response or {}).get("fallback_model") == (
        "inclusionai/ling-3.1-flash"
    )
    assert any(c.args and c.args[0] == "stage1" for c in validate_spy.call_args_list)
    pending.save_full.assert_called_once()


def test_submit_stage1_value_error_recovers_via_free() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = [
        ValueError("ordinary primary failure"),
        make_reply(VALID_STAGE2),
    ]
    orch = _orchestrator(settings, client)
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE1),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    assert record.exception is None
    assert record.stage1_diagnosis is not None


def test_submit_stage1_illegal_json_recovers_via_free() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    bad = make_reply({})
    bad.content = "NOT_JSON{{"
    client.stream_chat.side_effect = [bad, make_reply(VALID_STAGE2)]
    # Disable validation retries so illegal JSON becomes ValidationError immediately.
    orch = _orchestrator(settings, client)
    orch._settings.validation.retry_enabled = False  # type: ignore[union-attr]
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate(
                "kilo",
                "nvidia/nemotron-3.5-lightning:free",
                "https://api.kilo.ai/api/gateway",
            )
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE1),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    assert record.exception is None
    assert (record.stage1_response or {}).get("fallback_model") == (
        "nvidia/nemotron-3.5-lightning:free"
    )


def test_submit_stage1_validate_retry_exception_recovers_via_free() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    bad = make_reply({})
    bad.content = "NOT_JSON{{"
    client.stream_chat.side_effect = [
        bad,
        openai.APIConnectionError(
            request=MagicMock(), message="validate retry network fail"
        ),
        make_reply(VALID_STAGE2),
    ]
    orch = _orchestrator(settings, client)
    orch._settings.validation.retry_enabled = True  # type: ignore[union-attr]
    orch._settings.validation.retry_max = 2  # type: ignore[union-attr]
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE1),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    assert record.exception is None
    assert record.stage1_diagnosis is not None


def test_submit_stage2_network_and_value_error_and_json_and_retry() -> None:
    """Stage2: network / ValueError / illegal JSON / validate-retry each recover."""
    cases: list[tuple[str, Any]] = [
        ("network", openai.APIConnectionError(request=MagicMock(), message="s2 net")),
        ("value", ValueError("s2 value")),
    ]
    for label, exc in cases:
        reset_fallback_state_for_tests()
        settings = _sensenova_settings()
        client = MagicMock()
        client.stream_chat.side_effect = [make_reply(VALID_STAGE1), exc]
        orch = _orchestrator(settings, client)
        with patch(
            "pa_agent.ai.sensenova_fallback.build_candidates",
            return_value=[
                FreeCandidate(
                    "kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway"
                )
            ],
        ), patch(
            "pa_agent.ai.sensenova_fallback._default_http_post",
            side_effect=_http_post_stage(VALID_STAGE2),
        ):
            record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
        assert record.exception is None, label
        assert record.stage2_decision is not None, label
        assert (record.stage2_response or {}).get("fallback_provider") == "kilo", label

    # illegal JSON
    reset_fallback_state_for_tests()
    settings = _sensenova_settings()
    client = MagicMock()
    bad = make_reply({})
    bad.content = "%%%"
    client.stream_chat.side_effect = [make_reply(VALID_STAGE1), bad]
    orch = _orchestrator(settings, client)
    orch._settings.validation.retry_enabled = False  # type: ignore[union-attr]
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE2),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    assert record.exception is None
    assert record.stage2_decision is not None

    # validate_with_retry raises
    reset_fallback_state_for_tests()
    settings = _sensenova_settings()
    client = MagicMock()
    bad = make_reply({})
    bad.content = "%%%"
    client.stream_chat.side_effect = [
        make_reply(VALID_STAGE1),
        bad,
        RuntimeError("s2 retry boom"),
    ]
    orch = _orchestrator(settings, client)
    orch._settings.validation.retry_enabled = True  # type: ignore[union-attr]
    orch._settings.validation.retry_max = 2  # type: ignore[union-attr]
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE2),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    assert record.exception is None
    assert record.stage2_decision is not None


def test_submit_cancel_not_swallowed() -> None:
    from pa_agent.ai.deepseek_client import CancelledError

    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = openai.APIConnectionError(
        request=MagicMock(), message="primary"
    )
    orch = _orchestrator(settings, client)
    token = CancelToken()

    def _build(*_a, **_k):  # noqa: ANN001
        token.set()
        return [
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")
        ]

    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        side_effect=_build,
    ):
        with pytest.raises(CancelledError):
            orch.submit(make_frame(), token, lambda _e: None)


def test_submit_all_free_candidates_fail_closed_with_trace() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = openai.APIConnectionError(
        request=MagicMock(), message="primary"
    )
    pending = MagicMock()
    orch = _orchestrator(settings, client, pending_writer=pending)

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        return 503, {"error": {"message": "down"}}

    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate("kilo", "a", "https://api.kilo.ai/api/gateway"),
            FreeCandidate("kilo", "b", "https://api.kilo.ai/api/gateway"),
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_post,
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)

    assert record.exception is not None
    fb = record.exception.get("sensenova_free_fallback") or {}
    assert fb.get("triggered") is True
    assert fb.get("success") is False
    assert len(fb.get("attempts") or []) == 2
    pending.save_partial.assert_called()


def test_submit_clears_stale_fallback_failure_across_runs() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = openai.APIConnectionError(
        request=MagicMock(), message="primary"
    )
    orch = _orchestrator(settings, client)
    orch._last_sensenova_fallback_failure = FreeStageFailure(  # type: ignore[attr-defined]
        attempts=[],
        message="stale-from-previous-submit",
    )
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[],
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    fb = (record.exception or {}).get("sensenova_free_fallback") or {}
    assert fb.get("summary") != "stale-from-previous-submit"


def test_non_sensenova_value_error_still_raises() -> None:
    settings = Settings()
    settings.provider.base_url = "https://api.deepseek.com"
    client = MagicMock()
    client.stream_chat.side_effect = ValueError("not network")
    orch = _orchestrator(settings, client)
    with pytest.raises(ValueError, match="not network"):
        orch.submit(make_frame(), CancelToken(), lambda _e: None)


def test_network_error_types_are_ordinary() -> None:
    settings = Settings()
    orch = _orchestrator(settings)
    assert orch._is_network_error(
        openai.APIConnectionError(request=MagicMock(), message="Connection error.")
    )
    assert orch._is_network_error(TimeoutError("timed out"))
    from pa_agent.ai.deepseek_client import CancelledError

    assert not orch._is_network_error(CancelledError("stop"))


def test_build_candidates_filters_cooldown_before_cap() -> None:
    """Cooldown filter must run before the max-6 cut so usable models are kept."""

    def _get(url, *, headers, timeout):  # noqa: ANN001
        assert headers.get("user-agent", "").startswith("PA-Agent/")
        ids = [
            "cool-1",
            "cool-2",
            "cool-3",
            "cool-4",
            "cool-5",
            "cool-6",
            "poolside/laguna-s-2.1:free",
            "nvidia/nemotron-3.5-content-safety:free",
        ]
        return {"data": [{"id": i, "isFree": True} for i in ids]}

    # Put the first six sorted cool-* into cooldown; the live-verified preferred
    # model must still appear because cooldown is filtered before capping.
    for mid in ("cool-1", "cool-2", "cool-3", "cool-4", "cool-5", "cool-6"):
        mark_cooldown(FreeCandidate("kilo", mid, "https://api.kilo.ai/api/gateway"))

    cands = build_candidates(http_get=_get, max_candidates=6)
    models = [c.model for c in cands]
    assert "poolside/laguna-s-2.1:free" in models
    assert "nvidia/nemotron-3.5-content-safety:free" not in models
    assert models[0] == "poolside/laguna-s-2.1:free"
    assert all(not m.startswith("cool-") for m in models)
    assert len(models) <= 6


def test_preferred_order_puts_thinking_routes_first() -> None:
    """A route that reasons must rank ahead of one that never reasons."""
    from pa_agent.ai.sensenova_fallback import _PREFERRED_KILO, _THINKING_CAPABLE_KILO

    flags = [m in _THINKING_CAPABLE_KILO for m in _PREFERRED_KILO]
    # True block first, False block second, no interleaving.
    assert flags == sorted(flags, reverse=True), (
        f"thinking-capable routes must precede the rest: "
        f"{list(zip(_PREFERRED_KILO, flags))}"
    )
    # A preset must reference a real variant, or the first call would crash.
    from pa_agent.ai.sensenova_fallback import (
        _REASONING_VARIANTS_BY_NAME,
        _REASONING_VARIANT_PRESET,
    )

    for model, variant in _REASONING_VARIANT_PRESET.items():
        assert variant in _REASONING_VARIANTS_BY_NAME, (model, variant)


def test_preferred_order_is_sorted_by_chinese_thinking_share() -> None:
    """Ranking key is how Chinese the route's hidden reasoning is (user's policy)."""
    from pa_agent.ai.sensenova_fallback import (
        _PREFERRED_KILO,
        _REASONING_CJK_SHARE_KILO,
    )

    shares = [_REASONING_CJK_SHARE_KILO[m] for m in _PREFERRED_KILO]
    assert shares == sorted(shares, reverse=True), (
        f"_PREFERRED_KILO must be sorted by Chinese share desc: "
        f"{list(zip(_PREFERRED_KILO, shares))}"
    )
    # Every preferred route must have a measured share, or the key is incomplete.
    assert set(_PREFERRED_KILO) <= set(_REASONING_CJK_SHARE_KILO)
    # Routes that never reason must sort behind every route that does.
    for model, share in _REASONING_CJK_SHARE_KILO.items():
        if share < 0:
            assert _PREFERRED_KILO.index(model) >= len(_PREFERRED_KILO) - 2


def test_chinese_thinking_instruction_is_not_used_as_a_fix() -> None:
    """Language is a model property: the prompt must not carry that claim.

    Measured 2026-10-29: demanding Chinese thinking in the system prompt left
    super-vl at 0% Chinese and lightning at 15%, so the instruction was removed
    and the routes are ranked by language instead.
    """
    from pa_agent.ai.glm_flash_lite import build_messages

    messages = build_messages(make_frame(), "stage2")
    system = messages[0]["content"]
    assert "使用中文解释" in system
    assert "禁止用英文思考" not in system


def test_measured_preset_skips_discovery_on_first_call() -> None:
    """beam-free is measured to want effort=minimal, not the token budget."""
    from pa_agent.ai.sensenova_fallback import _REASONING_VARIANT_PRESET

    assert _REASONING_VARIANT_PRESET.get("reflection/beam-free") == "minimal"

    bodies: list[dict[str, Any]] = []

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        bodies.append(dict(body))
        return 200, _ok_wait_payload(body["model"])

    cand = FreeCandidate("kilo", "reflection/beam-free", "https://api.kilo.ai/api/gateway")
    call_free_candidate(cand, [{"role": "user", "content": "ping"}], http_post=_post)

    assert len(bodies) == 1
    assert bodies[0]["reasoning"] == {"effort": "minimal"}


def test_build_candidates_kilo_requires_is_free() -> None:
    def _get(url, *, headers, timeout):  # noqa: ANN001
        return {
            "data": [
                {"id": "paid/model", "isFree": False},
                {"id": "free/model", "isFree": True},
                {"id": "poolside/laguna-s-2.1:free", "isFree": True},
            ]
        }

    cands = build_candidates(http_get=_get, max_candidates=6)
    ids = [c.model for c in cands]
    assert "paid/model" not in ids
    assert "poolside/laguna-s-2.1:free" in ids
    assert ids[0] == "poolside/laguna-s-2.1:free"


def test_call_free_rejects_empty_reasoning_only_and_truncated() -> None:
    cand = FreeCandidate("kilo", "m", "https://api.kilo.ai/api/gateway")

    def _empty(url, *, headers, body, timeout):  # noqa: ANN001
        return 200, {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        }

    with pytest.raises(RuntimeError, match="empty response"):
        call_free_candidate(cand, [{"role": "user", "content": "x"}], http_post=_empty)

    def _reason_only(url, *, headers, body, timeout):  # noqa: ANN001
        return 200, {
            "choices": [
                {
                    "message": {"content": "", "reasoning": "thinking..."},
                    "finish_reason": "stop",
                }
            ],
        }

    with pytest.raises(RuntimeError, match="reasoning-only"):
        call_free_candidate(cand, [{"role": "user", "content": "x"}], http_post=_reason_only)

    def _trunc(url, *, headers, body, timeout):  # noqa: ANN001
        return 200, {
            "choices": [
                {
                    "message": {"content": '{"gate_result":"wait"'},
                    "finish_reason": "length",
                }
            ],
        }

    with pytest.raises(RuntimeError, match="truncated"):
        call_free_candidate(cand, [{"role": "user", "content": "x"}], http_post=_trunc)


def test_keyboard_interrupt_propagates_from_post() -> None:
    c1 = FreeCandidate("kilo", "a", "https://api.kilo.ai/api/gateway")

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_free_stage_fallback(
            messages=[{"role": "user", "content": "hi"}],
            validate=lambda _r: Ok(obj={}),
            primary_api_key="sk-PRIMARY",
            candidates=[c1],
            http_post=_post,
        )


def test_primary_api_key_never_sent_to_free_upstream() -> None:
    secret = "sk-MUST-NOT-LEAK-TO-FREE"
    seen: dict[str, Any] = {}

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        seen["headers"] = dict(headers)
        seen["body"] = body
        blob = str(headers) + str(body)
        assert secret not in blob
        return 200, {
            "id": "x",
            "model": body["model"],
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {},
        }

    reply = call_free_candidate(
        FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway"),
        [{"role": "user", "content": "ping"}],
        primary_api_key=secret,
        http_post=_post,
    )
    assert reply.content == "ok"
    assert "authorization" not in {k.lower() for k in seen["headers"]}
    assert seen["headers"].get("user-agent", "").startswith("PA-Agent/")
    assert seen['body']['reasoning'] == {'max_tokens': 512}


def _ok_wait_payload(model: str) -> dict[str, Any]:
    return {
        "id": "x",
        "model": model,
        "choices": [
            {
                "message": {"role": "assistant", "content": '{"gate_result":"wait"}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }


def test_rejected_reasoning_budget_degrades_to_minimal_effort() -> None:
    """A route refusing the token budget still gets the lowest effort tier."""
    bodies: list[dict[str, Any]] = []

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        bodies.append(dict(body))
        if body.get("reasoning") == {"max_tokens": 512}:
            return 400, {
                "error": {"message": "reasoning.max_tokens is not supported for this model"}
            }
        return 200, _ok_wait_payload(body["model"])

    cand = FreeCandidate(
        "kilo", "poolside/laguna-xs-2.1:free", "https://api.kilo.ai/api/gateway"
    )
    messages = [{"role": "user", "content": "ping"}]

    reply = call_free_candidate(cand, messages, http_post=_post)
    assert reply.content
    assert bodies[-1]["reasoning"] == {"effort": "minimal"}

    # The negotiated variant is remembered, so later calls skip discovery.
    before = len(bodies)
    call_free_candidate(cand, messages, http_post=_post)
    assert len(bodies) == before + 1
    assert bodies[-1]["reasoning"] == {"effort": "minimal"}


def test_route_refusing_every_reasoning_control_is_still_called() -> None:
    """A route that rejects every control shape is called without any of them."""
    bodies: list[dict[str, Any]] = []

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        bodies.append(dict(body))
        if "reasoning" in body or "reasoning_effort" in body:
            return 400, {"error": {"message": "unknown field: reasoning"}}
        return 200, _ok_wait_payload(body["model"])

    cand = FreeCandidate(
        "kilo", "stealth/glyph-cluster", "https://api.kilo.ai/api/gateway"
    )
    reply = call_free_candidate(cand, [{"role": "user", "content": "ping"}], http_post=_post)
    assert reply.content
    assert "reasoning" not in bodies[-1]
    assert "reasoning_effort" not in bodies[-1]


def test_wait_ok_from_validator_is_not_treated_as_error() -> None:
    c1 = FreeCandidate("kilo", "good", "https://api.kilo.ai/api/gateway")

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        return 200, {
            "choices": [
                {
                    "message": {"content": '{"gate_result":"wait"}'},
                    "finish_reason": "stop",
                }
            ],
        }

    outcome = run_free_stage_fallback(
        messages=[{"role": "user", "content": "hi"}],
        validate=lambda _r: Ok(obj={"gate_result": "wait"}),
        primary_api_key="sk-PRIMARY",
        candidates=[c1],
        http_post=_post,
    )
    assert isinstance(outcome, FreeStageSuccess)
    assert outcome.result.obj["gate_result"] == "wait"


# --- streaming: reasoning must reach the UI while it is produced ----------------


def test_sse_line_parser_reads_data_lines_only() -> None:
    from pa_agent.ai.sensenova_fallback import _sse_line_to_event

    assert _sse_line_to_event('data: {"choices": []}') == {"choices": []}
    assert _sse_line_to_event("") is None
    assert _sse_line_to_event("event: ping") is None
    assert _sse_line_to_event("data: [DONE]") is None
    assert _sse_line_to_event("data: not-json") is None


def test_streaming_call_forwards_reasoning_chunks_in_order() -> None:
    seen: dict[str, Any] = {}

    def _stream(url, *, headers, body, timeout, on_event):  # noqa: ANN001
        seen["body"] = body
        seen["accept"] = headers.get("accept")
        for piece in ("想", "一", "下"):
            on_event({"choices": [{"delta": {"reasoning_content": piece}}]})
        for piece in ('{"decision"', ": 1}"):
            on_event({"choices": [{"delta": {"content": piece}}]})
        return 200, {
            "id": "stream-1",
            "model": body["model"],
            "choices": [
                {
                    "message": {
                        "content": '{"decision": 1}',
                        "reasoning_content": "想一下",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"completion_tokens": 9},
        }

    reasoning: list[str] = []
    content: list[str] = []
    cand = FreeCandidate(
        "kilo", "reflection/beam-free", "https://api.kilo.ai/api/gateway"
    )
    reply = call_free_candidate(
        cand,
        [{"role": "user", "content": "ping"}],
        stream=True,
        http_stream=_stream,
        on_reasoning_token=reasoning.append,
        on_content_token=content.append,
    )

    assert seen["body"]["stream"] is True
    assert seen["accept"] == "text/event-stream"
    assert "".join(reasoning) == "想一下"
    assert "".join(content) == '{"decision": 1}'
    assert reply.reasoning_content == "想一下"
    assert reply.content == '{"decision": 1}'
    assert reply.usage.completion_tokens == 9


def test_streaming_truncation_rejects_the_candidate() -> None:
    def _stream(url, *, headers, body, timeout, on_event):  # noqa: ANN001
        on_event({"choices": [{"delta": {"content": "半"}}]})
        return 200, {
            "choices": [
                {
                    "message": {"content": "半", "reasoning_content": ""},
                    "finish_reason": "length",
                }
            ]
        }

    cand = FreeCandidate(
        "kilo", "cohere/north-mini-code:free", "https://api.kilo.ai/api/gateway"
    )
    with pytest.raises(RuntimeError, match="truncated"):
        call_free_candidate(
            cand,
            [{"role": "user", "content": "x"}],
            stream=True,
            http_stream=_stream,
        )


def test_streamed_answer_is_not_emitted_twice() -> None:
    seen: list[str] = []

    def _stream(url, *, headers, body, timeout, on_event):  # noqa: ANN001
        on_event({"choices": [{"delta": {"reasoning_content": "思"}}]})
        on_event({"choices": [{"delta": {"content": "答"}}]})
        return 200, {
            "choices": [
                {
                    "message": {"content": "答", "reasoning_content": "思"},
                    "finish_reason": "stop",
                }
            ]
        }

    c1 = FreeCandidate(
        "kilo", "reflection/beam-free", "https://api.kilo.ai/api/gateway"
    )
    outcome = run_free_stage_fallback(
        messages=[{"role": "user", "content": "hi"}],
        validate=lambda _r: Ok(obj={"gate_result": "wait"}),
        primary_api_key=None,
        candidates=[c1],
        http_stream=_stream,
        stream=True,
        heartbeat_seconds=0,
        on_reasoning_token=seen.append,
        on_content_token=seen.append,
    )

    assert isinstance(outcome, FreeStageSuccess)
    # Each delta once: no whole-blob replay after the call returns.
    assert seen == ["思", "答"]


def test_route_ignoring_stream_still_shows_its_answer() -> None:
    """A route that returns one JSON body despite stream=True still renders."""
    seen: list[str] = []

    def _fake(url, *, headers, body, timeout):  # noqa: ANN001
        return 200, {
            "choices": [
                {
                    "message": {"content": "答", "reasoning_content": "思"},
                    "finish_reason": "stop",
                }
            ]
        }

    c1 = FreeCandidate(
        "kilo", "stealth/glyph-cluster", "https://api.kilo.ai/api/gateway"
    )
    outcome = run_free_stage_fallback(
        messages=[{"role": "user", "content": "hi"}],
        validate=lambda _r: Ok(obj={"gate_result": "wait"}),
        primary_api_key=None,
        candidates=[c1],
        http_post=_fake,
        stream=True,
        heartbeat_seconds=0,
        on_reasoning_token=seen.append,
        on_content_token=seen.append,
    )

    assert isinstance(outcome, FreeStageSuccess)
    assert seen == ["思", "答"]


def test_spare_channel_runs_before_the_configured_provider_chain() -> None:
    """Primary failure => spare channel first; GLM only if the spare channel fails."""
    from pa_agent.config.settings import AIProviderSettings

    settings = _sensenova_settings()
    settings.provider.fallbacks = [
        AIProviderSettings(
            base_url="https://open.bigmodel.cn/api/paas/v4",
            model="glm-4-flash",
            api_key="sk-GLM-FALLBACK",
        )
    ]
    client = MagicMock()
    client.stream_chat.side_effect = RuntimeError("primary down")
    orch = _orchestrator(settings, client)

    order: list[str] = []

    def _free_lane(exc):  # noqa: ANN001
        order.append("free")
        return (make_reply(VALID_STAGE1), Ok(obj={}), {})

    def _provider_fallback(**kwargs):  # noqa: ANN001
        order.append("provider")
        return False

    with patch.object(orch, "_try_next_provider_fallback", side_effect=_provider_fallback):
        out = orch._stream_chat_resilient(
            [{"role": "user", "content": "hi"}],
            on_reasoning_token=None,
            on_content_token=None,
            cancel_token=CancelToken(),
            thinking=True,
            reasoning_effort="low",
            stage_label="Stage 1",
            frame=make_frame(),
            free_lane=_free_lane,
            free_lane_state={"tried": False},
        )

    assert out is not None
    # The spare channel answered, so the configured provider was never touched.
    assert order == ["free"]


def test_configured_provider_chain_runs_after_the_spare_channel_fails() -> None:
    from pa_agent.config.settings import AIProviderSettings

    settings = _sensenova_settings()
    settings.provider.fallbacks = [
        AIProviderSettings(
            base_url="https://open.bigmodel.cn/api/paas/v4",
            model="glm-4-flash",
            api_key="sk-GLM-FALLBACK",
        )
    ]
    client = MagicMock()
    client.stream_chat.side_effect = RuntimeError("primary down")
    orch = _orchestrator(settings, client)

    order: list[str] = []
    state = {"tried": False}

    def _free_lane(exc):  # noqa: ANN001
        order.append("free")
        return None

    def _provider_fallback(**kwargs):  # noqa: ANN001
        order.append("provider")
        return False

    with patch.object(orch, "_try_next_provider_fallback", side_effect=_provider_fallback):
        with pytest.raises(Exception):
            orch._stream_chat_resilient(
                [{"role": "user", "content": "hi"}],
                on_reasoning_token=None,
                on_content_token=None,
                cancel_token=CancelToken(),
                thinking=True,
                reasoning_effort="low",
                stage_label="Stage 1",
                frame=make_frame(),
                free_lane=_free_lane,
                free_lane_state=state,
            )

    assert order == ["free", "provider"]
    assert state["tried"] is True


def test_slow_candidate_announces_its_elapsed_wait() -> None:
    import time as _time

    lines: list[str] = []

    def _slow(url, *, headers, body, timeout):  # noqa: ANN001
        _time.sleep(0.3)
        return 200, {
            "choices": [
                {
                    "message": {"content": "答", "reasoning_content": ""},
                    "finish_reason": "stop",
                }
            ]
        }

    c1 = FreeCandidate(
        "kilo", "reflection/beam-free", "https://api.kilo.ai/api/gateway"
    )
    outcome = run_free_stage_fallback(
        messages=[{"role": "user", "content": "hi"}],
        validate=lambda _r: Ok(obj={"gate_result": "wait"}),
        primary_api_key=None,
        candidates=[c1],
        http_post=_slow,
        heartbeat_seconds=0.05,
        on_progress=lines.append,
    )

    assert isinstance(outcome, FreeStageSuccess)
    assert any("已等待" in line for line in lines), lines

    # The heartbeat must stop once the call returns.
    settled = len(lines)
    _time.sleep(0.2)
    assert len(lines) == settled
