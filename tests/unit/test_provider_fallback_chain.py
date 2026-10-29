"""Provider fallback chain tests."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pa_agent.config.settings import AIProviderSettings, Settings
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator


def _orch(provider: AIProviderSettings) -> TwoStageOrchestrator:
    settings = Settings()
    settings.provider = provider
    client = MagicMock()
    return TwoStageOrchestrator(
        client=client,
        assembler=MagicMock(),
        router=MagicMock(),
        validator=MagicMock(),
        pending_writer=MagicMock(),
        exp_reader=MagicMock(),
        settings=settings,
    )


def _prov(model, base, key="sk-x", fallbacks=None):
    return AIProviderSettings(
        model=model, base_url=base, api_key=key,
        fallbacks=fallbacks or [],
    )


def test_no_fallbacks_returns_false():
    o = _orch(_prov("m1", "https://a/v1"))
    assert o._try_next_provider_fallback(
        tried_keys=set(), fallback_chain=[], stage_label="Stage 1", exc=RuntimeError("x")
    ) is False


def test_first_fallback_applied_and_settings_swapped():
    fb = _prov("glm-4-flash", "https://open.bigmodel.cn/api/paas/v4", "sk-glm")
    o = _orch(_prov("deepseek-v4-flash", "https://token.sensenova.cn/v1", "sk-sn", [fb]))
    tried = set()
    assert o._try_next_provider_fallback(
        tried_keys=tried, fallback_chain=[fb], stage_label="Stage 1", exc=RuntimeError("429")
    ) is True
    assert o._provider().model == "glm-4-flash"
    assert o._settings.provider.model == "deepseek-v4-flash"  # saved config untouched
    o._client.update_provider.assert_called_once_with(fb)
    assert ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash") in tried


def test_chain_exhausts_after_all_tried():
    fb1 = _prov("m2", "https://b/v1", "sk-2")
    fb2 = _prov("m3", "https://c/v1", "sk-3")
    o = _orch(_prov("m1", "https://a/v1", "sk-1", [fb1, fb2]))
    tried = set()
    assert o._try_next_provider_fallback(tried_keys=tried, fallback_chain=[fb1, fb2], stage_label="S", exc=RuntimeError("x"))
    assert o._try_next_provider_fallback(tried_keys=tried, fallback_chain=[fb1, fb2], stage_label="S", exc=RuntimeError("x"))
    assert o._try_next_provider_fallback(tried_keys=tried, fallback_chain=[fb1, fb2], stage_label="S", exc=RuntimeError("x")) is False


def test_empty_api_key_skipped():
    bad = _prov("m2", "https://b/v1", "")
    good = _prov("m3", "https://c/v1", "sk-3")
    o = _orch(_prov("m1", "https://a/v1", "sk-1", [bad, good]))
    tried = set()
    assert o._try_next_provider_fallback(tried_keys=tried, fallback_chain=[bad, good], stage_label="S", exc=RuntimeError("x"))
    assert o._provider().model == "m3"
    assert o._settings.provider.model == "m1"


def test_duplicate_of_primary_skipped():
    same = _prov("m1", "https://a/v1", "sk-1")
    other = _prov("m2", "https://b/v1", "sk-2")
    o = _orch(_prov("m1", "https://a/v1", "sk-1", [same, other]))
    tried = set()
    assert o._try_next_provider_fallback(tried_keys=tried, fallback_chain=[same, other], stage_label="S", exc=RuntimeError("x"))
    assert o._provider().model == "m2"
    assert o._settings.provider.model == "m1"


def test_settings_json_not_written(monkeypatch):
    """Fallback must be in-memory only: save_settings must never be called."""
    import pa_agent.config.settings as cfg_mod
    called = []
    monkeypatch.setattr(cfg_mod, "save_settings", lambda *a, **k: called.append(1))
    fb = _prov("m2", "https://b/v1", "sk-2")
    o = _orch(_prov("m1", "https://a/v1", "sk-1", [fb]))
    o._try_next_provider_fallback(tried_keys=set(), fallback_chain=[fb], stage_label="S", exc=RuntimeError("x"))
    assert called == []


def test_stream_chat_cancel_does_not_fallback():
    """User cancel must propagate, not trigger provider switch."""
    from pa_agent.ai.deepseek_client import CancelledError
    fb = _prov("m2", "https://b/v1", "sk-2")
    o = _orch(_prov("m1", "https://a/v1", "sk-1", [fb]))
    o._client.stream_chat.side_effect = CancelledError("user cancelled")
    tok = MagicMock()
    tok.is_set.return_value = False
    with pytest.raises(CancelledError):
        o._stream_chat_resilient(
            [{"role": "user", "content": "x"}],
            on_reasoning_token=None, on_content_token=None,
            cancel_token=tok, thinking=False, reasoning_effort="low",
            stage_label="Stage 1",
        )
    assert o._settings.provider.model == "m1"


def test_stream_chat_falls_back_on_429():
    """429 from primary must trigger fallback and retry with the new provider."""
    fb = _prov("m2", "https://b/v1", "sk-2")
    o = _orch(_prov("m1", "https://a/v1", "sk-1", [fb]))
    err = RuntimeError("HTTP 429 rate limit")
    o._client.stream_chat.side_effect = [err, "OK_REPLY"]
    tok = MagicMock()
    tok.is_set.return_value = False
    out = o._stream_chat_resilient(
        [{"role": "user", "content": "x"}],
        on_reasoning_token=None, on_content_token=None,
        cancel_token=tok, thinking=False, reasoning_effort="low",
        stage_label="Stage 1",
    )
    assert out == "OK_REPLY"
    assert o._provider().model == "m2"
    assert o._settings.provider.model == "m1"  # in-run switch never rewrites config
    assert o._client.stream_chat.call_count == 2


def test_stream_chat_raises_when_all_fail():
    fb = _prov("m2", "https://b/v1", "sk-2")
    o = _orch(_prov("m1", "https://a/v1", "sk-1", [fb]))
    err = RuntimeError("HTTP 500")
    o._client.stream_chat.side_effect = [err, err, err]
    tok = MagicMock()
    tok.is_set.return_value = False
    with pytest.raises(RuntimeError):
        o._stream_chat_resilient(
            [{"role": "user", "content": "x"}],
            on_reasoning_token=None, on_content_token=None,
            cancel_token=tok, thinking=False, reasoning_effort="low",
            stage_label="Stage 1",
        )
