"""Stage-level spare channel: sensenova-6.8-flash-lite when the primary 429s.

Why this exists (measured 2026-10-30 on the real PA stage-2 prompt, ~107k
prompt tokens):

* ``deepseek-flash`` returns 429 (``tpm/rpm limit``) within a second.
* ``sensenova-6.8-flash-lite`` goes through the *same* gateway and the *same*
  key and never 429s (20+ calls, ~1.5M input tokens).
* It has two hard requirements, both found by measurement:
  1. the message list must end with a ``user`` turn — an assistant-tailed list
     answers 200 with an empty body (``finish_reason=stop``);
  2. thinking and output share ``max_tokens``, so the budget must be large
     (1024/2048 are entirely consumed by reasoning and the body stays empty).

Contract of this module:

* Only for a SenseNova primary (same key, same gateway base URL).
* Messages are re-shaped to end with ``user``; the trailing assistant turn is
  folded into the previous user turn, so no content is lost.
* ``max_tokens=16384``, ``reasoning_effort=low``, non-streaming.
* Returns ``None`` on any failure so the caller falls through to the free pool
  and then to the configured provider chain. Cancellation always propagates.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from pa_agent.ai.deepseek_client import AIUsage, AIReply, CancelledError

logger = logging.getLogger(__name__)

#: The spare model; must match the model id published by the gateway.
LITE_MODEL = "sensenova-6.8-flash-lite"
#: Thinking and answer share this budget — 1024/2048 return an empty body.
LITE_MAX_TOKENS = 16384
#: Lowest reasoning level; keeps the shared budget for the answer.
LITE_REASONING_EFFORT = "low"
#: A real PA stage-2 turn took 66–219 s in testing; leave plenty of room.
LITE_TIMEOUT_S = 600.0

ProgressFn = Callable[[str], None] | None
TokenFn = Callable[[str], None] | None


def is_lite_eligible(base_url: str | None) -> bool:
    """True only for the SenseNova gateway, where the primary key also works."""
    from pa_agent.ai.sensenova_fallback import is_sensenova_fallback_eligible

    return is_sensenova_fallback_eligible(base_url)


def shape_user_tail(messages: list[dict]) -> list[dict]:
    """Return a copy of *messages* that ends with a ``user`` turn.

    The trailing assistant answer is folded into the preceding user turn rather
    than dropped, because the caller loses nothing that way and the gateway
    stops returning an empty body.
    """
    msgs: list[dict] = [
        {"role": str(m.get("role") or "user"), "content": m.get("content") or ""}
        for m in messages
    ]
    if not msgs or msgs[-1]["role"] != "assistant":
        return msgs

    last = msgs.pop()
    tail = "\n\n【上一轮回复，供继续参考】\n" + str(last.get("content") or "")
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] = str(msgs[-1].get("content") or "") + tail
    else:
        msgs.append({"role": "user", "content": "请基于以上内容继续完成本次任务。" + tail})
    return msgs


def _progress(on_progress: ProgressFn, text: str) -> None:
    if on_progress is None:
        return
    try:
        on_progress(text)
    except Exception:  # noqa: BLE001
        logger.debug("lite fallback progress callback failed", exc_info=True)


def _post_json(url: str, payload: dict, api_key: str, timeout_s: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def call_lite(
    messages: list[dict],
    *,
    api_key: str,
    base_url: str,
    http_post: Callable[[str, dict, str, float], dict] | None = None,
) -> AIReply:
    """Send one non-streaming request to the spare model and return its reply."""
    url = (base_url or "").rstrip("/") + "/chat/completions"
    payload = {
        "model": LITE_MODEL,
        "messages": shape_user_tail(messages),
        "max_tokens": LITE_MAX_TOKENS,
        "reasoning_effort": LITE_REASONING_EFFORT,
        "stream": False,
        # NB: the string `thinking` field documented by the vendor is rejected
        # by this gateway (400 invalid arguments), so it is never sent.
    }
    poster = http_post or _post_json
    t0 = time.monotonic()
    body = poster(url, payload, api_key, LITE_TIMEOUT_S)
    latency_ms = (time.monotonic() - t0) * 1000.0

    choices = body.get("choices") or [{}]
    message = choices[0].get("message") or {}
    usage = body.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    return AIReply(
        content=str(message.get("content") or ""),
        reasoning_content=str(message.get("reasoning") or message.get("reasoning_content") or ""),
        raw=body,
        usage=AIUsage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            cached_prompt_tokens=int(prompt_details.get("cached_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        ),
        request_id=str(body.get("id") or ""),
        latency_ms=latency_ms,
    )


def try_lite_stage(
    messages: list[dict],
    *,
    api_key: str,
    base_url: str,
    cancel_token: Any = None,
    on_reasoning_token: TokenFn = None,
    on_content_token: TokenFn = None,
    on_progress: ProgressFn = None,
    http_post: Callable[[str, dict, str, float], dict] | None = None,
) -> AIReply | None:
    """Try the spare model once; return ``None`` when it cannot deliver.

    Cancellation is checked before the call and re-raised. Everything else
    (network, HTTP, empty body) returns ``None`` so the caller can fall through
    to the free pool without changing its behaviour.
    """
    if not is_lite_eligible(base_url):
        return None
    if not str(api_key or "").strip():
        logger.info("lite fallback skipped: no primary api key")
        return None

    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        raise CancelledError("Request cancelled before the spare-channel call")

    _progress(on_progress, "[提示] 主通道限流，已自动切换备用通道继续分析。\n")

    try:
        reply = call_lite(messages, api_key=api_key, base_url=base_url, http_post=http_post)
    except CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("lite spare channel failed: %s", exc)
        return None

    if not (reply.content or "").strip():
        # Same failure mode as the budget/shape problem: 200 with nothing in it.
        logger.warning(
            "lite spare channel returned an empty body (prompt_tokens=%s); falling through",
            reply.usage.prompt_tokens,
        )
        return None

    # Non-streaming transport: replay both channels so the pane still shows the
    # reasoning and the answer instead of nothing.
    if reply.reasoning_content and on_reasoning_token is not None:
        on_reasoning_token(reply.reasoning_content)
    if reply.content and on_content_token is not None:
        on_content_token(reply.content)

    logger.warning(
        "lite spare channel answered: model=%s prompt=%s completion=%s %.0fms",
        LITE_MODEL,
        reply.usage.prompt_tokens,
        reply.usage.completion_tokens,
        reply.latency_ms,
    )
    return reply
