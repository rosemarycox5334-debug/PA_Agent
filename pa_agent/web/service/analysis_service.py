"""Service layer for AI analysis — bridges sync TwoStageOrchestrator to async SSE.

Aligned with the PyQt ``AIStreamPanel`` lifecycle (snake_case event names) and
adds a server-generated ``run_id`` to every emitted event so web clients can
correlate an entire submission stream.

Lifecycle events emitted (snake_case):
    stage1_started, stage1_done, stage1_failed,
    stage2_started, stage2_done, stage2_failed,
    record_saved, cancelled, insufficient_data,
    stage_prompt, stage2_files, done, error.

Streaming events: stage{1,2}_reasoning, stage{1,2}_content.
Result events: stage1_result, stage2_decision.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pa_agent.util.threading import CancelToken, OrchestratorEvent
from pa_agent.web.service.cancel_service import CancelRegistry, default_registry

if TYPE_CHECKING:
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    from pa_agent.web.service.data_service import DataService

logger = logging.getLogger(__name__)


# Map OrchestratorEvent → snake_case event name (aligned with PyQt AIStreamPanel
# semantic: stage1_started / stage1_done / stage1_failed / etc.).
_ORCHESTRATOR_EVENT_NAME_MAP: dict[OrchestratorEvent, str] = {
    OrchestratorEvent.Stage1Started: "stage1_started",
    OrchestratorEvent.Stage1Done: "stage1_done",
    OrchestratorEvent.Stage1Failed: "stage1_failed",
    OrchestratorEvent.Stage2Started: "stage2_started",
    OrchestratorEvent.Stage2Done: "stage2_done",
    OrchestratorEvent.Stage2Failed: "stage2_failed",
    OrchestratorEvent.RecordSaved: "record_saved",
    OrchestratorEvent.Cancelled: "cancelled",
    OrchestratorEvent.InsufficientData: "insufficient_data",
}


def generate_run_id() -> str:
    """Return a short, unique run identifier (32 hex chars)."""
    return uuid.uuid4().hex


def _inject_run_id(event: dict, run_id: str) -> dict:
    """Return a shallow copy of *event* with ``run_id`` merged into its data dict.

    The returned dict always contains the ``event`` key first (so the SSE
    serializer can read it cheaply) and includes the original payload fields.
    """
    if "event" not in event:
        return event
    enriched: dict[str, Any] = {"event": event["event"], "run_id": run_id}
    for key, value in event.items():
        if key == "event":
            continue
        enriched[key] = value
    return enriched


def _map_lifecycle_event_name(event_name: str) -> str:
    """Translate a PascalCase ``OrchestratorEvent.name`` to snake_case.

    Unknown names pass through unchanged so that streaming/result events
    (``stage1_reasoning``, ``stage1_result``, ...) are not affected.
    """
    for enum_val, snake in _ORCHESTRATOR_EVENT_NAME_MAP.items():
        if event_name == enum_val.name:
            return snake
    return event_name


class AnalysisService:
    """Bridges the synchronous TwoStageOrchestrator to async SSE events."""

    def __init__(
        self,
        orchestrator: "TwoStageOrchestrator" | None = None,
        data_service: "DataService" | None = None,
        ledger: object | None = None,
        client: object | None = None,
        assembler: object | None = None,
        pending_writer: object | None = None,
        settings: object | None = None,
        cancel_registry: CancelRegistry | None = None,
        followup_service: object | None = None,
    ) -> None:
        self._orch = orchestrator
        self._data_service = data_service
        self._ledger = ledger
        self._client = client
        self._assembler = assembler
        self._pending_writer = pending_writer
        self._settings = settings
        # Registry mirrors PyQt MainWindow.self._cancel_token bookkeeping for
        # in-flight runs; fall back to the module-level singleton so embeddings
        # without explicit wiring still observe cancellations.
        self._cancel_registry: CancelRegistry = cancel_registry or default_registry()
        self._previous_record: "pa_agent.records.schema.AnalysisRecord | None" = None
        self._chat_session: object | None = None
        # P0 baseline: optional reference to the FollowupService that powers
        # ``POST /api/analysis/followup``.  When wired, the chat session built
        # from a successful analysis is shared with the followup service so
        # both pipelines observe the same conversation history.
        self._followup_service = followup_service
        # Prewarm state (phase-1 incremental prewarm at server startup).
        self._prewarm_done: bool = False
        self._prewarm_error: str | None = None
        self._prewarm_ts_ms: int | None = None

    @property
    def cancel_registry(self) -> CancelRegistry:
        """Return the registry used to track in-flight ``CancelToken``s."""
        return self._cancel_registry

    @property
    def previous_record(self) -> "pa_agent.records.schema.AnalysisRecord | None":
        """Return the most recent successful analysis record, or None."""
        return self._previous_record

    @property
    def is_prewarmed(self) -> bool:
        """Return True once the startup phase-1 prewarm has completed successfully."""
        return self._prewarm_done and self._prewarm_error is None

    @property
    def prewarm_error(self) -> str | None:
        """Return the prewarm error message, or None if prewarm succeeded."""
        return self._prewarm_error

    def prewarm(self, bar_count: int = 80) -> bool:
        """Phase-1 incremental prewarm: validate wiring and prime the data frame.

        Called at web-server startup so the first user-triggered analysis has a
        hot frame ready and any wiring issues surface early. Lightweight by
        design — never invokes the AI model. Returns ``True`` on success.
        """
        if self._orch is None:
            self._prewarm_error = "AnalysisService.orchestrator not wired"
            logger.warning("Prewarm: orchestrator not wired; skipping frame priming")
            return False
        try:
            # Prime a data frame so the first submit() has hot data.
            if self._data_service is not None:
                frame = self._data_service.get_frame(bar_count)
                if frame is None:
                    logger.info(
                        "Prewarm: data frame not yet available (OK at startup)"
                    )
                else:
                    logger.info(
                        "Prewarm: data frame ready (%d bars for %s %s)",
                        len(frame.bars),
                        frame.symbol,
                        frame.timeframe,
                    )

            # Exercise the assembler with a stage-1 prompt build to surface
            # import/config errors without spending an AI call. We skip this
            # gracefully if the data frame isn't ready.
            assembler_obj = getattr(self, "_assembler", None)
            if assembler_obj is not None and self._data_service is not None:
                frame = self._data_service.get_frame(bar_count)
                if frame is not None and hasattr(assembler_obj, "build_stage1"):
                    try:
                        _ = assembler_obj.build_stage1(frame)
                        logger.info("Prewarm: stage1 prompt builder OK")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Prewarm: stage1 prompt builder failed: %s", exc)

            from pa_agent.util.timefmt import now_local_ms

            self._prewarm_done = True
            self._prewarm_error = None
            self._prewarm_ts_ms = now_local_ms()
            logger.info("Prewarm: phase-1 incremental prewarm complete")
            return True
        except Exception as exc:  # noqa: BLE001
            self._prewarm_error = str(exc)
            logger.warning("Prewarm failed: %s", exc)
            return False

    async def submit(
        self,
        bar_count: int,
        stance: str,
        *,
        cancel_check_interval: float = 0.5,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        previous_record: "pa_agent.records.schema.AnalysisRecord | None" = None,
        incremental_new_bar_count: int | None = None,
        run_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Yield SSE-compatible events from the two-stage analysis pipeline.

        Every event dict is enriched with a ``run_id`` (server-generated if
        ``run_id`` is not provided). Lifecycle event names emitted by the
        orchestrator are normalised to snake_case to align with the PyQt
        ``AIStreamPanel`` contract.

        If *orchestrator* is None, falls back to mock events (for testing without
        a real AI backend).
        """
        rid = run_id or generate_run_id()

        if self._orch is None:
            async for ev in self._mock_submit(bar_count, stance, rid):
                yield ev
            return

        if self._data_service is None:
            yield _inject_run_id(
                {"event": "error", "message": "Data service not configured"}, rid
            )
            return

        frame = self._data_service.get_frame(bar_count)
        if frame is None:
            yield _inject_run_id(
                {"event": "error", "message": "Data not ready — no KlineFrame available"},
                rid,
            )
            return

        event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        cancel_token = CancelToken()
        loop = asyncio.get_running_loop()

        # Mirror PyQt MainWindow.self._cancel_token: expose the in-flight
        # token under the run_id so POST /api/analysis/{run_id}/cancel can
        # signal it (cleared in the ``finally`` block below).
        self._cancel_registry.register(rid, cancel_token)

        # Start cancel watcher
        watcher_task = asyncio.create_task(
            self._cancel_watcher(cancel_token, is_disconnected, cancel_check_interval)
        )

        # Run orchestrator in executor
        executor_task = loop.run_in_executor(
            None,
            self._run_sync,
            frame,
            cancel_token,
            event_queue,
            loop,
            bar_count,
            stance,
            previous_record,
            incremental_new_bar_count,
            rid,
        )

        try:
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                # Map OrchestratorEvent.name (PascalCase) → snake_case, then
                # ensure run_id is present in the event payload.
                event = _inject_run_id(event, rid)
                event_name = event.get("event")
                if isinstance(event_name, str):
                    mapped = _map_lifecycle_event_name(event_name)
                    if mapped != event_name:
                        event = {**event, "event": mapped}
                yield event
        finally:
            cancel_token.set()
            self._cancel_registry.unregister(rid)
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass
            if not executor_task.done():
                executor_task.cancel()
            try:
                await executor_task
            except asyncio.CancelledError:
                pass

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _cancel_watcher(
        self,
        cancel_token: CancelToken,
        is_disconnected: Callable[[], Awaitable[bool]] | None,
        interval: float,
    ) -> None:
        """Periodically check if the client disconnected; if so, signal cancellation."""
        try:
            while True:
                await asyncio.sleep(interval)
                if cancel_token.is_set():
                    return
                if is_disconnected is not None and await is_disconnected():
                    cancel_token.set()
                    return
        except asyncio.CancelledError:
            return

    def _run_sync(
        self,
        frame: "pa_agent.data.base.KlineFrame",
        cancel_token: CancelToken,
        event_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        bar_count: int,
        stance: str,
        previous_record: "pa_agent.records.schema.AnalysisRecord | None" = None,
        incremental_new_bar_count: int | None = None,
        run_id: str = "",
    ) -> None:
        """Run orchestrator.submit() in a thread and push events to the async queue."""

        def _put(item: dict) -> None:
            loop.call_soon_threadsafe(event_queue.put_nowait, item)

        def _on_event(ev: OrchestratorEvent) -> None:
            # Lifecycle event — name is normalised downstream in submit().
            _put({"event": ev.name})

        def _on_s1_reasoning(text: str) -> None:
            _put({"event": "stage1_reasoning", "text": text})

        def _on_s1_content(text: str) -> None:
            _put({"event": "stage1_content", "text": text})

        def _on_s2_reasoning(text: str) -> None:
            _put({"event": "stage2_reasoning", "text": text})

        def _on_s2_content(text: str) -> None:
            _put({"event": "stage2_content", "text": text})

        def _on_stage_prompt(stage: str, system: str, user: str) -> None:
            _put({"event": "stage_prompt", "stage": stage, "system": system, "user": user})

        def _on_stage2_files(files: list[str]) -> None:
            _put({"event": "stage2_files", "files": list(files)})

        try:
            record = self._orch.submit(
                frame=frame,
                cancel_token=cancel_token,
                on_event=_on_event,
                on_stage1_reasoning=_on_s1_reasoning,
                on_stage1_content=_on_s1_content,
                on_stage2_reasoning=_on_s2_reasoning,
                on_stage2_content=_on_s2_content,
                on_stage_prompt=_on_stage_prompt,
                on_stage2_files=_on_stage2_files,
                previous_record=previous_record,
                incremental_new_bar_count=incremental_new_bar_count,
            )
        except Exception as exc:
            _put({"event": "error", "type": "unexpected", "message": str(exc)})
            _put(None)
            return

        # Push final results or error details after submit returns
        if record.exception:
            exc_dict = record.exception
            if isinstance(exc_dict, dict):
                _put({"event": "error", **exc_dict})
            else:
                _put({"event": "error", "message": str(exc_dict)})
        else:
            if record.stage1_diagnosis:
                _put({"event": "stage1_result", **record.stage1_diagnosis})
            if record.stage2_decision:
                _put({"event": "stage2_decision", **record.stage2_decision})

        # Update ledger with token usage
        if self._ledger is not None:
            usage = record.usage_total
            if usage and isinstance(usage, dict) and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
                try:
                    from pa_agent.ai.deepseek_client import AIUsage
                    self._ledger.add(AIUsage(
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        cached_prompt_tokens=usage.get("cached_prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                        total_tokens=usage.get("total_tokens", 0),
                    ))
                except Exception:
                    pass  # ledger update failure should not break the flow

        # Store successful record for incremental analysis
        if not record.exception and record.stage2_decision:
            self._previous_record = record
            # Create chat session for follow-up
            if self._client is not None:
                try:
                    from pa_agent.orchestrator.free_chat import FreeChatSession
                    self._chat_session = FreeChatSession(
                        base_record=record,
                        client=self._client,
                        assembler=self._assembler,
                        pending_writer=self._pending_writer,
                        ledger=self._ledger,
                        settings=self._settings,
                    )
                    # P0: share the chat session with the FollowupService so
                    # the SSE endpoint can drive it from
                    # ``POST /api/analysis/followup``.
                    if self._followup_service is not None and hasattr(
                        self._followup_service, "set_chat_session"
                    ):
                        try:
                            self._followup_service.set_chat_session(self._chat_session)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:
                    pass  # chat session creation failure should not break the flow

        self._push_debug_turns(record)

        _put({"event": "done"})
        _put(None)

    # ── Debug turns helper ────────────────────────────────────────────────────

    def _push_debug_turns(self, record: object) -> None:
        """Extract stage1/stage2 debug turns from a completed record."""
        from pa_agent.records.schema import AnalysisRecord
        from pa_agent.web.api.debug import add_turn

        if not isinstance(record, AnalysisRecord):
            return

        # Stage 1
        if record.stage1_messages:
            s1_system = next(
                (m.get("content", "") for m in record.stage1_messages if m.get("role") == "system"),
                "",
            )
            s1_user = next(
                (m.get("content", "") for m in record.stage1_messages if m.get("role") == "user"),
                "",
            )

            validation_info: dict = {"status": "ok"}
            if record.exception and record.exception.get("stage") == "stage1":
                exc_type = record.exception.get("type")
                if exc_type == "validation_error":
                    validation_info = {
                        "status": "error",
                        "category": record.exception.get("category"),
                        "message": record.exception.get("message"),
                    }
                else:
                    validation_info = {
                        "status": "error",
                        "type": exc_type,
                        "message": record.exception.get("message"),
                    }
            elif not record.stage1_diagnosis:
                validation_info = {"status": "unknown"}

            add_turn(
                {
                    "label": "阶段一 · 市场诊断",
                    "system_prompt": s1_system,
                    "user_prompt": s1_user,
                    "raw_response": record.stage1_response,
                    "validation_info": validation_info,
                }
            )

        # Stage 2
        if record.stage2_messages:
            s2_system = next(
                (m.get("content", "") for m in record.stage2_messages if m.get("role") == "system"),
                "",
            )
            s2_user = next(
                (m.get("content", "") for m in reversed(record.stage2_messages) if m.get("role") == "user"),
                "",
            )

            validation_info = {"status": "ok"}
            if record.exception and record.exception.get("stage") == "stage2":
                exc_type = record.exception.get("type")
                if exc_type == "validation_error":
                    validation_info = {
                        "status": "error",
                        "category": record.exception.get("category"),
                        "message": record.exception.get("message"),
                    }
                else:
                    validation_info = {
                        "status": "error",
                        "type": exc_type,
                        "message": record.exception.get("message"),
                    }
            elif not record.stage2_decision:
                validation_info = {"status": "unknown"}

            add_turn(
                {
                    "label": "阶段二 · 交易决策",
                    "system_prompt": s2_system,
                    "user_prompt": s2_user,
                    "raw_response": record.stage2_response,
                    "validation_info": validation_info,
                }
            )
        elif record.stage2_decision and not record.stage2_messages:
            # Gate short-circuit: no API call was made for stage 2
            add_turn(
                {
                    "label": "阶段二 · 交易决策",
                    "system_prompt": "",
                    "user_prompt": "（阶段一闸门未通过，跳过阶段二模型调用）",
                    "raw_response": None,
                    "validation_info": {"status": "skipped", "reason": "gate_shortcircuited"},
                }
            )

    async def followup(
        self,
        text: str,
        *,
        cancel_check_interval: float = 0.5,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[dict]:
        """Yield SSE-compatible events from a follow-up chat turn."""
        if self._chat_session is None:
            yield {"event": "error", "message": "No active analysis record — submit an analysis first"}
            return

        event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        cancel_token = CancelToken()
        loop = asyncio.get_running_loop()

        # Start cancel watcher
        watcher_task = asyncio.create_task(
            self._cancel_watcher(cancel_token, is_disconnected, cancel_check_interval)
        )

        # Run chat in executor
        executor_task = loop.run_in_executor(
            None,
            self._run_followup_sync,
            text,
            cancel_token,
            event_queue,
            loop,
        )

        try:
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                yield event
        finally:
            cancel_token.set()
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass
            if not executor_task.done():
                executor_task.cancel()
            try:
                await executor_task
            except asyncio.CancelledError:
                pass

    def _run_followup_sync(
        self,
        text: str,
        cancel_token: CancelToken,
        event_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Run FreeChatSession.send() in a thread and push events to the async queue."""

        def _put(item: dict) -> None:
            loop.call_soon_threadsafe(event_queue.put_nowait, item)

        def _on_reasoning(chunk: str) -> None:
            _put({"event": "followup_reasoning", "text": chunk})

        def _on_content(chunk: str) -> None:
            _put({"event": "followup_content", "text": chunk})

        try:
            reply = self._chat_session.send(
                text,
                cancel_token=cancel_token,
                on_reasoning_token=_on_reasoning,
                on_content_token=_on_content,
            )
        except Exception as exc:
            _put({"event": "error", "type": "unexpected", "message": str(exc)})
            _put(None)
            return

        _put({"event": "followup_reply", "content": reply.content})
        _put({"event": "done"})
        _put(None)

    # ── Mock fallback (preserved for testing) ─────────────────────────────────

    async def _mock_submit(
        self, bar_count: int, stance: str, run_id: str = ""
    ) -> AsyncIterator[dict]:
        """Yield mock analysis events when no real orchestrator is wired.

        Events mirror the PyQt-aligned lifecycle (snake_case names) and every
        event dict carries the supplied *run_id* so mock output is
        indistinguishable from real output for client consumers.
        """
        rid = run_id or generate_run_id()
        yield _inject_run_id({"event": "stage1_started"}, rid)
        yield _inject_run_id(
            {"event": "stage1_reasoning", "text": "Analyzing market structure..."},
            rid,
        )
        await asyncio.sleep(0.05)
        yield _inject_run_id(
            {
                "event": "stage1_result",
                "diagnosis": "Bullish trend",
                "direction_prob": 0.75,
            },
            rid,
        )
        await asyncio.sleep(0.05)
        yield _inject_run_id({"event": "stage1_done"}, rid)
        yield _inject_run_id({"event": "stage2_started"}, rid)
        yield _inject_run_id(
            {"event": "stage2_content", "text": "Entry at support..."}, rid
        )
        await asyncio.sleep(0.05)
        yield _inject_run_id(
            {
                "event": "stage2_decision",
                "order_type": "限价单",
                "order_direction": "做多",
                "entry_price": 1215.0,
                "take_profit_price": 1230.0,
                "stop_loss_price": 1205.0,
            },
            rid,
        )
        yield _inject_run_id({"event": "stage2_done"}, rid)
        yield _inject_run_id({"event": "record_saved"}, rid)
        yield _inject_run_id({"event": "done"}, rid)
