"""方舟降级顺序（用户 2026-11-08 指定）：

    备用链路 → 方舟模型池（轮换 + 逐个换）→ GLM 备用

备用链路只在 Ark 主通道启用；其它网关行为不变。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pa_agent.ai.ark_gateway import ARK_BASE_URL, ensure_ark_fallbacks
from pa_agent.ai.deepseek_client import AIReply, AIUsage
from pa_agent.ai.glm_backup import GLM_BASE_URL, GLM_MODEL, ensure_glm_fallback
from pa_agent.config.settings import AIProviderSettings, Settings
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.util.threading import CancelToken


def _reply() -> AIReply:
    return AIReply(content='{"ok": true}', reasoning_content="", raw={},
                   usage=AIUsage(), request_id="r", latency_ms=1.0)


def _build(provider: AIProviderSettings, *, client_side_effect=None):
    settings = Settings()
    settings.provider = provider
    client = MagicMock()
    if client_side_effect:
        client.stream_chat.side_effect = client_side_effect
    else:
        client.stream_chat.return_value = _reply()
    orch = TwoStageOrchestrator(
        client=client, assembler=MagicMock(), router=MagicMock(),
        validator=MagicMock(), pending_writer=MagicMock(), exp_reader=MagicMock(),
        settings=settings,
    )
    return orch, client


def _ark_provider(with_glm: bool = True) -> AIProviderSettings:
    p = AIProviderSettings(model="deepseek-flash", base_url=ARK_BASE_URL, api_key="ark-key",
                           thinking=True, reasoning_effort="low")
    if with_glm:
        p.backup_glm_api_key = "glm-key"
    ensure_ark_fallbacks(p)
    ensure_glm_fallback(p)
    return p


def _call(orch, free_lane=None, state=None):
    return orch._stream_chat_resilient(
        [{"role": "user", "content": "hi"}],
        on_reasoning_token=None,
        on_content_token=None,
        cancel_token=CancelToken(),
        thinking=True,
        reasoning_effort="low",
        stage_label="Stage 1",
        free_lane=free_lane,
        free_lane_state=state if state is not None else {"tried": False},
    )


def test_ark_backup_runs_before_primary():
    """方舟下先用备用链路，主模型不该被调用。"""
    orch, client = _build(_ark_provider())
    seen: list[str] = []

    def free_lane(_exc):
        seen.append("free")
        return (_reply(), None, {"fallback_provider": "kilo"})

    reply = _call(orch, free_lane=free_lane, state={"tried": False})
    assert reply is not None
    assert seen == ["free"]
    assert client.stream_chat.call_count == 0


def test_ark_primary_used_after_backup_fails():
    """备用链路失败后才用轮换出来的主模型。"""
    orch, client = _build(_ark_provider())

    def free_lane(_exc):
        raise RuntimeError("free pool down")

    reply = _call(orch, free_lane=free_lane, state={"tried": False})
    assert reply is not None
    assert client.stream_chat.call_count == 1
    assert orch._provider().base_url == ARK_BASE_URL


def test_ark_pool_exhausted_then_glm():
    """备用链路 + 方舟池全失败后，落到 GLM 备用。"""
    p = _ark_provider()
    orch, client = _build(p, client_side_effect=RuntimeError("HTTP 500 server error"))

    def free_lane(_exc):
        raise RuntimeError("free pool down")

    with pytest.raises(Exception):
        _call(orch, free_lane=free_lane, state={"tried": False})
    # 方舟 12 个候选都试过之后才切 GLM
    assert orch._provider().base_url == GLM_BASE_URL
    assert orch._provider().model == GLM_MODEL


def test_account_overdue_skips_ark_pool_after_free_fails():
    """账号欠费：备用链路已试过且失败 → 不逐个试方舟模型，直接 GLM。"""
    p = _ark_provider()
    orch, client = _build(
        p, client_side_effect=RuntimeError(
            "403 AccountOverdueError: your account has an overdue balance"
        )
    )
    calls: list[str] = []

    def free_lane(_exc):
        calls.append("free")
        raise RuntimeError("free pool down")

    with pytest.raises(Exception):
        _call(orch, free_lane=free_lane, state={"tried": False})
    assert calls == ["free"]                      # 备用链路先试且只一次
    assert client.stream_chat.call_count == 1     # 只发了一次方舟请求
    assert orch._provider().base_url == GLM_BASE_URL


def test_ark_primary_used_when_backup_only_returns_local_fallback():
    """备用链路只给出本地规则兜底时，视为没跑通，改走主模型。"""
    orch, client = _build(_ark_provider())

    def free_lane(_exc):
        local = AIReply(content='{}', reasoning_content="", raw={"source": "local_compatibility"},
                        usage=AIUsage(), request_id="local", latency_ms=0.0)
        return (local, None, {"fallback_provider": "kilo"})

    reply = _call(orch, free_lane=free_lane, state={"tried": False})
    assert reply is not None
    assert client.stream_chat.call_count == 1     # 方舟主模型接管
    assert orch._provider().base_url == ARK_BASE_URL


def test_sensenova_unchanged():
    """SenseNova 仍然是主模型优先，不被方舟规则影响。"""
    from pa_agent.ai.sensenova_fallback import SENSENOVA_CANONICAL_BASE

    p = AIProviderSettings(model="deepseek-v4-flash", base_url=SENSENOVA_CANONICAL_BASE,
                           api_key="sn-key", thinking=True, reasoning_effort="low")
    orch, client = _build(p)
    seen: list[str] = []

    def free_lane(_exc):
        seen.append("free")
        return (_reply(), None, {})

    reply = _call(orch, free_lane=free_lane, state={"tried": False})
    assert reply is not None
    assert seen == []                              # 主模型先成功，备用链路不参与
    assert client.stream_chat.call_count == 1
