"""Application context wiring shared resources without global singletons."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AppContext:
    """Carries shared resources to GUI widgets and orchestrators."""

    settings: Any = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pa_agent"))
    event_bus: Any = None

    # Data layer
    data_source: Any = None       # DataSource implementation

    # AI / orchestration layer
    client: Any = None            # DeepSeekClient
    assembler: Any = None         # PromptAssembler
    router: Any = None            # route_strategy_files callable
    validator: Any = None         # JsonValidator
    pending_writer: Any = None    # PendingWriter
    exp_reader: Any = None        # ExperienceReader
    ledger: Any = None            # SessionTokenLedger

    @classmethod
    def bootstrap(cls) -> "AppContext":
        """Wire all real components and return a fully initialised AppContext."""
        from pa_agent.config.paths import (
            SETTINGS_JSON_PATH,
            RECORDS_PENDING_DIR,
            EXPERIENCE_DIR,
            PROMPT_DIR,
        )
        from pa_agent.config.settings import load_settings
        from pa_agent.util.logging import configure_logging, update_api_key
        from pa_agent.util.event_bus import EventBus
        from pa_agent.util.mask_secret import mask_secret
        from pa_agent.data.factory import create_data_source, normalize_data_source_kind
        from pa_agent.ai.client_factory import create_ai_client
        from pa_agent.ai.prompt_assembler import PromptAssembler
        from pa_agent.ai.router import route_strategy_files
        from pa_agent.ai.json_validator import JsonValidator
        from pa_agent.ai.session_ledger import SessionTokenLedger
        from pa_agent.records.pending_writer import PendingWriter
        from pa_agent.records.experience_reader import ExperienceReader

        # ── Settings ──────────────────────────────────────────────────────────
        settings = load_settings(SETTINGS_JSON_PATH)
        from pa_agent.ai.qclaw_connector import sync_qclaw_agent_provider_on_load
        from pa_agent.ai.workbuddy_connector import sync_workbuddy_provider_on_load
        from pa_agent.ai.cursor_connector import sync_cursor_provider_on_load
        from pa_agent.ai.trae_connector import sync_trae_cn_provider_on_load
        from pa_agent.ai.qoder_connector import sync_qoder_cn_provider_on_load

        sync_qclaw_agent_provider_on_load(settings, save_path=SETTINGS_JSON_PATH)
        sync_workbuddy_provider_on_load(settings, save_path=SETTINGS_JSON_PATH)
        sync_cursor_provider_on_load(settings, save_path=SETTINGS_JSON_PATH)
        sync_trae_cn_provider_on_load(settings, save_path=SETTINGS_JSON_PATH)
        sync_qoder_cn_provider_on_load(settings, save_path=SETTINGS_JSON_PATH)

        # ── Logging (with API key masking) ────────────────────────────────────
        configure_logging(api_key=settings.provider.api_key)

        app_logger = logging.getLogger("pa_agent")

        # ── Event bus ─────────────────────────────────────────────────────────
        event_bus = EventBus()

        # ── Data layer ────────────────────────────────────────────────────────
        from pa_agent.data.kline_adjust import apply_kline_adjust_from_settings

        apply_kline_adjust_from_settings(settings)
        ds_kind = normalize_data_source_kind(
            getattr(settings.general, "last_data_source", "mt5")
        )
        data_source = create_data_source(ds_kind)

        # Subscribe to the last-used symbol/timeframe from settings
        try:
            data_source.connect()
            if ds_kind == "tradingview":
                from pa_agent.data.tradingview import TradingViewSource

                if isinstance(data_source, TradingViewSource):
                    # Use saved exchange setting, default to auto (empty).
                    saved_exchange = getattr(settings.general, 'last_tradingview_exchange', '') or ''
                    data_source.set_exchange(saved_exchange)
            data_source.subscribe(
                settings.general.last_symbol,
                settings.general.last_timeframe,
            )
            app_logger.info(
                "Data source %s subscribed to %s %s",
                ds_kind,
                settings.general.last_symbol,
                settings.general.last_timeframe,
            )
        except Exception as exc:  # noqa: BLE001
            app_logger.warning("Initial data source subscription failed: %s", exc)

        # ── AI client ─────────────────────────────────────────────────────────
        client = create_ai_client(settings.provider, logger_=app_logger)

        # ── Prompt assembler ──────────────────────────────────────────────────
        exp_reader = ExperienceReader(experience_dir=EXPERIENCE_DIR, logger=app_logger)
        assembler = PromptAssembler(
            prompt_dir=PROMPT_DIR,
            experience_reader=exp_reader,
            prompt_settings=settings.prompt,
        )

        # ── Validator & router ────────────────────────────────────────────────
        validator = JsonValidator(settings)
        router = route_strategy_files

        # ── Pending writer ────────────────────────────────────────────────────
        pending_writer = PendingWriter(
            pending_dir=RECORDS_PENDING_DIR,
            event_bus=event_bus,
            api_key=settings.provider.api_key,
        )

        # ── Session ledger ────────────────────────────────────────────────────
        ledger = SessionTokenLedger(
            context_window=settings.provider.context_window,
            warn_pct=settings.general.context_warning_threshold_pct,
        )

        return cls(
            settings=settings,
            logger=app_logger,
            event_bus=event_bus,
            data_source=data_source,
            client=client,
            assembler=assembler,
            router=router,
            validator=validator,
            pending_writer=pending_writer,
            exp_reader=exp_reader,
            ledger=ledger,
        )
