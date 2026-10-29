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

On validation failure, ``validation_retry`` may append a feedback user turn and
re-call the API (see ``ValidationSettings.retry_*``). Semantic / safety errors
are not retried; immutable-field cheat detection rejects suspicious retries.
"""
from __future__ import annotations

# Legacy flag kept for tests/docs; retry is governed by ValidationSettings.
STAGE2_VALIDATION_AUTO_RETRY = False

import copy
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
from pa_agent.orchestrator.validation_retry import validate_with_retry
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
    from pa_agent.ai.provider_errors import (
        PROVIDER_QUOTA_USER_MESSAGE,
        is_provider_quota_exhausted,
    )

    if err.category == "e" or is_provider_quota_exhausted(err.raw_text):
        return err.message or PROVIDER_QUOTA_USER_MESSAGE
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
    from pa_agent.ai.provider_errors import (
        PROVIDER_QUOTA_USER_MESSAGE,
        is_provider_quota_exhausted,
    )

    if err.category == "e" or is_provider_quota_exhausted(err.raw_text):
        return err.message or PROVIDER_QUOTA_USER_MESSAGE
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


def _short_text(text: Any, limit: int = 140) -> str:
    """Flatten and truncate text for a one-line user-visible message."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


#: Friendly stage names for user-visible progress lines.
_STAGE_LABELS_ZH = {"stage1": "阶段一", "stage2": "阶段二"}


def _emit_progress(on_progress: Callable[[str], None] | None, text: str) -> None:
    """Best-effort progress line for the UI; never break analysis if it fails."""
    if on_progress is None or not text:
        return
    try:
        on_progress(text)
    except Exception:  # noqa: BLE001
        logger.debug("progress callback failed", exc_info=True)


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
        from pa_agent.util.mask_secret import mask_secret
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


def _accumulate_usage_calls(current: dict, usage_calls: list[Any]) -> dict:
    total = dict(current)
    for usage in usage_calls:
        if usage is not None:
            total = _accumulate_usage(total, usage)
    return total


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
        # Set to True as soon as a provider fallback (GLM-4-flash, ...) takes
        # over. While True, schema / enum validation is relaxed: the fallback
        # model's free-text answers are accepted so the GUI can still render,
        # instead of retrying forever on strict enums it does not understand.
        self._on_fallback_provider: bool = False
        # Provider actually used for THIS analysis after any in-run fallback.
        # It must never be written back into settings.provider, otherwise the
        # user's saved configuration would silently turn into the fallback model.
        self._active_provider: Any | None = None
        # Set by _stream_chat_resilient when the spare channel rescued a stage,
        # so the stage can attach the fallback trace to the reply it returns.
        self._pending_free_meta: dict[str, Any] | None = None

    def _provider(self) -> Any:
        """Provider for the current request: in-run fallback wins, else config."""
        if self._active_provider is not None:
            return self._active_provider
        return getattr(self._settings, "provider", None)

    def _validation_settings(self) -> Any:
        if self._settings is not None and hasattr(self._settings, "validation"):
            return self._settings.validation
        from pa_agent.config.settings import ValidationSettings

        return ValidationSettings()

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
        # Every analysis starts from the user's configured provider again.
        self._active_provider = None
        self._on_fallback_provider = False
        # Clear stale free-fallback traces from a previous submit on this instance.
        self._last_sensenova_fallback_failure = None  # type: ignore[attr-defined]
        self._pending_free_meta = None

        # ── Step 2: Pre-Stage-1 cancel check ─────────────────────────────────
        if cancel_token.is_set():
            self._pending_writer.save_partial(record, "user_cancelled")
            on_event(OrchestratorEvent.Cancelled)
            return record

        # ── Step 2.5: Preflight data gate (before Stage1Started) ─────────────
        from pa_agent.ai.decision_nodes import check_preflight_data
        pf = check_preflight_data(frame)
        if not pf.ok:
            record = record.model_copy(update={
                "exception": {
                    "type": "insufficient_data",
                    "stage": "preflight",
                    "failed_check": pf.failed_check,
                    "message": pf.reason,
                }
            })
            self._pending_writer.save_partial(record, "insufficient_data")
            on_event(OrchestratorEvent.InsufficientData)
            return record

        # ── Step 3: Stage 1 started ───────────────────────────────────────────
        on_event(OrchestratorEvent.Stage1Started)

        # Resolve analysis mode from settings (default: original)
        analysis_mode = "original"
        if self._settings is not None:
            analysis_mode = str(
                getattr(self._settings.general, "analysis_mode", "original") or "original"
            )

        # ── Step 4: Build Stage 1 messages ───────────────────────────────────
        if self._glm_flash_lite():
            from pa_agent.ai.glm_flash_lite import build_messages
            messages_s1 = build_messages(frame, "stage1")
        elif previous_record is not None and incremental_new_bar_count is not None:
            messages_s1 = self._assembler.build_incremental_stage1(
                frame,
                previous_record,
                incremental_new_bar_count,
                analysis_mode=analysis_mode,
                provider_settings=getattr(self._settings, "provider", None),
            )
        else:
            messages_s1 = self._assembler.build_stage1(frame, analysis_mode=analysis_mode)

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
        if on_stage_prompt is not None and not self._glm_flash_lite():
            # Lite route: stage headers are written together with the readable
            # summary, so the pane never shows an empty stage section first.
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

        prev_s1: dict[str, Any] | None = None
        if previous_record is not None and int(incremental_new_bar_count or 0) > 0:
            prev_s1 = previous_record.stage1_diagnosis

        s1_validate_kwargs = {
            "kline_frame": frame,
            "incremental_new_bar_count": int(incremental_new_bar_count or 0),
            "incremental_previous_stage1": prev_s1,
        }

        def _s1_validation_settings() -> Any:
            """Resolve contract after routing, including mid-call switches."""
            base = self._validation_settings()
            s1_validate_kwargs["glm_flash_lite"] = self._glm_flash_lite()
            if not self._on_fallback_provider and not self._glm_flash_lite():
                return base
            return base.model_copy(update={"retry_enabled": False})

        def _validate_s1_reply(reply: Any) -> Any:
            from pa_agent.ai.json_validator import coalesce_model_json_text

            content = coalesce_model_json_text(
                getattr(reply, "content", None) or "",
                getattr(reply, "reasoning_content", None) or "",
            )
            return self._validator.validate(
                "stage1", content, relaxed=self._on_fallback_provider, **s1_validate_kwargs
            )

        s1_usage_calls: list[Any] = []
        s1_fallback_meta: dict[str, Any] | None = None
        # Shared with _stream_chat_resilient so the spare channel is attempted
        # once, before the configured provider chain, and never twice.
        s1_free_lane_state: dict[str, bool] = {"tried": False}
        reply_s1: Any = None
        result_s1: Any = None
        s1_validation_attempts = 0
        s1_primary_error: Exception | None = None
        s1_primary_error_type = "network_error"

        try:
            reply_s1 = self._stream_chat_resilient(
                messages_s1,
                on_reasoning_token=_on_s1_reasoning,
                on_content_token=_on_s1_content,
                cancel_token=cancel_token,
                thinking=_thinking,
                reasoning_effort=_effort,
                stage_label="Stage 1",
                frame=frame,
                free_lane=lambda exc: self._try_sensenova_free_stage(
                    stage="stage1",
                    messages=messages_s1,
                    validate_reply=_validate_s1_reply,
                    on_reasoning_token=_on_s1_reasoning,
                    on_content_token=_on_s1_content,
                    cancel_token=cancel_token,
                    on_progress=on_stage1_content,
                    reason=str(exc),
                    frame=frame,
                ),
                free_lane_state=s1_free_lane_state,
            )
        except Exception as exc:
            from pa_agent.ai.sensenova_fallback import is_cancel_or_interrupt

            if is_cancel_or_interrupt(exc):
                raise
            # SenseNova: catch ordinary Exception (ValueError/RuntimeError/429/…)
            # so free fallback can run. Non-SenseNova keeps network-only behavior.
            if self._sensenova_free_eligible():
                s1_primary_error = exc
                s1_primary_error_type = (
                    "network_error" if self._is_network_error(exc) else "provider_error"
                )
                logger.warning("Stage 1 primary error (SenseNova fallback eligible): %s", exc)
            elif self._is_network_error(exc):
                s1_primary_error = exc
                s1_primary_error_type = "network_error"
                logger.warning("Stage 1 network error: %s", exc)
            else:
                raise

        # A spare-channel success inside the resilient call is already final;
        # its meta travels back through the orchestrator instance.
        if self._pending_free_meta is not None:
            s1_fallback_meta = self._pending_free_meta
            self._pending_free_meta = None

        if s1_primary_error is None and reply_s1 is not None:
            from pa_agent.ai.lite_output_normalize import is_local_stage1
            local_stage1 = is_local_stage1(raw=getattr(reply_s1, "raw", None))
            if not local_stage1:
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

            s1_usage_calls = [getattr(reply_s1, "usage", None)]

            def _call_s1_retry(msgs: list[dict]) -> Any:
                nonlocal s1_streamed_reasoning, s1_streamed_content
                on_event(OrchestratorEvent.Stage1Retry)
                s1_streamed_reasoning = False
                s1_streamed_content = False
                r = self._client.stream_chat(
                    msgs,
                    on_reasoning_token=_on_s1_reasoning,
                    on_content_token=_on_s1_content,
                    cancel_token=cancel_token,
                    thinking=_thinking,
                    reasoning_effort=_effort,
                )
                if not s1_streamed_reasoning and r.reasoning_content:
                    _emit_buffered_stream(r.reasoning_content, on_stage1_reasoning)
                if not s1_streamed_content and r.content:
                    _emit_buffered_stream(r.content, on_stage1_content)
                s1_usage_calls.append(getattr(r, "usage", None))
                return r

            try:
                vr_s1 = validate_with_retry(
                    stage="stage1",
                    messages=messages_s1,
                    reply=reply_s1,
                    validator=self._validator,
                    validation_settings=_s1_validation_settings(),
                    validate_kwargs=s1_validate_kwargs,
                    call_api=_call_s1_retry,
                    provider_settings=getattr(self._settings, "provider", None),
                )
                messages_s1 = vr_s1.messages
                reply_s1 = vr_s1.reply
                result_s1 = vr_s1.result
                s1_validation_attempts = vr_s1.attempts
                if vr_s1.attempts > 1:
                    logger.info(
                        "Stage 1 validation succeeded after %d attempt(s)", vr_s1.attempts
                    )
            except Exception as exc:
                from pa_agent.ai.sensenova_fallback import is_cancel_or_interrupt

                if is_cancel_or_interrupt(exc):
                    raise
                if self._sensenova_free_eligible():
                    s1_primary_error = exc
                    s1_primary_error_type = (
                        "network_error" if self._is_network_error(exc) else "provider_error"
                    )
                    logger.warning(
                        "Stage 1 validate_with_retry error (SenseNova fallback eligible): %s",
                        exc,
                    )
                else:
                    raise

        # SenseNova free fallback: primary Exception / ValidationError recoveries.
        # Skipped when the resilient call already ran the spare channel (it is
        # attempted once, ahead of the configured provider chain).
        if not s1_free_lane_state["tried"] and (
            s1_primary_error is not None or isinstance(result_s1, ValidationError)
        ):
            recovered = self._try_sensenova_free_stage(
                stage="stage1",
                messages=messages_s1,
                validate_reply=_validate_s1_reply,
                on_reasoning_token=_on_s1_reasoning,
                on_content_token=_on_s1_content,
                cancel_token=cancel_token,
                on_progress=on_stage1_content,
                reason=(
                    str(s1_primary_error)
                    if s1_primary_error is not None
                    else "阶段一输出未通过校验"
                ),
                frame=frame,
            )
            if recovered is not None:
                reply_s1, result_s1, s1_fallback_meta = recovered
                s1_primary_error = None
                s1_usage_calls.append(getattr(reply_s1, "usage", None))
                if s1_fallback_meta:
                    raw = dict(getattr(reply_s1, "raw", None) or {})
                    raw.update(s1_fallback_meta)
                    try:
                        reply_s1.raw = raw
                    except Exception:  # noqa: BLE001
                        pass

        if s1_primary_error is not None:
            save_reason = (
                "network_error" if s1_primary_error_type == "network_error" else "provider_error"
            )
            record = record.model_copy(
                update={
                    "stage1_messages": messages_s1,
                    "exception": self._sensenova_fallback_exception_fields(
                        primary_type=s1_primary_error_type,
                        primary_message=str(s1_primary_error),
                        stage="stage1",
                    ),
                }
            )
            self._pending_writer.save_partial(record, save_reason)
            on_event(OrchestratorEvent.Stage1Failed)
            return record

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
                    "stage1_response": reply_s1.raw if reply_s1 is not None else {},
                    "usage_total": _accumulate_usage_calls(record.usage_total, s1_usage_calls),
                    "exception": self._sensenova_fallback_exception_fields(
                        primary_type=(
                            "provider_error" if err.category == "e" else "validation_error"
                        ),
                        primary_message=err_message,
                        stage="stage1",
                        extra={
                            "category": err.category,
                            "missing_fields": err.missing_fields,
                            "invalid_fields": err.invalid_fields,
                            "raw_text": err.raw_text,
                            "parse_position": err.parse_position,
                            "validation_attempts": s1_validation_attempts or 1,
                        },
                    ),
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
        max_exp = getattr(prompt_cfg, "experience_max_entries", 0) if prompt_cfg else 0
        max_chars = (
            getattr(prompt_cfg, "experience_max_chars_per_entry", 400) if prompt_cfg else 400
        )
        if max_exp <= 0:
            experience_entries = []
        elif hasattr(self._exp_reader, "read_for_stage2"):
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
        _enable_next_bar = bool(
            getattr(getattr(self._settings, "general", None), "enable_next_bar_prediction", False)
        )
        _flip_cooldown = int(
            getattr(
                getattr(self._settings, "general", None),
                "structure_flip_cooldown_bars",
                3,
            )
            or 3
        )
        if self._glm_flash_lite():
            from pa_agent.ai.glm_flash_lite import build_messages
            messages_s2 = build_messages(frame, "stage2")
        else:
            messages_s2 = self._assembler.build_stage2_continuation(
                frame=frame,
                stage1_messages=messages_s1,
                stage1_reply_content=reply_s1.content,
                stage1_json=stage1_json,
                strategy_files=strategy_files,
                experience_entries=experience_entries,
                decision_stance=record.meta.decision_stance,
                previous_record=previous_record,
                enable_next_bar_prediction=_enable_next_bar,
                provider_settings=getattr(self._settings, "provider", None),
                structure_flip_cooldown_bars=_flip_cooldown,
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
        if on_stage_prompt is not None and not self._glm_flash_lite():
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
            if self._glm_flash_lite():
                # The lite contract streams raw JSON, which is unreadable in the
                # chat pane. The readable summary is pushed once the record is
                # ready; skip chunk replay so the pane is not filled with JSON.
                s2_streamed_content = True
                return
            s2_streamed_content = True
            if on_stage2_content is not None:
                on_stage2_content(chunk)

        s2_validate_kwargs = {
            "kline_frame": frame,
            "decision_stance": record.meta.decision_stance,
            "stage1_json": stage1_json,
            "skip_next_bar": not _enable_next_bar,
            "previous_record": previous_record,
            "structure_flip_cooldown_bars": _flip_cooldown,
        }

        def _s2_validation_settings() -> Any:
            """Resolve contract after routing, including mid-call switches."""
            base = self._validation_settings()
            s2_validate_kwargs["glm_flash_lite"] = self._glm_flash_lite()
            if not self._on_fallback_provider and not self._glm_flash_lite():
                return base
            return base.model_copy(update={"retry_enabled": False})

        def _validate_s2_reply(reply: Any) -> Any:
            from pa_agent.ai.json_validator import coalesce_model_json_text

            content = coalesce_model_json_text(
                getattr(reply, "content", None) or "",
                getattr(reply, "reasoning_content", None) or "",
            )
            return self._validator.validate(
                "stage2", content, relaxed=self._on_fallback_provider, **s2_validate_kwargs
            )

        s2_usage_calls: list[Any] = []
        s2_fallback_meta: dict[str, Any] | None = None
        # Shared with _stream_chat_resilient so the spare channel is attempted
        # once, before the configured provider chain, and never twice.
        s2_free_lane_state: dict[str, bool] = {"tried": False}
        reply_s2: Any = None
        result_s2: Any = None
        s2_validation_attempts = 0
        s2_primary_error: Exception | None = None
        s2_primary_error_type = "network_error"
        _exp_loaded = [
            e.model_dump() if hasattr(e, "model_dump") else dict(e)
            for e in experience_entries
        ]

        try:
            reply_s2 = self._stream_chat_resilient(
                messages_s2,
                on_reasoning_token=_on_s2_reasoning,
                on_content_token=_on_s2_content,
                cancel_token=cancel_token,
                thinking=_thinking,
                reasoning_effort=_effort,
                stage_label="Stage 2",
                frame=frame,
                free_lane=lambda exc: self._try_sensenova_free_stage(
                    stage="stage2",
                    messages=messages_s2,
                    validate_reply=_validate_s2_reply,
                    on_reasoning_token=_on_s2_reasoning,
                    on_content_token=_on_s2_content,
                    cancel_token=cancel_token,
                    on_progress=on_stage2_content,
                    reason=str(exc),
                    frame=frame,
                    stage1_json=stage1_json,
                ),
                free_lane_state=s2_free_lane_state,
            )
        except Exception as exc:
            from pa_agent.ai.sensenova_fallback import is_cancel_or_interrupt

            if is_cancel_or_interrupt(exc):
                raise
            if self._sensenova_free_eligible():
                s2_primary_error = exc
                s2_primary_error_type = (
                    "network_error" if self._is_network_error(exc) else "provider_error"
                )
                logger.warning("Stage 2 primary error (SenseNova fallback eligible): %s", exc)
            elif self._is_network_error(exc):
                s2_primary_error = exc
                s2_primary_error_type = "network_error"
                logger.warning("Stage 2 network error: %s", exc)
            else:
                raise

        if s2_primary_error is None and reply_s2 is not None:
            if not s2_streamed_reasoning and reply_s2.reasoning_content:
                _emit_buffered_stream(reply_s2.reasoning_content, on_stage2_reasoning)
            # Lite replies are raw JSON; main_window pushes a readable summary.
            if not s2_streamed_content and reply_s2.content and not self._glm_flash_lite():
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
                        "experience_loaded": _exp_loaded,
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

            s2_usage_calls = [getattr(reply_s2, "usage", None)]

            def _call_s2_retry(msgs: list[dict]) -> Any:
                nonlocal s2_streamed_reasoning, s2_streamed_content
                on_event(OrchestratorEvent.Stage2Retry)
                s2_streamed_reasoning = False
                s2_streamed_content = False
                r = self._client.stream_chat(
                    msgs,
                    on_reasoning_token=_on_s2_reasoning,
                    on_content_token=_on_s2_content,
                    cancel_token=cancel_token,
                    thinking=_thinking,
                    reasoning_effort=_effort,
                )
                if not s2_streamed_reasoning and r.reasoning_content:
                    _emit_buffered_stream(r.reasoning_content, on_stage2_reasoning)
                if not s2_streamed_content and r.content:
                    _emit_buffered_stream(r.content, on_stage2_content)
                s2_usage_calls.append(getattr(r, "usage", None))
                return r

            try:
                vr_s2 = validate_with_retry(
                    stage="stage2",
                    messages=messages_s2,
                    reply=reply_s2,
                    validator=self._validator,
                    validation_settings=_s2_validation_settings(),
                    validate_kwargs=s2_validate_kwargs,
                    call_api=_call_s2_retry,
                    provider_settings=getattr(self._settings, "provider", None),
                )
                messages_s2 = vr_s2.messages
                reply_s2 = vr_s2.reply
                result_s2 = vr_s2.result
                s2_validation_attempts = vr_s2.attempts
                if vr_s2.attempts > 1:
                    logger.info(
                        "Stage 2 validation succeeded after %d attempt(s)", vr_s2.attempts
                    )
            except Exception as exc:
                from pa_agent.ai.sensenova_fallback import is_cancel_or_interrupt

                if is_cancel_or_interrupt(exc):
                    raise
                if self._sensenova_free_eligible():
                    s2_primary_error = exc
                    s2_primary_error_type = (
                        "network_error" if self._is_network_error(exc) else "provider_error"
                    )
                    logger.warning(
                        "Stage 2 validate_with_retry error (SenseNova fallback eligible): %s",
                        exc,
                    )
                else:
                    raise

        # A spare-channel success inside the resilient call is already final.
        if self._pending_free_meta is not None:
            s2_fallback_meta = self._pending_free_meta
            self._pending_free_meta = None

        # Skipped when the resilient call already ran the spare channel (it is
        # attempted once, ahead of the configured provider chain).
        if not s2_free_lane_state["tried"] and (
            s2_primary_error is not None or isinstance(result_s2, ValidationError)
        ):
            recovered = self._try_sensenova_free_stage(
                stage="stage2",
                messages=messages_s2,
                validate_reply=_validate_s2_reply,
                on_reasoning_token=_on_s2_reasoning,
                on_content_token=_on_s2_content,
                cancel_token=cancel_token,
                on_progress=on_stage2_content,
                reason=(
                    str(s2_primary_error)
                    if s2_primary_error is not None
                    else "阶段二输出未通过校验"
                ),
                frame=frame,
                stage1_json=stage1_json,
            )
            if recovered is not None:
                reply_s2, result_s2, s2_fallback_meta = recovered
                s2_primary_error = None
                s2_usage_calls.append(getattr(reply_s2, "usage", None))
                if s2_fallback_meta:
                    raw = dict(getattr(reply_s2, "raw", None) or {})
                    raw.update(s2_fallback_meta)
                    try:
                        reply_s2.raw = raw
                    except Exception:  # noqa: BLE001
                        pass

        if s2_primary_error is not None:
            save_reason = (
                "network_error" if s2_primary_error_type == "network_error" else "provider_error"
            )
            record = record.model_copy(
                update={
                    "stage1_messages": messages_s1,
                    "stage1_response": reply_s1.raw,
                    "stage1_diagnosis": stage1_json,
                    "stage2_messages": messages_s2,
                    "strategy_files_used": strategy_files,
                    "experience_loaded": _exp_loaded,
                    "usage_total": _accumulate_usage(record.usage_total, reply_s1.usage),
                    "exception": self._sensenova_fallback_exception_fields(
                        primary_type=s2_primary_error_type,
                        primary_message=str(s2_primary_error),
                        stage="stage2",
                    ),
                }
            )
            self._pending_writer.save_partial(record, save_reason)
            on_event(OrchestratorEvent.Stage2Failed)
            return record

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
                    "stage2_response": reply_s2.raw if reply_s2 is not None else {},
                    "strategy_files_used": strategy_files,
                    "experience_loaded": _exp_loaded,
                    "usage_total": _accumulate_usage_calls(
                        _accumulate_usage_calls(record.usage_total, s1_usage_calls),
                        s2_usage_calls,
                    ),
                    "exception": self._sensenova_fallback_exception_fields(
                        primary_type=(
                            "provider_error" if err.category == "e" else "validation_error"
                        ),
                        primary_message=err_message,
                        stage="stage2",
                        extra={
                            "category": err.category,
                            "missing_fields": err.missing_fields,
                            "invalid_fields": err.invalid_fields,
                            "raw_text": err.raw_text,
                            "parse_position": err.parse_position,
                            "validation_attempts": s2_validation_attempts or 1,
                        },
                    ),
                }
            )
            self._pending_writer.save_partial(record, f"stage2_{err.category}")
            on_event(OrchestratorEvent.Stage2Failed)
            return record

        # Validation passed
        assert isinstance(result_s2, Ok)
        stage2_json: dict = result_s2.obj
        if not _enable_next_bar and isinstance(stage2_json, dict):
            stage2_json = copy.deepcopy(stage2_json)
            stage2_json.pop("next_bar_prediction", None)

        # ── Step 19: Stage 2 done ─────────────────────────────────────────────
        on_event(OrchestratorEvent.Stage2Done)

        # ── Step 19.5: Log next_bar_prediction (R9.3, NFR2.1) ───────────────────
        _pred = stage2_json if isinstance(stage2_json, dict) else {}
        _nb_pred = _pred.get("next_bar_prediction")
        if not _enable_next_bar:
            logger.info("next_bar_prediction omitted (feature disabled)")
        elif isinstance(_nb_pred, dict):
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
        elif _enable_next_bar:
            logger.info("next_bar_prediction absent from stage2 response")

        _nc_pred = _pred.get("next_cycle_prediction")
        if isinstance(_nc_pred, dict):
            if _nc_pred.get("unpredictable"):
                logger.info("next_cycle_prediction cycle=null unpredictable=true")
            else:
                logger.info(
                    "next_cycle_prediction cycle=%s direction=%s unpredictable=false",
                    _nc_pred.get("cycle"),
                    _nc_pred.get("direction"),
                )
        else:
            logger.info("next_cycle_prediction absent from stage2 response")

        # ── Step 20: Build final record ───────────────────────────────────────
        usage_total = _accumulate_usage_calls(
            _accumulate_usage_calls(record.usage_total, s1_usage_calls),
            s2_usage_calls,
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
        """Return (thinking, reasoning_effort) from the provider in use."""
        p = self._provider()
        if p is None:
            return True, "low"
        return p.thinking, p.reasoning_effort

    def _sensenova_free_eligible(self) -> bool:
        """True when configured base_url is the SenseNova token gateway."""
        from pa_agent.ai.sensenova_fallback import is_sensenova_fallback_eligible

        if self._settings is None:
            return False
        return is_sensenova_fallback_eligible(
            getattr(self._settings.provider, "base_url", "")
        )

    def _primary_api_key(self) -> str:
        if self._settings is None:
            return ""
        return str(getattr(self._settings.provider, "api_key", "") or "")

    def _accept_fallback_stage2(self, result: Any, content: str) -> Any:
        """Never reject a free-fallback stage-2 reply: rerun relaxed, else salvage.

        The free lane runs the small lite prompt on third-party models. A strict
        ValidationError here used to rotate to the next candidate, which burned
        the whole pool on contract mismatches the model never saw. Since this
        lane is explicitly best-effort, accept whatever parses and let the
        normalizer and the continuity/trade guards decide the plan.
        """
        from pa_agent.ai.json_validator import Ok, ValidationError

        if isinstance(result, Ok):
            return result
        # Preferred: the lite contract. It renders the model's plan as-is and
        # skips every RR / trader-equation / win-rate gate, which is exactly
        # what "do not validate the free lane" means here. Its own
        # check_contract is structural only (order_type, reasoning strings).
        lite = self._validator.validate(
            "stage2", content, relaxed=False, glm_flash_lite=True,
            **self._relaxed_kwargs("stage2"),
        )
        if isinstance(lite, Ok):
            logger.warning(
                "Free fallback stage2 accepted via lite contract (strict: %s)",
                getattr(result, "message", "")[:200],
            )
            return lite
        # Next: relaxed parse. No schema/enum checks, but the normalizer (and
        # therefore the trade-metric gate) still runs, so a structurally broken
        # plan can end up as 不下单 — acceptable as a last resort.
        relaxed = self._validator.validate(
            "stage2", content, relaxed=True, **self._relaxed_kwargs("stage2")
        )
        if isinstance(relaxed, Ok):
            logger.warning(
                "Free fallback stage2 accepted in relaxed mode (strict: %s, lite: %s)",
                getattr(result, "message", "")[:200],
                getattr(lite, "message", "")[:200],
            )
            return relaxed
        from pa_agent.ai.json_salvage import salvage_stage2

        try:
            salvaged = salvage_stage2(content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Free fallback stage2 salvage failed: %s", exc)
            return ValidationError(
                category="c", stage="stage2", raw_text=content,
                message=f"free fallback unrecoverable: {getattr(result, 'message', '')}",
            )
        logger.warning(
            "Free fallback stage2 rebuilt from raw text (strict: %s)",
            getattr(result, "message", "")[:200],
        )
        return Ok(obj=salvaged)

    def _accept_fallback_stage1(self, result: Any, content: str) -> Any:
        """Never reject a free-fallback stage-1 reply: fill from local diagnosis."""
        from pa_agent.ai.json_validator import Ok

        if isinstance(result, Ok):
            return result
        logger.warning(
            "Free fallback stage1 rejected by contract (%s); using local diagnosis",
            getattr(result, "message", "")[:200],
        )
        return Ok(obj=self._local_stage1_obj())

    def _local_stage1_obj(self) -> dict[str, Any]:
        """Rule-based stage-1 context used when the free lane cannot comply."""
        from pa_agent.ai.glm_flash_lite import local_diagnosis

        frame = getattr(self, "_fallback_frame", None)
        if frame is None:
            return {
                "cycle_position": "trading_range", "direction": "neutral",
                "diagnosis_confidence": 0, "market_phase": "stable",
                "detected_patterns": [], "key_signals": [],
                "htf_context": "本地规则示意（非模型推理，非逐节点验证）",
                "entry_setup": "由决策阶段独立判断", "strategy_files_needed": [],
                "bar_by_bar_summary": [], "gate_trace": [], "gate_result": "proceed",
                "trace_source": "local_rules", "diagnosis_source": "local_compatibility",
            }
        return local_diagnosis(frame)

    def _relaxed_kwargs(self, stage: str) -> dict[str, Any]:
        # relaxed=True already skips schema/enum/coherence checks and runs the
        # normalizer. Passing glm_flash_lite here would re-enable the lite
        # contract checks, which is stricter than intended for this lane.
        frame = getattr(self, "_fallback_frame", None)
        if stage == "stage1":
            return {"kline_frame": frame}
        return {
            "kline_frame": frame,
            "stage1_json": getattr(self, "_fallback_stage1", None),
        }

    def _try_sensenova_free_stage(
        self,
        *,
        stage: str,
        messages: list[dict[str, Any]],
        validate_reply: Callable[[Any], Any],
        on_reasoning_token: Callable[[str], None] | None,
        on_content_token: Callable[[str], None] | None,
        cancel_token: CancelToken,
        on_progress: Callable[[str], None] | None = None,
        reason: str = "",
        frame: Any = None,
        stage1_json: dict[str, Any] | None = None,
    ) -> tuple[Any, Any, dict[str, Any]] | None:
        """On SenseNova primary failure, try bounded free candidates.

        Returns ``(reply, Ok, meta)`` when a free call + validation succeed;
        otherwise ``None`` (caller keeps the original failure). Cancel /
        KeyboardInterrupt propagate.
        """
        from pa_agent.ai.json_validator import Ok
        from pa_agent.ai.sensenova_fallback import (
            FreeStageFailure,
            FreeStageSuccess,
            is_cancel_or_interrupt,
            run_free_stage_fallback,
        )

        if not self._sensenova_free_eligible():
            return None
        if cancel_token.is_set():
            from pa_agent.ai.deepseek_client import CancelledError

            raise CancelledError("Request cancelled before SenseNova free fallback")

        logger.warning(
            "SenseNova free fallback triggered for %s (base_url eligible) reason=%s",
            stage,
            _short_text(reason, 200),
        )
        # User-facing wording stays neutral: never advertise the free lane or the
        # upstream model, and never surface a raw upstream error payload.
        _emit_progress(
            on_progress,
            f"[提示] {_STAGE_LABELS_ZH.get(stage, stage)}的主通道暂时不可用，"
            "已自动切换备用通道继续分析。\n"
            "[提示] 界面会显示当前处理进度，请稍候。\n",
        )
        self._last_sensenova_fallback_failure = None  # type: ignore[attr-defined]
        self._fallback_frame = frame
        self._fallback_stage1 = stage1_json

        # The free lane is explicitly best-effort: never reject an upstream
        # reply on contract grounds and never rotate to the next candidate just
        # because a third-party model ignored the lite schema. Accept whatever
        # parses, re-validating in relaxed mode, then salvage, then fall back to
        # local rules. Only the strict/relaxed path is affected; the primary
        # provider keeps full validation.
        def _accept_any(reply: Any) -> Any:
            result = validate_reply(reply)
            from pa_agent.ai.json_validator import coalesce_model_json_text

            content = coalesce_model_json_text(
                getattr(reply, "content", None) or "",
                getattr(reply, "reasoning_content", None) or "",
            )
            if stage == "stage1":
                return self._accept_fallback_stage1(result, content)
            return self._accept_fallback_stage2(result, content)

        try:
            outcome = run_free_stage_fallback(
                messages=messages,
                validate=_accept_any,
                primary_api_key=self._primary_api_key(),
                cancel_token=cancel_token,
                on_reasoning_token=on_reasoning_token,
                on_content_token=on_content_token,
                on_progress=on_progress,
                stream=True,
            )
        except Exception as exc:
            if is_cancel_or_interrupt(exc):
                raise
            logger.warning("SenseNova free fallback aborted with error: %s", exc)
            return None

        if isinstance(outcome, FreeStageSuccess) and isinstance(outcome.result, Ok):
            meta = {
                "fallback_provider": outcome.candidate.provider,
                "fallback_model": outcome.candidate.model,
                "fallback_attempts": [
                    {
                        "provider": a.provider,
                        "model": a.model,
                        "ok": a.ok,
                        "error": a.error,
                        "latency_ms": round(a.latency_ms, 1),
                    }
                    for a in outcome.attempts
                ],
            }
            return outcome.reply, outcome.result, meta

        if isinstance(outcome, FreeStageFailure):
            # Surface the whole candidate trace in the app log (INFO is filtered there),
            # so a failed rotation is diagnosable without opening the record JSON.
            trace = "; ".join(
                f"{a.provider}/{a.model}: {a.error or 'failed'}" for a in outcome.attempts
            ) or "no candidate attempted"
            logger.warning(
                "SenseNova free fallback exhausted for %s: %s | attempts: %s",
                stage,
                outcome.message,
                trace,
            )
            self._last_sensenova_fallback_failure = outcome  # type: ignore[attr-defined]
        return None

    def _sensenova_fallback_exception_fields(
        self,
        *,
        primary_type: str,
        primary_message: str,
        stage: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from pa_agent.ai.sensenova_fallback import format_fallback_exception_meta

        failure = getattr(self, "_last_sensenova_fallback_failure", None)
        if failure is None and not self._sensenova_free_eligible():
            base: dict[str, Any] = {
                "type": primary_type,
                "stage": stage,
                "message": primary_message,
            }
            if extra:
                base.update(extra)
            return base
        meta = format_fallback_exception_meta(
            primary_message=primary_message,
            primary_type=primary_type,
            failure=failure,
        )
        meta["stage"] = stage
        if extra:
            meta.update(extra)
        return meta

    def _glm_flash_lite(self) -> bool:
        from pa_agent.ai.glm_flash_lite import is_glm_flash

        return is_glm_flash(self._provider())

    def _stream_chat_resilient(
        self,
        messages: list[dict[str, Any]],
        *,
        on_reasoning_token: Callable[[str], None] | None,
        on_content_token: Callable[[str], None] | None,
        cancel_token: CancelToken,
        thinking: bool,
        reasoning_effort: str,
        stage_label: str,
        frame: KlineFrame | None = None,
        free_lane: Callable[[BaseException], Any] | None = None,
        free_lane_state: dict[str, bool] | None = None,
    ) -> Any:
        """Call stream_chat with layered fallbacks.

        Layer 0: spare channel (``free_lane``), tried once, only when supplied.
        Layer 1: settings.provider.fallbacks chain (any non-cancel exception).
        Layer 2: on network error, WorkBuddy / TRAE Work CN / QClaw for openclaw_* models.
        Provider-chain switching is in-memory only; settings.json is never rewritten.
        """
        original_model = (
            self._settings.provider.model if self._settings is not None else ""
        )
        tried_qclaw = False
        tried_workbuddy = False
        tried_trae_cn = False
        tried_provider_keys: set[tuple[str, str]] = set()
        last_provider_exc: Exception | None = None
        # Capture the fallback chain once. After a successful switch,
        # settings.provider points at the fallback candidate (whose own
        # fallbacks list is empty), so re-reading it inside the loop would
        # lose the remaining candidates.
        if self._settings is not None:
            from pa_agent.ai.glm_backup import ensure_glm_fallback

            # SenseNova primary + configured GLM key => glm-4-flash backup.
            ensure_glm_fallback(self._settings.provider)
        provider_fallback_chain = (
            list(getattr(self._settings.provider, "fallbacks", []) or [])
            if self._settings is not None else []
        )
        while True:
            lite = self._glm_flash_lite()
            if lite:
                from pa_agent.ai.glm_flash_lite import build_messages, local_diagnosis
                if frame is None:
                    raise ValueError("备用通道精简分析缺少K线快照")
                stage = "stage1" if stage_label == "Stage 1" else "stage2"
                # Mutate the audit list too: record what was actually sent,
                # including when the primary failed midway through this call.
                messages[:] = build_messages(frame, stage)
                if stage == "stage1":
                    import json
                    from pa_agent.ai.deepseek_client import AIReply, AIUsage
                    context = local_diagnosis(frame)
                    return AIReply(
                        content=json.dumps(context, ensure_ascii=False),
                        reasoning_content="", raw={"source": "local_compatibility", "context": context},
                        usage=AIUsage(), request_id="local-stage1", latency_ms=0,
                    )
            try:
                return self._client.stream_chat(
                    messages,
                    on_reasoning_token=on_reasoning_token,
                    on_content_token=on_content_token,
                    cancel_token=cancel_token,
                    thinking=False if lite else thinking,
                    reasoning_effort="low" if lite else reasoning_effort,
                )
            except Exception as exc:
                from pa_agent.ai.sensenova_fallback import is_cancel_or_interrupt

                if is_cancel_or_interrupt(exc):
                    raise
                # Layer 0: spare channel. Preferred over the configured provider
                # chain because it costs nothing and usually answers faster than
                # moving the whole session onto another paid provider. The lane
                # validates internally and returns (reply, Ok, meta) only when a
                # candidate passed, so a failure here falls through unchanged.
                if (
                    free_lane is not None
                    and free_lane_state is not None
                    and not free_lane_state.get("tried")
                ):
                    free_lane_state["tried"] = True
                    recovered = None
                    try:
                        recovered = free_lane(exc)
                    except Exception as lane_exc:  # noqa: BLE001
                        if is_cancel_or_interrupt(lane_exc):
                            raise
                        logger.warning(
                            "%s spare-channel attempt failed: %s", stage_label, lane_exc
                        )
                    if recovered is not None:
                        reply_free = recovered[0]
                        self._pending_free_meta = recovered[2]
                        return reply_free
                # Layer 1: generic provider chain. Triggers on any non-cancel
                # exception (network / 429 / 5xx / timeout). In-memory only.
                if self._try_next_provider_fallback(
                    tried_keys=tried_provider_keys,
                    fallback_chain=provider_fallback_chain,
                    stage_label=stage_label,
                    exc=exc,
                ):
                    last_provider_exc = exc
                    continue
                # Layer 2: openclaw_* network routing (unchanged).
                if not self._is_network_error(exc):
                    raise last_provider_exc or exc
                # Try WorkBuddy fallback first (if model is openclaw_wb),
                # then TRAE Work CN (if model is openclaw_twc),
                # then QClaw fallback (if model is openclaw)
                if not tried_workbuddy and self._try_workbuddy_fallback(
                    original_model=original_model
                ):
                    tried_workbuddy = True
                    logger.info(
                        "%s network error (%s); applied WorkBuddy provider — retrying",
                        stage_label,
                        exc,
                    )
                elif not tried_trae_cn and self._try_trae_cn_fallback(
                    original_model=original_model
                ):
                    tried_trae_cn = True
                    logger.info(
                        "%s network error (%s); applied TRAE Work CN provider — retrying",
                        stage_label,
                        exc,
                    )
                elif not tried_qclaw and self._try_qclaw_fallback(
                    original_model=original_model
                ):
                    tried_qclaw = True
                    logger.info(
                        "%s network error (%s); applied QClaw provider — retrying",
                        stage_label,
                        exc,
                    )
                else:
                    raise last_provider_exc or exc

    def _try_next_provider_fallback(
        self,
        *,
        tried_keys: set[tuple[str, str]],
        fallback_chain: list[Any],
        stage_label: str,
        exc: Exception,
    ) -> bool:
        """Switch the in-memory client to the next unused provider in the chain.

        Returns True when a fallback was applied (caller should retry), False
        when the chain is exhausted. settings.json is never touched: the switch
        is session-scoped so a transient outage cannot permanently rewrite the
        user's primary provider configuration.

        fallback_chain is passed explicitly because the active provider changes
        on each successful switch; re-reading it would lose the remaining
        candidates after the first switch.
        """
        if self._settings is None:
            return False
        fallbacks = list(fallback_chain or [])
        if not fallbacks:
            return False
        cur = self._provider()
        tried_keys.add((str(cur.base_url or "").strip(), str(cur.model or "").strip()))
        for cand in fallbacks:
            key = (str(cand.base_url or "").strip(), str(cand.model or "").strip())
            if key in tried_keys:
                continue
            if not (str(cand.api_key or "").strip()):
                logger.warning(
                    "%s provider fallback skipped (empty api_key): model=%s base_url=%s",
                    stage_label,
                    cand.model,
                    cand.base_url,
                )
                tried_keys.add(key)
                continue
            tried_keys.add(key)
            logger.warning(
                "%s provider error (%s); switching to fallback model=%s base_url=%s",
                stage_label,
                type(exc).__name__,
                cand.model,
                cand.base_url,
            )
            try:
                self._client.update_provider(cand)
            except Exception as swap_exc:  # noqa: BLE001
                logger.warning(
                    "%s provider fallback client update failed: %s",
                    stage_label,
                    swap_exc,
                )
                continue
            self._active_provider = cand
            self._on_fallback_provider = True
            return True
        return False

    def _try_qclaw_fallback(self, *, original_model: str = "") -> bool:
        """Apply local QClaw provider (like settings Save with model=openclaw)."""
        from pa_agent.ai.qclaw_connector import (
            apply_qclaw_provider_to_settings,
            is_openclaw_model,
        )
        from pa_agent.config.paths import SETTINGS_JSON_PATH

        if not is_openclaw_model(original_model):
            return False
        if self._settings is None:
            return False

        from pa_agent.config.settings import save_settings
        from pa_agent.util.logging import update_api_key

        err = apply_qclaw_provider_to_settings(self._settings)
        if err:
            logger.warning("QClaw auto-fallback unavailable: %s", err)
            return False

        self._client.update_provider(self._settings.provider)
        try:
            save_settings(self._settings, SETTINGS_JSON_PATH)
            update_api_key(self._settings.provider.api_key)
        except Exception as save_exc:  # noqa: BLE001
            logger.warning("QClaw fallback applied but settings save failed: %s", save_exc)

        logger.info(
            "QClaw auto-fallback: model=%s base_url=%s",
            self._settings.provider.model,
            self._settings.provider.base_url,
        )
        return True

    def _try_workbuddy_fallback(self, *, original_model: str = "") -> bool:
        """Apply WorkBuddy provider (like settings Save with model=openclaw_wb)."""
        from pa_agent.ai.workbuddy_connector import (
            apply_workbuddy_provider_to_settings,
            is_openclaw_wb_model,
        )
        from pa_agent.config.paths import SETTINGS_JSON_PATH

        if not is_openclaw_wb_model(original_model):
            return False
        if self._settings is None:
            return False

        from pa_agent.config.settings import save_settings
        from pa_agent.util.logging import update_api_key

        err = apply_workbuddy_provider_to_settings(self._settings)
        if err:
            logger.warning("WorkBuddy auto-fallback unavailable: %s", err)
            return False

        self._client.update_provider(self._settings.provider)
        try:
            save_settings(self._settings, SETTINGS_JSON_PATH)
            update_api_key(self._settings.provider.api_key)
        except Exception as save_exc:  # noqa: BLE001
            logger.warning("WorkBuddy fallback applied but settings save failed: %s", save_exc)

        logger.info(
            "WorkBuddy auto-fallback: model=%s base_url=%s",
            self._settings.provider.model,
            self._settings.provider.base_url,
        )
        return True

    def _try_trae_cn_fallback(self, *, original_model: str = "") -> bool:
        """Apply TRAE Work CN provider (like settings Save with model=openclaw_twc)."""
        from pa_agent.ai.trae_connector import (
            apply_trae_cn_provider_to_settings,
            is_openclaw_twc_model,
        )
        from pa_agent.config.paths import SETTINGS_JSON_PATH

        if not is_openclaw_twc_model(original_model):
            return False
        if self._settings is None:
            return False

        from pa_agent.config.settings import save_settings
        from pa_agent.util.logging import update_api_key

        err = apply_trae_cn_provider_to_settings(
            self._settings, preferred_model=original_model
        )
        if err:
            logger.warning("TRAE Work CN auto-fallback unavailable: %s", err)
            return False

        self._client.update_provider(self._settings.provider)
        try:
            save_settings(self._settings, SETTINGS_JSON_PATH)
            update_api_key(self._settings.provider.api_key)
        except Exception as save_exc:  # noqa: BLE001
            logger.warning(
                "TRAE Work CN fallback applied but settings save failed: %s", save_exc
            )

        logger.info(
            "TRAE Work CN auto-fallback: model=%s base_url=%s",
            self._settings.provider.model,
            self._settings.provider.base_url,
        )
        return True

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
