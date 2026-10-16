"""QClaw Gateway connector for PA Agent.

Detects the local QClaw Gateway, reads its endpoint and token from the
OpenClaw config file, and routes PA Agent through the public gateway's
``openclaw`` Agent model (chat completions on the gateway port).

Usage::

    from pa_agent.ai.qclaw_connector import detect_qclaw, qclaw_provider_settings

    if detect_qclaw():
        settings.provider = qclaw_provider_settings()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import json
import logging
from pathlib import Path
from typing import Any

if TYPE_CHECKING:  # type-checker-only import (annotations stay lazy at runtime)
    from pa_agent.config.settings import AIProviderSettings


logger = logging.getLogger(__name__)

# Known QClaw config path (Windows)
_QCLAW_CONFIG_PATH = Path.home() / ".qclaw" / "openclaw.json"
# Fallback: Linux/macOS
_QCLAW_CONFIG_PATH_ALT = Path("~/.qclaw/openclaw.json").expanduser()

_RELAY_PROXY_PORT = 19004
_RELAY_PROXY_MODEL = "pool-deepseek-v4-flash"
_PUBLIC_GATEWAY_MODEL = "openclaw"


def is_openclaw_model(model: str | None) -> bool:
    """True when the user selected QClaw's OpenClaw Agent route.

    Accepts the bare alias ``openclaw`` and gateway variants such as
    ``openclaw/main`` (shown in QClaw's /models list).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    return m == _PUBLIC_GATEWAY_MODEL or m.startswith(f"{_PUBLIC_GATEWAY_MODEL}/")


def should_use_qclaw_provider(
    model: str | None,
    base_url: str | None = None,
) -> bool:
    """True when settings Save should auto-configure from local QClaw."""
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qoder_connector import is_openclaw_qc_model
    from pa_agent.ai.trae_connector import is_openclaw_twc_model
    from pa_agent.ai.workbuddy_connector import is_openclaw_wb_model

    # Other openclaw routes take precedence over QClaw.
    if (
        is_openclaw_wb_model(model)
        or is_openclaw_cs_model(model)
        or is_openclaw_twc_model(model)
        or is_openclaw_qc_model(model)
    ):
        return False
    if is_openclaw_model(model):
        return True
    if not detect_qclaw():
        return False
    info = _get_qclaw_gateway_info()
    if info is None:
        return False
    _host, port, _token = info
    base = (base_url or "").strip().lower()
    if not base:
        return False
    return (
        f":{port}" in base
        and ("127.0.0.1" in base or "localhost" in base)
    )


def is_qclaw_agent_route(model: str | None) -> bool:
    """True when API calls use the OpenClaw Agent on the public gateway."""
    return is_openclaw_model(model)


def _uses_qclaw_gateway(provider: Any) -> bool:
    """True when provider already targets the local QClaw public gateway."""
    info = _get_qclaw_gateway_info()
    if info is None:
        return False
    _host, port, _token = info
    base_url = str(getattr(provider, "base_url", "") or "").strip().lower()
    return f":{port}" in base_url


def sync_qclaw_agent_provider_on_load(
    settings: Any,
    *,
    save_path: Path | None = None,
) -> None:
    """Refresh token/base_url for openclaw Agent routing (no relay on 19004)."""
    from pa_agent.ai.workbuddy_connector import is_openclaw_wb_model

    if not detect_qclaw():
        return
    provider = settings.provider
    model = str(getattr(provider, "model", "") or "").strip().lower()
    if is_openclaw_wb_model(model):
        return
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model

    if is_openclaw_cs_model(model):
        return
    if not is_openclaw_model(model) and not _uses_qclaw_gateway(provider):
        return

    before_url = str(getattr(provider, "base_url", "") or "")
    before_model = str(getattr(provider, "model", "") or "")
    err = apply_qclaw_provider_to_settings(settings)
    if err:
        logger.warning("QClaw agent provider sync failed: %s", err)
        return

    after_url = str(getattr(provider, "base_url", "") or "")
    after_model = str(getattr(provider, "model", "") or "")
    if save_path is not None and (before_url != after_url or before_model != after_model):
        try:
            from pa_agent.config.settings import save_settings

            save_settings(settings, save_path)
            logger.info(
                "QClaw agent provider synced on load: %s @ %s",
                after_model,
                after_url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist synced QClaw provider: %s", exc)


def _find_qclaw_config() -> Path | None:
    """Return the first existing OpenClaw config file path, or None."""
    for candidate in (_QCLAW_CONFIG_PATH, _QCLAW_CONFIG_PATH_ALT):
        if candidate.exists():
            return candidate
    return None


def _read_qclaw_config(config_path: Path) -> dict | None:
    """Parse the OpenClaw JSON config file; returns None on error."""
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read QClaw config %s: %s", config_path, exc)
        return None


def detect_qclaw() -> bool:
    """Return True if QClaw Gateway is configured and chat completions are enabled."""
    config_path = _find_qclaw_config()
    if config_path is None:
        return False
    data = _read_qclaw_config(config_path)
    if data is None:
        return False
    gw = data.get("gateway", {})
    http = gw.get("http", {})
    eps = http.get("endpoints", {})
    chat = eps.get("chatCompletions", {})
    if not chat.get("enabled", False):
        logger.debug("QClaw chatCompletions endpoint not enabled")
        return False
    token = gw.get("auth", {}).get("token", "")
    if not token:
        logger.debug("QClaw gateway token is empty")
        return False
    return True


def _get_qclaw_gateway_info() -> tuple[str, int, str] | None:
    """Return (host, port, token) for the QClaw gateway, or None if unavailable."""
    config_path = _find_qclaw_config()
    if config_path is None:
        return None
    data = _read_qclaw_config(config_path)
    if data is None:
        return None
    gw = data.get("gateway", {})
    token = gw.get("auth", {}).get("token", "")
    if not token:
        return None
    port = int(gw.get("port", 51187))
    host = "127.0.0.1"
    bind = gw.get("bind", "127.0.0.1")
    if bind and bind not in ("0.0.0.0", "loopback"):
        host = bind
    return host, port, token


def _fetch_public_gateway_models(base_url: str, token: str, *, timeout: float = 2.0) -> list[str]:
    try:
        import httpx

        resp = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [str(m.get("id", "")) for m in data.get("data", []) if m.get("id")]
    except Exception as exc:
        logger.debug("QClaw public gateway models probe failed for %s: %s", base_url, exc)
        return []


def _probe_public_gateway(base_url: str, token: str, *, timeout: float = 2.0) -> bool:
    return bool(_fetch_public_gateway_models(base_url, token, timeout=timeout))


def _pick_public_gateway_model(model_ids: list[str]) -> str:
    """Prefer the bare ``openclaw`` Agent alias on the public gateway."""
    if _PUBLIC_GATEWAY_MODEL in model_ids:
        return _PUBLIC_GATEWAY_MODEL
    for model_id in model_ids:
        if str(model_id).startswith("openclaw"):
            return str(model_id)
    return _PUBLIC_GATEWAY_MODEL


def _resolve_qclaw_endpoint(
    host: str,
    port: int,
    token: str,
    *,
    prefer_relay: bool = False,
) -> tuple[str, str, str]:
    """Pick API base URL, model name, and human-readable mode label."""
    public_base = f"http://{host}:{port}/v1"
    if prefer_relay:
        from pa_agent.ai.qclaw_relay_manager import ensure_qclaw_relay

        relay_base = f"http://127.0.0.1:{_RELAY_PROXY_PORT}"
        relay_ok, relay_msg = ensure_qclaw_relay(token, port=_RELAY_PROXY_PORT)
        if relay_ok:
            logger.info("QClaw relay ready at %s (%s)", relay_base, relay_msg)
            return relay_base, _RELAY_PROXY_MODEL, "中继代理（含 reasoning）"
        logger.info("QClaw relay unavailable (%s); using public gateway", relay_msg)

    model_ids = _fetch_public_gateway_models(public_base, token)
    public_model = _pick_public_gateway_model(model_ids) if model_ids else _PUBLIC_GATEWAY_MODEL
    mode = f"公开网关（OpenClaw Agent / {public_model}）"
    logger.info("QClaw using public gateway at %s (model=%s)", public_base, public_model)
    return public_base, public_model, mode


def apply_qclaw_provider_to_settings(
    settings: Any,
    *,
    preferred_model: str | None = None,
) -> str | None:
    """Populate *settings.provider* from local QClaw (settings Save with model=openclaw).

    Returns None on success, or a user-facing error string.
    """
    if not detect_qclaw():
        return (
            "未检测到本地 QClaw Gateway。\n\n"
            "请确认：\n"
            "1. QClaw Gateway 正在运行\n"
            "2. config 中 chatCompletions 端点已启用\n"
            "3. gateway.auth.token 已配置"
        )

    model_hint = (preferred_model or getattr(settings.provider, "model", "") or "").strip()
    model_arg = model_hint if is_openclaw_model(model_hint) else _PUBLIC_GATEWAY_MODEL
    resolved = qclaw_provider_settings(model=model_arg, prefer_relay=False)
    if resolved is None:
        return "QClaw 配置读取失败。"

    provider = settings.provider
    provider.model = resolved.model
    provider.base_url = resolved.base_url
    provider.api_key = resolved.api_key
    provider.context_window = resolved.context_window
    # Preserve user/GUI thinking prefs — only connector fields (url/key/model) are authoritative.

    ok, health_msg = qclaw_health_check_base(provider.base_url, provider.api_key)
    if not ok:
        return f"QClaw 连通性检查失败：\n\n{health_msg}"
    return None


def qclaw_provider_settings(
    model: str | None = None,
    thinking: bool = True,
    reasoning_effort: str = "high",
    context_window: int = 2_000_000,
    *,
    prefer_relay: bool = False,
) -> "AIProviderSettings | None":
    """Return AIProviderSettings for QClaw's public-gateway OpenClaw Agent."""
    from pa_agent.config.settings import AIProviderSettings

    info = _get_qclaw_gateway_info()
    if info is None:
        return None
    host, port, token = info
    base_url, resolved_model, _mode = _resolve_qclaw_endpoint(
        host,
        port,
        token,
        prefer_relay=prefer_relay,
    )
    logger.info("QClaw detected at %s (model=%s)", base_url, resolved_model)
    return AIProviderSettings(
        model=model or resolved_model,
        base_url=base_url,
        api_key=token,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        context_window=context_window,
    )


def qclaw_health_check(*, prefer_relay: bool = False) -> tuple[bool, str]:
    """Perform a quick health check against the QClaw gateway.

    Returns a (ok, message) tuple.
    """
    info = _get_qclaw_gateway_info()
    if info is None:
        return False, "QClaw 配置文件未找到或 token 为空"

    host, port, token = info
    base_url, model, mode = _resolve_qclaw_endpoint(
        host,
        port,
        token,
        prefer_relay=prefer_relay,
    )
    ok, msg = qclaw_health_check_base(base_url, token)
    if not ok:
        return False, msg
    return True, f"QClaw 连接正常（{mode}，推荐模型 {model}），{msg}"


def qclaw_health_check_base(base_url: str, token: str) -> tuple[bool, str]:
    """Health check for a specific QClaw base_url/token pair.

    This avoids re-scanning QClaw config multiple times in one Save cycle
    (which may pick different configs and lead to mismatched ports).
    """
    try:
        import httpx

        if base_url.endswith(f":{_RELAY_PROXY_PORT}"):
            probe_url = f"{base_url.rstrip('/')}/health"
        else:
            probe_url = f"{base_url.rstrip('/')}/models"

        resp = httpx.get(
            probe_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        if resp.status_code != 200:
            return False, f"QClaw 返回 HTTP {resp.status_code}: {resp.text[:200]}"

        if probe_url.endswith("/health"):
            detail = "reasoning 中继正常"
        else:
            models_data = resp.json()
            model_ids = [m.get("id", "?") for m in models_data.get("data", [])]
            detail = (
                f"可用模型: {', '.join(model_ids) if model_ids else '(列表为空)'}"
            )

        return True, detail
    except Exception as exc:
        return False, f"无法连接 QClaw ({base_url}): {exc}"
