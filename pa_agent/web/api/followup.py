"""Follow-up chat API endpoint (SSE).

Exposes ``POST /api/analysis/followup`` and streams events from the
:class:`FollowupService`. Two P0 baseline events are added to the stream:

- ``run_id``             — turn identifier emitted at the start.
- ``token_usage_update`` — per-turn usage + running session ledger.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from pa_agent.web.service.followup_service import FollowupService

router = APIRouter(prefix="/api/analysis")


class FollowupRequest(BaseModel):
    """Request body for follow-up chat."""

    text: str


@router.post("/followup")
async def followup(request: Request, req: FollowupRequest) -> StreamingResponse:
    """Send a follow-up message and receive the reply via SSE."""
    service: FollowupService | None = getattr(
        request.app.state, "followup_service", None
    )
    if service is None:
        raise HTTPException(
            status_code=503, detail="Followup service not initialized"
        )

    async def _event_stream() -> AsyncIterator[str]:
        async for event in service.send(
            req.text,
            is_disconnected=request.is_disconnected,
        ):
            yield f"event: {event['event']}\ndata: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
    )
