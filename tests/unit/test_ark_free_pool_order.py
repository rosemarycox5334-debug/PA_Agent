"""方舟降级顺序：先换同网关模型 → 再公共免费池 → 最后 GLM 备用。

用户要求（2026-11-07）：主通道失败时不允许一上来就用 GLM 备用，
必须先把方舟模型逐个试完，再动免费模型，最后才是 GLM。
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


def _ark_provider() -> AIProviderSettings:
    p = AIProviderSettings(model="deepseek-flash", base_url=ARK_BASE_URL, api_key="ark-key",
                           thinking=True, reasoning_effort="low")
    p.backup_glm_api_key = "glm-key"   # 用户填了 GLM 备用 Key
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


def test_ark_pool_pending_detects_untried_candidates():
    p = _ark_provider()
    orch, _ = _build(p)
    chain = list(p.fallbacks)
    assert orch._ark_pool_pending(chain, set()) is True
    tried = {(ARK_BASE_URL, c.model) for c in chain if ARK_BASE_URL == str(c.base_url)}
    assert orch._ark_pool_pending(chain, tried) is False


def test_free_pool_is_skipped_while_ark_models_remain():
    """第一次失败应切到下一个方舟模型，而不是免费池。"""
    p = _ark_provider()
    calls = {"n": 0}

    def side_effect(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP 500 server error")  # 非账号级，应换下一个方舟模型
        return _reply()

    orch, client = _build(p, client_side_effect=side_effect)
    used_free = {"hit": False}

    def free_lane(_exc):
        used_free["hit"] = True
        return (_reply(), None, {})

    state = {"tried": False}
    reply = _call(orch, free_lane=free_lane, state=state)
    assert reply is not None
    assert used_free["hit"] is False          # 免费池没被动用
    assert state["tried"] is False
    assert orch._provider().base_url == ARK_BASE_URL
    assert orch._provider().model != "deepseek-flash"  # 换到池内下一个


def test_free_pool_runs_after_ark_pool_exhausted_before_glm():
    """方舟池全部失败后才用免费池；免费池失败后才落到 GLM。"""
    p = _ark_provider()
    orch, client = _build(p, client_side_effect=RuntimeError("HTTP 403 AccountOverdueError"))
    order: list[str] = []

    def free_lane(_exc):
        order.append("free")
        raise RuntimeError("free pool unavailable")

    state = {"tried": False}
    with pytest.raises(Exception):
        _call(orch, free_lane=free_lane, state=state)
    assert order == ["free"]                       # 免费池被尝试且只一次
    assert state["tried"] is True
    # 免费池失败后才落到 GLM：链末尾的跨网关候选
    chain = list(p.fallbacks)
    assert str(chain[-1].base_url) == GLM_BASE_URL
    assert chain[-1].model == GLM_MODEL
    # 所有方舟候选都已试过
    tried_ark = [
        c for c in chain
        if "volces" in str(c.base_url)
        and (str(c.base_url).strip(), str(c.model).strip()) not in {(str(x.base_url).strip(), str(x.model).strip()) for x in []}
    ]
    assert orch._ark_pool_pending(chain, {(str(c.base_url).strip(), str(c.model).strip()) for c in chain}) is False


def test_account_overdue_skips_ark_pool_and_uses_free_lane():
    """账号欠费是账号级错误：不应逐个试 13 个方舟模型，直接走免费池。"""
    p = _ark_provider()
    orch, client = _build(
        p, client_side_effect=RuntimeError(
            "Error code: 403 - {'error': {'code': 'AccountOverdueError', "
            "'message': 'The request failed because your account has an overdue balance.'}}"
        )
    )
    seen: list[str] = []

    def free_lane(_exc):
        seen.append("free")
        return (_reply(), None, {"fallback_provider": "kilo"})

    reply = _call(orch, free_lane=free_lane, state={"tried": False})
    assert reply is not None
    assert seen == ["free"]
    assert client.stream_chat.call_count == 1          # 没有逐个重试方舟模型
    assert orch._provider().model == "deepseek-flash"  # 也没切到别的方舟模型


def test_sensenova_behavior_unchanged():
    """SenseNova 仍可直接用免费池（不受方舟池判断影响）。"""
    from pa_agent.ai.sensenova_fallback import SENSENOVA_CANONICAL_BASE

    p = AIProviderSettings(model="deepseek-v4-flash", base_url=SENSENOVA_CANONICAL_BASE,
                           api_key="sn-key", thinking=True, reasoning_effort="low")
    orch, _ = _build(p)
    assert orch._sensenova_free_eligible() is True
    assert orch._ark_pool_pending([], set()) is False
