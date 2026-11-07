"""火山方舟自动降级链的单元测试。

覆盖用户要求的四条行为：
  1. 模型填 ``deepseek-flash`` 时自动翻译成方舟真实模型 ID；
  2. 方舟一律使用 low 思考强度（不论设置里选了什么）；
  3. 额度用完/未开通时在同网关模型池里逐个换；
  4. 方舟池全部失败后，还能落到 GLM 备用通道。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pa_agent.ai.ark_gateway import (
    ARK_BASE_URL,
    ARK_MODEL_POOL,
    ARK_THINKING_EFFORT,
    ark_fallback_models,
    ensure_ark_fallbacks,
    is_ark_base,
    resolve_ark_model,
)
from pa_agent.ai.deepseek_client import _effective_api_model, _resolve_thinking_params
from pa_agent.ai.glm_backup import GLM_BASE_URL, GLM_MODEL, ensure_glm_fallback, is_glm_backup_eligible
from pa_agent.config.settings import AIProviderSettings, Settings
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator


def _ark(model="deepseek-flash", key="ark-test", fallbacks=None) -> AIProviderSettings:
    return AIProviderSettings(
        model=model, base_url=ARK_BASE_URL, api_key=key,
        thinking=True, reasoning_effort="high", fallbacks=fallbacks or [],
    )


def test_is_ark_base():
    assert is_ark_base(ARK_BASE_URL)
    assert is_ark_base("https://ark.cn-beijing.volces.com/api/v3/")
    assert not is_ark_base("https://token.sensenova.cn/v1")
    assert not is_ark_base("")


def test_alias_maps_to_real_model_id():
    assert resolve_ark_model("deepseek-flash") == "deepseek-v4-1-flash-economy-261017"
    assert resolve_ark_model("DeepSeek-Flash") == "deepseek-v4-1-flash-economy-261017"
    assert resolve_ark_model("doubao-seed-evolving") == "doubao-seed-evolving"


def test_alias_applied_only_on_ark_gateway():
    assert _effective_api_model(_ark("deepseek-flash")) == "deepseek-v4-1-flash-economy-261017"
    native = AIProviderSettings(model="deepseek-flash", base_url="https://api.deepseek.com", api_key="k")
    assert _effective_api_model(native) == "deepseek-flash"


def test_ark_always_uses_low_effort():
    for effort in ("low", "medium", "high", "max"):
        p = _ark()
        p.reasoning_effort = effort
        body, sent_effort = _resolve_thinking_params(p, thinking=None, reasoning_effort=None)
        assert body["thinking"]["type"] == "enabled"
        assert sent_effort == ARK_THINKING_EFFORT == "low"


def test_ark_thinking_off_sends_disabled():
    p = _ark()
    p.thinking = False
    body, effort = _resolve_thinking_params(p, thinking=None, reasoning_effort=None)
    assert body["thinking"]["type"] == "disabled"
    assert effort is None


def test_fallback_pool_excludes_primary_and_keeps_order():
    models = ark_fallback_models("deepseek-flash")
    assert "deepseek-v4-1-flash-economy-261017" not in models  # 别名解析后再排除
    assert models[0] == ARK_MODEL_POOL[0]
    assert len(models) == len(ARK_MODEL_POOL) - 1


def test_ensure_ark_fallbacks_adds_pool_and_is_idempotent():
    p = _ark()
    added = ensure_ark_fallbacks(p)
    assert added == len(ARK_MODEL_POOL) - 1  # primary 是池内模型
    assert all(fb.base_url == ARK_BASE_URL for fb in p.fallbacks)
    assert all(fb.reasoning_effort == "low" for fb in p.fallbacks)
    assert all(fb.api_key == "ark-test" for fb in p.fallbacks)
    assert ensure_ark_fallbacks(p) == 0  # 幂等
    assert len(p.fallbacks) == len(ARK_MODEL_POOL) - 1


def test_ensure_ark_fallbacks_noop_off_ark_or_without_key():
    other = AIProviderSettings(model="m", base_url="https://api.deepseek.com", api_key="k")
    assert ensure_ark_fallbacks(other) == 0
    assert other.fallbacks == []
    assert ensure_ark_fallbacks(_ark(key="")) == 0


def test_glm_backup_eligible_for_ark():
    assert is_glm_backup_eligible(ARK_BASE_URL)
    assert is_glm_backup_eligible("https://token.sensenova.cn/v1")


def test_glm_fallback_attached_to_ark_when_key_present():
    p = _ark()
    p.backup_glm_api_key = "glm-key"
    assert ensure_glm_fallback(p) is True
    assert p.fallbacks[-1].model == GLM_MODEL
    assert p.fallbacks[-1].base_url == GLM_BASE_URL
    assert ensure_glm_fallback(p) is False  # 幂等


def _orch(provider: AIProviderSettings) -> TwoStageOrchestrator:
    settings = Settings()
    settings.provider = provider
    return TwoStageOrchestrator(
        client=MagicMock(), assembler=MagicMock(), router=MagicMock(),
        validator=MagicMock(), pending_writer=MagicMock(), exp_reader=MagicMock(),
        settings=settings,
    )


def test_same_gateway_switch_keeps_strict_validation():
    """方舟内部换模型不应触发 relaxed 校验。"""
    fb = AIProviderSettings(model="glm-5-3-flash-260828", base_url=ARK_BASE_URL, api_key="ark-test")
    o = _orch(_ark())
    assert o._try_next_provider_fallback(
        tried_keys=set(), fallback_chain=[fb], stage_label="Stage 1", exc=RuntimeError("quota")
    ) is True
    assert o._provider().model == "glm-5-3-flash-260828"
    assert o._on_fallback_provider is False  # 同网关，严格校验


def test_cross_gateway_switch_relaxes_validation():
    """落到 GLM 备用通道时才放宽校验。"""
    fb = AIProviderSettings(model=GLM_MODEL, base_url=GLM_BASE_URL, api_key="glm-key")
    o = _orch(_ark())
    assert o._try_next_provider_fallback(
        tried_keys=set(), fallback_chain=[fb], stage_label="Stage 1", exc=RuntimeError("quota")
    ) is True
    assert o._on_fallback_provider is True


def test_full_chain_ark_pool_then_glm():
    """方舟池全部失败后仍能落到 GLM。"""
    p = _ark()
    p.backup_glm_api_key = "glm-key"
    ensure_ark_fallbacks(p)
    ensure_glm_fallback(p)
    chain = list(p.fallbacks)
    assert chain[-1].base_url == GLM_BASE_URL
    o = _orch(p)
    tried: set[tuple[str, str]] = set()
    for _ in range(len(chain)):
        assert o._try_next_provider_fallback(
            tried_keys=tried, fallback_chain=chain, stage_label="S", exc=RuntimeError("quota")
        )
    assert o._provider().base_url == GLM_BASE_URL
    assert o._try_next_provider_fallback(
        tried_keys=tried, fallback_chain=chain, stage_label="S", exc=RuntimeError("quota")
    ) is False
