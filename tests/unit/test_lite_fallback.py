"""Tests for the spare-model layer (sensenova-6.8-flash-lite).

Covers the two measured requirements (user-tailed messages, 16384 shared
budget), the streaming transport, the heartbeat, the eligibility gate, the
fall-through contract, and the wiring into both the two-stage pipeline and the
follow-up chat.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from pa_agent.ai import lite_fallback
from pa_agent.ai.deepseek_client import CancelledError
from pa_agent.config.settings import AIProviderSettings, Settings
from pa_agent.orchestrator.free_chat import FreeChatSession
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.records.schema import AnalysisRecord, RecordMeta
from pa_agent.util.threading import CancelToken

SENSENOVA = "https://token.sensenova.cn/v1"
OTHER = "https://api.deepseek.com/v1"


def _body(*, content: str = '{"decision": {}}', reasoning: str = "思考") -> dict:
    return {
        "id": "req-lite-1",
        "choices": [
            {
                "message": {"content": content, "reasoning": reasoning},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 107640,
            "completion_tokens": 4433,
            "total_tokens": 112073,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

class TestEligibility:
    def test_sensenova_gateway_is_eligible(self):
        assert lite_fallback.is_lite_eligible(SENSENOVA) is True

    def test_other_gateway_is_not_eligible(self):
        assert lite_fallback.is_lite_eligible(OTHER) is False

    def test_none_is_not_eligible(self):
        assert lite_fallback.is_lite_eligible(None) is False


# ---------------------------------------------------------------------------
# Message shaping — the assistant tail makes the gateway answer 200 with nothing
# ---------------------------------------------------------------------------

class TestShapeUserTail:
    def test_assistant_tail_is_folded_into_previous_user(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        out = lite_fallback.shape_user_tail(msgs)

        assert out[-1]["role"] == "user"
        assert "q2" in out[-1]["content"]
        assert "a2" in out[-1]["content"]  # nothing is lost
        assert len(out) == 4
        # The caller's list must not be mutated.
        assert msgs[-1]["role"] == "assistant"

    def test_user_tail_passes_through(self):
        msgs = [{"role": "user", "content": "q"}]
        assert lite_fallback.shape_user_tail(msgs) == msgs

    def test_assistant_only_list_gets_a_user_turn(self):
        out = lite_fallback.shape_user_tail([{"role": "assistant", "content": "a"}])
        assert out[-1]["role"] == "user"
        assert "a" in out[-1]["content"]


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------

class TestTryLiteStage:
    def test_ineligible_gateway_never_calls_http(self):
        calls: list[tuple] = []

        def stream(*args):
            calls.append(args)
            return _body()

        out = lite_fallback.try_lite_stage(
            [{"role": "user", "content": "hi"}],
            api_key="sk-sn",
            base_url=OTHER,
            http_stream=stream,
        )
        assert out is None
        assert calls == []

    def test_missing_key_returns_none(self):
        calls: list[int] = []

        def stream(*args):
            calls.append(1)
            return _body()

        out = lite_fallback.try_lite_stage(
            [{"role": "user", "content": "hi"}],
            api_key="",
            base_url=SENSENOVA,
            http_stream=stream,
        )
        assert out is None
        assert calls == []

    def test_streams_reasoning_and_answer_to_the_callbacks(self):
        captured: dict = {}
        reasoning_chunks: list[str] = []
        content_chunks: list[str] = []

        def stream(url, payload, api_key, timeout_s, on_reasoning, on_content):
            captured.update(url=url, payload=payload, api_key=api_key, timeout=timeout_s)
            on_reasoning("思考")
            on_content('{"decision": {}}')
            return _body()

        out = lite_fallback.try_lite_stage(
            [
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ],
            api_key="sk-sn",
            base_url=SENSENOVA,
            on_reasoning_token=reasoning_chunks.append,
            on_content_token=content_chunks.append,
            http_stream=stream,
        )

        assert out is not None
        assert out.content == '{"decision": {}}'
        assert out.usage.prompt_tokens == 107640
        # Deltas reach the pane while the call runs, not as one blob afterwards.
        assert reasoning_chunks == ["思考"]
        assert content_chunks == ['{"decision": {}}']

        payload = captured["payload"]
        assert payload["model"] == "sensenova-6.8-flash-lite"
        assert payload["max_tokens"] == 16384, "shared thinking/answer budget must be large"
        assert payload["reasoning_effort"] == "low"
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert "thinking" not in payload, "the gateway rejects the string thinking field"
        assert payload["messages"][-1]["role"] == "user"
        assert captured["url"].endswith("/chat/completions")

    def test_heartbeat_reports_the_wait(self):
        progress: list[str] = []

        def slow_stream(url, payload, api_key, timeout_s, on_reasoning, on_content):
            time.sleep(0.12)
            if on_content is not None:
                on_content("ok")
            return _body(content="ok")

        out = lite_fallback.try_lite_stage(
            [{"role": "user", "content": "hi"}],
            api_key="sk-sn",
            base_url=SENSENOVA,
            on_progress=progress.append,
            heartbeat_seconds=0.02,
            http_stream=slow_stream,
        )

        assert out is not None
        assert any("正在正常分析，请稍等。" in line for line in progress), progress
        # Wording stays neutral: no model name, no upstream error, no counters.
        joined = "".join(progress)
        assert "sensenova" not in joined.lower()
        assert "6.8" not in joined
        assert "已等待" not in joined
        assert "[提示]" not in joined

    def test_empty_content_falls_through(self):
        def stream(url, payload, api_key, timeout_s, on_reasoning, on_content):
            return _body(content="")

        out = lite_fallback.try_lite_stage(
            [{"role": "user", "content": "hi"}],
            api_key="sk-sn",
            base_url=SENSENOVA,
            http_stream=stream,
        )
        assert out is None

    def test_http_error_falls_through(self):
        def boom(*args):
            raise RuntimeError("connection reset")

        out = lite_fallback.try_lite_stage(
            [{"role": "user", "content": "hi"}],
            api_key="sk-sn",
            base_url=SENSENOVA,
            http_stream=boom,
        )
        assert out is None

    def test_cancelled_before_call_raises(self):
        token = CancelToken()
        token.set()
        with pytest.raises(CancelledError):
            lite_fallback.try_lite_stage(
                [{"role": "user", "content": "hi"}],
                api_key="sk-sn",
                base_url=SENSENOVA,
                cancel_token=token,
                http_stream=lambda *a: _body(),
            )

    def test_data_lines_are_parsed(self):
        assert lite_fallback._parse_data_line("data: [DONE]") is None
        assert lite_fallback._parse_data_line(": keep-alive") is None
        assert lite_fallback._parse_data_line('data: {"id":"x"}') == {"id": "x"}


# ---------------------------------------------------------------------------
# Wiring — two-stage pipeline
# ---------------------------------------------------------------------------

def _orchestrator(*, base_url: str = SENSENOVA, api_key: str = "sk-sn", exc: BaseException) -> TwoStageOrchestrator:
    provider = AIProviderSettings(model="deepseek-flash", base_url=base_url, api_key=api_key)
    settings = Settings()
    settings.provider = provider
    client = MagicMock()
    client.stream_chat.side_effect = exc
    return TwoStageOrchestrator(
        client=client,
        assembler=MagicMock(),
        router=MagicMock(),
        validator=MagicMock(),
        pending_writer=MagicMock(),
        exp_reader=MagicMock(),
        settings=settings,
    )


class TestOrchestratorWiring:
    def test_rate_limit_tries_the_spare_model_first(self, monkeypatch):
        orch = _orchestrator(exc=RuntimeError("429 rate limit exceeded"))
        spare_reply = MagicMock()
        calls: list[dict] = []

        def fake_try(messages, **kwargs):
            calls.append(kwargs)
            return spare_reply

        monkeypatch.setattr("pa_agent.ai.lite_fallback.try_lite_stage", fake_try)

        progress: list[str] = []
        out = orch._stream_chat_resilient(
            [{"role": "user", "content": "hi"}],
            on_reasoning_token=None,
            on_content_token=None,
            cancel_token=CancelToken(),
            thinking=True,
            reasoning_effort="low",
            stage_label="Stage 1",
            on_progress=progress.append,
        )

        assert out is spare_reply
        assert len(calls) == 1
        assert calls[0]["base_url"] == SENSENOVA
        assert calls[0]["api_key"] == "sk-sn"
        # The stage's content callback is forwarded so progress can be shown.
        assert "on_progress" in calls[0]

    def test_non_rate_limit_error_skips_the_spare_model(self, monkeypatch):
        orch = _orchestrator(exc=RuntimeError("connection refused"))
        calls: list[dict] = []

        def fake_try(messages, **kwargs):  # pragma: no cover - must not run
            calls.append(kwargs)
            return MagicMock()

        monkeypatch.setattr("pa_agent.ai.lite_fallback.try_lite_stage", fake_try)

        with pytest.raises(RuntimeError, match="connection refused"):
            orch._stream_chat_resilient(
                [{"role": "user", "content": "hi"}],
                on_reasoning_token=None,
                on_content_token=None,
                cancel_token=CancelToken(),
                thinking=True,
                reasoning_effort="low",
                stage_label="Stage 1",
            )
        assert calls == []


# ---------------------------------------------------------------------------
# Wiring — follow-up chat
# ---------------------------------------------------------------------------

def _base_record() -> AnalysisRecord:
    meta = RecordMeta(
        timestamp_local_iso="2026-10-30T10:00:00.000",
        timestamp_local_ms=1_761_714_000_000,
        symbol="XAUUSD",
        timeframe="15m",
        bar_count=2,
        ai_provider={"model": "deepseek-flash"},
    )
    return AnalysisRecord(
        meta=meta,
        kline_data=[],
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis=None,
        stage2_messages=[],
        stage2_response={"content": "", "reasoning_content": ""},
        stage2_decision={"decision": {"order_type": "不下单"}},
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )


def _chat_session(exc: BaseException, *, base_url: str = SENSENOVA) -> FreeChatSession:
    provider = AIProviderSettings(model="deepseek-flash", base_url=base_url, api_key="sk-sn")
    settings = Settings()
    settings.provider = provider
    client = MagicMock()
    client.stream_chat.side_effect = exc
    return FreeChatSession(
        base_record=_base_record(),
        client=client,
        assembler=MagicMock(),
        pending_writer=MagicMock(),
        ledger=MagicMock(),
        settings=settings,
    )


class TestFollowUpWiring:
    def test_rate_limit_uses_the_spare_model(self, monkeypatch):
        session = _chat_session(RuntimeError("429 rate limit"))
        spare_reply = MagicMock()
        spare_reply.content = "备用通道的回答"
        spare_reply.reasoning_content = ""

        def fake_try(messages, **kwargs):
            return spare_reply

        monkeypatch.setattr("pa_agent.ai.lite_fallback.try_lite_stage", fake_try)

        reply = session.send("为什么不下单？", CancelToken())

        assert reply is spare_reply
        assert session.spare_channel_used == "spare_model"

    def test_non_rate_limit_error_skips_the_spare_model(self, monkeypatch):
        session = _chat_session(RuntimeError("connection refused"))
        called: list[int] = []

        def fake_try(messages, **kwargs):  # pragma: no cover - must not run
            called.append(1)
            return MagicMock()

        # Keep the free pool offline: this test is about the spare model being
        # skipped, not about which channel eventually answers.
        from pa_agent.ai.sensenova_fallback import FreeStageFailure

        monkeypatch.setattr("pa_agent.ai.lite_fallback.try_lite_stage", fake_try)
        monkeypatch.setattr(
            "pa_agent.ai.sensenova_fallback.run_free_stage_fallback",
            lambda **kwargs: FreeStageFailure(attempts=[], message="all failed"),
        )

        with pytest.raises(RuntimeError, match="connection refused"):
            session.send("q", CancelToken())
        assert called == []
