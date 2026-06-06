"""Service layer for enumerating available data sources.

This service is consumed by:
  - ``SettingsDialog`` (data-source group picker)
  - ``TradingView Connectivity`` panel (data-source switcher)

It provides a *static* enumeration of the data sources the application
supports, plus optional *live* metadata (e.g. default symbol, connection
status) that is queried from the injected :class:`DataService` when
available.  No business logic lives in the router — the router simply
translates HTTP concerns and delegates to this service.
"""
from __future__ import annotations

from typing import Any

from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    default_symbol_for_kind,
)


class DataSourceService:
    """Enumerate the data sources supported by the application.

    Parameters
    ----------
    data_service:
        Optional :class:`DataService` instance.  When supplied, the service
        can enrich each entry with a default symbol sourced from
        ``settings.general`` (e.g. for the TV Connectivity switcher).
    settings:
        Optional :class:`Settings` object.  Read-only — used to look up the
        user's last-used data source and default symbol per kind.
    """

    def __init__(self, data_service: Any | None = None, settings: Any | None = None) -> None:
        self._data_service = data_service
        self._settings = settings

    def list_sources(self) -> list[dict]:
        """Return the catalogue of supported data sources.

        Each entry has the shape::

            {
                "id":          "tradingview",
                "label":       "TradingView",
                "default_symbol": "GC1!",
                "is_active":   True,            # matches last_data_source
                "ready":       True,            # data service wired up
            }

        ``ready`` is always ``True`` when the data service is injected and
        ``False`` otherwise so the UI can disable a switcher gracefully.
        """
        active_kind = self._resolve_active_kind()
        ready = self._data_service is not None

        sources: list[dict] = []
        for kind, label in DATA_SOURCE_CHOICES:
            sources.append(
                {
                    "id": kind,
                    "label": label,
                    "default_symbol": default_symbol_for_kind(kind),
                    "is_active": kind == active_kind,
                    "ready": ready,
                }
            )
        return sources

    # ── internal helpers ────────────────────────────────────────────────

    def _resolve_active_kind(self) -> str | None:
        """Return the kind id marked as active in settings (if any)."""
        if self._settings is None:
            return None
        general = getattr(self._settings, "general", None)
        if general is None:
            return None
        kind = getattr(general, "last_data_source", None)
        if not kind:
            return None
        # Only return kinds that are actually part of the catalogue so the
        # caller can do an exact string match.
        for supported_kind, _ in DATA_SOURCE_CHOICES:
            if supported_kind == kind:
                return supported_kind
        return None
