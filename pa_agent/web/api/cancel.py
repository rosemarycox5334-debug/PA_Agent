"""Cancel API — trigger ``CancelToken`` for an in-flight analysis run.

Mirrors the PyQt ``on_stop_clicked`` behavior: looks up the active run by
``run_id`` and calls ``.set()`` on its :class:`CancelToken` so the underlying
``TwoStageOrchestrator.submit()`` can return early via its periodic cancel
checks.

The endpoint deliberately performs no business logic — it only resolves the
registry from ``app.state`` and forwards to
:meth:`CancelRegistry.cancel`. All bookkeeping (registration / lifecycle /
SSE wiring) lives in :mod:`pa_agent.web.service.cancel_service` and
:mod:`pa_agent.web.service.analysis_service`.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from pa_agent.web.service.cancel_service import CancelRegistry, default_registry

router = APIRouter(prefix="/api/analysis")


def _resolve_registry(request: Request) -> CancelRegistry:
    """Pull the per-app ``CancelRegistry`` from ``app.state``, fall back to the
    module-level singleton so tests and embeddings don't need extra wiring.
    """
    registry = getattr(request.app.state, "cancel_registry", None)
    if isinstance(registry, CancelRegistry):
        return registry
    return default_registry()


@router.post("/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str) -> dict:
    """Signal cancellation for the analysis run identified by ``run_id``.

    Returns ``{"status": "cancelled", "run_id": ...}`` on success or
    HTTP 404 if no in-flight run matches the identifier (already finished,
    never started, or the worker process restarted).
    """
    if not run_id or len(run_id) > 128:
        # Reject obviously malformed ids early — no body required.
        raise HTTPException(status_code=400, detail="Invalid run_id")

    registry = _resolve_registry(request)
    cancelled = registry.cancel(run_id)
    if not cancelled:
        raise HTTPException(
            status_code=404,
            detail=f"No active analysis run for run_id={run_id!r}",
        )
    return {"status": "cancelled", "run_id": run_id}
