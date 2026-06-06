"""Dedicated router for ``POST /api/analysis/submit``.

Aligns the SSE event stream with the PyQt ``AIStreamPanel`` lifecycle
(snake_case lifecycle event names) and guarantees every event carries a
``run_id`` for client correlation.

All business logic lives in
:mod:`pa_agent.web.service.analysis_service`; this router is a thin
adapter that:

* reads the existing :class:`AnalysisService` from ``app.state`` (populated
  by :func:`pa_agent.web.server.create_app` from the ``AppContext``);
* resolves the previous record for incremental submissions;
* wraps the service's async generator in a FastAPI ``StreamingResponse``
  with ``text/event-stream`` media type.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from pa_agent.web.service.analysis_service import AnalysisService

router = APIRouter(prefix="/api/analysis", tags=["analysis-submit"])


class SubmitRequest(BaseModel):
    """Request body for ``POST /api/analysis/submit``.

    Attributes
    ----------
    bar_count:
        Number of bars to feed into the analysis (default 80).
    stance:
        Decision stance — one of ``conservative`` / ``balanced`` / ``aggressive``
        / ``extreme_aggressive``.
    incremental:
        Whether this is an incremental submission that builds on a previous
        analysis record. Requires ``incremental_new_bars``.
    incremental_new_bars:
        Number of new bars since the previous analysis; used by the assembler
        to fold the diff.
    run_id:
        Optional client-supplied run id for correlation. If ``None`` the
        server generates a UUID4 hex string at the start of submit and
        echoes it in every emitted event's ``run_id`` field.
    """

    bar_count: int = Field(default=80, ge=1, le=5000)
    stance: str = "balanced"
    incremental: bool = False
    incremental_new_bars: int | None = None
    run_id: str | None = None


@router.post("/submit")
async def submit(request: Request, req: SubmitRequest) -> StreamingResponse:
    """Submit an analysis request and receive PyQt-aligned events via SSE.

    The response is a ``text/event-stream`` of newline-delimited SSE events.
    Each ``data`` line is a JSON object whose first key is ``event`` (the
    lifecycle name, snake_case) and that always includes a ``run_id`` field
    for client correlation.

    Lifecycle events (snake_case, matching the PyQt ``AIStreamPanel``):
        ``stage1_started``, ``stage1_done``, ``stage1_failed``,
        ``stage2_started``, ``stage2_done``, ``stage2_failed``,
        ``record_saved``, ``cancelled``, ``insufficient_data``,
        ``stage_prompt``, ``stage2_files``, ``done``, ``error``.

    Streaming events: ``stage1_reasoning``, ``stage1_content``,
    ``stage2_reasoning``, ``stage2_content``.

    Result events: ``stage1_result``, ``stage2_decision``.
    """
    service: AnalysisService | None = getattr(
        request.app.state, "analysis_service", None
    )
    if service is None:
        raise HTTPException(
            status_code=503, detail="Analysis service not initialized"
        )

    previous_record = None
    incremental_new_bar_count = None
    if req.incremental:
        previous_record = service.previous_record
        if previous_record is None:
            raise HTTPException(
                status_code=400,
                detail="No previous analysis record available for incremental analysis",
            )
        incremental_new_bar_count = req.incremental_new_bars

    async def _event_stream() -> AsyncIterator[str]:
        async for event in service.submit(
            req.bar_count,
            req.stance,
            is_disconnected=request.is_disconnected,
            previous_record=previous_record,
            incremental_new_bar_count=incremental_new_bar_count,
            run_id=req.run_id,
        ):
            yield f"event: {event['event']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
    )
