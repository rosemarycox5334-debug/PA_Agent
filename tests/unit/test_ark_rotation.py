"""方舟模型轮换：每次分析换一个，游标持久化，重启继续。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pa_agent.ai.ark_gateway import (
    ARK_BASE_URL,
    ARK_ROTATION_POOL,
    next_rotation_model,
)
from pa_agent.config.settings import AIProviderSettings, Settings
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator


def _orch(provider: AIProviderSettings) -> TwoStageOrchestrator:
    settings = Settings()
    settings.provider = provider
    return TwoStageOrchestrator(
        client=MagicMock(), assembler=MagicMock(), router=MagicMock(),
        validator=MagicMock(), pending_writer=MagicMock(), exp_reader=MagicMock(),
        settings=settings,
    )


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "ark_rotation_state.json"
    monkeypatch.setattr("pa_agent.ai.ark_gateway.rotation_state_path", lambda: path)
    return path


def test_rotation_cycles_in_requested_order(state_file):
    got = [next_rotation_model(state_file) for _ in range(3)]
    assert got == ["kimi-k3-1-pro-261024",
                   "deepseek-v4-1-flash-economy-261017",
                   "doubao-seed-2-1-lite-260915"]


def test_rotation_wraps_around(state_file):
    n = len(ARK_ROTATION_POOL)
    got = [next_rotation_model(state_file) for _ in range(n + 2)]
    assert got[n] == got[0]
    assert got[n + 1] == got[1]


def test_cursor_survives_new_process(state_file):
    first = next_rotation_model(state_file)
    # 新调用方重新读文件即可继续（模拟重启）
    second = next_rotation_model(state_file)
    assert first != second


def test_orchestrator_rotates_each_analysis(state_file):
    """每次分析都换一个候选，但用户在设置里填的模型名保持不变。"""
    provider = AIProviderSettings(model="doubao-seed-2-1-turbo-260628", base_url=ARK_BASE_URL,
                                   api_key="ark-key", thinking=True, reasoning_effort="low")
    orch = _orch(provider)
    seen = []
    for _ in range(3):
        orch._apply_ark_rotation()
        seen.append(orch._provider().model)
        # 配置里的模型名始终是用户填的那个
        assert orch._settings.provider.model == "doubao-seed-2-1-turbo-260628"
    assert seen == ["kimi-k3-1-pro-261024",
                    "deepseek-v4-1-flash-economy-261017",
                    "doubao-seed-2-1-lite-260915"]


def test_rotation_reaches_the_request_client(state_file):
    """轮换必须同步给请求客户端，否则实际发出的仍是配置里的模型名。"""
    provider = AIProviderSettings(model="deepseek-flash", base_url=ARK_BASE_URL,
                                   api_key="ark-key", thinking=True, reasoning_effort="low")
    orch = _orch(provider)
    orch._apply_ark_rotation()
    sent = orch._client.update_provider.call_args[0][0]
    assert sent.model == "kimi-k3-1-pro-261024"
    assert orch._settings.provider.model == "deepseek-flash"


def test_rotation_rebuilds_fallback_chain(state_file):
    provider = AIProviderSettings(model="deepseek-flash", base_url=ARK_BASE_URL,
                                   api_key="ark-key", thinking=True, reasoning_effort="low")
    orch = _orch(provider)
    orch._apply_ark_rotation()
    current = orch._provider().model
    chain = [fb.model for fb in (orch._provider().fallbacks or [])]
    assert current not in chain          # 主模型不在自己的降级链里
    assert chain and all(ARK_BASE_URL == fb.base_url for fb in orch._provider().fallbacks)


def test_non_ark_gateway_is_not_rotated(state_file):
    provider = AIProviderSettings(model="deepseek-v4-flash",
                                   base_url="https://token.sensenova.cn/v1",
                                   api_key="k", thinking=True, reasoning_effort="low")
    orch = _orch(provider)
    orch._apply_ark_rotation()
    assert orch._provider().model == "deepseek-v4-flash"
    assert not state_file.exists()        # 非方舟不推进游标
