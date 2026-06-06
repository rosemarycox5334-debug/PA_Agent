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
        from pa_agent.data.factory import (
            configure_data_source,
            create_data_source,
            normalize_data_source_kind,
        )
        from pa_agent.ai.deepseek_client import DeepSeekClient
        from pa_agent.ai.prompt_assembler import PromptAssembler
        from pa_agent.ai.router import route_strategy_files
        from pa_agent.ai.json_validator import JsonValidator
        from pa_agent.ai.session_ledger import SessionTokenLedger
        from pa_agent.records.pending_writer import PendingWriter
        from pa_agent.records.experience_reader import ExperienceReader

        # ── Settings ──────────────────────────────────────────────────────────
        settings = load_settings(SETTINGS_JSON_PATH)

        # ── Logging (with API key masking) ────────────────────────────────────
        configure_logging(api_key=settings.provider.api_key)

        app_logger = logging.getLogger("pa_agent")

        # ── Event bus ─────────────────────────────────────────────────────────
        event_bus = EventBus()

        # ── Data layer ────────────────────────────────────────────────────────
        ds_kind = normalize_data_source_kind(
            getattr(settings.general, "last_data_source", "mt5")
        )
        data_source = create_data_source(ds_kind)

        # Pre-configure credentials/exchange before connect().
        configure_data_source(data_source, ds_kind, settings)

        # Subscribe to the last-used symbol/timeframe from settings
        try:
            data_source.connect()
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
        client = DeepSeekClient(settings=settings.provider, logger_=app_logger)

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
