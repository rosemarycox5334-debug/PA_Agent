"""Unit tests for DeepSeekClient (task 6.5)."""
from __future__ import annotations

import sys
import pytest
from unittest.mock import MagicMock, patch, call
from pa_agent.config.settings import AIProviderSettings
from pa_agent.ai.deepseek_client import (
    DeepSeekClient,
    AIReply,
    AIUsage,
    CancelledError,
    _completion_max_tokens,
    _is_deepseek_model,
    _openclaw_agent_request_extra,
)


def _make_settings(api_key: str = "sk-test-1234abcd") -> AIProviderSettings:
    s = AIProviderSettings()
    s.api_key = api_key
    return s


def _make_mock_response(content: str = "hello", reasoning: str = "thinking...") -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.reasoning_content = reasoning
    choice = MagicMock()
    choice.message = msg
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    usage.total_tokens = 150
    usage.prompt_tokens_details = MagicMock()
    usage.prompt_tokens_details.cached_tokens = 20
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    resp.id = "req-abc123"
    resp.model = "deepseek-v4-pro"
    return resp


def test_chat_does_not_send_forbidden_params():
    """chat() must never pass temperature/top_p/presence_penalty/frequency_penalty."""
    settings = _make_settings()
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        reply = client.chat([{"role": "user", "content": "hi"}])

    call_kwargs = mock_openai.return_value.chat.completions.create.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
    all_kwargs = {**(call_kwargs.args[0] if call_kwargs.args else {}), **kwargs}

    for forbidden in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        assert forbidden not in all_kwargs, f"Forbidden param '{forbidden}' was sent to API"


def test_chat_extra_body_thinking_enabled():
    """DeepSeek v4+ uses thinking.type=adaptive + output_config.effort."""
    settings = _make_settings()
    settings.base_url = "https://api.deepseek.com"
    settings.model = "deepseek-v4-pro"
    settings.thinking = True
    settings.reasoning_effort = "max"
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    call_kwargs = mock_openai.return_value.chat.completions.create.call_args
    kwargs = call_kwargs.kwargs
    assert kwargs["extra_body"]["thinking"]["type"] == "adaptive"
    assert kwargs["extra_body"]["output_config"]["effort"] == "max"
    assert kwargs["reasoning_effort"] == "max"


def test_completion_max_tokens_deepseek_cap():
    settings = _make_settings()
    settings.base_url = "https://api.deepseek.com"
    settings.model = "deepseek-v4-pro"
    assert _completion_max_tokens(settings, extra_body={}, effort="max") == 393_216


def test_completion_max_tokens_deepseek_proxy_cap():
    settings = _make_settings()
    settings.base_url = "http://distributor.example/v1"
    settings.model = "deepseek-v4-pro"
    assert _completion_max_tokens(settings, extra_body={}, effort="max") == 393_216


def test_completion_max_tokens_packy_claude_cap():
    settings = _make_settings()
    settings.base_url = "https://www.packyapi.com/v1"
    extra_body = {"thinking": {"type": "enabled", "budget_tokens": 127_999}}
    assert _completion_max_tokens(settings, extra_body=extra_body, effort="max") == 128_000


def test_packy_hoists_system_message_to_extra_body():
    from pa_agent.ai.deepseek_client import _prepare_chat_messages

    settings = _make_settings()
    settings.base_url = "https://www.packyapi.com/v1"
    settings.model = "claude-sonnet-4-6"
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]
    api_msgs, system = _prepare_chat_messages(settings, msgs)
    assert system == "SYS"
    assert api_msgs == [{"role": "user", "content": "USR"}]


def test_packy_claude_thinking_uses_budget_not_reasoning_effort():
    settings = _make_settings()
    settings.base_url = "https://www.packyapi.com/v1"
    settings.model = "claude-sonnet-4-6"
    settings.thinking = True
    from pa_agent.ai.deepseek_client import _resolve_thinking_params

    extra, effort = _resolve_thinking_params(settings, thinking=True, reasoning_effort="max")
    assert effort is None
    assert extra["thinking"]["type"] == "enabled"
    assert extra["thinking"]["budget_tokens"] == 127_999


def test_chat_sends_max_tokens_when_thinking():
    settings = _make_settings()
    settings.base_url = "https://api.deepseek.com"
    settings.model = "deepseek-v4-pro"
    settings.thinking = True
    settings.reasoning_effort = "medium"
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["max_tokens"] == 393_216


def test_chat_kkai_sends_thinking_object_not_reasoning_effort():
    """KKAI Claude: thinking budget in extra_body; reasoning_effort rejected upstream."""
    settings = _make_settings()
    settings.base_url = "https://api.kkone.vip/v1"
    settings.thinking = True
    settings.reasoning_effort = "high"
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"]["thinking"] == {"type": "enabled", "budget_tokens": 999_998}
    assert "reasoning_effort" not in kwargs


def test_chat_kkai_thinking_off_sends_no_thinking_params():
    settings = _make_settings()
    settings.base_url = "https://api.kkone.vip/v1"
    settings.thinking = False
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert "reasoning_effort" not in kwargs
    assert "extra_body" not in kwargs


def test_chat_yunwu_opus_47_sends_adaptive_thinking():
    settings = _make_settings()
    settings.base_url = "https://yunwu.ai/v1"
    settings.model = "claude-opus-4-7"
    settings.thinking = True
    settings.reasoning_effort = "high"
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"]["thinking"] == {"type": "adaptive"}
    assert kwargs["extra_body"]["output_config"] == {"effort": "high"}
    assert kwargs["reasoning_effort"] == "high"


def test_chat_yunwu_thinking_off_sends_nothing():
    settings = _make_settings()
    settings.base_url = "https://yunwu.ai/v1"
    settings.model = "claude-opus-4-7"
    settings.thinking = False
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert "extra_body" not in kwargs
    assert "reasoning_effort" not in kwargs


def test_stream_kkai_passes_thinking_extra_body():
    settings = _make_settings()
    settings.base_url = "https://api.kkone.vip/v1"
    settings.thinking = True
    settings.reasoning_effort = "medium"
    client = DeepSeekClient(settings)

    chunk_reason = MagicMock()
    chunk_reason.choices = [MagicMock()]
    delta = MagicMock()
    delta.reasoning_content = "think"
    delta.content = None
    chunk_reason.choices[0].delta = delta
    chunk_reason.usage = None
    chunk_reason.id = "id-1"
    chunk_reason.model = "claude-opus-4-5"

    chunk_done = MagicMock()
    chunk_done.choices = []
    chunk_done.usage = MagicMock(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        prompt_tokens_details=MagicMock(cached_tokens=0),
    )

    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = iter(
        [chunk_reason, chunk_done]
    )

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        reply = client.stream_chat(
            [{"role": "user", "content": "hi"}],
            on_reasoning_token=lambda c: None,
        )

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"]["thinking"]["budget_tokens"] == 999_998
    assert "reasoning_effort" not in kwargs
    assert reply.reasoning_content == "think"


def test_chat_cancel_token_raises():
    """If cancel_token is set, chat() raises CancelledError before calling API."""
    from pa_agent.util.threading import CancelToken
    settings = _make_settings()
    client = DeepSeekClient(settings)

    token = CancelToken()
    token.set()

    mock_openai = MagicMock()
    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        with pytest.raises(CancelledError):
            client.chat([{"role": "user", "content": "hi"}], cancel_token=token)

    # API must NOT have been called
    mock_openai.return_value.chat.completions.create.assert_not_called()


def test_chat_no_plaintext_key_in_logs(caplog):
    """API key must not appear in log output."""
    import logging
    settings = _make_settings(api_key="sk-super-secret-9999")
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with caplog.at_level(logging.DEBUG, logger="pa_agent.ai.deepseek_client"):
        with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
            client.chat([{"role": "user", "content": "hi"}])

    for record in caplog.records:
        assert "sk-super-secret-9999" not in record.getMessage(), (
            f"Plaintext API key found in log: {record.getMessage()}"
        )


def test_chat_returns_aireply_fields():
    """chat() returns an AIReply with all expected fields populated."""
    settings = _make_settings()
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response(content="answer", reasoning="thought")
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        reply = client.chat([{"role": "user", "content": "hi"}])

    assert isinstance(reply, AIReply)
    assert reply.content == "answer"
    assert reply.reasoning_content == "thought"
    assert reply.usage.prompt_tokens == 100
    assert reply.usage.completion_tokens == 50
    assert reply.request_id == "req-abc123"
    assert reply.latency_ms >= 0


def test_openclaw_is_not_treated_as_deepseek_model() -> None:
    assert _is_deepseek_model("openclaw") is False
    assert _is_deepseek_model("deepseek-v4-pro") is True


def test_openclaw_agent_request_includes_tool_choice_none() -> None:
    settings = _make_settings()
    settings.model = "openclaw"
    settings.base_url = "http://127.0.0.1:58579/v1"
    with patch("pa_agent.ai.qclaw_connector.detect_qclaw", return_value=True):
        assert _openclaw_agent_request_extra(settings) == {"tool_choice": "none"}


def test_stream_chat_passes_tool_choice_none_for_openclaw() -> None:
    settings = _make_settings()
    settings.model = "openclaw"
    settings.base_url = "http://127.0.0.1:58579/v1"
    settings.thinking = False
    client = DeepSeekClient(settings)

    mock_openai = MagicMock()
    mock_stream = iter([])

    def _create(**kwargs):
        mock_openai.last_kwargs = kwargs
        return mock_stream

    mock_openai.return_value.chat.completions.create.side_effect = _create

    with patch("pa_agent.ai.qclaw_connector.detect_qclaw", return_value=True):
        with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
            try:
                client.stream_chat([{"role": "user", "content": "hi"}])
            except Exception:
                pass

    extra = mock_openai.last_kwargs.get("extra_body") or {}
    assert extra.get("tool_choice") == "none"


def test_mimo_chat_sends_enable_thinking_extra_body() -> None:
    settings = _make_settings()
    settings.base_url = "https://api.xiaomimimo.com/v1"
    settings.model = "mimo-v2-flash"
    settings.thinking = True
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert kwargs["max_tokens"] == 65_536


def test_mimo_chat_patches_tool_call_messages_before_send() -> None:
    settings = _make_settings()
    settings.base_url = "https://api.xiaomimimo.com/v1"
    settings.model = "mimo-v2.5-pro"
    settings.thinking = False
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        },
    ]

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat(messages)

    sent_messages = mock_openai.return_value.chat.completions.create.call_args.kwargs["messages"]
    assert sent_messages[1]["reasoning_content"] == ""


def test_provider_max_output_tokens_override_takes_priority():
    """用户手动设置的 max_output_tokens 优先于模型/网关自动判断。"""
    from pa_agent.ai.deepseek_client import _provider_max_output_tokens

    settings = _make_settings()
    settings.base_url = "https://api.deepseek.com"
    settings.model = "deepseek-v4-pro"  # 自动分支本应取 393216
    settings.max_output_tokens = 131072
    assert _provider_max_output_tokens(settings) == 131072


def test_provider_max_output_tokens_auto_zero_falls_through():
    """max_output_tokens=0 时不覆盖,沿用自动分支(deepseek 仍 393216)。"""
    from pa_agent.ai.deepseek_client import _provider_max_output_tokens

    settings = _make_settings()
    settings.base_url = "https://api.deepseek.com"
    settings.model = "deepseek-v4-pro"
    settings.max_output_tokens = 0
    assert _provider_max_output_tokens(settings) == 393_216


def test_provider_max_output_tokens_auto_unknown_model_is_safe_default():
    """未识别模型(glm 等)走兜底,返回 131072 而非激进的 524288,避免网关 400。

    复现用户场景:私有中转网关(http://...:3000/v1)+ glm-5.2,网关上限 131072。
    """
    from pa_agent.ai.deepseek_client import _provider_max_output_tokens

    settings = _make_settings()
    settings.base_url = "http://198.200.42.42:3000/v1"
    settings.model = "glm-5.2"
    settings.max_output_tokens = 0  # 自动
    assert _provider_max_output_tokens(settings) == 131072
