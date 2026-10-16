"""Qoder CN connector for PA Agent.

Detects the local Qoder CN installation, reads its machine_token,
and routes PA Agent through Qoder CN's local sidecar WebSocket API
(``ws://127.0.0.1:36510/ws``) using JSON-RPC 2.0 over LSP framing.

Usage::

    from pa_agent.ai.qoder_connector import (
        detect_qoder_cn,
        qoder_cn_provider_settings,
    )

    if detect_qoder_cn():
        settings.provider = qoder_cn_provider_settings()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import json
import logging
import os
import socket
from pathlib import Path
from typing import Any

if TYPE_CHECKING:  # type-checker-only import (annotations stay lazy at runtime)
    from pa_agent.config.settings import AIProviderSettings


logger = logging.getLogger(__name__)

_QODER_MODEL = "openclaw_qc"
# Default model key for Qoder CN's chat/ask API.
# "auto" lets Qoder CN select the best model; "qmodel_38max" = Qwen3.8-Max.
_QODER_DEFAULT_INTERNAL_MODEL = "auto"

# Qoder CN sidecar WebSocket endpoint (local only).
_QODER_WS_HOST = "127.0.0.1"
_QODER_WS_PORT = 36510
_QODER_WS_PATH = "/ws"
_QODER_WS_URL = f"ws://{_QODER_WS_HOST}:{_QODER_WS_PORT}{_QODER_WS_PATH}"

# Qoder CN known data directories (Windows).
_APPDATA = os.environ.get("APPDATA", "").strip()


def _candidate_data_dirs() -> list[Path]:
    """Return candidate Qoder CN data dirs, most-likely-first."""
    dirs: list[Path] = []
    env_override = os.environ.get("QODER_CN_DATA_DIR", "").strip()
    if env_override:
        dirs.append(Path(env_override))
    if _APPDATA:
        dirs.append(Path(_APPDATA) / "QoderCN")
    dirs.append(Path.home() / ".qoder-cn")
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _find_data_dir() -> Path | None:
    """Return the first candidate data dir that has machine_token.json."""
    for d in _candidate_data_dirs():
        token_file = d / "SharedClientCache" / "cache" / "machine_token.json"
        if token_file.exists():
            return d
    return None


_QODER_CN_DATA_DIR = _find_data_dir()


def _token_path() -> Path | None:
    if _QODER_CN_DATA_DIR is None:
        return None
    p = _QODER_CN_DATA_DIR / "SharedClientCache" / "cache" / "machine_token.json"
    return p if p.exists() else None


_QODER_CN_TOKEN_FILE = Path.home() / ".qoder_cn_token"


# ── Model alias detection ────────────────────────────────────────────────────

def is_openclaw_qc_model(model: str | None) -> bool:
    """True when the user selected Qoder CN's model route.

    Accepts the bare alias ``openclaw_qc`` and variants such as
    ``openclaw_qc/qmodel_38max`` (specific model under Qoder CN route).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    return m == _QODER_MODEL or m.startswith(f"{_QODER_MODEL}/")


def should_use_qoder_cn_provider(
    model: str | None,
    base_url: str | None = None,
) -> bool:
    """True when settings Save should auto-configure from Qoder CN."""
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import is_openclaw_model
    from pa_agent.ai.trae_connector import is_openclaw_twc_model
    from pa_agent.ai.workbuddy_connector import is_openclaw_wb_model

    if (
        is_openclaw_model(model)
        or is_openclaw_cs_model(model)
        or is_openclaw_wb_model(model)
        or is_openclaw_twc_model(model)
    ):
        return False
    if is_openclaw_qc_model(model):
        return True
    if not detect_qoder_cn():
        return False
    base = (base_url or "").strip().lower()
    if not base:
        return False
    return f"127.0.0.1:{_QODER_WS_PORT}" in base or f"localhost:{_QODER_WS_PORT}" in base


def is_qoder_cn_route(provider: Any) -> bool:
    """True when provider targets Qoder CN's WebSocket API."""
    model = str(getattr(provider, "model", "") or "").strip().lower()
    if is_openclaw_qc_model(model):
        return True
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import is_openclaw_model
    from pa_agent.ai.trae_connector import is_openclaw_twc_model
    from pa_agent.ai.workbuddy_connector import is_openclaw_wb_model

    if (
        is_openclaw_model(model)
        or is_openclaw_cs_model(model)
        or is_openclaw_wb_model(model)
        or is_openclaw_twc_model(model)
    ):
        return False
    base = str(getattr(provider, "base_url", "") or "").strip().lower()
    return f"127.0.0.1:{_QODER_WS_PORT}" in base or f"localhost:{_QODER_WS_PORT}" in base


def resolve_qoder_cn_api_model(model: str | None) -> str:
    """Resolve the actual model key to send to Qoder CN's API.

    ``openclaw_qc/qmodel_38max`` -> ``qmodel_38max``
    ``openclaw_qc`` -> ``auto`` (default)
    """
    raw = (model or "").strip()
    if raw.lower().startswith(f"{_QODER_MODEL}/"):
        suffix = raw[len(_QODER_MODEL) + 1:]
        return suffix.strip() or _QODER_DEFAULT_INTERNAL_MODEL
    return _QODER_DEFAULT_INTERNAL_MODEL


# ── Detection ─────────────────────────────────────────────────────────────────

def detect_qoder_cn() -> bool:
    """Return True if Qoder CN is installed (machine_token.json exists)."""
    if os.environ.get("QODER_CN_API_TOKEN"):
        return True
    if _QODER_CN_TOKEN_FILE.exists():
        return True
    if _QODER_CN_DATA_DIR is not None:
        return True
    return False


def is_qoder_cn_sidecar_running(*, timeout: float = 2.0) -> bool:
    """Check if the Qoder CN sidecar WebSocket port is accepting connections."""
    try:
        with socket.create_connection(
            (_QODER_WS_HOST, _QODER_WS_PORT), timeout=timeout
        ):
            return True
    except (OSError, socket.timeout):
        return False


# ── Token extraction ──────────────────────────────────────────────────────────

def _extract_qoder_cn_token() -> str | None:
    """Try to extract Qoder CN machine_token from multiple sources.

    Priority:
    1. ``QODER_CN_API_TOKEN`` env var — manual override
    2. ``~/.qoder_cn_token`` file — manual override
    3. ``machine_token.json`` from Qoder CN's SharedClientCache (primary)
    """
    token = os.environ.get("QODER_CN_API_TOKEN", "").strip()
    if token:
        logger.debug("Using token from env var QODER_CN_API_TOKEN")
        return token

    if _QODER_CN_TOKEN_FILE.exists():
        try:
            token = _QODER_CN_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                logger.debug("Using token from %s", _QODER_CN_TOKEN_FILE)
                return token
        except OSError:
            pass

    tp = _token_path()
    if tp is not None:
        try:
            data = json.loads(tp.read_text(encoding="utf-8"))
            token = str(data.get("token", "")).strip()
            if token:
                logger.debug("Using token from machine_token.json")
                return token
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Failed to read machine_token.json: %s", exc)

    return None


# ── Provider settings ─────────────────────────────────────────────────────────

def _get_qoder_cn_info() -> tuple[str, str] | None:
    """Return (ws_url, token) for Qoder CN, or None."""
    token = _extract_qoder_cn_token()
    if not token:
        return None
    return _QODER_WS_URL, token


def qoder_cn_provider_settings(
    model: str | None = None,
    thinking: bool = True,
    reasoning_effort: str = "high",
    context_window: int = 180_000,
) -> "AIProviderSettings | None":
    """Return AIProviderSettings for Qoder CN's model route."""
    from pa_agent.config.settings import AIProviderSettings

    info = _get_qoder_cn_info()
    if info is None:
        logger.debug(
            "Qoder CN info unavailable; set QODER_CN_API_TOKEN env var or "
            "ensure Qoder CN is installed and has been launched at least once."
        )
        return None

    ws_url, token = info

    route_model = (
        (model or "").strip()
        if is_openclaw_qc_model(model)
        else _QODER_MODEL
    )
    api_model = resolve_qoder_cn_api_model(route_model)

    logger.info(
        "Qoder CN detected (ws=%s route=%s api_model=%s data_dir=%s)",
        ws_url,
        route_model,
        api_model,
        _QODER_CN_DATA_DIR,
    )
    settings = AIProviderSettings(
        model=route_model,
        base_url=ws_url,
        api_key=token,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        context_window=context_window,
    )
    return settings


# ── Apply to settings ─────────────────────────────────────────────────────────

def apply_qoder_cn_provider_to_settings(
    settings: Any,
    *,
    preferred_model: str | None = None,
) -> str | None:
    """Populate *settings.provider* from Qoder CN environment.

    Returns None on success, or a user-facing error string.
    """
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import is_openclaw_model
    from pa_agent.ai.trae_connector import is_openclaw_twc_model
    from pa_agent.ai.workbuddy_connector import is_openclaw_wb_model

    model_hint = (preferred_model or getattr(settings.provider, "model", "") or "").strip()
    if is_openclaw_model(model_hint):
        return "模型 openclaw 属于 QClaw 路由，不应走 Qoder CN。"
    if is_openclaw_cs_model(model_hint):
        return "模型 openclaw_cs 属于 Cursor 订阅路由，不应走 Qoder CN。"
    if is_openclaw_wb_model(model_hint):
        return "模型 openclaw_wb 属于 WorkBuddy 路由，不应走 Qoder CN。"
    if is_openclaw_twc_model(model_hint):
        return "模型 openclaw_twc 属于 TRAE Work CN 路由，不应走 Qoder CN。"

    if not detect_qoder_cn():
        return (
            "未检测到 Qoder CN 环境。\n\n"
            "请确认：\n"
            "1. Qoder CN 已安装在本机\n"
            "2. Qoder CN 已启动并完成登录（至少一次）\n\n"
            "也可手动配置 Token（任选其一）：\n"
            f"• 在终端执行：echo \"你的machine_token\" > {_QODER_CN_TOKEN_FILE}\n"
            "• 设置环境变量：QODER_CN_API_TOKEN"
        )

    model_arg = (
        model_hint
        if is_openclaw_qc_model(model_hint)
        else _QODER_MODEL
    )
    resolved = qoder_cn_provider_settings(model=model_arg)
    if resolved is None:
        return (
            "未找到 Qoder CN 的 machine_token。\n\n"
            "请确认 Qoder CN 已启动并完成登录，然后重试。\n\n"
            "也可手动配置 Token（任选其一）：\n"
            f"• echo \"你的machine_token\" > {_QODER_CN_TOKEN_FILE}\n"
            "• 设置环境变量：QODER_CN_API_TOKEN"
        )

    provider = settings.provider
    provider.model = resolved.model
    provider.base_url = resolved.base_url
    provider.api_key = resolved.api_key
    provider.context_window = resolved.context_window

    if not is_qoder_cn_sidecar_running():
        return (
            "Qoder CN sidecar 未运行（端口 36510 不通）。\n\n"
            "请启动 Qoder CN 应用程序，然后重试。"
        )

    return None


# ── Sync on load ──────────────────────────────────────────────────────────────

def sync_qoder_cn_provider_on_load(
    settings: Any,
    *,
    save_path: Path | None = None,
) -> None:
    """Refresh token for openclaw_qc routing on load."""
    if not detect_qoder_cn():
        return
    provider = settings.provider
    if not is_qoder_cn_route(provider):
        return

    before_token = str(getattr(provider, "api_key", "") or "")
    err = apply_qoder_cn_provider_to_settings(settings)
    if err:
        logger.warning("Qoder CN provider sync failed: %s", err)
        return

    after_token = str(getattr(provider, "api_key", "") or "")
    if save_path is not None and before_token != after_token:
        try:
            from pa_agent.config.settings import save_settings

            save_settings(settings, save_path)
            logger.info("Qoder CN provider synced on load (token refreshed)")
        except Exception as exc:
            logger.warning("Failed to persist synced Qoder CN provider: %s", exc)


# ── Health check ──────────────────────────────────────────────────────────────

def qoder_cn_health_check(*, timeout: float = 10.0) -> tuple[bool, str]:
    """Perform a quick health check against Qoder CN's sidecar.

    Returns a (ok, message) tuple.
    """
    info = _get_qoder_cn_info()
    if info is None:
        if detect_qoder_cn():
            return (
                False,
                "Qoder CN 环境已检测到，但未找到 machine_token。\n\n"
                "请启动 Qoder CN 并登录，然后重试。\n"
                "也可手动配置：设置环境变量 QODER_CN_API_TOKEN",
            )
        return False, "Qoder CN 环境未检测到"

    ws_url, token = info

    if not is_qoder_cn_sidecar_running():
        return (
            False,
            "Qoder CN sidecar 未运行（端口 36510 不通）。\n"
            "请启动 Qoder CN 应用程序，然后重试。",
        )

    return True, (
        f"Qoder CN 连接配置就绪 ({ws_url})，"
        f"模型默认使用 {_QODER_DEFAULT_INTERNAL_MODEL}"
    )
