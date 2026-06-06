"""Analysis latest API — P1 auto-incremental preflight.

Exposes ``GET /api/analysis/latest`` which is the HTTP equivalent of
:func:`pa_agent.records.analysis_history.find_latest_successful_record`.

The endpoint is intentionally a regular (non-SSE) ``GET`` request: a
preflight is a cheap, single-shot read of a JSON file on disk, so there
is no reason to push it through ``StreamingResponse``. The router is
deliberately thin — all business logic lives in
:class:`pa_agent.web.service.analysis_latest_service.AnalysisLatestService`
and is bootstrapped from the shared
:class:`pa_agent.app_context.AppContext`.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from pa_agent.web.service.analysis_latest_service import AnalysisLatestService

router = APIRouter(prefix="/api/analysis")


@router.get("/latest")
async def get_latest(
    request: Request,
    symbol: str | None = Query(
        default=None,
        description="Trading symbol. Falls back to settings.general.last_symbol.",
    ),
    timeframe: str | None = Query(
        default=None,
        description="Bar timeframe. Falls back to settings.general.last_timeframe.",
    ),
) -> dict:
    """Return a JSON summary of the latest successful analysis record.

    Responses:

    * ``200`` — record found, body is the summary dict.
    * ``404`` — no successful record matches the resolved
      symbol/timeframe (or symbol/timeframe could not be resolved).
    * ``503`` — the service is not wired into the app (AppContext not
      bootstrapped).
    """
    service: AnalysisLatestService | None = getattr(
        request.app.state, "analysis_latest_service", None
    )
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Analysis latest service not initialized",
        )

    result = service.get_latest(symbol=symbol, timeframe=timeframe)
    if result is None:
        # Mirror the caller's inputs (or the resolved ones) in the error so
        # the UI can show *which* (symbol, timeframe) had no record.
        _, tf_resolved = service._resolve_symbol_tf(symbol, timeframe)  # type: ignore[attr-defined]
        sym_resolved, _ = service._resolve_symbol_tf(symbol, timeframe)  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=404,
            detail=(
                f"No successful analysis record for symbol="
                f"{sym_resolved or '<unset>'} timeframe={tf_resolved or '<unset>'}"
            ),
        )
    return result
