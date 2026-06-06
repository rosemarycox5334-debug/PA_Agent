"""Service layer for the P1 auto-incremental preflight lookup.

Wraps :func:`pa_agent.records.analysis_history.find_latest_successful_record`
so that the web layer can ask "is there a previous successful analysis for
the current symbol/timeframe?" without re-implementing the file scan or
record validation rules. The service is built from the shared
:class:`pa_agent.app_context.AppContext` and therefore reuses the already
configured ``ctx.client``, ``ctx.assembler``, ``ctx.ledger`` and
``ctx.settings`` instances.

The service is intentionally side-effect free: it only reads from
``records/pending/`` and returns a JSON-friendly summary. It does not
mutate the ledger or trigger another analysis.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pa_agent.records.analysis_history import find_latest_successful_record

if TYPE_CHECKING:
    from pa_agent.app_context import AppContext
    from pa_agent.records.schema import AnalysisRecord


class AnalysisLatestService:
    """Read-only lookup of the most recent successful analysis record.

    The service is the single place where the mapping from an
    ``AnalysisRecord`` (a pydantic model) to a JSON-serialisable dict
    lives. Routers must not perform that conversion themselves.
    """

    def __init__(
        self,
        settings: Any | None = None,
        ledger: Any | None = None,
    ) -> None:
        # ``ledger`` is kept for future preflight telemetry (e.g. logging the
        # lookup cost or surfacing token usage) and to preserve the AppContext
        # wiring contract — every service in ``pa_agent/web/service/`` is
        # bootstrapped with the same dependency set.
        self._settings = settings
        self._ledger = ledger

    @classmethod
    def from_ctx(cls, ctx: "AppContext") -> "AnalysisLatestService":
        """Build a service that reuses the bootstrapped AppContext."""
        return cls(settings=ctx.settings, ledger=ctx.ledger)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_latest(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> dict | None:
        """Return a JSON summary of the latest successful record, or ``None``.

        If ``symbol``/``timeframe`` are omitted they fall back to
        ``settings.general.last_symbol`` / ``settings.general.last_timeframe``
        so the caller can simply ask "is incremental available for the
        current context?" without threading the symbol through every
        preflight call.
        """
        resolved_symbol, resolved_timeframe = self._resolve_symbol_tf(
            symbol, timeframe
        )
        if not resolved_symbol or not resolved_timeframe:
            return None

        record = find_latest_successful_record(
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
        )
        if record is None:
            return None
        return self._summarize(record)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _resolve_symbol_tf(
        self, symbol: str | None, timeframe: str | None
    ) -> tuple[str | None, str | None]:
        if (symbol and timeframe):
            return symbol, timeframe
        if self._settings is None:
            return symbol, timeframe
        general = getattr(self._settings, "general", None)
        if general is None:
            return symbol, timeframe
        return (
            symbol or getattr(general, "last_symbol", None),
            timeframe or getattr(general, "last_timeframe", None),
        )

    @staticmethod
    def _summarize(record: "AnalysisRecord") -> dict:
        """Convert an ``AnalysisRecord`` to a JSON-friendly dict.

        The shape is intentionally compact: enough for the UI to decide
        whether to enable an "incremental" CTA and to show the timestamp /
        anchor bar of the previous analysis. Raw Stage-1 / Stage-2
        messages are omitted because they are large and the UI does not
        need them at preflight time.
        """
        meta = record.meta
        kline_data = record.kline_data or []
        latest_bar = kline_data[0] if kline_data else None
        return {
            "found": True,
            "meta": {
                "symbol": meta.symbol,
                "timeframe": meta.timeframe,
                "timestamp_local_iso": meta.timestamp_local_iso,
                "timestamp_local_ms": meta.timestamp_local_ms,
                "bar_count": meta.bar_count,
                "decision_stance": meta.decision_stance,
            },
            "kline_data_count": len(kline_data),
            "latest_bar": latest_bar,
            "has_stage1": bool(record.stage1_diagnosis),
            "has_stage2": bool(record.stage2_decision),
            "stage1_diagnosis": record.stage1_diagnosis,
            "stage2_decision": record.stage2_decision,
            "usage_total": record.usage_total or {},
            "strategy_files_used": list(record.strategy_files_used or []),
            "exception": record.exception,
        }
