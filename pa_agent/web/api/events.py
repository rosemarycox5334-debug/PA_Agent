"""Event replay API — exposes historical analysis events for auto-resume.

``GET /api/analysis/{run_id}/events`` streams the reconstructed event sequence
of a persisted ``AnalysisRecord`` over Server-Sent Events. Used by
``AIStreamPanel`` to rehydrate state after a page refresh or to attach to a
recorded run from the records list.

This router intentionally lives in its own module (kept separate from
``analysis.py`` which serves *live* analysis submissions) so the replay
surface area can evolve independently — e.g. a future JSON-snapshot mode or
WebSocket variant — without touching the submit endpoint.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from pa_agent.web.service.events_service import EventReplayService, RunNotFoundError

router = APIRouter(prefix="/api/analysis")


def _get_service(request: Request) -> EventReplayService:
    """Return the per-app ``EventReplayService``, lazily creating one if absent.

    The service is stateless and cheap to construct; we cache on ``app.state``
    so that a custom instance wired by ``main.py`` (with the real AppContext
    resources) takes precedence when present.
    """
    svc: EventReplayService | None = getattr(
        request.app.state, "events_service", None
    )
    if svc is None:
        svc = EventReplayService()
        request.app.state.events_service = svc
    return svc


@router.get("/{run_id}/events", response_model=None)
async def replay_events(
    request: Request,
    run_id: str,
    cursor: int = Query(0, ge=0, description="Exclusive seq high-water mark"),
    format: str = Query(
        "sse",
        pattern="^(sse|json)$",
        description="'sse' streams events; 'json' returns the full list",
    ),
) -> Any:
    """Replay the event sequence for a persisted analysis run.

    Path params:
        run_id: filename stem of the pending record (no ``.json``).

    Query params:
        cursor: only events with ``seq > cursor`` are returned (default 0).
        format: ``sse`` (default) for streaming, ``json`` for batched response.
    """
    service = _get_service(request)

    if format == "json":
        try:
            record = service.load_record(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        events = [ev for ev in service.build_events(record) if ev["seq"] > cursor]
        return {"run_id": run_id, "cursor": cursor, "events": events}

    async def _sse_stream() -> AsyncIterator[str]:
        try:
            async for event in service.stream_events(run_id, cursor=cursor):
                # Mirror the framing used by /api/analysis/submit so the
                # front-end EventSource handlers stay identical.
                yield f"event: {event['event']}\ndata: {json.dumps(event)}\n\n"
        except RunNotFoundError as exc:
            payload = json.dumps({"event": "error", "message": str(exc)})
            yield f"event: error\ndata: {payload}\n\n"

    # Pre-flight the existence check so callers get a proper HTTP 404 instead
    # of an SSE error event (the stream variant still emits one defensively in
    # case the file is deleted between this check and the generator running).
    try:
        service.resolve_record_path(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return StreamingResponse(
        _sse_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
