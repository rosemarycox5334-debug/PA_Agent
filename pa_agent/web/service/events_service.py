"""Service layer for replaying historical analysis events.

Reconstructs the SSE event stream from a persisted ``AnalysisRecord`` JSON file
so that the front-end can rehydrate ``AIStreamPanel`` after a page refresh or
auto-resume from a snapshot, without re-running the AI pipeline.

The replayed event schema is intentionally a strict subset of what
``AnalysisService.submit`` yields live:

    record_started      meta + stance + bar_count
    stage1_started
    stage1_reasoning    text chunks of ``stage1_response.reasoning_content``
    stage1_content      text chunks of ``stage1_response.content``
    stage1_result       full ``stage1_diagnosis`` dict (flattened with **)
    stage1_done         marker
    stage2_started
    stage2_reasoning    text chunks
    stage2_content      text chunks
    stage2_decision     full ``stage2_decision`` dict (flattened with **)
    stage2_done
    error               raised when ``record.exception`` is present
    record_saved        marker (record exists on disk by definition)
    done                terminal marker

Each event carries a numeric ``seq`` so a client can resume with
``?cursor=N`` and receive only events with ``seq >= N``.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from pa_agent.config.paths import RECORDS_PENDING_DIR


# ── Chunking ──────────────────────────────────────────────────────────────────
# Replaying a 7 000-token reasoning blob as a single SSE message defeats the
# purpose of incremental rendering. We slice text by *characters* into roughly
# token-sized fragments. The exact value is not load-bearing for correctness;
# downstream the front-end simply concatenates.
DEFAULT_CHUNK_CHARS = 256


class RunNotFoundError(LookupError):
    """Raised when no record matching ``run_id`` exists on disk."""


class EventReplayService:
    """Reconstructs and serves the SSE event sequence for a persisted run.

    The service is intentionally stateless and does *not* hold AppContext-level
    resources (client/assembler/ledger) — they are accepted for symmetry with
    other services and so callers can be wired uniformly, but only ``settings``
    is consulted (for an optional ``records_dir`` override). Disk I/O is
    performed lazily inside ``stream_events`` so a missing record produces a
    proper ``RunNotFoundError`` rather than a constructor failure.
    """

    def __init__(
        self,
        *,
        records_dir: Path | None = None,
        client: Any = None,
        assembler: Any = None,
        ledger: Any = None,
        settings: Any = None,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
    ) -> None:
        self._records_dir = records_dir or RECORDS_PENDING_DIR
        self._client = client
        self._assembler = assembler
        self._ledger = ledger
        self._settings = settings
        self._chunk_chars = max(1, int(chunk_chars))

    # ── Public API ────────────────────────────────────────────────────────

    def resolve_record_path(self, run_id: str) -> Path:
        """Return the on-disk path for ``run_id`` or raise ``RunNotFoundError``.

        ``run_id`` is the filename stem of the record (without ``.json``).
        Path traversal is rejected by requiring the resolved path to live
        inside ``records_dir``.
        """
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            raise RunNotFoundError(f"invalid run_id: {run_id!r}")
        candidate = (self._records_dir / f"{run_id}.json").resolve()
        try:
            candidate.relative_to(self._records_dir.resolve())
        except ValueError as exc:
            raise RunNotFoundError(f"run_id escapes records dir: {run_id!r}") from exc
        if not candidate.is_file():
            raise RunNotFoundError(f"no record for run_id: {run_id!r}")
        return candidate

    def load_record(self, run_id: str) -> dict:
        """Load and JSON-decode the record for ``run_id``."""
        path = self.resolve_record_path(run_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunNotFoundError(f"record unreadable for {run_id!r}: {exc}") from exc

    def build_events(self, record: dict) -> list[dict]:
        """Materialize the full ordered event list for a record.

        Pure function — no I/O — so the same record always produces the same
        sequence (deterministic replay).
        """
        events: list[dict] = []

        meta = record.get("meta") or {}
        events.append(
            {
                "event": "record_started",
                "meta": {
                    "symbol": meta.get("symbol"),
                    "timeframe": meta.get("timeframe"),
                    "bar_count": meta.get("bar_count"),
                    "timestamp_local_iso": meta.get("timestamp_local_iso"),
                    "decision_stance": meta.get("decision_stance"),
                },
            }
        )

        stage1_response = record.get("stage1_response") or {}
        stage1_diagnosis = record.get("stage1_diagnosis")
        stage1_messages = record.get("stage1_messages") or []
        if stage1_messages or stage1_response or stage1_diagnosis:
            events.append({"event": "stage1_started"})
            for chunk in self._chunk_text(stage1_response.get("reasoning_content") or ""):
                events.append({"event": "stage1_reasoning", "text": chunk})
            for chunk in self._chunk_text(stage1_response.get("content") or ""):
                events.append({"event": "stage1_content", "text": chunk})
            if isinstance(stage1_diagnosis, dict):
                events.append({"event": "stage1_result", **stage1_diagnosis})
            events.append({"event": "stage1_done"})

        stage2_response = record.get("stage2_response") or {}
        stage2_decision = record.get("stage2_decision")
        stage2_messages = record.get("stage2_messages") or []
        if stage2_messages or stage2_response or stage2_decision:
            events.append({"event": "stage2_started"})
            for chunk in self._chunk_text(stage2_response.get("reasoning_content") or ""):
                events.append({"event": "stage2_reasoning", "text": chunk})
            for chunk in self._chunk_text(stage2_response.get("content") or ""):
                events.append({"event": "stage2_content", "text": chunk})
            if isinstance(stage2_decision, dict):
                events.append({"event": "stage2_decision", **stage2_decision})
            events.append({"event": "stage2_done"})

        exception = record.get("exception")
        if isinstance(exception, dict):
            err = {"event": "error", **exception}
            # If the exception belongs to a stage that never emitted *_done, we
            # leave that absent — the live pipeline does the same.
            events.append(err)
            # Stage-specific failure marker the front-end already understands.
            stage = exception.get("stage")
            if stage == "stage1":
                events.append({"event": "stage1_failed", "message": exception.get("message", "")})
            elif stage == "stage2":
                events.append({"event": "stage2_failed", "message": exception.get("message", "")})
        elif isinstance(exception, str):
            events.append({"event": "error", "message": exception})

        events.append({"event": "record_saved"})
        events.append({"event": "done"})

        # Stamp deterministic sequence numbers (1-based, matches cursor semantics).
        for idx, ev in enumerate(events, start=1):
            ev["seq"] = idx
        return events

    async def stream_events(
        self, run_id: str, *, cursor: int = 0
    ) -> AsyncIterator[dict]:
        """Async generator yielding event dicts for ``run_id``.

        ``cursor`` is the *exclusive* high-water mark from the client; only
        events with ``seq > cursor`` are yielded. The generator finishes after
        emitting the ``done`` event — replay is not a long-lived subscription.
        """
        record = self.load_record(run_id)
        for ev in self.build_events(record):
            if ev["seq"] > cursor:
                yield ev

    # ── Internal helpers ──────────────────────────────────────────────────

    def _chunk_text(self, text: str) -> Iterable[str]:
        """Split ``text`` into chunks of at most ``self._chunk_chars`` characters.

        Empty / non-string input yields nothing so callers can use
        ``for chunk in self._chunk_text(maybe_none): ...`` unguarded.
        """
        if not text or not isinstance(text, str):
            return ()
        size = self._chunk_chars
        return (text[i : i + size] for i in range(0, len(text), size))
