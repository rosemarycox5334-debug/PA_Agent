"""Service layer for post-analysis follow-up chat (FreeChatSession).

Bridges the synchronous FreeChatSession to async SSE events. Emits two
P0 baseline events in addition to the streaming events:

- ``run_id``            — emitted once at the start of each follow-up call;
                          carries a unique run identifier for the turn.
- ``token_usage_update``— emitted after the API call completes; carries the
                          per-turn token usage plus the running session
                          ledger breakdown.

The service is stateless except for the *current* ``FreeChatSession``
reference, which the owning analysis pipeline sets via
:meth:`set_chat_session` after a successful two-stage analysis.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from pa_agent.util.threading import CancelToken

if TYPE_CHECKING:
    from pa_agent.orchestrator.free_chat import FreeChatSession

logger = logging.getLogger(__name__)


class FollowupService:
    """Async SSE bridge for ``FreeChatSession.send()``.

    All AppContext-owned dependencies (``client``, ``assembler``,
    ``pending_writer``, ``ledger``, ``settings``) are passed in through
    the constructor so the service can be tested in isolation.
    """

    def __init__(
        self,
        client: object | None = None,
        assembler: object | None = None,
        pending_writer: object | None = None,
        ledger: object | None = None,
        settings: object | None = None,
    ) -> None:
        self._client = client
        self._assembler = assembler
        self._pending_writer = pending_writer
        self._ledger = ledger
        self._settings = settings
        self._chat_session: "FreeChatSession | None" = None

    # ── Session wiring ──────────────────────────────────────────────────────

    def set_chat_session(self, chat_session: "FreeChatSession | None") -> None:
        """Attach the chat session produced by a successful analysis."""
        self._chat_session = chat_session

    @property
    def chat_session(self) -> "FreeChatSession | None":
        """Return the currently active chat session, or ``None``."""
        return self._chat_session

    @property
    def has_session(self) -> bool:
        """Whether a follow-up session is currently available."""
        return self._chat_session is not None

    # ── Public API ──────────────────────────────────────────────────────────

    async def send(
        self,
        text: str,
        *,
        cancel_check_interval: float = 0.5,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[dict]:
        """Yield SSE events for one follow-up turn.

        Event order
        -----------
        1. ``run_id``             — once, at the start (identifies the turn)
        2. ``followup_reasoning`` — zero or more reasoning-token deltas
        3. ``followup_content``   — zero or more content-token deltas
        4. ``token_usage_update`` — once, after the API call completes
        5. ``followup_reply``     — once, with the final content
        6. ``done``               — terminal marker
        """
        if self._chat_session is None:
            yield {
                "event": "error",
                "message": "No active analysis record — submit an analysis first",
            }
            return

        # P0: announce run_id before any token streaming starts.
        run_id = uuid.uuid4().hex
        raw_turn = getattr(self._chat_session, "_turn", 0)
        try:
            turn = int(raw_turn) + 1  # type: ignore[arg-type]
        except (TypeError, ValueError):
            turn = 1
        yield {"event": "run_id", "run_id": run_id, "turn": turn}

        event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        cancel_token = CancelToken()
        loop = asyncio.get_running_loop()

        watcher_task = asyncio.create_task(
            self._cancel_watcher(cancel_token, is_disconnected, cancel_check_interval)
        )
        executor_task = loop.run_in_executor(
            None,
            self._run_sync,
            text,
            cancel_token,
            event_queue,
            loop,
            run_id,
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

    # ── Internal helpers ────────────────────────────────────────────────────

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
        text: str,
        cancel_token: CancelToken,
        event_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        run_id: str,
    ) -> None:
        """Run ``FreeChatSession.send()`` in a worker thread."""

        def _put(item: dict) -> None:
            loop.call_soon_threadsafe(event_queue.put_nowait, item)

        def _on_reasoning(chunk: str) -> None:
            _put({"event": "followup_reasoning", "text": chunk})

        def _on_content(chunk: str) -> None:
            _put({"event": "followup_content", "text": chunk})

        try:
            reply = self._chat_session.send(  # type: ignore[union-attr]
                text,
                cancel_token=cancel_token,
                on_reasoning_token=_on_reasoning,
                on_content_token=_on_content,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("FreeChatSession.send failed: %s", exc)
            _put({"event": "error", "type": "unexpected", "message": str(exc)})
            _put(None)
            return

        # P0: push per-turn token usage update alongside the running totals.
        usage = getattr(reply, "usage", None)
        usage_dict: dict = {}
        if usage is not None:
            def _as_int(value: object) -> int:
                if isinstance(value, int):
                    return value
                try:
                    return int(value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    return 0

            usage_dict = {
                "prompt_tokens": _as_int(getattr(usage, "prompt_tokens", 0)),
                "cached_prompt_tokens": _as_int(getattr(usage, "cached_prompt_tokens", 0)),
                "completion_tokens": _as_int(getattr(usage, "completion_tokens", 0)),
                "total_tokens": _as_int(getattr(usage, "total_tokens", 0)),
            }

        ledger_breakdown: dict = {}
        if self._ledger is not None and hasattr(self._ledger, "breakdown"):
            try:
                ledger_breakdown = self._ledger.breakdown() or {}
            except Exception:  # noqa: BLE001
                ledger_breakdown = {}

        _put(
            {
                "event": "token_usage_update",
                "run_id": run_id,
                "turn_usage": usage_dict,
                "ledger": ledger_breakdown,
            }
        )

        _put({"event": "followup_reply", "content": getattr(reply, "content", "")})
        _put({"event": "done"})
        _put(None)
