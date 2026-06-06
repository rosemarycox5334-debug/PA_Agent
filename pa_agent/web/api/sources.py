"""Data-source enumeration API.

Exposes ``GET /api/data/sources`` so the front-end can populate:

  - ``SettingsDialog`` — data-source group picker
  - ``TradingView Connectivity`` panel — data-source switcher UI

The endpoint is intentionally read-only; it returns a static catalogue of
the data sources the app knows how to instantiate plus lightweight
metadata (default symbol, ``is_active``, ``ready``).  All enumeration
logic lives in :class:`DataSourceService` — this module only handles
HTTP wiring.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from pa_agent.web.service.data_source_service import DataSourceService

router = APIRouter(prefix="/api/data")


def _get_service(request: Request) -> DataSourceService:
    """Resolve (or lazily build) a :class:`DataSourceService` for this request."""
    service: DataSourceService | None = getattr(
        request.app.state, "data_source_service", None
    )
    if service is not None:
        return service

    data_service = getattr(request.app.state, "data_service", None)
    settings = getattr(data_service, "_settings", None)
    if settings is None:
        # Fall back to a bare service that knows only the static catalogue.
        service = DataSourceService(data_service=None, settings=None)
    else:
        service = DataSourceService(data_service=data_service, settings=settings)
    request.app.state.data_source_service = service
    return service


@router.get("/sources")
async def list_sources(request: Request) -> dict:
    """Return the catalogue of supported data sources.

    The response shape is::

        {
            "sources": [
                {
                    "id":            "mt5" | "tradingview" | "rqdata",
                    "label":         <human readable>,
                    "default_symbol": <symbol string>,
                    "is_active":     <bool>,
                    "ready":         <bool>,
                },
                ...
            ]
        }
    """
    service = _get_service(request)
    return {"sources": service.list_sources()}
