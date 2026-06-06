"""Settings API endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Request

from pa_agent.config.settings import Settings, load_settings, save_settings

router = APIRouter(prefix="/api/settings")


@router.get("")
async def get_settings() -> dict:
    """Return current settings (api_key / rqdata_license_key are masked)."""
    settings = load_settings()
    g = settings.general
    p = settings.provider
    return {
        "provider": {
            "model": p.model,
            "base_url": p.base_url,
            "api_key": "***" if p.api_key else "",
            "thinking": p.thinking,
            "reasoning_effort": p.reasoning_effort,
            "context_window": p.context_window,
        },
        "general": {
            "analysis_bar_count": g.analysis_bar_count,
            "refresh_interval_ms": g.refresh_interval_ms,
            "context_warning_threshold_pct": g.context_warning_threshold_pct,
            "last_data_source": g.last_data_source,
            "last_symbol": g.last_symbol,
            "last_timeframe": g.last_timeframe,
            "decision_stance": g.decision_stance,
            "rqdata_license_key": "***" if g.rqdata_license_key else "",
            "auto_resume_chart_after_analysis": g.auto_resume_chart_after_analysis,
            "stream_pane_font_pt": g.stream_pane_font_pt,
            "chart_seq_label_font_pt": g.chart_seq_label_font_pt,
            "incremental_max_new_bars": g.incremental_max_new_bars,
            "decision_flow_auto_play": g.decision_flow_auto_play,
            "decision_flow_play_seconds": g.decision_flow_play_seconds,
            "decision_flow_default_zoom_pct": g.decision_flow_default_zoom_pct,
        },
    }


@router.post("")
async def update_settings(request: Request, payload: dict) -> dict:
    """Update general and provider settings.

    provider.api_key and general.rqdata_license_key accept "***"
    as a sentinel meaning "keep existing value".
    """
    settings = load_settings()

    if "general" in payload:
        for key, value in payload["general"].items():
            if not hasattr(settings.general, key):
                continue
            if key in ("rqdata_license_key",) and value == "***":
                continue
            setattr(settings.general, key, value)

    if "provider" in payload:
        for key, value in payload["provider"].items():
            if not hasattr(settings.provider, key):
                continue
            if key == "api_key" and value == "***":
                continue
            setattr(settings.provider, key, value)

    save_settings(settings)

    data_source_error = None

    # Sync data service after symbol/timeframe, provider, or credential changes.
    data_service = getattr(request.app.state, "data_service", None)
    if data_service is not None:
        try:
            if hasattr(data_service, "apply_settings"):
                data_service.apply_settings(settings)
            else:
                new_symbol = getattr(settings.general, "last_symbol", None)
                new_timeframe = getattr(settings.general, "last_timeframe", None)
                if new_symbol and new_timeframe:
                    data_service.update_subscription(new_symbol, new_timeframe)
        except Exception as exc:  # noqa: BLE001
            data_source_error = str(exc)
            try:
                data_service.last_error = data_source_error
            except Exception:
                pass

    result = {"status": "ok"}
    if data_source_error:
        result["data_source_error"] = data_source_error
    return result
