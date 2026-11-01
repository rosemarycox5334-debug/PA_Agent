"""FreeChatSession — post-analysis free-chat session.

Maintains a conversation history anchored to a completed two-stage
AnalysisRecord and sends follow-up messages to the DeepSeek API.

Design reference: design.md §B.17
"""
from __future__ import annotations

import logging
import json
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from pa_agent.ai.deepseek_client import DeepSeekClient
    from pa_agent.ai.prompt_assembler import PromptAssembler
    from pa_agent.ai.session_ledger import SessionTokenLedger
    from pa_agent.config.settings import Settings
    from pa_agent.records.pending_writer import PendingWriter

from pa_agent.ai.deepseek_client import AIReply
from pa_agent.ai.sensenova_fallback import NEUTRAL_PROGRESS_TEXT
from pa_agent.records.schema import AnalysisRecord, FollowupTurn
from pa_agent.util.threading import CancelToken
from pa_agent.util.timefmt import now_local_ms

logger = logging.getLogger(__name__)


def _derive_record_id(record: AnalysisRecord) -> str:
    """Derive the record basename (without extension) from an AnalysisRecord.

    Uses the same logic as ``_build_basename`` in pending_writer.py.
    """
    from datetime import datetime, timezone

    ms = record.meta.timestamp_local_ms
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()
    ts_str = dt.strftime("%Y-%m-%d_%H-%m-%S")
    symbol = record.meta.symbol
    timeframe = record.meta.timeframe
    return f"{ts_str}_{symbol}_{timeframe}"


def _strip_reasoning(message: dict) -> dict:
    """Return a copy of *message* without the ``reasoning_content`` key."""
    return {k: v for k, v in message.items() if k != "reasoning_content"}


def _accept_any_reply(reply: object) -> object:
    """Free-chat has no JSON contract: any reply counts as a valid pass.

    The two-stage pipeline validates stage JSON here; a follow-up answer is
    plain prose, so the only thing worth rejecting is an empty reply (handled
    by the caller after the call).
    """
    from pa_agent.ai.json_validator import Ok

    return Ok({"content": getattr(reply, "content", "") or ""})


def _progress(on_progress: "Callable[[str], None] | None", text: str) -> None:
    """Best-effort progress line; a broken callback never breaks the answer."""
    if on_progress is None:
        return
    try:
        on_progress(text)
    except Exception:  # noqa: BLE001
        logger.debug("free-chat progress callback failed", exc_info=True)



class FreeChatSession:
    """Manages a free-chat conversation anchored to a completed analysis.

    Parameters
    ----------
    base_record:
        The fully completed AnalysisRecord from the two-stage pipeline.
    client:
        DeepSeekClient instance for API calls.
    assembler:
        PromptAssembler kept for future use. Follow-up chat builds its own
        advisory prompt instead of reusing the Stage 2 decision contract.
    pending_writer:
        PendingWriter for appending FollowupTurn entries to the JSONL
        sidecar file.
    ledger:
        SessionTokenLedger for accumulating token usage and cost.
    settings:
        Optional Settings object; used for ``reasoning_effort`` forwarding.
    kline_snapshot_fn:
        Optional callable that returns the latest closed K-line data as a
        text table string.  Called on each ``send()`` so the AI always
        sees the most recent market data.
    """

    #: When True, ``reasoning_content`` is preserved in assistant messages
    #: sent back to the API (for future tool-call scenarios).
    keep_reasoning_in_resend: bool = False

    def __init__(
        self,
        base_record: AnalysisRecord,
        client: "DeepSeekClient",
        assembler: "PromptAssembler",
        pending_writer: "PendingWriter",
        ledger: "SessionTokenLedger",
        settings: Optional["Settings"] = None,
        kline_snapshot_fn: Optional[Callable[[], str]] = None,
    ) -> None:
        self._base_record = base_record
        self._client = client
        self._assembler = assembler
        self._pending_writer = pending_writer
        self._ledger = ledger
        self._settings = settings
        self._kline_snapshot_fn = kline_snapshot_fn

        # In-memory provider override once a layered fallback succeeded; never
        # written back to settings.json.
        self._active_provider: Any = None
        #: Diagnostic marker: "free" / "provider" once a spare channel answered
        #: this session, empty while the primary provider keeps working.
        self.spare_channel_used: str = ""

        # Turn counter — incremented before each send so the first turn is 1.
        self._turn: int = 0

        # Full history including reasoning_content (for UI display and
        # persistence).  Each entry is a plain dict with at least
        # ``role`` and ``content``; assistant entries also carry
        # ``reasoning_content``.
        self._history_full: list[dict] = []

        # Derived record ID used as the JSONL sidecar basename.
        self._record_id: str = _derive_record_id(base_record)

        # ── Pre-build stable prefix (cached for all turns in this session) ────
        # These three messages are byte-for-byte identical across every turn of
        # the same session, so they form a stable prefix that the API can cache.
        # Building them once at session start avoids repeated JSON serialisation.
        self._cached_prefix: list[dict] = self._build_prefix(base_record)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def history_full(self) -> list[dict]:
        """Read-only view of the full message history (includes reasoning)."""
        return list(self._history_full)

    @property
    def record_id(self) -> str:
        """The record basename used for the JSONL sidecar file."""
        return self._record_id

    @staticmethod
    def _build_prefix(base_record: AnalysisRecord) -> list[dict]:
        """Build the stable prefix messages for this session (built once, reused each turn).

        Structure:
          [0] system  — follow-up advisor instructions (fully static across sessions)
          [1] user    — compact analysis reference (static within this session;
                        meta timestamps removed so the block stays stable)
          [2] assistant — Stage 2 original AI reply (static within this session;
                        lets the model see its own prose, not just the parsed JSON)

        Keeping these byte-identical across all turns of the same session means
        the API prefix cache is warm from turn 2 onwards, cutting prompt token
        cost significantly for multi-turn follow-up conversations.
        """
        prefix: list[dict] = []

        # [0] System — completely static, shared across all sessions
        prefix.append(
            {
                "role": "system",
                "content": (
                    "你是 PA Agent 的【追问助手】（post-analysis advisor），不是在执行新的完整两阶段分析。\n"
                    "你的目标是：优先、直接回答用户当前问题；必要时引用价格行为/关键价位/风险控制。\n"
                    "\n"
                    "严格规则：\n"
                    "1) 默认用自然语言回答；除非用户明确要求 JSON/决策树，否则不要输出二元决策树 JSON。\n"
                    "2) 如果用户问的是【已有仓位管理】（止损/止盈/减仓/持有/加仓）：\n"
                    "   - 只围绕持仓管理回答，不要重新跑完整下单决策。\n"
                    "   - 先给结论（可以/不建议/条件允许），再给依据（结构/关键位/信号），再给风险控制（最大亏损、触发条件）。\n"
                    "3) 如果用户问题信息不足，最多问 1-2 个澄清点（例如仓位大小、入场价、止损距离）。\n"
                    "4) 不要编造数据；以用户消息附带的「当前图表K线数据」为准（与发送追问时屏幕上冻结的图表一致）。\n"
                    "5) K线棒型描述（上影线/下影线/实体大小/涨跌方向）必须以「K1数据·程序计算」字段中的数值为准，\n"
                    "   禁止凭记忆或猜测描述棒型特征——程序计算的 upper_wick/lower_wick/body 是唯一可信来源。\n"
                ),
            }
        )

        # [1] User — compact analysis reference (stable within this session)
        # Exclude volatile meta fields (timestamps, api_key) that change every run
        # and would break prefix caching across analysis records.
        meta = getattr(base_record, "meta", None)
        meta_stable: dict = {}
        if meta is not None:
            raw = meta.model_dump()
            meta_stable = {
                "symbol": raw.get("symbol", ""),
                "timeframe": raw.get("timeframe", ""),
                "bar_count": raw.get("bar_count", 0),
                "decision_stance": raw.get("decision_stance", ""),
                "model": (raw.get("ai_provider") or {}).get("model", ""),
            }
        s1 = getattr(base_record, "stage1_diagnosis", None)
        s2 = getattr(base_record, "stage2_decision", None)
        ref = {
            "meta": meta_stable,
            "stage1_diagnosis": s1 or {},
            "stage2_decision": s2 or {},
        }
        prefix.append(
            {
                "role": "user",
                "content": (
                    "## 上次分析结果（仅供参考，不是新的决策任务）\n\n"
                    f"```json\n{json.dumps(ref, ensure_ascii=False, indent=2)}\n```\n"
                ),
            }
        )

        # [2] Assistant — synthesise a reliable recall summary from the *parsed*
        # stage2_decision (program-validated JSON), NOT from stage2_response.content
        # (the raw model output which may contain hallucinated descriptions such as
        # wrong bar types or non-existent wick characteristics).
        #
        # Using the raw response content is dangerous: the model treats whatever it
        # previously said as ground truth, so any hallucination in the original reply
        # (e.g. "K1 has a long upper wick") gets recycled verbatim in follow-up turns.
        # The parsed stage2_decision has already been validated and normalised by the
        # program, so it is the authoritative source for follow-up context.
        s2_decision = getattr(base_record, "stage2_decision", None) or {}
        kline_data = getattr(base_record, "kline_data", None) or []
        # Build a concise, factual assistant recall message from validated fields only.
        recall_parts: list[str] = []
        decision = s2_decision.get("decision") or {}
        order_type = decision.get("order_type", "不下单")
        order_dir = decision.get("order_direction")
        reasoning = decision.get("reasoning", "")
        watch_points = decision.get("watch_points") or []
        bar_analysis = s2_decision.get("bar_analysis") or {}
        # ── K1 factual description (derived directly from kline_data, not from
        #    the model's potentially erroneous prose) ──────────────────────────
        k1_desc = ""
        if kline_data:
            k1 = kline_data[0]  # seq=1, newest closed bar
            k1_open = k1.get("open", 0)
            k1_high = k1.get("high", 0)
            k1_low = k1.get("low", 0)
            k1_close = k1.get("close", 0)
            k1_vol = k1.get("volume", 0)
            upper_wick = round(k1_high - max(k1_open, k1_close), 3)
            lower_wick = round(min(k1_open, k1_close) - k1_low, 3)
            body = round(abs(k1_close - k1_open), 3)
            full_range = round(k1_high - k1_low, 3)
            body_ratio = round(body / full_range, 2) if full_range > 0 else 0
            direction_zh = "阴线" if k1_close < k1_open else ("阳线" if k1_close > k1_open else "平盘")
            k1_bar_type = bar_analysis.get("bar_type", "")
            k1_desc = (
                f"K1（最新已收盘）：{direction_zh}，开={k1_open}，高={k1_high}，"
                f"低={k1_low}，收={k1_close}，量={k1_vol}；"
                f"实体={body}（占比{body_ratio:.0%}），上影={upper_wick}，下影={lower_wick}；"
                f"程序分类：{k1_bar_type}。"
            )
        recall_parts.append(f"【上次决策结果】{order_type}" + (f"（{order_dir}）" if order_dir else ""))
        if k1_desc:
            recall_parts.append(f"【K1数据·程序计算】{k1_desc}")
        if reasoning:
            # Truncate to avoid token bloat; the key facts are already in k1_desc
            recall_parts.append(f"【决策推理摘要】{reasoning[:600]}" + ("…" if len(reasoning) > 600 else ""))
        if watch_points:
            recall_parts.append("【关注点】" + "；".join(watch_points[:3]))
        recall_content = "\n".join(recall_parts)
        if recall_content.strip():
            prefix.append(
                {
                    "role": "assistant",
                    "content": recall_content,
                }
            )

        return prefix

    # ── Layered spare channels ────────────────────────────────────────────────

    def _chat_with_spare_channels(
        self,
        messages: list[dict],
        *,
        cancel_token: CancelToken,
        on_reasoning_token: "Callable[[str], None] | None",
        on_content_token: "Callable[[str], None] | None",
        reasoning_effort: str,
        on_progress: "Callable[[str], None] | None" = None,
    ) -> AIReply:
        """Ask the primary provider, then the layered spare channels.

        Layer 0 — spare channel (free pool): only for an eligible SenseNova
                  primary, exactly like the two-stage pipeline.
        Layer 1 — ``settings.provider.fallbacks`` (e.g. glm-4-flash), applied in
                  memory only; settings.json is never rewritten.

        A follow-up answer carries no JSON contract, so the free lane accepts
        any non-empty prose instead of validating a stage schema. Cancellation
        and interrupt errors always propagate untouched.
        """
        from pa_agent.ai.deepseek_client import CancelledError as _CancelledError
        from pa_agent.ai.sensenova_fallback import is_cancel_or_interrupt as _is_cancel

        primary_exc: BaseException | None = None
        try:
            return self._client.stream_chat(
                messages,
                on_reasoning_token=on_reasoning_token,
                on_content_token=on_content_token,
                cancel_token=cancel_token,
                reasoning_effort=reasoning_effort,
            )
        except _CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if _is_cancel(exc):
                raise
            primary_exc = exc

        logger.warning(
            "free-chat primary provider failed (%s); trying spare channels",
            type(primary_exc).__name__,
        )
        _progress(on_progress, NEUTRAL_PROGRESS_TEXT + "\n")

        # Rate limits get the spare model first: same gateway and key, so no
        # credential switch, and it does not consume the tpm/rpm budget the
        # primary just tripped.
        from pa_agent.ai.rate_limit import is_rate_limit_error

        if is_rate_limit_error(primary_exc):
            reply = self._try_spare_model(
                messages,
                cancel_token=cancel_token,
                on_reasoning_token=on_reasoning_token,
                on_content_token=on_content_token,
                on_progress=on_progress,
            )
            if reply is not None:
                return reply

        reply = self._try_free_pool(
            messages,
            cancel_token=cancel_token,
            on_reasoning_token=on_reasoning_token,
            on_content_token=on_content_token,
            on_progress=on_progress,
        )
        if reply is not None:
            return reply

        reply = self._try_provider_fallback(
            messages,
            cancel_token=cancel_token,
            on_reasoning_token=on_reasoning_token,
            on_content_token=on_content_token,
            on_progress=on_progress,
        )
        if reply is not None:
            return reply

        assert primary_exc is not None
        raise primary_exc

    def _try_spare_model(
        self,
        messages: list[dict],
        *,
        cancel_token: CancelToken,
        on_reasoning_token: "Callable[[str], None] | None",
        on_content_token: "Callable[[str], None] | None",
        on_progress: "Callable[[str], None] | None",
    ) -> AIReply | None:
        """Layer 0a: the spare model, for rate-limited primaries only.

        Shares the primary gateway and key, so nothing has to be swapped; the
        empty-body pitfalls are handled inside ``pa_agent.ai.lite_fallback``.
        """
        if self._settings is None:
            return None
        provider = getattr(self._settings, "provider", None)
        if provider is None:
            return None

        from pa_agent.ai.lite_fallback import try_lite_stage

        try:
            reply = try_lite_stage(
                messages,
                api_key=str(getattr(provider, "api_key", "") or ""),
                base_url=str(getattr(provider, "base_url", "") or ""),
                cancel_token=cancel_token,
                on_reasoning_token=on_reasoning_token,
                on_content_token=on_content_token,
                on_progress=on_progress,
            )
        except BaseException as exc:  # noqa: BLE001
            from pa_agent.ai.sensenova_fallback import is_cancel_or_interrupt as _is_cancel

            if _is_cancel(exc):
                raise
            logger.warning("free-chat spare-model attempt failed: %s", exc)
            return None
        if reply is not None:
            self.spare_channel_used = "spare_model"
        return reply

    def _try_free_pool(
        self,
        messages: list[dict],
        *,
        cancel_token: CancelToken,
        on_reasoning_token: "Callable[[str], None] | None",
        on_content_token: "Callable[[str], None] | None",
        on_progress: "Callable[[str], None] | None",
    ) -> AIReply | None:
        """Layer 0: rotate the free pool and accept the first usable answer."""
        if self._settings is None:
            return None
        provider = getattr(self._settings, "provider", None)
        if provider is None:
            return None

        from pa_agent.ai.sensenova_fallback import (
            FreeStageSuccess,
            is_cancel_or_interrupt as _is_cancel,
            is_sensenova_fallback_eligible,
            run_free_stage_fallback,
        )

        if not is_sensenova_fallback_eligible(getattr(provider, "base_url", "") or ""):
            return None

        try:
            outcome = run_free_stage_fallback(
                messages=list(messages),
                validate=_accept_any_reply,
                primary_api_key=str(getattr(provider, "api_key", "") or ""),
                cancel_token=cancel_token,
                on_reasoning_token=on_reasoning_token,
                on_content_token=on_content_token,
                on_progress=on_progress,
                stream=True,
            )
        except BaseException as exc:  # noqa: BLE001
            if _is_cancel(exc):
                raise
            logger.warning("free-chat spare-channel rotation failed: %s", exc)
            return None

        if isinstance(outcome, FreeStageSuccess):
            reply = getattr(outcome, "reply", None)
            if reply is not None and str(getattr(reply, "content", "") or "").strip():
                self.spare_channel_used = "free"
                logger.warning(
                    "free-chat follow-up answered by spare channel: %s",
                    getattr(getattr(outcome, "candidate", None), "model", "?"),
                )
                return reply
        return None

    def _try_provider_fallback(
        self,
        messages: list[dict],
        *,
        cancel_token: CancelToken,
        on_reasoning_token: "Callable[[str], None] | None",
        on_content_token: "Callable[[str], None] | None",
        on_progress: "Callable[[str], None] | None",
    ) -> AIReply | None:
        """Layer 1: switch the in-memory provider to the configured fallbacks."""
        if self._settings is None:
            return None
        provider = getattr(self._settings, "provider", None)
        if provider is None:
            return None

        from pa_agent.ai.glm_backup import ensure_glm_fallback
        from pa_agent.ai.sensenova_fallback import is_cancel_or_interrupt as _is_cancel

        try:
            ensure_glm_fallback(provider)
        except Exception:  # noqa: BLE001
            logger.debug("free-chat ensure_glm_fallback failed", exc_info=True)

        chain = list(getattr(provider, "fallbacks", []) or [])
        if not chain:
            return None

        current = self._active_provider if self._active_provider is not None else provider
        tried: set[tuple[str, str]] = {
            (
                str(getattr(current, "base_url", "") or "").strip(),
                str(getattr(current, "model", "") or "").strip(),
            )
        }
        for cand in chain:
            key = (
                str(getattr(cand, "base_url", "") or "").strip(),
                str(getattr(cand, "model", "") or "").strip(),
            )
            if key in tried:
                continue
            tried.add(key)
            if not str(getattr(cand, "api_key", "") or "").strip():
                logger.warning(
                    "free-chat provider fallback skipped (empty api_key): model=%s",
                    getattr(cand, "model", "?"),
                )
                continue
            try:
                self._client.update_provider(cand)
            except Exception as exc:  # noqa: BLE001
                logger.warning("free-chat provider fallback update failed: %s", exc)
                continue
            _progress(on_progress, NEUTRAL_PROGRESS_TEXT + "\n")
            try:
                reply = self._client.stream_chat(
                    messages,
                    on_reasoning_token=on_reasoning_token,
                    on_content_token=on_content_token,
                    cancel_token=cancel_token,
                )
            except BaseException as exc:  # noqa: BLE001
                if _is_cancel(exc):
                    raise
                logger.warning(
                    "free-chat provider fallback failed: model=%s (%s)",
                    getattr(cand, "model", "?"),
                    exc,
                )
                continue
            if reply is not None and str(getattr(reply, "content", "") or "").strip():
                self._active_provider = cand
                self.spare_channel_used = "provider"
                logger.warning(
                    "free-chat follow-up answered by provider fallback: model=%s",
                    getattr(cand, "model", "?"),
                )
                return reply
        return None

    def send(
        self,
        user_text: str,
        cancel_token: CancelToken,
        on_reasoning_token: "Callable[[str], None] | None" = None,
        on_content_token: "Callable[[str], None] | None" = None,
        on_progress: "Callable[[str], None] | None" = None,
    ) -> AIReply:
        """Send *user_text* to the AI and return the reply.

        Steps
        -----
        1. Build ``history_for_api`` from:
           - A follow-up advisory system prompt.
           - A compact reference summary of the completed analysis.
           - All previous free-chat turns.
           - New user message
        2. Call ``client.chat(history_for_api, cancel_token=cancel_token)``.
        3. Append to ``_history_full`` (with ``reasoning_content`` preserved).
        4. Call ``ledger.add(reply.usage)`` and
           ``pending_writer.append_followup(record_id, turn)``.
        5. Return the AIReply.

        When *cancel_token* is already set before the call, a
        ``FollowupTurn`` with ``cancelled=True`` is persisted and the
        ``CancelledError`` is re-raised.
        """
        self._turn += 1
        turn_number = self._turn

        # ── 1. Build history_for_api ──────────────────────────────────────────
        history_for_api: list[dict] = list(self._cached_prefix)  # copy stable prefix

        # Previous free-chat turns from history_full
        preserve_mimo = False
        if self._settings is not None:
            from pa_agent.ai.mimo_compat import is_mimo_provider

            provider = getattr(self._settings, "provider", None)
            if provider is not None:
                preserve_mimo = is_mimo_provider(
                    getattr(provider, "base_url", ""),
                    getattr(provider, "model", ""),
                )
        for msg in self._history_full:
            if msg["role"] == "user":
                history_for_api.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                assistant_msg: dict = {"role": "assistant", "content": msg["content"]}
                if (self.keep_reasoning_in_resend or preserve_mimo) and msg.get(
                    "reasoning_content"
                ):
                    assistant_msg["reasoning_content"] = msg["reasoning_content"]
                history_for_api.append(assistant_msg)

        # New user message — prepend latest K-line snapshot if available
        user_content = user_text
        if self._kline_snapshot_fn is not None:
            try:
                kline_table = self._kline_snapshot_fn()
                if kline_table:
                    user_content = (
                        "## 当前图表K线数据（发送追问时已刷新并冻结图表，与屏幕一致）\n\n"
                        f"{kline_table}\n\n"
                        f"---\n\n{user_text}"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("kline_snapshot_fn failed: %s", exc)

        history_for_api.append({"role": "user", "content": user_content})

        # ── 2. Resolve reasoning_effort ───────────────────────────────────────
        reasoning_effort = "low"
        if self._settings is not None:
            reasoning_effort = getattr(
                self._settings.provider, "reasoning_effort", "low"
            )

        # ── 3. Check cancellation before API call ─────────────────────────────
        from pa_agent.ai.deepseek_client import CancelledError

        if cancel_token.is_set():
            # Persist a cancelled turn and re-raise
            cancelled_turn = FollowupTurn(
                turn=turn_number,
                ts_ms=now_local_ms(),
                user=user_text,
                ai_content="",
                ai_reasoning=None,
                usage={
                    "prompt_tokens": 0,
                    "cached_prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                cancelled=True,
            )
            self._pending_writer.append_followup(self._record_id, cancelled_turn)
            raise CancelledError("FreeChatSession.send cancelled before API call")

        # ── 4. Call the API (streaming, layered spare channels) ───────────────
        try:
            reply = self._chat_with_spare_channels(
                history_for_api,
                cancel_token=cancel_token,
                on_reasoning_token=on_reasoning_token,
                on_content_token=on_content_token,
                reasoning_effort=reasoning_effort,
                on_progress=on_progress,
            )
        except CancelledError:
            # Persist a cancelled turn and re-raise
            cancelled_turn = FollowupTurn(
                turn=turn_number,
                ts_ms=now_local_ms(),
                user=user_text,
                ai_content="",
                ai_reasoning=None,
                usage={
                    "prompt_tokens": 0,
                    "cached_prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                cancelled=True,
            )
            self._pending_writer.append_followup(self._record_id, cancelled_turn)
            raise

        # ── 5. Append to history_full (with reasoning preserved) ──────────────
        self._history_full.append({"role": "user", "content": user_text})
        self._history_full.append({
            "role": "assistant",
            "content": reply.content,
            "reasoning_content": reply.reasoning_content,
        })

        # ── 6. Accumulate usage in ledger ─────────────────────────────────────
        self._ledger.add(reply.usage)

        # ── 7. Persist the followup turn ──────────────────────────────────────
        usage_dict = {
            "prompt_tokens": reply.usage.prompt_tokens,
            "cached_prompt_tokens": reply.usage.cached_prompt_tokens,
            "completion_tokens": reply.usage.completion_tokens,
            "total_tokens": reply.usage.total_tokens,
        }
        followup_turn = FollowupTurn(
            turn=turn_number,
            ts_ms=now_local_ms(),
            user=user_text,
            ai_content=reply.content,
            ai_reasoning=reply.reasoning_content or None,
            usage=usage_dict,
            cancelled=False,
        )
        self._pending_writer.append_followup(self._record_id, followup_turn)

        logger.debug(
            "FreeChatSession.send: turn=%d tokens=%d/%d",
            turn_number,
            reply.usage.prompt_tokens,
            reply.usage.completion_tokens,
        )

        return reply
