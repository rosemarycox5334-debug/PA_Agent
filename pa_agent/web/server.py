"""FastAPI app factory for PA Agent Web UI."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pa_agent.web.api import analysis_latest as analysis_latest_api
from pa_agent.web.api import analysis_submit as analysis_api
from pa_agent.web.api import cancel as cancel_api
from pa_agent.web.api import data as data_api
from pa_agent.web.api import debug as debug_api
from pa_agent.web.api import decision_tree as dt_api
from pa_agent.web.api import events as events_api
from pa_agent.web.api import followup as followup_api
from pa_agent.web.api import ledger as ledger_api
from pa_agent.web.api import records as records_api
from pa_agent.web.api import settings as settings_api
from pa_agent.web.api import sources as sources_api
from pa_agent.web.api import stream as stream_api
from pa_agent.web.service.analysis_latest_service import AnalysisLatestService
from pa_agent.web.service.analysis_service import AnalysisService
from pa_agent.web.service.cancel_service import CancelRegistry, default_registry
from pa_agent.web.service.data_service import DataService
from pa_agent.web.service.data_source_service import DataSourceService
from pa_agent.web.service.followup_service import FollowupService

STATIC_DIR: Path = Path(__file__).parent / "static"


def create_app(
    data_service: DataService | None = None,
    analysis_service: AnalysisService | None = None,
    followup_service: FollowupService | None = None,
    ledger: object | None = None,
    analysis_latest_service: AnalysisLatestService | None = None,
    cancel_registry: CancelRegistry | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    ``analysis_latest_service`` powers the P1 auto-incremental preflight
    endpoint (``GET /api/analysis/latest``). When the caller does not
    supply it, ``create_app`` falls back to constructing a service from
    the data service's settings (when available) so the endpoint remains
    usable in lightweight test harnesses that bootstrap only the ledger.

    ``followup_service`` powers the P0 follow-up SSE endpoint
    (``POST /api/analysis/followup``). It is wired from the AppContext by
    :func:`pa_agent.web.main.main` and shares the same FreeChatSession
    produced by a successful two-stage analysis.

    ``cancel_registry`` is the shared :class:`CancelRegistry` used by both
    :class:`AnalysisService` (to register a token at submit start) and the
    cancel router (to trigger it from
    ``POST /api/analysis/{run_id}/cancel``). When not supplied, the
    registry attached to ``analysis_service`` is reused; otherwise the
    module-level singleton is used so both producer and consumer agree.
    """
    app = FastAPI(title="PA Agent Web", version="1.0.0")
    app.state.data_service = data_service
    app.state.analysis_service = analysis_service
    app.state.ledger = ledger
    settings = getattr(data_service, '_settings', None)
    if followup_service is None and analysis_service is not None:
        from pa_agent.web.service.followup_service import FollowupService
        followup_service = FollowupService(ledger=ledger, settings=settings)
        followup_service.set_chat_session(
            getattr(analysis_service, "_chat_session", None)
        )
    app.state.followup_service = followup_service
    # Mirror the PyQt ``MainWindow.self._cancel_token`` registry on the app
    # state so the cancel router can resolve in-flight runs by run_id.
    if cancel_registry is None and analysis_service is not None:
        cancel_registry = getattr(analysis_service, "cancel_registry", None)
    app.state.cancel_registry = cancel_registry or default_registry()

    # Pre-warm the data-source enumeration service so the
    # ``GET /api/data/sources`` endpoint does not need lazy fallback paths.
    app.state.data_source_service = DataSourceService(
        data_service=data_service, settings=settings
    )

    # Resolve the analysis-latest service. If the caller wired a real one
    # (typically built from AppContext in main.py) use it; otherwise fall
    # back to the data service's settings so the endpoint does not 503 in
    # tests that do not bootstrap the full AppContext.
    app.state.analysis_latest_service = (
        analysis_latest_service
        if analysis_latest_service is not None
        else AnalysisLatestService(settings=settings, ledger=ledger)
    )

    app.include_router(data_api.router)
    app.include_router(sources_api.router)
    app.include_router(stream_api.router)
    app.include_router(analysis_api.router)
    app.include_router(analysis_latest_api.router)
    app.include_router(cancel_api.router)
    app.include_router(events_api.router)
    app.include_router(followup_api.router)
    app.include_router(settings_api.router)
    app.include_router(ledger_api.router)
    app.include_router(records_api.router)
    app.include_router(debug_api.router)
    app.include_router(dt_api.router)

    # Mount static files
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        """Serve the main HTML page."""
        return FileResponse(STATIC_DIR / "index.html")

    return app
