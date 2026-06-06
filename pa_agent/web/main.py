"""Web UI entry point — bootstrap AppContext and start Uvicorn."""
from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles

from pa_agent.app_context import AppContext
from pa_agent.web.server import create_app
from pa_agent.web.service.analysis_latest_service import AnalysisLatestService
from pa_agent.web.service.data_service import DataService
from pa_agent.web.service.analysis_service import AnalysisService
from pa_agent.web.service.followup_service import FollowupService
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator


# Resolve the optional Vite-built SPA produced by ``pa_agent/webui-frontend``.
# ``pa_agent/webui-frontend/dist`` is git-ignored; it is created locally by
# ``npm run build`` inside the frontend package. The legacy single-page
# build under ``pa_agent/web/static`` continues to be served from ``/`` so
# the existing UI keeps working even when the Vite bundle is absent.
WEBUI_FRONTEND_DIR: Path = Path(__file__).resolve().parent.parent / "webui-frontend"
WEBUI_DIST_DIR: Path = WEBUI_FRONTEND_DIR / "dist"


def _mount_webui_spa(app) -> bool:
    """Mount the Vite-built SPA at ``/webui`` when its dist directory exists.

    Returns ``True`` when the mount was applied, ``False`` when the
    directory is missing (the legacy ``/`` SPA fallback remains active in
    that case). ``html=True`` lets StaticFiles fall back to the bundle's
    ``index.html`` for any sub-path that has no matching asset — this is
    the standard SPA fallback behaviour.
    """
    if not WEBUI_DIST_DIR.is_dir():
        return False
    app.mount(
        "/webui",
        StaticFiles(directory=str(WEBUI_DIST_DIR), html=True),
        name="webui-frontend",
    )
    return True


def main(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Bootstrap the application context and launch the FastAPI web server."""
    ctx = AppContext.bootstrap()
    data_service = DataService(
        ctx.data_source,
        symbol=ctx.settings.general.last_symbol,
        timeframe=ctx.settings.general.last_timeframe,
        settings=ctx.settings,
    )

    # Wire real orchestrator if all dependencies are available
    orchestrator = None
    if ctx.client is not None:
        orchestrator = TwoStageOrchestrator(
            client=ctx.client,
            assembler=ctx.assembler,
            router=ctx.router,
            validator=ctx.validator,
            pending_writer=ctx.pending_writer,
            exp_reader=ctx.exp_reader,
            settings=ctx.settings,
        )

    # P0 follow-up service — receives the same AppContext-owned
    # dependencies as AnalysisService so it can share the FreeChatSession
    # produced after a successful two-stage analysis.
    followup_service = FollowupService(
        client=ctx.client,
        assembler=ctx.assembler,
        pending_writer=ctx.pending_writer,
        ledger=ctx.ledger,
        settings=ctx.settings,
    )

    analysis_service = AnalysisService(
        orchestrator=orchestrator,
        data_service=data_service,
        ledger=ctx.ledger,
        client=ctx.client,
        assembler=ctx.assembler,
        pending_writer=ctx.pending_writer,
        settings=ctx.settings,
        followup_service=followup_service,
    )

    # P1 auto-incremental preflight: reuses the same AppContext (client,
    # assembler, ledger, settings) as the analysis pipeline.
    analysis_latest_service = AnalysisLatestService.from_ctx(ctx)

    # P0 baseline: phase-1 incremental prewarm — prime the data frame and
    # exercise the stage-1 prompt builder so the first user-triggered
    # ``POST /api/analysis/submit`` is snappy and any wiring issues surface
    # at server startup rather than on the first request.
    prewarm_ok = analysis_service.prewarm(
        bar_count=int(
            getattr(ctx.settings.general, "analysis_bar_count", 80) or 80
        )
    )
    if prewarm_ok:
        ctx.logger.info("Startup prewarm OK; first submit() will be hot")
    else:
        ctx.logger.warning(
            "Startup prewarm skipped: %s", analysis_service.prewarm_error
        )

    app = create_app(
        data_service=data_service,
        analysis_service=analysis_service,
        followup_service=followup_service,
        ledger=ctx.ledger,
        analysis_latest_service=analysis_latest_service,
    )

    # Conditionally mount the Vite-built SPA. When the dist directory is
    # absent (typical for a fresh checkout without ``npm run build``), the
    # legacy ``/`` single-page build remains the only UI entry point.
    if _mount_webui_spa(app):
        ctx.logger.info(
            "Vite SPA mounted at /webui (dist=%s)", WEBUI_DIST_DIR
        )
    else:
        ctx.logger.info(
            "Vite SPA not built; /webui unavailable. Build with "
            "'cd pa_agent/webui-frontend && npm install && npm run build'."
        )

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
