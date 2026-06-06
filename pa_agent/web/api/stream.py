"""SSE streaming endpoint for real-time data."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from pa_agent.web.service.data_service import DataService

router = APIRouter(prefix="/api/stream")


async def _event_stream(data_service: DataService) -> AsyncIterator[str]:
    """Yield SSE events with kline_frame data every second."""
    while True:
        try:
            snapshot = data_service.get_snapshot()
        except Exception:
            snapshot = None
        if snapshot:
            yield f"event: kline_frame\ndata: {json.dumps(snapshot)}\n\n"
        await asyncio.sleep(1.0)


@router.get("")
async def stream(request: Request) -> StreamingResponse:
    """SSE endpoint that streams K-line frames."""
    service: DataService | None = request.app.state.data_service
    if service is None:
        raise HTTPException(status_code=503, detail="Data service not initialized")
    return StreamingResponse(
        _event_stream(service),
        media_type="text/event-stream",
    )
