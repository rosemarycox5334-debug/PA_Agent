"""Analysis API: re-export the dedicated submit router for backward compatibility.

The :class:`APIRouter` defined here re-exports the router from
:mod:`pa_agent.web.api.analysis_submit` so legacy imports
(``from pa_agent.web.api import analysis as analysis_api``) keep working
without registering duplicate routes. All submit-related logic lives in
the dedicated :mod:`pa_agent.web.api.analysis_submit` module and the
business layer in :mod:`pa_agent.web.service.analysis_service`.
"""
from __future__ import annotations

from pa_agent.web.api.analysis_submit import router

__all__ = ["router"]
