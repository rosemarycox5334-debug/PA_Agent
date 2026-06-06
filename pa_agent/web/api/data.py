"""Data API endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from pa_agent.web.service.data_service import DataService

router = APIRouter(prefix="/api/data")


@router.get("/snapshot")
async def get_snapshot(request: Request) -> dict:
    """Return the current K-line snapshot."""
    service: DataService | None = request.app.state.data_service
    if service is None:
        raise HTTPException(status_code=503, detail="Data service not initialized")
    snapshot = service.get_snapshot()
    if snapshot is None:
        detail = getattr(service, "last_error", None) or "Data source not ready"
        raise HTTPException(status_code=503, detail=detail)
    return snapshot
