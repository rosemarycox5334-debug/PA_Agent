"""火山方舟 (Ark) 网关适配的单元测试。

覆盖三处实测差异（2026-11，真实 API 验证）：
  1. max_tokens 上限远低于默认 384000（豆包 262144），否则 400；
  2. thinking.type 只认 enabled/disabled，模型名含 deepseek 也不能发 adaptive；
  3. 关闭思考时不能注入 temperature=0（Kimi 只接受 1）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pa_agent.config.settings import AIProviderSettings
from pa_agent.ai.deepseek_client import (
    DeepSeekClient,
    _ARK_MAX_OUTPUT_TOKENS,
    _is_ark,
    _resolve_thinking_params,
)

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"


def _settings(model: str, *, thinking: bool, effort: str = "low") -> AIProviderSettings:
    return AIProviderSettings(
        model=model,
        base_url=ARK_BASE,
        api_key="sk-test-ark",
        thinking=thinking,
        reasoning_effort=effort,
    )


def _mock_response() -> MagicMock:
    msg = MagicMock()
    msg.content = '{"ok": true}'
    msg.reasoning_content = "思考中"
    choice = MagicMock()
    choice.message = msg
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15
    usage.prompt_tokens_details = MagicMock()
    usage.prompt_tokens_details.cached_tokens = 0
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    resp.id = "req-ark"
    resp.model = "ark-model"
    return resp


def test_is_ark_recognises_volces_url():
    assert _is_ark(ARK_BASE)
    assert _is_ark("https://ark.volcengine.com/api/v3")
    assert not _is_ark("https://api.deepseek.com")


def test_ark_thinking_enabled_uses_enabled_not_adaptive():
    extra, effort = _resolve_thinking_params(
        _settings("deepseek-v4-1-flash-261017", thinking=True, effort="low"),
        thinking=None,
        reasoning_effort=None,
    )
    # 模型名含 deepseek，但必须按网关注入 enabled，而不是 adaptive
    assert extra["thinking"]["type"] == "enabled"
    assert effort == "low"


def test_ark_effort_always_low():
    """方舟一律使用 low（用户要求：不论设置里选什么）。"""
    extra, effort = _resolve_thinking_params(
        _settings("glm-5-3-flash-260828", thinking=True, effort="max"),
        thinking=None,
        reasoning_effort=None,
    )
    assert extra["thinking"]["type"] == "enabled"
    assert effort == "low"


def test_ark_thinking_disabled():
    extra, effort = _resolve_thinking_params(
        _settings("kimi-k3-1-pro-261024", thinking=False),
        thinking=None,
        reasoning_effort=None,
    )
    assert extra["thinking"]["type"] == "disabled"
    assert effort is None


@pytest.mark.parametrize("model", ["doubao-seed-2-1-lite-260915", "deepseek-v4-1-flash-261017"])
def test_ark_chat_omits_temperature_and_caps_max_tokens(model):
    settings = _settings(model, thinking=False)
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = _mock_response()

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        DeepSeekClient(settings).chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert "temperature" not in kwargs
    assert kwargs["max_tokens"] == _ARK_MAX_OUTPUT_TOKENS
    assert kwargs["extra_body"]["thinking"]["type"] == "disabled"


def test_ark_chat_sends_reasoning_effort_when_thinking_on():
    settings = _settings("doubao-seed-2-1-lite-260915", thinking=True, effort="low")
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = _mock_response()

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        DeepSeekClient(settings).chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["extra_body"]["thinking"]["type"] == "enabled"
    assert "temperature" not in kwargs
