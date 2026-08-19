"""DeepSeek AI client (OpenAI-compatible API)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from pa_agent.util.threading import CancelToken

from pa_agent.config.settings import AIProviderSettings
from pa_agent.util.mask_secret import mask_secret
from pa_agent.ai.mimo_compat import (
    ReasoningCache,
    is_mimo_provider,
    mimo_max_output_tokens,
    patch_messages_for_mimo,
    resolve_mimo_thinking_extra_body,
    response_message_dict,
    store_reasoning_from_response,
)

try:
    from openai import OpenAI as _OpenAI  # type: ignore[import]
except ImportError as _exc:
    _OpenAI = None  # type: ignore[assignment,misc]
    _OPENAI_IMPORT_ERROR = _exc
else:
    _OPENAI_IMPORT_ERROR = None

logger = logging.getLogger(__name__)

_MIMO_REASONING_CACHE = ReasoningCache()


@dataclass
class AIUsage:
    """Token usage from a single API call."""
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of prompt tokens served from KV cache (0.0–1.0).

        DeepSeek 硬盘缓存命中率。值越高，费用越低。
        0.0 = 无缓存命中；1.0 = 全部命中缓存。
        """
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_prompt_tokens / self.prompt_tokens

    @property
    def cache_miss_tokens(self) -> int:
        """Prompt tokens that were NOT served from cache (billed at full rate)."""
        return max(0, self.prompt_tokens - self.cached_prompt_tokens)


@dataclass
class AIReply:
    """Structured response from a single AI API call."""
    content: str
    reasoning_content: str
    raw: dict[str, Any]          # full raw response dict for debug tab
    usage: AIUsage
    request_id: str
    latency_ms: float


class CancelledError(Exception):
    """Raised when a cancel_token is set before or during an API call."""


def _is_deepseek_native(base_url: str) -> bool:
    return "deepseek.com" in (base_url or "").lower()


def _is_deepseek_model(model: str) -> bool:
    """True for DeepSeek model ids; excludes QClaw/WorkBuddy/Cursor/TRAE/Qoder Agent aliases."""
    m = (model or "").lower()
    if m in ("openclaw", "openclaw_wb", "openclaw_cs", "openclaw_twc", "openclaw_qc"):
        return False
    if m.startswith("openclaw/") or m.startswith("openclaw_wb/") or m.startswith("openclaw_cs/") or m.startswith("openclaw_twc/") or m.startswith("openclaw_qc/"):
        return False
    return "deepseek" in m


def _is_qclaw_openclaw_agent(settings: AIProviderSettings) -> bool:
    """True when requests go through QClaw's public-gateway OpenClaw Agent."""
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import detect_qclaw, is_openclaw_model

    if not detect_qclaw():
        return False
    model = settings.model or ""
    return is_openclaw_model(model) or is_openclaw_cs_model(model)


def _openclaw_agent_request_extra(settings: AIProviderSettings) -> dict[str, Any]:
    """Ask QClaw/WorkBuddy Agent to answer in-chat only (no exec/write tool loop)."""
    if _is_qclaw_openclaw_agent(settings) or _is_workbuddy_agent(settings):
        return {"tool_choice": "none"}
    return {}


def _is_workbuddy_agent(settings: AIProviderSettings) -> bool:
    """True when requests go through WorkBuddy's model route."""
    from pa_agent.ai.workbuddy_connector import is_workbuddy_route

    return is_workbuddy_route(settings)


def _is_openclaw_agent_model(model: str) -> bool:
    """True for QClaw/WorkBuddy/Cursor/TRAE/Qoder OpenClaw Agent model aliases."""
    m = (model or "").lower()
    return (
        m in ("openclaw", "openclaw_wb", "openclaw_cs", "openclaw_twc", "openclaw_qc")
        or m.startswith("openclaw/")
        or m.startswith("openclaw_wb/")
        or m.startswith("openclaw_cs/")
        or m.startswith("openclaw_twc/")
        or m.startswith("openclaw_qc/")
    )


def supports_kv_prefix_chain(settings: AIProviderSettings | None) -> bool:
    """Whether Stage 2 may chain after Stage 1 messages for DeepSeek KV prefix cache.

    OpenClaw Agent routes misread ``system + stage1_user + stage2_user`` as a
    finished chat and reply with prose menus; those providers stay standalone.
    """
    if settings is None:
        return True
    if _is_qclaw_openclaw_agent(settings) or _is_workbuddy_agent(settings):
        return False
    if _is_openclaw_agent_model(settings.model):
        return False
    return _is_deepseek_native(settings.base_url) or _is_deepseek_model(settings.model)


def _extract_cached_prompt_tokens(usage: Any) -> int:
    """Read KV-cache hit count from provider usage (DeepSeek or OpenAI-compat)."""
    if usage is None:
        return 0
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is not None:
        return int(hit or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0)
        if cached:
            return int(cached)
    return 0


def _effective_api_model(settings: AIProviderSettings) -> str:
    """Model id sent to the upstream API (resolve provider aliases)."""
    if _is_workbuddy_agent(settings):
        from pa_agent.ai.workbuddy_connector import resolve_workbuddy_api_model

        return resolve_workbuddy_api_model(settings.model)
    return settings.model


def _workbuddy_agent_request_extra(settings: AIProviderSettings) -> dict[str, Any]:
    """Add WorkBuddy-specific request parameters.

    Returns empty dict if not using WorkBuddy agent route.
    WorkBuddy uses the same tool_choice: none strategy as QClaw.
    """
    return _openclaw_agent_request_extra(settings)


def _is_kkai_openai_proxy(base_url: str) -> bool:
    """KKAI (api.kkone.vip) OpenAI-compatible gateway."""
    url = (base_url or "").lower()
    return "kkone.vip" in url


def _is_packyapi(base_url: str) -> bool:
    return "packyapi.com" in (base_url or "").lower()


def _is_minimax(base_url: str) -> bool:
    """MiniMax (api.minimax.io) OpenAI-compatible gateway."""
    url = (base_url or "").lower()
    return "minimax.io" in url or "minimax.com" in url


def _is_sensenova(base_url: str) -> bool:
    """SenseNova (token.sensenova.cn) OpenAI-compatible gateway.

    Provides deepseek-v4-flash 等 DeepSeek 模型的免费代理；其 max_tokens 上限为
    384000（低于默认 _PRACTICAL_UNLIMITED_MAX_TOKENS），需单独限流以避免 400。
    """
    return "sensenova.cn" in (base_url or "").lower()


# Packy claude-officially returns 400 if max_tokens exceeds model output cap.
_PACKY_CLAUDE_MAX_OUTPUT_TOKENS = 128_000
# DeepSeek API: max_tokens must be in [1, 393216].
_DEEPSEEK_MAX_OUTPUT_TOKENS = 393_216
# SenseNova API: max_tokens is model-specific (per /v1/models max_output_length).
# glm-5.2: [1, 131072]; deepseek-v4-flash / sensenova-*-flash-lite: [1, 65536].
_SENSENOVA_GLM_MAX_OUTPUT_TOKENS = 131_072
_SENSENOVA_DEFAULT_MAX_OUTPUT_TOKENS = 65_536


def _model_uses_claude_adaptive(model: str) -> bool:
    """Claude models that require thinking.type=adaptive (not budget_tokens)."""
    m = (model or "").lower()
    return any(
        token in m
        for token in (
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
        )
    )


_EFFORT_TO_ADAPTIVE_OUTPUT: dict[str, str] = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
    "xhigh": "max",
}


def _adaptive_output_effort(reasoning_effort: str | None) -> str:
    key = (reasoning_effort or "medium").strip().lower()
    return _EFFORT_TO_ADAPTIVE_OUTPUT.get(key, "medium")


# 通用 OpenAI 兼容网关的安全上限;部分网关(如 one-api/new-api)不自动钳制而是直接 400。
_PRACTICAL_UNLIMITED_MAX_TOKENS = 131072
# Anthropic-style thinking requires budget_tokens < max_tokens.
_PRACTICAL_UNLIMITED_THINKING_BUDGET = 131071


def _effort_budget_tokens(effort: str | None, *, max_output: int) -> int:
    """Thinking budget; must stay below max_output (Anthropic/Packy rule)."""
    del effort  # reserved for future per-effort tuning
    return min(_PRACTICAL_UNLIMITED_THINKING_BUDGET, max(1024, max_output - 1))


def _thinking_enabled(extra_body: dict[str, Any], effort: str | None) -> bool:
    if extra_body:
        if extra_body.get("chat_template_kwargs", {}).get("enable_thinking"):
            return True
        return extra_body.get("thinking", {}).get("type") in ("enabled", "adaptive")
    return effort is not None and effort != "none"


def _packy_anthropic_messages_api(settings: AIProviderSettings) -> bool:
    """Packy claude-officially uses Anthropic Messages API (no role=system in messages)."""
    return _is_packyapi(settings.base_url) and "claude" in (settings.model or "").lower()


def _is_mimo(settings: AIProviderSettings) -> bool:
    return is_mimo_provider(settings.base_url, settings.model)


def _prepare_chat_messages(
    settings: AIProviderSettings,
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Hoist system turns to top-level ``system`` for Anthropic-native Packy routes."""
    if not _packy_anthropic_messages_api(settings):
        return messages, None
    system_parts: list[str] = []
    api_messages: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            text = msg.get("content", "")
            if isinstance(text, str) and text.strip():
                system_parts.append(text)
            continue
        api_messages.append(msg)
    system_param = "\n\n".join(system_parts) if system_parts else None
    return api_messages, system_param


def _prepare_api_messages(
    settings: AIProviderSettings,
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Normalize messages for the active provider before API submission."""
    api_messages, system_param = _prepare_chat_messages(settings, messages)
    if _is_mimo(settings):
        api_messages = patch_messages_for_mimo(
            api_messages,
            model=settings.model,
            reasoning_cache=_MIMO_REASONING_CACHE,
        )
    return api_messages, system_param


def _provider_max_output_tokens(settings: AIProviderSettings) -> int:
    """Per-gateway completion cap (max_tokens); avoids 400 from provider limits."""
    override = int(getattr(settings, "max_output_tokens", 0) or 0)
    if override > 0:
        return override  # 用户手动指定,优先于自动判断
    model = (settings.model or "").lower()
    if _is_packyapi(settings.base_url) and "claude" in model:
        return _PACKY_CLAUDE_MAX_OUTPUT_TOKENS
    # DeepSeek output limits apply to the model even when it is served through
    # an OpenAI-compatible distributor/proxy rather than api.deepseek.com.
    if _is_deepseek_native(settings.base_url) or _is_deepseek_model(model):
        return _DEEPSEEK_MAX_OUTPUT_TOKENS
    if _is_sensenova(settings.base_url):
        _smodel = (settings.model or "").lower()
        if "glm" in _smodel:
            return _SENSENOVA_GLM_MAX_OUTPUT_TOKENS
        return _SENSENOVA_DEFAULT_MAX_OUTPUT_TOKENS
    if _is_mimo(settings):
        return mimo_max_output_tokens(settings.model)
    return _PRACTICAL_UNLIMITED_MAX_TOKENS


def _completion_max_tokens(
    settings: AIProviderSettings,
    *,
    extra_body: dict[str, Any],
    effort: str | None,
) -> int:
    """Total completion budget (thinking + content) for OpenAI-compatible APIs."""
    del effort, extra_body
    return _provider_max_output_tokens(settings)


def _resolve_thinking_params(
    settings: AIProviderSettings,
    *,
    thinking: bool | None,
    reasoning_effort: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Return (extra_body, reasoning_effort) for chat.completions.create."""
    _thinking = thinking if thinking is not None else settings.thinking
    _effort = reasoning_effort if reasoning_effort is not None else settings.reasoning_effort
    model = settings.model or ""

    if _is_deepseek_native(settings.base_url) or _is_deepseek_model(model):
        # DeepSeek v4+ requires thinking.type=adaptive + output_config.effort;
        # the old "enabled"/"disabled" values are no longer accepted.
        # Also covers DeepSeek models proxied through non-native gateways (e.g. QClaw).
        if _thinking:
            extra_body: dict[str, Any] = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": _adaptive_output_effort(_effort)},
            }
            return extra_body, _effort or "medium"
        else:
            extra_body = {
                "thinking": {"type": "disabled"},
            }
            return extra_body, None

    if _is_minimax(settings.base_url):
        # MiniMax (api.minimax.io):
        # - thinking.type only accepts "adaptive" (on) or "disabled" (off); no budget_tokens
        # - reasoning_split=True exposes thinking via reasoning_content / reasoning_details
        # - M2.x cannot disable thinking; "disabled" is accepted but ignored
        if _thinking:
            extra_body = {
                "thinking": {"type": "adaptive"},
                "reasoning_split": True,
            }
        else:
            extra_body = {
                "thinking": {"type": "disabled"},
                "reasoning_split": True,
            }
        # MiniMax does not use reasoning_effort
        return extra_body, None

    if _is_mimo(settings):
        # MiMo: DeepSeek-style reasoning via chat_template_kwargs.enable_thinking
        return resolve_mimo_thinking_extra_body(thinking=_thinking), (
            _effort or "medium" if _thinking else None
        )

    if not _thinking:
        return {}, None

    max_out = _completion_max_tokens(
        settings, extra_body={}, effort=_effort
    )

    if _is_packyapi(settings.base_url) and "claude" in model.lower():
        # Packy (e.g. claude-officially): budget_tokens only; reasoning_effort rejected.
        budget = _effort_budget_tokens(_effort, max_output=max_out)
        return (
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
            None,
        )

    if _is_kkai_openai_proxy(settings.base_url):
        # KKAI claude-opus-4-5: reasoning_effort -> 503 paprika_mode on some routes.
        budget = _effort_budget_tokens(_effort, max_output=max_out)
        return (
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
            None,
        )

    if _model_uses_claude_adaptive(model):
        # Yunwu / New-API style gateways: Opus 4.7+ needs adaptive thinking.
        return (
            {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": _adaptive_output_effort(_effort)},
            },
            _effort or "medium",
        )

    if "claude" in model.lower():
        budget = _effort_budget_tokens(_effort, max_output=max_out)
        return (
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
            _effort or "medium",
        )

    # Other models on OpenAI-compatible proxies (o-series, deepseek-reasoner, etc.)
    return {}, _effort or "medium"


class DeepSeekClient:
    """Thin wrapper around the OpenAI-compatible DeepSeek API."""

    def __init__(self, settings: AIProviderSettings, logger_: logging.Logger | None = None) -> None:
        self._settings = settings
        self._log = logger_ or logger

    def update_provider(self, settings: AIProviderSettings) -> None:
        """Replace in-memory provider settings (e.g. after QClaw auto-fallback)."""
        self._settings = settings

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        context_window: int | None = None,
        cancel_token: "CancelToken | None" = None,
        timeout_s: float = 600.0,
    ) -> AIReply:
        """Send *messages* to the DeepSeek API and return a structured reply.

        Raises CancelledError if cancel_token is set before the call.
        Never sends temperature/top_p/presence_penalty/frequency_penalty.
        """
        # Check cancellation before making the network call
        if cancel_token is not None and cancel_token.is_set():
            raise CancelledError("Request cancelled before API call")

        extra_body, _effort = _resolve_thinking_params(
            self._settings, thinking=thinking, reasoning_effort=reasoning_effort
        )
        extra_body = {**extra_body, **_openclaw_agent_request_extra(self._settings)}
        api_messages, system_param = _prepare_api_messages(self._settings, messages)
        if system_param:
            extra_body = {**extra_body, "system": system_param}
        _thinking_on = _thinking_enabled(extra_body, _effort)
        _max_tokens = _completion_max_tokens(
            self._settings, extra_body=extra_body, effort=_effort
        )

        masked_key = mask_secret(self._settings.api_key)
        self._log.debug(
            "DeepSeekClient.chat: model=%s thinking=%s effort=%s max_tokens=%s "
            "system_hoisted=%s key=...%s msgs=%d",
            self._settings.model,
            _thinking_on,
            _effort,
            _max_tokens,
            bool(system_param),
            masked_key[-4:] if len(masked_key) >= 4 else "****",
            len(api_messages),
        )

        if _OpenAI is None:
            raise RuntimeError("openai package is not installed") from _OPENAI_IMPORT_ERROR

        client = _OpenAI(
            base_url=self._settings.base_url,
            api_key=self._settings.api_key,
        )

        t0 = time.monotonic()
        create_kwargs: dict[str, Any] = {
            "model": _effective_api_model(self._settings),
            "messages": api_messages,
            "timeout": timeout_s,
            "max_tokens": _max_tokens,
        }
        if extra_body:
            create_kwargs["extra_body"] = extra_body
        if _effort is not None:
            create_kwargs["reasoning_effort"] = _effort
        # When thinking mode is OFF, set temperature=0 for maximum instruction-following
        # fidelity and JSON format compliance.  Thinking mode is incompatible with
        # temperature (DeepSeek/Anthropic spec), so we only inject it when safe.
        if not _thinking_on:
            create_kwargs["temperature"] = 0
        try:
            response = client.chat.completions.create(
                **create_kwargs,
                # IMPORTANT: do NOT add temperature, top_p, presence_penalty,
                # frequency_penalty — they are incompatible with thinking mode.
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self._log.error("DeepSeekClient API error after %.0f ms: %s", latency_ms, exc)
            raise

        latency_ms = (time.monotonic() - t0) * 1000

        msg = response.choices[0].message
        content = msg.content or ""
        reasoning_content = getattr(msg, "reasoning_content", None) or ""
        # MiniMax with reasoning_split=True may also use reasoning_details
        if not reasoning_content:
            details = getattr(msg, "reasoning_details", None)
            if details:
                parts = []
                for detail in details:
                    t = detail.get("text") if isinstance(detail, dict) else getattr(detail, "text", None)
                    if t:
                        parts.append(t)
                reasoning_content = "".join(parts)

        if _is_mimo(self._settings):
            store_reasoning_from_response(
                api_messages,
                response_message_dict(content, reasoning_content, msg),
                _MIMO_REASONING_CACHE,
            )

        # Build usage
        u = response.usage
        usage = AIUsage(
            prompt_tokens=getattr(u, "prompt_tokens", 0),
            cached_prompt_tokens=_extract_cached_prompt_tokens(u),
            completion_tokens=getattr(u, "completion_tokens", 0),
            total_tokens=getattr(u, "total_tokens", 0),
        )

        request_id = getattr(response, "id", "") or ""

        # Build raw dict for debug tab — mask API key if it somehow appears
        raw: dict[str, Any] = {
            "id": request_id,
            "model": getattr(response, "model", ""),
            "content": content,
            "reasoning_content": reasoning_content,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "cached_prompt_tokens": usage.cached_prompt_tokens,
                "cache_miss_tokens": usage.cache_miss_tokens,
                "cache_hit_rate_pct": round(usage.cache_hit_rate * 100, 1),
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
            "latency_ms": latency_ms,
        }

        self._log.debug(
            "DeepSeekClient.chat done: latency=%.0f ms tokens=%d/%d",
            latency_ms, usage.prompt_tokens, usage.completion_tokens,
        )

        # Log KV-cache hit rate so operators can monitor savings.
        # DeepSeek硬盘缓存：prompt_cache_hit_tokens 是命中缓存的 token 数。
        if usage.prompt_tokens > 0:
            hit_rate = usage.cached_prompt_tokens / usage.prompt_tokens * 100
            self._log.info(
                "KV-cache: hit=%d miss=%d total_prompt=%d hit_rate=%.1f%%",
                usage.cached_prompt_tokens,
                usage.prompt_tokens - usage.cached_prompt_tokens,
                usage.prompt_tokens,
                hit_rate,
            )

        return AIReply(
            content=content,
            reasoning_content=reasoning_content,
            raw=raw,
            usage=usage,
            request_id=request_id,
            latency_ms=latency_ms,
        )

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        on_reasoning_token: Callable[[str], None] | None = None,
        on_content_token: Callable[[str], None] | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        cancel_token: "CancelToken | None" = None,
        timeout_s: float = 600.0,
    ) -> AIReply:
        """Stream *messages* to the DeepSeek API, calling callbacks per token.

        Follows the official DeepSeek streaming example exactly:
        - reasoning_content tokens arrive first (thinking phase)
        - content tokens arrive after (answer phase)
        - delta.reasoning_content is None (not empty string) when absent

        Parameters
        ----------
        on_reasoning_token:
            Called with each reasoning/thinking token chunk as it arrives.
        on_content_token:
            Called with each content token chunk as it arrives.

        Returns the same AIReply as chat() once the stream is complete.
        Raises CancelledError if cancel_token is set before or during the call.
        """
        if cancel_token is not None and cancel_token.is_set():
            raise CancelledError("Request cancelled before API call")

        from pa_agent.ai.cursor_connector import is_openclaw_cs_model
        from pa_agent.ai.qoder_connector import is_openclaw_qc_model
        from pa_agent.ai.trae_connector import is_openclaw_twc_model

        if is_openclaw_cs_model(self._settings.model):
            raise RuntimeError(
                "模型 openclaw_cs 必须使用 Cursor SDK 路由，但当前仍在使用 DeepSeekClient。"
                "请在「AI 模型」设置中重新保存，或重启应用后再分析。"
            )
        if is_openclaw_twc_model(self._settings.model):
            raise RuntimeError(
                "模型 openclaw_twc 必须使用 TRAE Work CN 路由，但当前仍在使用 DeepSeekClient。"
                "请在「AI 模型」设置中重新保存，或重启应用后再分析。"
            )
        if is_openclaw_qc_model(self._settings.model):
            raise RuntimeError(
                "模型 openclaw_qc 必须使用 Qoder CN 路由，但当前仍在使用 DeepSeekClient。"
                "请在「AI 模型」设置中重新保存，或重启应用后再分析。"
            )

        extra_body, _effort = _resolve_thinking_params(
            self._settings, thinking=thinking, reasoning_effort=reasoning_effort
        )
        extra_body = {**extra_body, **_openclaw_agent_request_extra(self._settings)}
        api_messages, system_param = _prepare_api_messages(self._settings, messages)
        if system_param:
            extra_body = {**extra_body, "system": system_param}
        _thinking_on = _thinking_enabled(extra_body, _effort)
        _max_tokens = _completion_max_tokens(
            self._settings, extra_body=extra_body, effort=_effort
        )

        self._log.info(
            "DeepSeekClient.stream_chat: model=%s thinking=%s reasoning_effort=%s "
            "max_tokens=%s system_hoisted=%s msgs=%d",
            self._settings.model,
            _thinking_on,
            _effort,
            _max_tokens,
            bool(system_param),
            len(api_messages),
        )

        if _OpenAI is None:
            raise RuntimeError("openai package is not installed") from _OPENAI_IMPORT_ERROR

        client = _OpenAI(
            base_url=self._settings.base_url,
            api_key=self._settings.api_key,
        )

        t0 = time.monotonic()
        reasoning_content = ""
        content = ""
        request_id = ""
        model_name = ""
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        cached_tokens = 0

        try:
            # Build kwargs with stream_options to get usage in the final chunk.
            # Some providers may not support it; if the create() call itself
            # rejects stream_options we retry without it.
            stream_kwargs: dict[str, Any] = {
                "model": _effective_api_model(self._settings),
                "messages": api_messages,
                "timeout": timeout_s,
                "max_tokens": _max_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if extra_body:
                stream_kwargs["extra_body"] = extra_body
            if _effort is not None:
                stream_kwargs["reasoning_effort"] = _effort

            try:
                stream = client.chat.completions.create(**stream_kwargs)
            except Exception:
                # Retry without stream_options if provider rejects it
                self._log.debug("stream_options not supported; retrying without it")
                stream_kwargs.pop("stream_options", None)
                stream = client.chat.completions.create(**stream_kwargs)

            for chunk in stream:
                # Check cancellation on each chunk
                if cancel_token is not None and cancel_token.is_set():
                    raise CancelledError("Request cancelled during streaming")

                # Extract usage from the final chunk (stream_options)
                if hasattr(chunk, "usage") and chunk.usage is not None:
                    u = chunk.usage
                    prompt_tokens = getattr(u, "prompt_tokens", 0) or prompt_tokens
                    completion_tokens = getattr(u, "completion_tokens", 0) or completion_tokens
                    total_tokens = getattr(u, "total_tokens", 0) or total_tokens
                    cached_tokens = _extract_cached_prompt_tokens(u) or cached_tokens

                if not getattr(chunk, "choices", None):
                    continue

                request_id = request_id or (getattr(chunk, "id", "") or "")
                model_name = model_name or (getattr(chunk, "model", "") or "")

                choice0 = chunk.choices[0]
                delta = getattr(choice0, "delta", None)
                if delta is None:
                    continue

                # Official pattern: reasoning_content is None when absent, not ""
                # reasoning_content arrives first (thinking phase), then content
                # MiniMax with reasoning_split=True uses delta.reasoning_details[].text
                # instead of delta.reasoning_content.
                r = getattr(delta, "reasoning_content", None)
                if not r:
                    # MiniMax streaming: reasoning_details is a list of dicts
                    details = getattr(delta, "reasoning_details", None)
                    if details:
                        for detail in details:
                            t = detail.get("text") if isinstance(detail, dict) else getattr(detail, "text", None)
                            if t:
                                r = (r or "") + t
                if r:
                    reasoning_content += r
                    if on_reasoning_token is not None:
                        on_reasoning_token(r)
                elif delta.content:
                    content += delta.content
                    if on_content_token is not None:
                        on_content_token(delta.content)

        except CancelledError:
            raise
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self._log.error("DeepSeekClient stream error after %.0f ms: %s", latency_ms, exc)
            raise

        latency_ms = (time.monotonic() - t0) * 1000

        usage = AIUsage(
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        raw: dict[str, Any] = {
            "id": request_id,
            "model": model_name,
            "content": content,
            "reasoning_content": reasoning_content,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "cached_prompt_tokens": usage.cached_prompt_tokens,
                "cache_miss_tokens": usage.cache_miss_tokens,
                "cache_hit_rate_pct": round(usage.cache_hit_rate * 100, 1),
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
            "latency_ms": latency_ms,
        }

        self._log.info(
            "DeepSeekClient.stream_chat done: latency=%.0f ms "
            "reasoning_chars=%d content_chars=%d deepseek_thinking=%s effort=%s",
            latency_ms,
            len(reasoning_content),
            len(content),
            _thinking_on,
            _effort,
        )

        # Log KV-cache hit rate for stream calls as well.
        if usage.prompt_tokens > 0:
            hit_rate = usage.cached_prompt_tokens / usage.prompt_tokens * 100
            self._log.info(
                "KV-cache: hit=%d miss=%d total_prompt=%d hit_rate=%.1f%%",
                usage.cached_prompt_tokens,
                usage.prompt_tokens - usage.cached_prompt_tokens,
                usage.prompt_tokens,
                hit_rate,
            )
        if not content.strip():
            self._log.warning(
                "API returned empty content (model=%s base_url=%s). "
                "Check 原始 tab Raw Response; for KKAI/Claude ensure model ID and token group match.",
                self._settings.model,
                self._settings.base_url,
            )
        if _thinking_on and len(reasoning_content) < 80:
            self._log.warning(
                "Thinking enabled but reasoning_content is very short (%d chars). "
                "For KKAI/Claude use reasoning_effort (not DeepSeek extra_body); "
                "check model ID, token group, and reasoning_effort=%s.",
                len(reasoning_content),
                _effort,
            )

        if _is_mimo(self._settings):
            store_reasoning_from_response(
                api_messages,
                {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning_content,
                },
                _MIMO_REASONING_CACHE,
            )

        return AIReply(
            content=content,
            reasoning_content=reasoning_content,
            raw=raw,
            usage=usage,
            request_id=request_id,
            latency_ms=latency_ms,
        )
