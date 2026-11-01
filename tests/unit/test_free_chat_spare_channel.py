"""Tests for FreeChatSession spare-channel routing.

The follow-up ("分析完成后可继续追问") path used to talk to the primary
provider only. These tests pin the layered behaviour it now shares with the
two-stage pipeline:

  Layer 0 — spare channel (free pool), SenseNova primary only.
  Layer 1 — settings.provider.fallbacks, applied in memory only.

Cancellation must never be swallowed by the spare channels.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pa_agent.ai.deepseek_client import CancelledError
from pa_agent.ai.sensenova_fallback import FreeStageFailure, FreeStageSuccess
from pa_agent.config.settings import AIProviderSettings, Settings
from pa_agent.orchestrator.free_chat import FreeChatSession
from pa_agent.records.schema import AnalysisRecord, RecordMeta
from pa_agent.util.threading import CancelToken

SENSENOVA = "https://token.sensenova.cn/v1"
GLM = "https://open.bigmodel.cn/api/paas/v4"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_reply(content: str = "AI response", reasoning: str = "AI reasoning") -> MagicMock:
    reply = MagicMock()
    reply.content = content
    reply.reasoning_content = reasoning
    reply.raw = {}
    reply.usage = MagicMock()
    reply.usage.prompt_tokens = 10
    reply.usage.completion_tokens = 5
    reply.usage.cached_prompt_tokens = 0
    reply.usage.total_tokens = 15
    return reply


def _make_base_record() -> AnalysisRecord:
    meta = RecordMeta(
        timestamp_local_iso="2026-10-29T10:00:00.000",
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
        stage2_response={"content": "stage2 content", "reasoning_content": ""},
        stage2_decision={"decision": {"order_type": "不下单"}},
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )


def _make_settings(
    *,
    base_url: str = SENSENOVA,
    fallbacks: list[AIProviderSettings] | None = None,
) -> Settings:
    return Settings(
        provider=AIProviderSettings(
            model="deepseek-flash",
            base_url=base_url,
            api_key="sk-primary-key",
            fallbacks=fallbacks or [],
            backup_glm_api_key="",
        )
    )


def _make_session(client: MagicMock, settings: Settings | None = None) -> FreeChatSession:
    return FreeChatSession(
        base_record=_make_base_record(),
        client=client,
        assembler=MagicMock(),
        pending_writer=MagicMock(),
        ledger=MagicMock(),
        settings=settings,
    )


def _free_success(reply: MagicMock) -> FreeStageSuccess:
    return FreeStageSuccess(
        reply=reply,
        candidate=SimpleNamespace(provider="kilo", model="lightning"),
        result=None,
        attempts=[],
    )


# ---------------------------------------------------------------------------
# Layer 0 — free pool
# ---------------------------------------------------------------------------

class TestFreePoolLayer:
    def test_primary_failure_answers_from_free_pool(self, monkeypatch):
        """A failing primary is answered by the free pool, not surfaced to the user."""
        client = MagicMock()
        client.stream_chat.side_effect = RuntimeError("primary 429")
        session = _make_session(client, _make_settings())

        pool_reply = _make_reply("备用通道的回答", "备用通道的思考")
        calls: list[dict] = []

        def fake_run(**kwargs):
            calls.append(kwargs)
            return _free_success(pool_reply)

        import pa_agent.ai.sensenova_fallback as sf

        monkeypatch.setattr(sf, "run_free_stage_fallback", fake_run)

        progress: list[str] = []
        cancel = CancelToken()
        reply = session.send("为什么不下单？", cancel, on_progress=progress.append)

        assert reply is pool_reply
        assert session.spare_channel_used == "free"
        assert len(calls) == 1
        # The follow-up prompt is what the spare channel receives.
        assert calls[0]["messages"]
        assert calls[0]["stream"] is True
        # Neutral wording only — a single sentence, no model name, no raw error.
        joined = "".join(progress)
        assert "正在正常分析，请稍等。" in joined
        assert "429" not in joined
        assert "lightning" not in joined

    def test_free_pool_failure_falls_through_to_provider_chain(self, monkeypatch):
        """When the pool rotates out, the configured fallback provider answers."""
        client = MagicMock()
        glm_provider = AIProviderSettings(
            model="glm-4-flash",
            base_url=GLM,
            api_key="glm-key",
            thinking=False,
        )
        fallback_reply = _make_reply("GLM 的回答")
        client.stream_chat.side_effect = [RuntimeError("primary down"), fallback_reply]
        session = _make_session(
            client, _make_settings(fallbacks=[glm_provider])
        )

        import pa_agent.ai.sensenova_fallback as sf

        monkeypatch.setattr(
            sf,
            "run_free_stage_fallback",
            lambda **kwargs: FreeStageFailure(attempts=[], message="all failed"),
        )

        reply = session.send("再来一次", CancelToken())

        assert reply is fallback_reply
        assert session.spare_channel_used == "provider"
        client.update_provider.assert_called_once()
        assert client.update_provider.call_args[0][0] is glm_provider

    def test_non_sensenova_primary_skips_free_pool(self, monkeypatch):
        """A non-SenseNova primary never touches the free pool."""
        client = MagicMock()
        glm_provider = AIProviderSettings(
            model="glm-4-flash", base_url=GLM, api_key="glm-key", thinking=False
        )
        client.stream_chat.side_effect = [RuntimeError("boom"), _make_reply("ok")]
        session = _make_session(
            client,
            _make_settings(base_url="https://api.deepseek.com", fallbacks=[glm_provider]),
        )

        import pa_agent.ai.sensenova_fallback as sf

        called: list[int] = []

        def fake_run(**kwargs):  # pragma: no cover - must never run here
            called.append(1)
            return _free_success(_make_reply("pool"))

        monkeypatch.setattr(sf, "run_free_stage_fallback", fake_run)

        reply = session.send("hello", CancelToken())

        assert reply.content == "ok"
        assert called == []
        assert session.spare_channel_used == "provider"


# ---------------------------------------------------------------------------
# Failure and cancellation boundaries
# ---------------------------------------------------------------------------

class TestBoundaries:
    def test_all_channels_failing_raises_primary_error(self, monkeypatch):
        client = MagicMock()
        client.stream_chat.side_effect = RuntimeError("primary exploded")
        session = _make_session(client, _make_settings())

        import pa_agent.ai.sensenova_fallback as sf

        monkeypatch.setattr(
            sf,
            "run_free_stage_fallback",
            lambda **kwargs: FreeStageFailure(attempts=[], message="all failed"),
        )

        with pytest.raises(RuntimeError, match="primary exploded"):
            session.send("q", CancelToken())
        assert session.spare_channel_used == ""

    def test_cancel_is_never_handled_by_spare_channels(self, monkeypatch):
        client = MagicMock()
        client.stream_chat.side_effect = CancelledError("user cancelled")
        session = _make_session(client, _make_settings())

        import pa_agent.ai.sensenova_fallback as sf

        called: list[int] = []

        def fake_run(**kwargs):  # pragma: no cover - must never run here
            called.append(1)
            return _free_success(_make_reply("pool"))

        monkeypatch.setattr(sf, "run_free_stage_fallback", fake_run)

        with pytest.raises(CancelledError):
            session.send("q", CancelToken())
        assert called == []

    def test_primary_success_never_touches_spare_channels(self, monkeypatch):
        client = MagicMock()
        client.stream_chat.return_value = _make_reply("primary answer")
        session = _make_session(client, _make_settings())

        import pa_agent.ai.sensenova_fallback as sf

        called: list[int] = []

        def fake_run(**kwargs):  # pragma: no cover - must never run here
            called.append(1)
            return _free_success(_make_reply("pool"))

        monkeypatch.setattr(sf, "run_free_stage_fallback", fake_run)

        progress: list[str] = []
        reply = session.send("q", CancelToken(), on_progress=progress.append)

        assert reply.content == "primary answer"
        assert called == []
        assert progress == []
        assert session.spare_channel_used == ""
