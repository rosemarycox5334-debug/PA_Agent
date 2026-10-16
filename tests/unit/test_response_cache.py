"""LLM 响应缓存的行为测试。

核心断言：输入完全相同 → 第二次调用**不产生任何网络请求**，且内容与第一次一致；
输入有任何变化 → 缓存失效（指纹变化）。
"""

from __future__ import annotations

import json
import time

import pytest

from pa_agent.ai import response_cache
from pa_agent.ai.deepseek_client import AIUsage, DeepSeekClient
from pa_agent.config.settings import AIProviderSettings

MSGS = [{"role": "user", "content": "诊断一下这 100 根 K 线"}]


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """把缓存目录指到临时目录，避免污染真实缓存。"""
    monkeypatch.setattr(response_cache, "CACHE_DIR", tmp_path / "llm_cache")
    yield


def _key(**overrides):
    kwargs = dict(
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        api_messages=MSGS,
        system_param="你是价格行为分析师",
        thinking=True,
        effort="low",
        max_tokens=384000,
    )
    kwargs.update(overrides)
    return response_cache.make_key(**kwargs)


# ------------------------------------------------------------------ 指纹
def test_same_input_same_key() -> None:
    assert _key() == _key()


@pytest.mark.parametrize(
    "change",
    [
        {"model": "deepseek-v4-pro"},
        {"system_param": "另一套提示词"},
        {"thinking": False},
        {"effort": "high"},
        {"max_tokens": 1000},
        {"api_messages": [{"role": "user", "content": "换了 K 线数据"}]},
    ],
)
def test_any_input_change_changes_key(change) -> None:
    assert _key() != _key(**change)


# ------------------------------------------------------------------ 存取
def test_store_and_load_roundtrip() -> None:
    key = _key()
    assert response_cache.load(key) is None
    response_cache.store(
        key,
        content='{"decision": {"order_type": "不下单"}}',
        reasoning_content="想了一下",
        request_id="req-1",
        prompt_tokens=1234,
        completion_tokens=56,
        model="deepseek-flash",
    )
    got = response_cache.load(key)
    assert got is not None
    assert got.request_id == "req-1"
    assert got.prompt_tokens == 1234 and got.completion_tokens == 56


def test_ttl_expiry_deletes_entry() -> None:
    key = _key()
    response_cache.store(
        key,
        content="{ }",
        reasoning_content="",
        request_id="req-2",
        prompt_tokens=10,
        completion_tokens=1,
        model="m",
    )
    assert response_cache.load(key, ttl_s=60) is not None
    assert response_cache.load(key, ttl_s=0) is not None  # 0 = 永不过期
    # 手工把写入时间改成 2 小时前 → 过期
    path = response_cache.CACHE_DIR / f"{key}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["stored_at"] = time.time() - 7200
    path.write_text(json.dumps(data), encoding="utf-8")
    assert response_cache.load(key, ttl_s=1800) is None
    assert not path.exists()  # 过期即删除


def test_empty_content_is_not_cached() -> None:
    key = _key()
    response_cache.store(
        key, content="   ", reasoning_content="", request_id="r",
        prompt_tokens=1, completion_tokens=0, model="m",
    )
    assert response_cache.load(key) is None


def test_stats_and_clear() -> None:
    response_cache.store(
        _key(), content="{}", reasoning_content="", request_id="r",
        prompt_tokens=1, completion_tokens=1, model="m",
    )
    assert response_cache.stats()["entries"] == 1
    assert response_cache.clear() == 1
    assert response_cache.stats()["entries"] == 0


# ------------------------------------------------------------------ 客户端接线
class _ExplodingOpenAI:
    """任何网络调用都会炸——用来证明缓存命中时**没有**发起请求。"""

    def __init__(self, *a, **kw) -> None:
        raise AssertionError("缓存命中却仍然创建了 OpenAI 客户端（说明没有走缓存）")


def _settings(**overrides) -> AIProviderSettings:
    base = dict(api_key="sk-test", model="deepseek-flash", base_url="https://api.deepseek.com")
    base.update(overrides)
    return AIProviderSettings(**base)  # type: ignore[arg-type]


def _client_key(settings: AIProviderSettings, messages: list[dict], effort: str = "low") -> str:
    """用客户端自己的解析逻辑算出真实缓存键（避免测试里"猜"键）。"""
    import pa_agent.ai.deepseek_client as dc

    api_messages, system_param = dc._prepare_api_messages(settings, messages)
    extra_body, eff = dc._resolve_thinking_params(settings, thinking=None, reasoning_effort=effort)
    thinking_on = dc._thinking_enabled(extra_body, eff)
    max_tokens = dc._completion_max_tokens(settings, extra_body=extra_body, effort=eff)
    return response_cache.make_key(
        base_url=settings.base_url,
        model=dc._effective_api_model(settings),
        api_messages=api_messages,
        system_param=system_param,
        thinking=thinking_on,
        effort=eff,
        max_tokens=max_tokens,
    )


def test_stream_chat_second_call_hits_cache(monkeypatch) -> None:
    """第二次相同调用必须命中缓存：不碰网络，内容一致，usage 计 0。"""
    import pa_agent.ai.deepseek_client as dc

    settings = _settings()
    client = DeepSeekClient(settings=settings)
    key = _client_key(settings, MSGS)
    response_cache.store(
        key,
        content='{"decision": {"order_type": "不下单"}}',
        reasoning_content="推理原文",
        request_id="req-cached",
        prompt_tokens=99_000,
        completion_tokens=1_200,
        model="deepseek-flash",
    )

    # 任何真实网络路径都被掐断
    monkeypatch.setattr(dc, "_OpenAI", _ExplodingOpenAI)

    seen_reasoning: list[str] = []
    seen_content: list[str] = []
    reply = client.stream_chat(
        MSGS,
        on_reasoning_token=seen_reasoning.append,
        on_content_token=seen_content.append,
        reasoning_effort="low",
    )

    assert reply.content == '{"decision": {"order_type": "不下单"}}'
    assert reply.raw.get("from_cache") is True
    assert reply.raw["usage"]["saved_prompt_tokens"] == 99_000
    assert reply.latency_ms == 0.0
    assert reply.usage == AIUsage()  # 没有消耗 token
    assert seen_content == ['{"decision": {"order_type": "不下单"}}']  # 重放给界面
    assert seen_reasoning == ["推理原文"]


def test_stream_chat_cache_can_be_disabled(monkeypatch) -> None:
    """关掉开关后不应命中缓存（会走网络 → 这里用爆炸替身证明它试图联网）。"""
    import pa_agent.ai.deepseek_client as dc

    settings = _settings(response_cache_enabled=False)
    client = DeepSeekClient(settings=settings)
    response_cache.store(
        _client_key(settings, MSGS),
        content="{}",
        reasoning_content="",
        request_id="r",
        prompt_tokens=1,
        completion_tokens=1,
        model="m",
    )
    monkeypatch.setattr(dc, "_OpenAI", _ExplodingOpenAI)
    with pytest.raises(AssertionError, match="创建了 OpenAI 客户端"):
        client.stream_chat(MSGS, reasoning_effort="low")
