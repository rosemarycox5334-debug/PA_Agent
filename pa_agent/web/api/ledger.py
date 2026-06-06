"""Ledger API endpoints for token usage."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/ledger")


@router.get("")
async def get_ledger(request: Request) -> dict:
    """Return current token usage breakdown."""
    ledger = getattr(request.app.state, "ledger", None)
    if ledger is None:
        raise HTTPException(status_code=503, detail="Ledger not initialized")
    return ledger.breakdown()
