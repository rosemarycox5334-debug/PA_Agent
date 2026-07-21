"""Headless 装配：无 EventBus/SessionTokenLedger/Qt，供服务端使用.

复刻 :meth:`pa_agent.app_context.AppContext.bootstrap` 的接线，差异：

- 不创建 ``EventBus`` / ``SessionTokenLedger``（仅有的两个 Qt 组件）；
- 不在启动时预订阅品种（由轮巡调度器按品种订阅）；
- ``mt5`` 数据源强制回退 ``tradingview``（Linux 容器无 MT5）。
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROVIDER_SYNCS = (
    ("pa_agent.ai.qclaw_connector", "sync_qclaw_agent_provider_on_load"),
    ("pa_agent.ai.workbuddy_connector", "sync_workbuddy_provider_on_load"),
    ("pa_agent.ai.cursor_connector", "sync_cursor_provider_on_load"),
)


@dataclass(slots=True)
class ServerContext:
    """服务端共享组件容器（对应 GUI 侧的 AppContext）."""

    settings: Any
    logger: logging.Logger
    data_source: Any
    client: Any
    assembler: Any
    router: Any
    validator: Any
    pending_writer: Any
    exp_reader: Any


def bootstrap_headless(settings_path: Path | None = None) -> ServerContext:
    """装配全部引擎组件并返回 ServerContext（不含任何 Qt 对象）."""
    from pa_agent.config.paths import (
        EXPERIENCE_DIR,
        PROMPT_DIR,
        RECORDS_PENDING_DIR,
        SETTINGS_JSON_PATH,
    )
    from pa_agent.config.settings import load_settings
    from pa_agent.util.logging import configure_logging

    path = settings_path or SETTINGS_JSON_PATH
    settings = load_settings(path)

    # 网关型 provider 的本地同步（QClaw/WorkBuddy/Cursor）；失败不阻塞启动
    for mod_name, fn_name in _PROVIDER_SYNCS:
        try:
            getattr(importlib.import_module(mod_name), fn_name)(
                settings, save_path=path
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("pa_agent").warning(
                "provider 同步跳过 %s: %s", fn_name, exc
            )

    configure_logging(api_key=settings.provider.api_key)
    app_logger = logging.getLogger("pa_agent.server")

    from pa_agent.data.kline_adjust import apply_kline_adjust_from_settings

    apply_kline_adjust_from_settings(settings)

    data_source = _create_data_source_from_settings(settings)

    from pa_agent.ai.client_factory import create_ai_client

    client = create_ai_client(settings.provider, logger_=app_logger)

    from pa_agent.ai.prompt_assembler import PromptAssembler
    from pa_agent.records.experience_reader import ExperienceReader

    exp_reader = ExperienceReader(experience_dir=EXPERIENCE_DIR, logger=app_logger)
    assembler = PromptAssembler(
        prompt_dir=PROMPT_DIR,
        experience_reader=exp_reader,
        prompt_settings=settings.prompt,
    )

    from pa_agent.ai.json_validator import JsonValidator
    from pa_agent.ai.router import route_strategy_files
    from pa_agent.records.pending_writer import PendingWriter

    pending_writer = PendingWriter(
        pending_dir=RECORDS_PENDING_DIR,
        event_bus=None,
        api_key=settings.provider.api_key,
    )

    return ServerContext(
        settings=settings,
        logger=app_logger,
        data_source=data_source,
        client=client,
        assembler=assembler,
        router=route_strategy_files,
        validator=JsonValidator(settings),
        pending_writer=pending_writer,
        exp_reader=exp_reader,
    )


def _create_data_source_from_settings(settings: Any) -> Any:
    from pa_agent.data.factory import create_data_source, normalize_data_source_kind

    kind = normalize_data_source_kind(
        getattr(settings.general, "last_data_source", "tradingview")
    )
    if kind == "mt5":  # 服务端（Linux 容器）无 MT5，强制回退
        kind = "tradingview"
    ds = create_data_source(kind)
    if kind == "tradingview":
        from pa_agent.data.tradingview import TradingViewSource

        if isinstance(ds, TradingViewSource):
            ds.set_exchange(
                getattr(settings.general, "last_tradingview_exchange", "") or ""
            )
    return ds


def build_orchestrator(ctx: ServerContext) -> Any:
    """用 ctx 组件构建 TwoStageOrchestrator（与 GUI _build_orchestrator 等价）."""
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator

    return TwoStageOrchestrator(
        client=ctx.client,
        assembler=ctx.assembler,
        router=ctx.router,
        validator=ctx.validator,
        pending_writer=ctx.pending_writer,
        exp_reader=ctx.exp_reader,
        settings=ctx.settings,
    )


def rebuild_client(ctx: ServerContext) -> None:
    """按 ctx.settings.provider 重建 LLM 客户端（配置保存后调用）."""
    from pa_agent.ai.client_factory import create_ai_client

    ctx.client = create_ai_client(ctx.settings.provider, logger_=ctx.logger)


def rebuild_data_source(ctx: ServerContext) -> None:
    """按 ctx.settings 重建数据源；不主动 connect（由使用方连接）."""
    try:
        if ctx.data_source is not None:
            ctx.data_source.disconnect()
    except Exception as exc:  # noqa: BLE001
        ctx.logger.warning("旧数据源断开失败: %s", exc)
    ctx.data_source = _create_data_source_from_settings(ctx.settings)
