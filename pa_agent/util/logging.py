"""Centralised logging configuration for PA Agent.

Public API
----------
configure_logging(api_key: str = "") -> None
update_api_key(new_key: str) -> None
verify_logging_handlers() -> bool
"""
from __future__ import annotations

import copy
import logging
import logging.handlers
from pathlib import Path
from typing import List

from pa_agent.config.paths import LOG_FILE_PATH
from pa_agent.util.mask_secret import mask_secret

# ── Module-level state ────────────────────────────────────────────────────────

_active_formatters: List["MaskingFormatter"] = []
_configured: bool = False

# ── MaskingFormatter ──────────────────────────────────────────────────────────


class MaskingFormatter(logging.Formatter):
    """Logging formatter that replaces the plaintext API key with its masked form."""

    def __init__(self, fmt: str, api_key: str = "") -> None:
        super().__init__(fmt)
        self._api_key = api_key

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        message = super().format(record)
        if self._api_key:
            message = message.replace(self._api_key, mask_secret(self._api_key))
        return message

    def set_api_key(self, new_key: str) -> None:
        self._api_key = new_key


class ConsoleFormatter(MaskingFormatter):
    """Neutral console progress; keep the original record for file diagnostics."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        is_backup = (
            record.name == "pa_agent.ai.sensenova_fallback"
            or "SenseNova free fallback" in message
        )
        if not is_backup:
            return super().format(record)

        # Never mutate a shared LogRecord: the file handler must retain details.
        public_record = copy.copy(record)
        public_record.name = "pa_agent.analysis"
        if "triggered" in message:
            text = "正在通过备用通道继续分析，请稍等。"
        elif "已由免费模型接管" in message:
            text = "备用通道分析完成，结果已通过校验。"
        elif "exhausted" in message:
            text = "备用通道多次尝试均未成功，本阶段分析失败。"
        elif "aborted" in message:
            text = "备用通道发生异常，本阶段分析未完成。"
        elif "listing failed" in message:
            text = "备用通道暂时无法连接，请稍后重试。"
        elif "validation failed" in message:
            text = "本次返回未通过校验，正在尝试其他通道。"
        elif "miss:" in message:
            text = "本次请求未成功，正在尝试其他通道，请稍等。"
        else:
            text = "正在进行分析，请稍等。"
        public_record.msg = text
        public_record.args = ()
        public_record.exc_info = None
        public_record.exc_text = None
        public_record.stack_info = None
        return super().format(public_record)


# ── Public functions ──────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

_THIRD_PARTY_LOGGERS = ("urllib3", "openai", "httpx")

# tvdatafeed opens a websocket every refresh tick and logs at DEBUG — keep quiet
_QUIET_LOGGER_NAMES = (
    "urllib3",
    "openai",
    "httpx",
    "tvDatafeed",
    "tvDatafeed.main",
    "websocket",
)


def verify_logging_handlers() -> bool:
    """Return True when the expected rotating file handler is attached to root."""
    expected = str(LOG_FILE_PATH.resolve())
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            base = getattr(handler, "baseFilename", "")
            if str(Path(base).resolve()) == expected:
                return True
    return False


def configure_logging(api_key: str = "") -> None:
    """Configure the root logger with rotating file handler and console handler.

    Both handlers use MaskingFormatter that replaces api_key with mask_secret(api_key).
    Third-party loggers (urllib3, openai, httpx) are also attached to the same handlers.

    If handlers were removed after a prior configure_logging call, re-attaches them.
    """
    global _configured  # noqa: PLW0603

    if _configured:
        if api_key:
            update_api_key(api_key)
        if verify_logging_handlers():
            return
        # Handlers missing (e.g. external code cleared root.handlers) — re-install.
        _configured = False

    # Build formatters
    file_formatter = MaskingFormatter(_LOG_FORMAT, api_key=api_key)
    console_formatter = ConsoleFormatter(_LOG_FORMAT, api_key=api_key)

    # Track all active formatters so update_api_key can reach them
    _active_formatters.clear()
    _active_formatters.append(file_formatter)
    _active_formatters.append(console_formatter)

    # Rotating file handler
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)

    # Console (stream) handler — INFO+ only; file keeps DEBUG for troubleshooting
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)

    handlers: list[logging.Handler] = [file_handler, console_handler]

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    # Remove any previously installed handlers to avoid duplicates on re-call
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
    for h in handlers:
        root_logger.addHandler(h)

    # Attach the same handlers to third-party loggers
    for name in _THIRD_PARTY_LOGGERS:
        tp_logger = logging.getLogger(name)
        for h in list(tp_logger.handlers):
            tp_logger.removeHandler(h)
        for h in handlers:
            tp_logger.addHandler(h)
        # Prevent double-logging via root propagation
        tp_logger.propagate = False

    _silence_noisy_libraries()

    _configured = True
    logging.getLogger("pa_agent.diagnostics").info(
        "configure_logging: handlers attached (log_file=%s)", LOG_FILE_PATH
    )


def _silence_noisy_libraries() -> None:
    """Turn down chatty third-party DEBUG loggers (tvdatafeed websocket spam)."""
    for name in _QUIET_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.WARNING)


def update_api_key(new_key: str) -> None:
    """Update the masking key in all active MaskingFormatter instances."""
    for formatter in _active_formatters:
        formatter.set_api_key(new_key)
