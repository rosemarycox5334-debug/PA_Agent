"""SenseNova-only free-model fallback (public Kilo isFree pool).

Trigger gate: normalized ``provider.base_url`` equals
``https://token.sensenova.cn/v1``. On ordinary analysis failures (transport,
HTTP/429, empty body, JSON parse / schema validation, etc.) the orchestrator
may rotate a bounded set of public free candidates.

Rules:
- Never send the primary SenseNova API key to free upstreams.
- Kilo ``isFree: true`` discovery only; no OpenCode / EAC / login channels.
- User cancel / KeyboardInterrupt / SystemExit must propagate.
- Finite candidates + cooldowns; no infinite retry.
- Success requires a real upstream reply that also passes stage validation.
"""
from __future__ import annotations

import inspect
import json
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

SENSENOVA_CANONICAL_BASE = "https://token.sensenova.cn/v1"

KILO_BASE = "https://api.kilo.ai/api/gateway"
_KILO_UA = "PA-Agent/sensenova-fallback"

_LISTING_TTL_S = 30 * 60
_COOLDOWN_S = 5 * 60
_MAX_CANDIDATES = 6
_DEFAULT_TIMEOUT_S = 120.0
_DEFAULT_MAX_TOKENS = 8192
#: How often the spare channel reports that it is still waiting. The UI shows a
#: live counter so a slow route does not look like a frozen window.
_HEARTBEAT_S = 5.0

# 用真实的 PA lite 第二阶段提示词实测（2026-10-29，XAUUSD 5m、100 根K线），
# 思考开启，每条路由都用它能接受的最小思考预算。
#
# 排序规则（用户 2026-10-29 指定，两次调整后的最终版）：
# **思考过程是中文的优先**，同一档内推理字数越多的越优先。
#
# 为什么按语言排序：实测证明思考语言是模型自带的属性，改提示词没用——
# 在系统提示里显式要求「思考必须用简体中文」后，super-vl 仍是 0% 中文、
# lightning 15%、openrouter 29%，而 beam-free 97%、north-mini-code 93% 本来就写中文。
# 所以只能按模型挑，不能靠提示词纠正。不思考的路由没有中文可言，一律垫底。
#
# 每条后面的注释是：中文占比、实测隐藏推理字数、该模式下的延迟。
_REASONING_CJK_SHARE_KILO: dict[str, float] = {
    "reflection/beam-free": 0.967,
    "cohere/north-mini-code:free": 0.933,
    "recursive/flint-v1.0:free": 0.460,
    "openrouter/free": 0.287,
    "nvidia/nemotron-3.5-lightning:free": 0.146,
    "nvidia/nemotron-3.5-super-vl:free": 0.000,
    # 不产生隐藏推理：没有中文可谈，排最后
    "poolside/laguna-s-2.1:free": -1.0,
    "stealth/glyph-cluster": -1.0,
}

_PREFERRED_KILO = (
    # -- 中文思考的 --
    "reflection/beam-free",                              # 中文 97%   269 字   4.0 秒（effort=minimal）
    "cohere/north-mini-code:free",                       # 中文 93%  1843 字  14.6 秒
    # -- 中英混杂 --
    "recursive/flint-v1.0:free",                         # 中文 46%  9131 字  20.9 秒
    # -- 基本英文思考 --
    "openrouter/free",                                   # 中文 29%  1803 字  18.8 秒
    "nvidia/nemotron-3.5-lightning:free",                # 中文 15%  1732 字  11.3 秒
    "nvidia/nemotron-3.5-super-vl:free",                 # 中文  0%  1528 字   6.5 秒
    # -- 不思考的，只作垫底 --
    "poolside/laguna-s-2.1:free",                        # 无推理     0 字   8.7 秒
    "stealth/glyph-cluster",                             # 无推理     0 字  36.5 秒
)

#: 同一份 lite 第二阶段提示词下，各路由的实测隐藏推理字数（最近一次实测）。
#: 排序的次键：同一语言档内字数多的优先。注意单次采样波动很大
#: （north-mini-code 曾测到 12 378 字，复测只有 1 843 字），所以只作次键。
_REASONING_CHARS_KILO: dict[str, int] = {
    "recursive/flint-v1.0:free": 9131,
    "cohere/north-mini-code:free": 1843,
    "openrouter/free": 1803,
    "nvidia/nemotron-3.5-lightning:free": 1732,
    "nvidia/nemotron-3.5-super-vl:free": 1528,
    "reflection/beam-free": 269,
    "dots-studio/dots-3-note-preview:free": 8903,
    "poolside/laguna-s-2.1:free": 0,
    "stealth/glyph-cluster": 0,
}
#: 实测确实会产生隐藏推理的路由（由上面的字数表派生，不手工维护）。
_THINKING_CAPABLE_KILO = frozenset(
    model for model, chars in _REASONING_CHARS_KILO.items() if chars > 0
)

# Probed and rejected. Keeping them here (instead of relying on the live pool
# to drop them) means a model that fails today is not retried tomorrow just
# because the listing still advertises it as free.
#
#   kilo-auto/free                      ignores every thinking control: 15 265
#                                       reasoning chars, 89.3 s
#   nemotron-3-nano-omni-30b-a3b-...    leaves order_type and decision.reasoning
#                                       empty under every control shape
_EXCLUDED_KILO = frozenset({
    "kilo-auto/free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "liquid/lfm-2.5-2.6b:free",
    "xingchen-agi/xing4.0-29b-a4b:free",
    "inclusionai/ling-3.1-flash",
})

# Working but currently degrading upstream: pushed behind everything else rather
# than dropped, since both failures were the provider, not the thinking control.
#   poolside/laguna-xs-2.1:free          HTTP 429 (rate limited) on every shape
#   nvidia/nemotron-3-ultra-...          HTTP 502 "Upstream error from Nvidia"
#   dots-studio/dots-3-note-preview:free 本轮在 8192 输出预算下截断，语言未测
_UNSTABLE_KILO = frozenset({
    "poolside/laguna-xs-2.1:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "dots-studio/dots-3-note-preview:free",
})

# The spare channel prefers a route that can think, but pins every route to the
# smallest thinking budget it will accept.
#
# Measured on 2026-10-29 (same lite Stage-2 prompt): asking for the lowest
# *effort* is not enough — ``effort="minimal"`` was accepted yet ignored, so one
# route spent 17 165 reasoning chars and 65 s, another 10 369 chars and 94 s.
# A token budget is what actually binds: ``reasoning={"max_tokens": 512}``
# brought the same routes to 8.3 s / 1 818 chars and 15.1 s / 860 chars with the
# contract still clean, while keeping thinking enabled.
#
# ``reasoning.effort`` and ``reasoning.max_tokens`` are mutually exclusive on
# this gateway, so exactly one shape is sent per attempt. A route that rejects
# the budget drops to the lowest effort tier, then to no field at all; the
# negotiated variant is cached per model so only the first call discovers it.
_REASONING_BUDGET_TOKENS = 512
_REASONING_VARIANTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("budget", {"reasoning": {"max_tokens": _REASONING_BUDGET_TOKENS}}),
    ("minimal", {"reasoning": {"effort": "minimal"}}),
    ("low", {"reasoning": {"effort": "low"}}),
    ("unset", {}),
)
_REASONING_VARIANTS_BY_NAME: dict[str, dict[str, Any]] = {
    name: extra for name, extra in _REASONING_VARIANTS
}

# Which shape actually binds thinking is a per-route fact, measured 2026-10-29:
# some routes honour the token budget (lightning 65 s -> 9 s), while beam-free is
# the opposite (budget 36 s and a broken contract, minimal effort 7.7 s and
# clean). Seeding the cache with the measured answer means the first real call
# already uses the right shape instead of discovering it by trial.
_REASONING_VARIANT_PRESET: dict[str, str] = {
    "reflection/beam-free": "minimal",
}
_reasoning_variant_by_model: dict[str, str] = dict(_REASONING_VARIANT_PRESET)

ValidateFn = Callable[[Any], Any]


def _post_with_minimal_thinking(
    *,
    url: str,
    headers: dict[str, str],
    candidate: FreeCandidate,
    messages: list[dict[str, Any]],
    max_tokens: int,
    primary_api_key: str | None,
    poster: Callable[..., Any],
    timeout_s: float,
    stream: bool = False,
) -> tuple[int, Any]:
    """POST one completion, preferring a route that thinks as little as possible."""
    model = candidate.model
    known = _reasoning_variant_by_model.get(model)
    if known:
        order = [known] + [
            name for name, _ in _REASONING_VARIANTS if name != known
        ]
    else:
        order = [name for name, _ in _REASONING_VARIANTS]

    last_reasoning_error = ""
    for name in order:
        extra = _REASONING_VARIANTS_BY_NAME[name]
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "stream": bool(stream),
        }
        body.update(extra)

        _assert_key_isolation(
            primary_api_key=primary_api_key,
            headers=headers,
            body=body,
        )

        try:
            status, payload = poster(
                url, headers=headers, body=body, timeout=timeout_s
            )
        except Exception as exc:
            if is_cancel_or_interrupt(exc):
                raise
            raise RuntimeError(f"{candidate.provider}/{model}: {exc}") from exc

        # Only a rejected reasoning field is worth retrying: any other 4xx
        # (or a 429/5xx) is the caller's problem or a cooldown, not a variant
        # mismatch.
        if status in (400, 422) and extra:
            last_reasoning_error = _error_message(payload)
            _reasoning_variant_by_model.pop(model, None)
            continue

        _reasoning_variant_by_model[model] = name
        return status, payload

    raise RuntimeError(
        f"{candidate.provider}/{model}: upstream rejected all reasoning controls"
        + (f" ({last_reasoning_error})" if last_reasoning_error else "")
    )


def _error_message(payload: Any) -> str:
    """Best-effort upstream error text for a log line (never raises)."""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or "")[:120]
    if isinstance(err, str):
        return err[:120]
    return ""


def normalize_base_url(url: str | None) -> str:
    """Normalize an OpenAI-compatible base_url for exact SenseNova matching."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    if path and not path.startswith("/"):
        path = "/" + path
    return urlunsplit((scheme, netloc, path, "", ""))


def is_sensenova_fallback_eligible(base_url: str | None) -> bool:
    """True only when *base_url* normalizes to the SenseNova token gateway."""
    return normalize_base_url(base_url) == SENSENOVA_CANONICAL_BASE


def is_cancel_or_interrupt(exc: BaseException) -> bool:
    """True for user cancel / KeyboardInterrupt (must not be swallowed)."""
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return True
    try:
        from pa_agent.ai.deepseek_client import CancelledError

        if isinstance(exc, CancelledError):
            return True
    except Exception:  # noqa: BLE001
        pass
    name = type(exc).__name__
    return name in {"CancelledError", "KeyboardInterrupt", "SystemExit"}


@dataclass(frozen=True)
class FreeCandidate:
    """One free upstream target (provider + model)."""

    provider: str  # "kilo"
    model: str
    base_url: str


@dataclass
class FreeAttemptRecord:
    provider: str
    model: str
    ok: bool
    error: str = ""
    latency_ms: float = 0.0
    http_status: int | None = None
    traceback: str = ""


@dataclass
class FreeStageSuccess:
    reply: Any
    candidate: FreeCandidate
    result: Any = None  # JsonValidator Ok
    attempts: list[FreeAttemptRecord] = field(default_factory=list)


@dataclass
class FreeStageFailure:
    attempts: list[FreeAttemptRecord] = field(default_factory=list)
    message: str = "all free candidates failed"


class _ListingCache:
    def __init__(self) -> None:
        self.kilo_rows: list[dict[str, Any]] | None = None
        self.kilo_expires = 0.0


_CACHE = _ListingCache()
_COOLDOWNS: dict[tuple[str, str], float] = {}


def reset_fallback_state_for_tests() -> None:
    """Clear listing cache, cooldowns and negotiated reasoning variants."""
    _CACHE.kilo_rows = None
    _CACHE.kilo_expires = 0.0
    _COOLDOWNS.clear()
    _reasoning_variant_by_model.clear()
    _reasoning_variant_by_model.update(_REASONING_VARIANT_PRESET)


def _cooldown_key(candidate: FreeCandidate) -> tuple[str, str]:
    return (candidate.provider, candidate.model)


def mark_cooldown(candidate: FreeCandidate, *, seconds: float = _COOLDOWN_S) -> None:
    _COOLDOWNS[_cooldown_key(candidate)] = time.monotonic() + max(0.0, seconds)


def _in_cooldown(candidate: FreeCandidate) -> bool:
    until = _COOLDOWNS.get(_cooldown_key(candidate), 0.0)
    return time.monotonic() < until


def _is_kilo_free_row(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and row.get("isFree") is True
        and isinstance(row.get("id"), str)
        and bool(row["id"].strip())
    )


def _is_safety_only_model(model_id: str) -> bool:
    """Exclude content-safety / guardrail models from chat fallback rotation."""
    mid = (model_id or "").lower()
    needles = (
        "content-safety",
        "llama-guard",
        "prompt-guard",
        "safety-guard",
        "moderation",
        "/guard-",
        "-guard:",
        "-guard/",
    )
    if any(n in mid for n in needles):
        return True
    # Common trailing /guard patterns: "...-guard", ".../guard"
    return mid.endswith("-guard") or mid.endswith("/guard") or ":guard" in mid


def fetch_kilo_free_models(
    *,
    http_get: Callable[..., Any] | None = None,
    now: Callable[[], float] | None = None,
    cancel_token: Any | None = None,
) -> list[str]:
    """Return Kilo free-pool model ids (``isFree: true`` only)."""
    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        from pa_agent.ai.deepseek_client import CancelledError

        raise CancelledError("Request cancelled before free-model listing")

    clock = now or time.monotonic
    if _CACHE.kilo_rows is not None and clock() < _CACHE.kilo_expires:
        return [str(r["id"]) for r in _CACHE.kilo_rows if _is_kilo_free_row(r)]

    getter = http_get or _default_http_get
    payload = getter(
        f"{KILO_BASE}/models",
        headers={
            "accept": "application/json",
            "user-agent": _KILO_UA,
        },
        timeout=30.0,
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = []
    free_rows = [r for r in rows if _is_kilo_free_row(r)]
    _CACHE.kilo_rows = free_rows
    _CACHE.kilo_expires = clock() + _LISTING_TTL_S
    return [str(r["id"]) for r in free_rows]


def _default_http_get(url: str, *, headers: dict[str, str], timeout: float) -> Any:
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


def build_candidates(
    *,
    http_get: Callable[..., Any] | None = None,
    max_candidates: int = _MAX_CANDIDATES,
    cancel_token: Any | None = None,
) -> list[FreeCandidate]:
    """Build a bounded rotation: filter cooldown first, then cap at *max_candidates*."""
    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        from pa_agent.ai.deepseek_client import CancelledError

        raise CancelledError("Request cancelled before free candidate build")

    kilo_ids: list[str] = []
    try:
        kilo_ids = fetch_kilo_free_models(http_get=http_get, cancel_token=cancel_token)
    except Exception as exc:
        if is_cancel_or_interrupt(exc):
            raise
        logger.warning("Kilo free listing failed: %s", exc)

    kilo_set = {
        m
        for m in kilo_ids
        if isinstance(m, str)
        and m.strip()
        and not _is_safety_only_model(m)
        and m not in _EXCLUDED_KILO
    }
    ordered_kilo = [m for m in _PREFERRED_KILO if m in kilo_set]
    ordered_kilo.extend(
        sorted(m for m in kilo_set if m not in ordered_kilo and m not in _UNSTABLE_KILO)
    )
    # Unstable models only get a turn after every stable one has been tried.
    ordered_kilo.extend(sorted(m for m in kilo_set if m in _UNSTABLE_KILO))

    out: list[FreeCandidate] = []
    for mid in ordered_kilo:
        cand = FreeCandidate("kilo", mid, KILO_BASE)
        if _in_cooldown(cand):
            continue
        out.append(cand)
        if len(out) >= max_candidates:
            break
    return out


def _assert_key_isolation(
    *,
    primary_api_key: str | None,
    headers: dict[str, str],
    body: dict[str, Any],
) -> None:
    key = (primary_api_key or "").strip()
    if not key:
        return
    blob = json.dumps(headers, ensure_ascii=False) + json.dumps(body, ensure_ascii=False)
    if key in blob:
        raise RuntimeError("refusing to send primary API key to free upstream")


def _extract_message_text(payload: dict[str, Any]) -> tuple[str, str]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return "", ""
    content = msg.get("content") or ""
    reasoning = (
        msg.get("reasoning_content")
        or msg.get("reasoning")
        or ""
    )
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        content = "".join(parts)
    if not isinstance(content, str):
        content = str(content or "")
    if not isinstance(reasoning, str):
        reasoning = str(reasoning or "")
    return content, reasoning


def _choice_finish_reason(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    return str(choices[0].get("finish_reason") or choices[0].get("native_finish_reason") or "")


def _to_ai_reply(
    payload: dict[str, Any],
    *,
    candidate: FreeCandidate,
    latency_ms: float,
) -> Any:
    from pa_agent.ai.deepseek_client import AIReply, AIUsage

    content, reasoning = _extract_message_text(payload)
    usage_raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    usage = AIUsage(
        prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
        cached_prompt_tokens=0,
        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        total_tokens=int(usage_raw.get("total_tokens") or 0),
    )
    raw = {
        "id": payload.get("id") or "",
        "model": payload.get("model") or candidate.model,
        "content": content,
        "reasoning_content": reasoning,
        "finish_reason": _choice_finish_reason(payload),
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cost": usage_raw.get("cost"),
        },
        "latency_ms": latency_ms,
        "fallback_provider": candidate.provider,
        "fallback_model": candidate.model,
        "fallback_base_url": candidate.base_url,
    }
    return AIReply(
        content=content,
        reasoning_content=reasoning,
        raw=raw,
        usage=usage,
        request_id=str(payload.get("id") or ""),
        latency_ms=latency_ms,
    )


def call_free_candidate(
    candidate: FreeCandidate,
    messages: list[dict[str, Any]],
    *,
    primary_api_key: str | None = None,
    cancel_token: Any | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    http_post: Callable[..., Any] | None = None,
    http_stream: Callable[..., Any] | None = None,
    stream: bool = False,
    on_reasoning_token: Callable[[str], None] | None = None,
    on_content_token: Callable[[str], None] | None = None,
) -> Any:
    """Call one free upstream, streaming when *stream* is set.

    Streaming keeps the same reply shape and the same truncation rule; the only
    difference is that reasoning/content deltas reach the callbacks as they
    arrive instead of after the call returns.

    Raises CancelledError / KeyboardInterrupt / SystemExit unchanged. Other
    failures raise ``RuntimeError`` with a concise message (no secrets).
    """
    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        from pa_agent.ai.deepseek_client import CancelledError

        raise CancelledError("Request cancelled before free-model call")

    if candidate.provider != "kilo":
        raise RuntimeError(f"unsupported free provider: {candidate.provider}")

    headers: dict[str, str] = {
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
        "user-agent": _KILO_UA,
    }
    url = f"{candidate.base_url.rstrip('/')}/chat/completions"

    emitted: dict[str, bool] = {"reasoning": False, "content": False}
    if stream:
        transport = http_stream or http_post or _default_http_post

        def _streaming_poster(
            url_: str, *, headers: dict[str, str], body: dict[str, Any], timeout: float
        ) -> tuple[int, Any]:
            def _on_event(event: dict[str, Any]) -> None:
                _emit_stream_delta(
                    event, on_reasoning_token, on_content_token, emitted
                )

            if _accepts_on_event(transport):
                return transport(
                    url_,
                    headers=headers,
                    body=body,
                    timeout=timeout,
                    on_event=_on_event,
                )
            # A test double that returns a complete payload: no deltas, the
            # caller falls back to emitting the finished text once.
            return transport(url_, headers=headers, body=body, timeout=timeout)

        poster: Callable[..., Any] = _streaming_poster
    else:
        poster = http_post or _default_http_post

    t0 = time.monotonic()
    status, payload = _post_with_minimal_thinking(
        url=url,
        headers=headers,
        candidate=candidate,
        messages=messages,
        max_tokens=max_tokens,
        primary_api_key=primary_api_key,
        poster=poster,
        timeout_s=timeout_s,
        stream=stream,
    )

    latency_ms = (time.monotonic() - t0) * 1000
    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        from pa_agent.ai.deepseek_client import CancelledError

        raise CancelledError("Request cancelled after free-model call")

    if status == 429:
        mark_cooldown(candidate)
        raise RuntimeError(f"{candidate.provider}/{candidate.model}: HTTP 429 rate limited")
    if status >= 500:
        mark_cooldown(candidate, seconds=60.0)
        raise RuntimeError(f"{candidate.provider}/{candidate.model}: HTTP {status} server error")
    if status >= 400:
        err_msg = ""
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                err_msg = str(err.get("message") or "")
            elif isinstance(err, str):
                err_msg = err
        raise RuntimeError(
            f"{candidate.provider}/{candidate.model}: HTTP {status}"
            + (f" {err_msg}" if err_msg else "")
        )

    if not isinstance(payload, dict):
        raise RuntimeError(f"{candidate.provider}/{candidate.model}: non-JSON response")
    if payload.get("error"):
        raise RuntimeError(
            f"{candidate.provider}/{candidate.model}: {payload.get('error')!r}"
        )

    finish_reason = _choice_finish_reason(payload)
    if finish_reason == "length":
        raise RuntimeError(
            f"{candidate.provider}/{candidate.model}: truncated (finish_reason=length)"
        )

    reply = _to_ai_reply(payload, candidate=candidate, latency_ms=latency_ms)
    content = (reply.content or "").strip()
    reasoning = (reply.reasoning_content or "").strip()
    if not content:
        if reasoning:
            raise RuntimeError(
                f"{candidate.provider}/{candidate.model}: reasoning-only / empty content"
            )
        raise RuntimeError(f"{candidate.provider}/{candidate.model}: empty response")
    return reply


def _default_http_post(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[int, Any]:
    """POST one chat completion, streaming when ``body["stream"]`` is set.

    Keeping both modes behind one injectable function means a test double (or a
    caller) only has to replace a single seam.
    """
    if body.get("stream"):
        return _default_http_stream(
            url, headers=headers, body=body, timeout=timeout, on_event=on_event
        )

    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        resp = client.post(url, headers=headers, json=body)
        try:
            payload: Any = resp.json()
        except Exception:  # noqa: BLE001
            payload = {"error": {"message": (resp.text or "")[:300]}}
        return resp.status_code, payload


def _accepts_on_event(fn: Callable[..., Any]) -> bool:
    """True when *fn* can be handed a per-chunk callback.

    ``unittest.mock.patch`` replaces the function with a mock whose signature is
    ``(*args, **kwargs)``, which would wrongly look compatible; inspect the
    substituted ``side_effect`` instead.
    """
    target: Any = fn
    side_effect = getattr(fn, "side_effect", None)
    if callable(side_effect):
        target = side_effect
    try:
        params = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return False
    return "on_event" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _sse_line_to_event(line: str) -> dict[str, Any] | None:
    """Decode one Server-Sent-Events line into a chat-completion chunk."""
    if not line:
        return None
    text = line.strip()
    if not text.startswith("data:"):
        return None
    data = text[len("data:"):].strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def _default_http_stream(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[int, Any]:
    """Stream one SSE completion, forwarding each chunk to *on_event*.

    Returns ``(status, payload)`` shaped exactly like the non-stream reply, so
    callers can share the truncation / usage / reply-building code.
    """
    import httpx

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason = ""
    usage: dict[str, Any] = {}
    response_id = ""
    model = ""

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream("POST", url, headers=headers, json=body) as resp:
            status = resp.status_code
            if status >= 400:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    payload: Any = json.loads(raw)
                except Exception:  # noqa: BLE001
                    payload = {"error": {"message": raw[:300]}}
                return status, payload

            for line in resp.iter_lines():
                event = _sse_line_to_event(line)
                if event is None:
                    continue
                if event.get("id"):
                    response_id = str(event["id"])
                if event.get("model"):
                    model = str(event["model"])
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]

                choices = event.get("choices")
                choice = choices[0] if isinstance(choices, list) and choices else None
                if isinstance(choice, dict):
                    reason = choice.get("finish_reason")
                    if reason:
                        finish_reason = str(reason)
                    delta = choice.get("delta") or choice.get("message") or {}
                    if isinstance(delta, dict):
                        r = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(r, str) and r:
                            reasoning_parts.append(r)
                        c = delta.get("content")
                        if isinstance(c, str) and c:
                            content_parts.append(c)

                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception:  # noqa: BLE001
                        logger.debug("stream chunk callback failed", exc_info=True)

    payload = {
        "id": response_id,
        "model": model or body.get("model"),
        "choices": [
            {
                "message": {
                    "content": "".join(content_parts),
                    "reasoning_content": "".join(reasoning_parts),
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
    return 200, payload


def _emit_stream_delta(
    event: dict[str, Any],
    on_reasoning_token: Callable[[str], None] | None,
    on_content_token: Callable[[str], None] | None,
    counter: dict[str, bool],
) -> None:
    """Forward one streamed chunk to the UI callbacks, in arrival order."""
    choices = event.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict):
        return
    delta = choice.get("delta") or {}
    if not isinstance(delta, dict):
        return
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    if isinstance(reasoning, str) and reasoning and on_reasoning_token is not None:
        counter["reasoning"] = True
        try:
            on_reasoning_token(reasoning)
        except Exception:  # noqa: BLE001
            logger.debug("reasoning token callback failed", exc_info=True)
    content = delta.get("content")
    if isinstance(content, str) and content and on_content_token is not None:
        counter["content"] = True
        try:
            on_content_token(content)
        except Exception:  # noqa: BLE001
            logger.debug("content token callback failed", exc_info=True)


def _short_error(exc: BaseException, limit: int = 120) -> str:
    """One-line, truncated error text suitable for a log line."""
    text = " ".join(str(exc or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _start_heartbeat(
    progress: Callable[[str], None],
    interval: float,
) -> tuple[threading.Event | None, threading.Thread | None]:
    """Report elapsed wait time while a spare-channel call is in flight."""
    if not interval or interval <= 0:
        return None, None
    stop = threading.Event()
    started = time.monotonic()

    def _loop() -> None:
        while not stop.wait(interval):
            elapsed = int(time.monotonic() - started)
            progress(
                f"[提示] 备用通道仍在分析中（已等待 {elapsed} 秒），没有卡顿，请稍等…\n"
            )

    thread = threading.Thread(target=_loop, daemon=True, name="pa-spare-channel-heartbeat")
    thread.start()
    return stop, thread


def run_free_stage_fallback(
    *,
    messages: list[dict[str, Any]],
    validate: ValidateFn,
    primary_api_key: str | None,
    cancel_token: Any | None = None,
    on_reasoning_token: Callable[[str], None] | None = None,
    on_content_token: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    candidates: Iterable[FreeCandidate] | None = None,
    http_get: Callable[..., Any] | None = None,
    http_post: Callable[..., Any] | None = None,
    http_stream: Callable[..., Any] | None = None,
    stream: bool = False,
    heartbeat_seconds: float = _HEARTBEAT_S,
    max_candidates: int = _MAX_CANDIDATES,
) -> FreeStageSuccess | FreeStageFailure:
    """Try free candidates until one call + validation both succeed.

    ``on_progress`` receives short human-readable lines so the UI can show that
    the analysis is still alive during a multi-minute fallback rotation. It is
    best-effort: a failing callback never breaks the analysis.

    ``stream`` is opt-in: the production callers enable it so reasoning reaches
    the UI as it is produced instead of arriving in one block after a call that
    may run for a minute. Unit tests keep the non-streaming default.
    """
    from pa_agent.ai.json_validator import Ok, ValidationError

    def _progress(text: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(text)
        except Exception:  # noqa: BLE001
            logger.debug("free fallback progress callback failed", exc_info=True)

    use_stream = bool(stream)
    streamed_flags = {"reasoning": False, "content": False}

    def _on_reasoning(chunk: str) -> None:
        streamed_flags["reasoning"] = True
        if on_reasoning_token is not None:
            on_reasoning_token(chunk)

    def _on_content(chunk: str) -> None:
        streamed_flags["content"] = True
        if on_content_token is not None:
            on_content_token(chunk)

    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        from pa_agent.ai.deepseek_client import CancelledError

        raise CancelledError("Request cancelled before free candidate loop")

    if candidates is not None:
        cand_list = [c for c in candidates if not _in_cooldown(c)][:max_candidates]
    else:
        cand_list = build_candidates(
            http_get=http_get,
            max_candidates=max_candidates,
            cancel_token=cancel_token,
        )
    attempts: list[FreeAttemptRecord] = []
    if not cand_list:
        _progress("[提示] 备用通道当前不可用，本次分析将按原错误结束。\n")
        return FreeStageFailure(attempts=attempts, message="no free candidates available")

    total = len(cand_list)
    for index, candidate in enumerate(cand_list, start=1):
        if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
            from pa_agent.ai.deepseek_client import CancelledError

            raise CancelledError("Request cancelled before free candidate loop")

        _progress(
            f"[提示] 正在等待备用通道返回（第 {index}/{total} 次尝试，"
            f"单次最长约 {int(_DEFAULT_TIMEOUT_S)} 秒，请勿关闭窗口）…\n"
        )
        t0 = time.monotonic()
        reply: Any = None
        heartbeat_stop, _heartbeat_thread = _start_heartbeat(_progress, heartbeat_seconds)
        try:
            reply = call_free_candidate(
                candidate,
                messages,
                primary_api_key=primary_api_key,
                cancel_token=cancel_token,
                http_post=http_post,
                http_stream=http_stream,
                stream=use_stream,
                on_reasoning_token=_on_reasoning,
                on_content_token=_on_content,
            )
            _progress("[提示] 已收到返回，正在校验结果…\n")
            result = validate(reply)
        except Exception as exc:
            if is_cancel_or_interrupt(exc):
                raise
            if use_stream and (streamed_flags["reasoning"] or streamed_flags["content"]):
                # Part of this attempt already reached the pane; say so plainly
                # instead of leaving unexplained text above the next attempt.
                _progress("[提示] 该次返回未完整，已切换到下一个通道继续…\n")
            latency_ms = (time.monotonic() - t0) * 1000
            attempts.append(
                FreeAttemptRecord(
                    provider=candidate.provider,
                    model=candidate.model,
                    ok=False,
                    error=str(exc),
                    latency_ms=latency_ms,
                    traceback=traceback.format_exc(),
                )
            )
            logger.info(
                "SenseNova free fallback miss: %s/%s (%s)",
                candidate.provider,
                candidate.model,
                exc,
            )
            continue
        finally:
            if heartbeat_stop is not None:
                heartbeat_stop.set()

        latency_ms = (time.monotonic() - t0) * 1000
        if isinstance(result, Ok):
            # Legal wait/reject Ok results are success — never treat as errors.
            attempts.append(
                FreeAttemptRecord(
                    provider=candidate.provider,
                    model=candidate.model,
                    ok=True,
                    latency_ms=latency_ms,
                )
            )
            logger.warning(
                "SenseNova 主接口失败，已由免费模型接管: provider=%s model=%s (%.0f ms)",
                candidate.provider,
                candidate.model,
                latency_ms,
            )
            # Announce success *before* streaming the answer so the UI reads in order.
            _progress(
                f"[提示] 校验通过（用时 {latency_ms / 1000:.1f} 秒），"
                "以下是本次分析结果：\n"
            )
            # In streaming mode the deltas already reached the pane as they were
            # produced; re-sending the whole blob would duplicate every line. A
            # route that ignored stream=True delivers no deltas at all, so each
            # channel falls back to its finished text independently.
            if not streamed_flags["reasoning"] and getattr(
                reply, "reasoning_content", None
            ):
                _on_reasoning(reply.reasoning_content)
            if not streamed_flags["content"] and getattr(reply, "content", None):
                _on_content(reply.content)
            return FreeStageSuccess(
                reply=reply,
                candidate=candidate,
                result=result,
                attempts=attempts,
            )

        if not isinstance(result, ValidationError):
            err_msg = f"unexpected validate result: {type(result).__name__}"
        else:
            err_msg = result.message or "validation failed"
        attempts.append(
            FreeAttemptRecord(
                provider=candidate.provider,
                model=candidate.model,
                ok=False,
                error=f"validation: {err_msg}",
                latency_ms=latency_ms,
            )
        )
        logger.info(
            "SenseNova free fallback validation failed: %s/%s (%s)",
            candidate.provider,
            candidate.model,
            err_msg,
        )

    _progress(
        f"[提示] 备用通道 {total} 次尝试均未成功，本次分析将按原错误结束。\n"
    )
    return FreeStageFailure(
        attempts=attempts,
        message="all free candidates failed call or validation",
    )


def format_fallback_exception_meta(
    *,
    primary_message: str,
    primary_type: str,
    failure: FreeStageFailure | None,
) -> dict[str, Any]:
    """Structured exception metadata including full free-attempt trace."""
    attempts = []
    if failure is not None:
        for a in failure.attempts:
            attempts.append(
                {
                    "provider": a.provider,
                    "model": a.model,
                    "ok": a.ok,
                    "error": a.error,
                    "latency_ms": round(a.latency_ms, 1),
                    "http_status": a.http_status,
                    "traceback": a.traceback,
                }
            )
    return {
        "type": primary_type,
        "message": primary_message,
        "sensenova_free_fallback": {
            "triggered": True,
            "success": False,
            "attempts": attempts,
            "summary": failure.message if failure else "fallback not attempted",
        },
    }
