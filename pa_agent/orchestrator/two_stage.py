"""Two-stage AI analysis orchestrator.

Coordinates the full Stage 1 (diagnosis) → Stage 2 (decision) pipeline:
  1. Build Stage 1 prompt via PromptAssembler
  2. Call DeepSeekClient
  3. Validate Stage 1 JSON
  4. Route strategy files
  5. Load experience entries
  6. Build Stage 2 prompt
  7. Call DeepSeekClient
  8. Validate Stage 2 JSON
  9. Persist full record

Cancel checks are performed before each stage and after each API call.
Network/timeout errors are caught and recorded on the partial record.

Stage 2 validation failure ends the run immediately (``STAGE2_VALIDATION_AUTO_RETRY``
is always False): no second ``stream_chat`` and no automatic fix-and-revalidate loop.
"""
from __future__ import annotations

# Explicit policy: never re-call Stage 2 API after validation failure.
STAGE2_VALIDATION_AUTO_RETRY = False

import dataclasses
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from pa_agent.ai.deepseek_client import DeepSeekClient
    from pa_agent.ai.json_validator import JsonValidator
    from pa_agent.ai.prompt_assembler import PromptAssembler
    from pa_agent.config.settings import Settings
    from pa_agent.records.experience_reader import ExperienceReader
    from pa_agent.records.pending_writer import PendingWriter

from pa_agent.ai.json_validator import Ok, ValidationError
from pa_agent.data.base import KlineFrame
from pa_agent.records.schema import AnalysisRecord, RecordMeta
from pa_agent.util.threading import CancelToken, OrchestratorEvent
from pa_agent.util.timefmt import now_local_ms

logger = logging.getLogger(__name__)


def _latency_ms_label(latency_ms: object) -> str:
    """Format API latency for console logs; tolerate mocks or missing values."""
    try:
        return f"{float(latency_ms):.0f}ms"
    except (TypeError, ValueError):
        return "?"

# When the gateway buffers the full reply, emit pseudo-stream chunks to the UI.
_FALLBACK_STREAM_CHUNK = 48


def _json_truncation_hint(content: str, err: ValidationError) -> str | None:
    """Detect incomplete JSON (stream stopped mid-object) vs a stray syntax typo."""
    if err.category != "a":
        return None
    stripped = (content or "").strip()
    if not stripped.startswith("{"):
        return None
    depth = 0
    in_string = False
    escape = False
    for ch in stripped:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    if depth > 0 or not stripped.rstrip().endswith("}"):
        return (
            f"阶段 JSON 正文约 {len(stripped)} 字符，在输出过程中被截断"
            f"（未闭合对象约 {max(depth, 1)} 层，解析位置 {err.parse_position}）。"
            " 常见原因：completion 额度主要在思考区用尽，正文 JSON 只写了一小段。"
        )
    return None


def _enrich_stage2_validation_message(err: ValidationError, reply: Any) -> str:
    """Add actionable context for empty content or truncated JSON."""
    from pa_agent.ai.validation_messages import format_validation_errors

    detail = format_validation_errors(
        err.invalid_fields, missing_fields=err.missing_fields
    )
    content = (getattr(reply, "content", None) or "").strip()
    trunc = _json_truncation_hint(content, err)
    if trunc:
        usage = getattr(reply, "usage", None)
        completion = getattr(usage, "completion_tokens", 0) if usage else 0
        reasoning_len = len(getattr(reply, "reasoning_content", None) or "")
        msg = f"{err.message}。{trunc} completion_tokens≈{completion}，思考区约 {reasoning_len} 字。"
        return f"{msg}。{detail}" if detail else msg
    if err.category != "d" or (err.raw_text or "").strip():
        return f"{err.message}。{detail}" if detail else err.message
    if content:
        return f"{err.message}。{detail}" if detail else err.message
    reasoning = getattr(reply, "reasoning_content", None) or ""
    usage = getattr(reply, "usage", None)
    completion = getattr(usage, "completion_tokens", 0) if usage else 0
    if reasoning and "{" not in reasoning:
        return (
            f"{err.message}：扩展思考已输出约 {len(reasoning)} 字，但正文 content 为空，"
            f"且思考中未见 JSON（completion_tokens≈{completion}）。"
            " 常见原因是思考在输出阶段二 JSON 前被截断或网关提前结束流。"
            " 请缩短 prompt、检查 Packy 分组限额，或调整模型/Reasoning Effort 后重新分析。"
        )
    if reasoning:
        return (
            f"{err.message}：正文 content 为空；请把阶段二 JSON 写在 content 正文，"
            "不要只写在思考区。"
        )
    return f"{err.message}。{detail}" if detail else err.message


def _enrich_stage1_validation_message(err: ValidationError, reply: Any) -> str:
    """Add actionable context for empty content or truncated JSON."""
    from pa_agent.ai.validation_messages import format_validation_errors

    detail = format_validation_errors(
        err.invalid_fields, missing_fields=err.missing_fields
    )
    content = (getattr(reply, "content", None) or "").strip()
    trunc = _json_truncation_hint(content, err)
    if trunc:
        usage = getattr(reply, "usage", None)
        completion = getattr(usage, "completion_tokens", 0) if usage else 0
        reasoning_len = len(getattr(reply, "reasoning_content", None) or "")
        msg = f"{err.message}。{trunc} completion_tokens≈{completion}，思考区约 {reasoning_len} 字。"
        return f"{msg}。{detail}" if detail else msg
    if err.category != "d" or (err.raw_text or "").strip():
        return f"{err.message}。{detail}" if detail else err.message
    if content:
        return f"{err.message}。{detail}" if detail else err.message
    reasoning = getattr(reply, "reasoning_content", None) or ""
    usage = getattr(reply, "usage", None)
    completion = getattr(usage, "completion_tokens", 0) if usage else 0
    if reasoning and "{" not in reasoning:
        return (
            f"{err.message}：扩展思考已输出约 {len(reasoning)} 字，但正文 content 为空，"
            f"且思考中未见 JSON（completion_tokens≈{completion}）。"
            " 常见原因是思考占满输出额度后被截断。"
            " 请缩短 prompt、检查网关输出上限，或调整 Reasoning Effort 后重新分析。"
        )
    if reasoning:
        return (
            f"{err.message}：正文 content 为空；请把阶段一 JSON 写在 content 正文，"
            "不要只写在思考区。"
        )
    return f"{err.message}。{detail}" if detail else err.message


def _emit_buffered_stream(
    text: str,
    on_token: Callable[[str], None] | None,
    *,
    chunk_size: int = _FALLBACK_STREAM_CHUNK,
) -> bool:
    """Push *text* through *on_token* in slices if the API did not stream deltas."""
    if on_token is None or not text:
        return False
    for i in range(0, len(text), chunk_size):
        on_token(text[i : i + chunk_size])
    return True


def _build_empty_record(
    frame: KlineFrame,
    settings: Optional["Settings"],
) -> AnalysisRecord:
    """Build a partial AnalysisRecord with meta populated from the frame."""
    ts_ms = now_local_ms()
    ts_iso = datetime.fromtimestamp(ts_ms / 1000).isoformat(timespec="milliseconds")

    # Build masked provider snapshot
    ai_provider: dict[str, Any] = {}
    if settings is not None:
        from pa_agent.security.secret_store import mask_secret
        p = settings.provider
        ai_provider = {
            "model": p.model,
            "base_url": p.base_url,
            "api_key": mask_secret(p.api_key) if p.api_key else "****",
            "thinking": p.thinking,
            "reasoning_effort": p.reasoning_effort,
            "context_window": p.context_window,
        }

    # Serialize kline bars
    kline_data: list[dict] = []
    for bar in frame.bars:
        if dataclasses.is_dataclass(bar) and not isinstance(bar, type):
            kline_data.append(dataclasses.asdict(bar))
        else:
            kline_data.append(bar.__dict__)

    from pa_agent.ai.decision_stance import normalize_stance

    decision_stance = "conservative"
    if settings is not None:
        decision_stance = normalize_stance(
            getattr(settings.general, "decision_stance", "conservative")
        )

    meta = RecordMeta(
        timestamp_local_iso=ts_iso,
        timestamp_local_ms=ts_ms,
        symbol=frame.symbol,
        timeframe=frame.timeframe,
        bar_count=len(frame.bars),
        ai_provider=ai_provider,
        decision_stance=decision_stance,
    )

    return AnalysisRecord(
        meta=meta,
        kline_data=kline_data,
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis=None,
        stage2_messages=[],
        stage2_response=None,
        stage2_decision=None,
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )


def _accumulate_usage(current: dict, reply_usage: Any) -> dict:
    """Merge an AIUsage object into the running usage_total dict."""
    result = dict(current)
    result["prompt_tokens"] = (
        result.get("prompt_tokens", 0) + getattr(reply_usage, "prompt_tokens", 0)
    )
    result["cached_prompt_tokens"] = (
        result.get("cached_prompt_tokens", 0)
        + getattr(reply_usage, "cached_prompt_tokens", 0)
    )
    result["completion_tokens"] = (
        result.get("completion_tokens", 0) + getattr(reply_usage, "completion_tokens", 0)
    )
    result["total_tokens"] = (
        result.get("total_tokens", 0) + getattr(reply_usage, "total_tokens", 0)
    )
    return result


class TwoStageOrchestrator:
    """Orchestrates the two-stage AI analysis pipeline.

    Parameters
    ----------
    client:
        DeepSeekClient instance for API calls.
    assembler:
        PromptAssembler for building Stage 1 and Stage 2 message lists.
    router:
        Either the ``route_strategy_files`` function or an object with a
        ``.route()`` method.
    validator:
        JsonValidator for validating Stage 1 and Stage 2 responses.
    pending_writer:
        PendingWriter for persisting full and partial records.
    exp_reader:
        ExperienceReader for loading top-5 experience entries.
    settings:
        Optional Settings object; used for ``ai_provider`` meta and
        ``reasoning_effort`` forwarding.
    """

    def __init__(
        self,
        client: "DeepSeekClient",
        assembler: "PromptAssembler",
        router: Any,
        validator: "JsonValidator",
        pending_writer: "PendingWriter",
        exp_reader: "ExperienceReader",
        settings: Optional["Settings"] = None,
    ) -> None:
        self._client = client
        self._assembler = assembler
        self._router = router
        self._validator = validator
        self._pending_writer = pending_writer
        self._exp_reader = exp_reader
        self._settings = settings

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(
        self,
        frame: KlineFrame,
        cancel_token: CancelToken,
        on_event: Callable[[OrchestratorEvent], None],
        *,
        on_stage1_reasoning: Callable[[str], None] | None = None,
        on_stage1_content: Callable[[str], None] | None = None,
        on_stage2_reasoning: Callable[[str], None] | None = None,
        on_stage2_content: Callable[[str], None] | None = None,
        on_stage_prompt: Callable[[str, str, str], None] | None = None,
        on_stage2_files: Callable[[list[str]], None] | None = None,
        previous_record: AnalysisRecord | None = None,
        incremental_new_bar_count: int | None = None,
    ) -> AnalysisRecord:
        """Run the two-stage analysis pipeline and return an AnalysisRecord.

        The ``on_event`` callback is called synchronously at each pipeline
        milestone.  The returned record is always fully populated with
        whatever data was collected before the pipeline terminated (whether
        by success, validation failure, cancellation, or network error).

        Parameters
        ----------
        frame:
            Immutable KlineFrame snapshot to analyse.
        cancel_token:
            Token checked before each stage and after each API call.
        on_event:
            Callback invoked with OrchestratorEvent values.

        Returns
        -------
        AnalysisRecord
            Fully or partially populated record.
        """
        # ── Step 1: Build partial record ──────────────────────────────────────
        record = _build_empty_record(frame, self._settings)

        # ── Step 2: Pre-Stage-1 cancel check ─────────────────────────────────
        if cancel_token.is_set():
            self._pending_writer.save_partial(record, "user_cancelled")
            on_event(OrchestratorEvent.Cancelled)
            return record

        # ── Step 3: Stage 1 started ───────────────────────────────────────────
        on_event(OrchestratorEvent.Stage1Started)

        # ── Step 4: Build Stage 1 messages ───────────────────────────────────
        if previous_record is not None and incremental_new_bar_count is not None:
            messages_s1 = self._assembler.build_incremental_stage1(
                frame,
                previous_record,
                incremental_new_bar_count,
            )
        else:
            messages_s1 = self._assembler.build_stage1(frame)

        # ── Step 5: Call AI for Stage 1 ───────────────────────────────────────
        logger.debug("\n" + "="*80)
        logger.debug("【Stage 1 发送的完整 Prompt】")
        logger.debug("="*80)
        for msg in messages_s1:
            role = msg.get("role", "?").upper()
            content = msg.get("content", "")
            logger.debug("\n--- [%s] ---\n%s", role, content)
        logger.debug("="*80 + "\n")

        # Notify conversation tab of the prompt being sent
        if on_stage_prompt is not None:
            s1_system = next((m.get("content", "") for m in messages_s1 if m.get("role") == "system"), "")
            s1_user = next((m.get("content", "") for m in messages_s1 if m.get("role") == "user"), "")
            on_stage_prompt("stage1", s1_system, s1_user)

        _thinking, _effort = self._thinking_params()
        s1_streamed_reasoning = False
        s1_streamed_content = False

        def _on_s1_reasoning(chunk: str) -> None:
            nonlocal s1_streamed_reasoning
            s1_streamed_reasoning = True
            if on_stage1_reasoning is not None:
                on_stage1_reasoning(chunk)

        def _on_s1_content(chunk: str) -> None:
            nonlocal s1_streamed_content
            s1_streamed_content = True
            if on_stage1_content is not None:
                on_stage1_content(chunk)

        try:
            reply_s1 = self._client.stream_chat(
                messages_s1,
                on_reasoning_token=_on_s1_reasoning,
                on_content_token=_on_s1_content,
                cancel_token=cancel_token,
                thinking=_thinking,
                reasoning_effort=_effort,
            )
        except Exception as exc:
            if self._is_network_error(exc):
                logger.warning("Stage 1 network error: %s", exc)
                record = record.model_copy(
                    update={
                        "stage1_messages": messages_s1,
                        "exception": {
                            "type": "network_error",
                            "stage": "stage1",
                            "message": str(exc),
                        },
                    }
                )
                self._pending_writer.save_partial(record, "network_error")
                on_event(OrchestratorEvent.Stage1Failed)
                return record
            raise

        if not s1_streamed_reasoning and reply_s1.reasoning_content:
            _emit_buffered_stream(reply_s1.reasoning_content, on_stage1_reasoning)
        if not s1_streamed_content and reply_s1.content:
            _emit_buffered_stream(reply_s1.content, on_stage1_content)

        # ── Step 6: Post-Stage-1-call cancel check ────────────────────────────
        if cancel_token.is_set():
            record = record.model_copy(
                update={
                    "stage1_messages": messages_s1,
                    "stage1_response": reply_s1.raw,
                    "usage_total": _accumulate_usage(record.usage_total, reply_s1.usage),
                }
            )
            self._pending_writer.save_partial(record, "user_cancelled")
            on_event(OrchestratorEvent.Cancelled)
            return record

        # ── Step 7: Validate Stage 1 ──────────────────────────────────────────
        logger.debug("\n" + "="*80)
        logger.debug("【Stage 1 AI 完整响应】")
        logger.debug("="*80)
        logger.debug(reply_s1.content)
        if reply_s1.reasoning_content:
            logger.debug("\n--- [思考过程] ---\n%s", reply_s1.reasoning_content)
        logger.debug(
            "\n--- [Token 用量] prompt=%s completion=%s latency=%s ---",
            reply_s1.usage.prompt_tokens,
            reply_s1.usage.completion_tokens,
            _latency_ms_label(reply_s1.latency_ms),
        )
        logger.debug("="*80 + "\n")

        prev_s1: dict[str, Any] | None = None
        if previous_record is not None and int(incremental_new_bar_count or 0) > 0:
            prev_s1 = previous_record.stage1_diagnosis

        result_s1 = self._validator.validate(
            "stage1",
            reply_s1.content,
            kline_frame=frame,
            incremental_new_bar_count=int(incremental_new_bar_count or 0),
            incremental_previous_stage1=prev_s1,
        )

        if isinstance(result_s1, ValidationError):
            err = result_s1
            err_message = _enrich_stage1_validation_message(err, reply_s1)
            logger.warning(
                "Stage 1 validation failed: category=%s message=%s",
                err.category,
                err_message,
            )
            record = record.model_copy(
                update={
                    "stage1_messages": messages_s1,
                    "stage1_response": reply_s1.raw,
                    "usage_total": _accumulate_usage(record.usage_total, reply_s1.usage),
                    "exception": {
                        "type": "validation_error",
                        "stage": "stage1",
                        "category": err.category,
                        "message": err_message,
                        "missing_fields": err.missing_fields,
                        "invalid_fields": err.invalid_fields,
                        "raw_text": err.raw_text,
                        "parse_position": err.parse_position,
                    },
                }
            )
            self._pending_writer.save_partial(record, f"stage1_{err.category}")
            on_event(OrchestratorEvent.Stage1Failed)
            return record

        # Validation passed — extract the parsed JSON
        assert isinstance(result_s1, Ok)
        stage1_json: dict = result_s1.obj

        # ── Step 9: Stage 1 done ──────────────────────────────────────────────
        on_event(OrchestratorEvent.Stage1Done)

        # ── Step 10: Route strategy files ─────────────────────────────────────
        if callable(self._router) and not hasattr(self._router, "route"):
            strategy_files: list[str] = self._router(stage1_json)
        else:
            strategy_files = self._router.route(stage1_json)

        # ── Step 11: Load experience entries ──────────────────────────────────
        cycle_position: str = stage1_json.get("cycle_position", "unknown")
        direction = str(stage1_json.get("direction", "") or "")
        patterns = stage1_json.get("detected_patterns") or []
        prompt_cfg = getattr(self._settings, "prompt", None) if self._settings else None
        max_exp = getattr(prompt_cfg, "experience_max_entries", 3) if prompt_cfg else 3
        max_chars = (
            getattr(prompt_cfg, "experience_max_chars_per_entry", 400) if prompt_cfg else 400
        )
        if hasattr(self._exp_reader, "read_for_stage2"):
            experience_entries = self._exp_reader.read_for_stage2(
                cycle_position,
                direction=direction,
                patterns=patterns,
                max_entries=max_exp,
                max_chars_per_entry=max_chars,
            )
        else:
            experience_entries = self._exp_reader.read_top5(cycle_position)[:max_exp]

        # ── Step 12: Pre-Stage-2 cancel check ────────────────────────────────
        if cancel_token.is_set():
            record = record.model_copy(
                update={
                    "stage1_messages": messages_s1,
                    "stage1_response": reply_s1.raw,
                    "stage1_diagnosis": stage1_json,
                    "strategy_files_used": strategy_files,
                    "experience_loaded": [
                        e.model_dump() if hasattr(e, "model_dump") else dict(e)
                        for e in experience_entries
                    ],
                    "usage_total": _accumulate_usage(record.usage_total, reply_s1.usage),
                }
            )
            self._pending_writer.save_partial(record, "user_cancelled")
            on_event(OrchestratorEvent.Cancelled)
            return record

        # ── Step 13: Stage 2 started ──────────────────────────────────────────
        on_event(OrchestratorEvent.Stage2Started)
        if on_stage2_files is not None:
            on_stage2_files(list(strategy_files))

        gate_result = str(stage1_json.get("gate_result", "proceed")).lower()
        if gate_result in ("wait", "unknown"):
            from pa_agent.ai.decision_tree import build_stage2_gate_wait_response

            if on_stage_prompt is not None:
                on_stage_prompt("stage2", "", "（阶段一闸门未通过，跳过阶段二模型调用）")
            short_msg = (
                f"阶段一 gate_result={gate_result}，程序已短路生成阶段二结果，"
                "未向模型发起请求。\n"
            )
            _emit_buffered_stream(short_msg, on_stage2_content)

            stage2_json = build_stage2_gate_wait_response(stage1_json)
            on_event(OrchestratorEvent.Stage2Done)
            logger.info("next_bar_prediction direction=null probs=null/null/null unpredictable=true (gate short-circuit)")
            usage_total = _accumulate_usage(record.usage_total, reply_s1.usage)
            record = record.model_copy(
                update={
                    "stage1_messages": messages_s1,
                    "stage1_response": reply_s1.raw,
                    "stage1_diagnosis": stage1_json,
                    "stage2_messages": [],
                    "stage2_response": None,
                    "stage2_decision": stage2_json,
                    "strategy_files_used": strategy_files,
                    "experience_loaded": [
                        e.model_dump() if hasattr(e, "model_dump") else dict(e)
                        for e in experience_entries
                    ],
                    "usage_total": usage_total,
                    "exception": None,
                }
            )
            self._pending_writer.save_full(record)
            on_event(OrchestratorEvent.RecordSaved)
            return record

        # ── Step 14: Build Stage 2 messages ───────────────────────────────────
        messages_s2 = self._assembler.build_stage2_continuation(
            frame=frame,
            stage1_messages=messages_s1,
            stage1_reply_content=reply_s1.content,
            stage1_json=stage1_json,
            strategy_files=strategy_files,
            experience_entries=experience_entries,
            decision_stance=record.meta.decision_stance,
            previous_record=previous_record,
        )

        # ── Step 15: Call AI for Stage 2 ──────────────────────────────────────
        logger.debug("\n" + "="*80)
        logger.debug("【Stage 2 发送的完整 Prompt】")
        logger.debug("="*80)
        for msg in messages_s2:
            role = msg.get("role", "?").upper()
            content = msg.get("content", "")
            logger.debug("\n--- [%s] ---\n%s", role, content)
        logger.debug("="*80 + "\n")

        # Notify conversation tab of the prompt being sent
        if on_stage_prompt is not None:
            s2_system = next((m.get("content", "") for m in messages_s2 if m.get("role") == "system"), "")
            s2_user = next((m.get("content", "") for m in reversed(messages_s2) if m.get("role") == "user"), "")
            on_stage_prompt("stage2", s2_system, s2_user)

        s2_streamed_reasoning = False
        s2_streamed_content = False

        def _on_s2_reasoning(chunk: str) -> None:
            nonlocal s2_streamed_reasoning
            s2_streamed_reasoning = True
            if on_stage2_reasoning is not None:
                on_stage2_reasoning(chunk)

        def _on_s2_content(chunk: str) -> None:
            nonlocal s2_streamed_content
            s2_streamed_content = True
            if on_stage2_content is not None:
                on_stage2_content(chunk)

        try:
            reply_s2 = self._client.stream_chat(
                messages_s2,
                on_reasoning_token=_on_s2_reasoning,
                on_content_token=_on_s2_content,
                cancel_token=cancel_token,
                thinking=_thinking,
                reasoning_effort=_effort,
            )
        except Exception as exc:
            if self._is_network_error(exc):
                logger.warning("Stage 2 network error: %s", exc)
                record = record.model_copy(
                    update={
                        "stage1_messages": messages_s1,
                        "stage1_response": reply_s1.raw,
                        "stage1_diagnosis": stage1_json,
                        "stage2_messages": messages_s2,
                        "strategy_files_used": strategy_files,
                        "experience_loaded": [
                            e.model_dump() if hasattr(e, "model_dump") else dict(e)
                            for e in experience_entries
                        ],
                        "usage_total": _accumulate_usage(record.usage_total, reply_s1.usage),
                        "exception": {
                            "type": "network_error",
                            "stage": "stage2",
                            "message": str(exc),
                        },
                    }
                )
                self._pending_writer.save_partial(record, "network_error")
                on_event(OrchestratorEvent.Stage2Failed)
                return record
            raise

        if not s2_streamed_reasoning and reply_s2.reasoning_content:
            _emit_buffered_stream(reply_s2.reasoning_content, on_stage2_reasoning)
        if not s2_streamed_content and reply_s2.content:
            _emit_buffered_stream(reply_s2.content, on_stage2_content)

        # ── Step 16: Post-Stage-2-call cancel check ───────────────────────────
        if cancel_token.is_set():
            record = record.model_copy(
                update={
                    "stage1_messages": messages_s1,
                    "stage1_response": reply_s1.raw,
                    "stage1_diagnosis": stage1_json,
                    "stage2_messages": messages_s2,
                    "stage2_response": reply_s2.raw,
                    "strategy_files_used": strategy_files,
                    "experience_loaded": [
                        e.model_dump() if hasattr(e, "model_dump") else dict(e)
                        for e in experience_entries
                    ],
                    "usage_total": _accumulate_usage(
                        _accumulate_usage(record.usage_total, reply_s1.usage),
                        reply_s2.usage,
                    ),
                }
            )
            self._pending_writer.save_partial(record, "user_cancelled")
            on_event(OrchestratorEvent.Cancelled)
            return record

        # ── Step 17: Validate Stage 2 ─────────────────────────────────────────
        logger.debug("\n" + "="*80)
        logger.debug("【Stage 2 AI 完整响应】")
        logger.debug("="*80)
        logger.debug(reply_s2.content)
        if reply_s2.reasoning_content:
            logger.debug("\n--- [思考过程] ---\n%s", reply_s2.reasoning_content)
        logger.debug(
            "\n--- [Token 用量] prompt=%s completion=%s latency=%s ---",
            reply_s2.usage.prompt_tokens,
            reply_s2.usage.completion_tokens,
            _latency_ms_label(reply_s2.latency_ms),
        )
        logger.debug("="*80 + "\n")

        result_s2 = self._validator.validate(
            "stage2",
            reply_s2.content,
            decision_stance=record.meta.decision_stance,
            kline_frame=frame,
            stage1_json=stage1_json,
        )

        if isinstance(result_s2, ValidationError):
            err = result_s2
            err_message = _enrich_stage2_validation_message(err, reply_s2)
            logger.warning(
                "Stage 2 validation failed: category=%s message=%s",
                err.category,
                err_message,
            )
            record = record.model_copy(
                update={
                    "stage1_messages": messages_s1,
                    "stage1_response": reply_s1.raw,
                    "stage1_diagnosis": stage1_json,
                    "stage2_messages": messages_s2,
                    "stage2_response": reply_s2.raw,
                    "strategy_files_used": strategy_files,
                    "experience_loaded": [
                        e.model_dump() if hasattr(e, "model_dump") else dict(e)
                        for e in experience_entries
                    ],
                    "usage_total": _accumulate_usage(
                        _accumulate_usage(record.usage_total, reply_s1.usage),
                        reply_s2.usage,
                    ),
                    "exception": {
                        "type": "validation_error",
                        "stage": "stage2",
                        "category": err.category,
                        "message": err_message,
                        "missing_fields": err.missing_fields,
                        "invalid_fields": err.invalid_fields,
                        "raw_text": err.raw_text,
                        "parse_position": err.parse_position,
                    },
                }
            )
            self._pending_writer.save_partial(record, f"stage2_{err.category}")
            on_event(OrchestratorEvent.Stage2Failed)
            return record

        # Validation passed
        assert isinstance(result_s2, Ok)
        stage2_json: dict = result_s2.obj

        # ── Step 19: Stage 2 done ─────────────────────────────────────────────
        on_event(OrchestratorEvent.Stage2Done)

        # ── Step 19.5: Log next_bar_prediction (R9.3, NFR2.1) ───────────────────
        _pred = stage2_json if isinstance(stage2_json, dict) else {}
        _nb_pred = _pred.get("next_bar_prediction")
        if isinstance(_nb_pred, dict):
            if _nb_pred.get("unpredictable"):
                logger.info("next_bar_prediction direction=null probs=null/null/null unpredictable=true")
            else:
                _probs = _nb_pred.get("probabilities") or {}
                logger.info(
                    "next_bar_prediction direction=%s probs=%s/%s/%s unpredictable=false",
                    _nb_pred.get("direction"),
                    _probs.get("bullish"),
                    _probs.get("bearish"),
                    _probs.get("neutral"),
                )
        else:
            logger.info("next_bar_prediction absent from stage2 response")

        # ── Step 20: Build final record ───────────────────────────────────────
        usage_total = _accumulate_usage(
            _accumulate_usage(record.usage_total, reply_s1.usage),
            reply_s2.usage,
        )
        record = record.model_copy(
            update={
                "stage1_messages": messages_s1,
                "stage1_response": reply_s1.raw,
                "stage1_diagnosis": stage1_json,
                "stage2_messages": messages_s2,
                "stage2_response": reply_s2.raw,
                "stage2_decision": stage2_json,
                "strategy_files_used": strategy_files,
                "experience_loaded": [
                    e.model_dump() if hasattr(e, "model_dump") else dict(e)
                    for e in experience_entries
                ],
                "usage_total": usage_total,
                "exception": None,
            }
        )

        # ── Step 22: Persist full record ──────────────────────────────────────
        self._pending_writer.save_full(record)

        # ── Step 23: Record saved event ───────────────────────────────────────
        on_event(OrchestratorEvent.RecordSaved)

        # ── Step 24: Return ───────────────────────────────────────────────────
        return record

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _thinking_params(self) -> tuple[bool, str]:
        """Return (thinking, reasoning_effort) from settings defaults."""
        if self._settings is None:
            return True, "max"
        p = self._settings.provider
        return p.thinking, p.reasoning_effort

    @staticmethod
    def _is_network_error(exc: Exception) -> bool:
        """Return True if *exc* is a network/timeout error (SDK, httpx, or OS reset)."""
        from pa_agent.ai.deepseek_client import CancelledError

        if isinstance(exc, CancelledError):
            return False

        try:
            import openai  # type: ignore[import]

            if isinstance(
                exc,
                (
                    openai.APITimeoutError,
                    openai.APIConnectionError,
                    openai.APIStatusError,
                ),
            ):
                return True
        except ImportError:
            pass

        try:
            import httpx  # type: ignore[import]

            if isinstance(
                exc,
                (
                    httpx.ReadError,
                    httpx.ConnectError,
                    httpx.TimeoutException,
                    httpx.RemoteProtocolError,
                ),
            ):
                return True
        except ImportError:
            pass

        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return True
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (
            10054,  # WSAECONNRESET — remote host closed connection
            10053,  # WSAECONNABORTED
            10060,  # WSAETIMEDOUT
        ):
            return True

        cause = exc.__cause__
        if cause is not None and cause is not exc:
            return TwoStageOrchestrator._is_network_error(cause)
        return False
