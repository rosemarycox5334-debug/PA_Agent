"""WorkBuddy connector for PA Agent.

Detects the WorkBuddy environment (env vars, config files), reads its
API endpoint and authentication token, and routes PA Agent through
WorkBuddy's model infrastructure.

Usage::

    from pa_agent.ai.workbuddy_connector import (
        detect_workbuddy,
        workbuddy_provider_settings,
    )

    if detect_workbuddy():
        settings.provider = workbuddy_provider_settings()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import json
import logging
import os
from pathlib import Path
from typing import Any

if TYPE_CHECKING:  # type-checker-only import (annotations stay lazy at runtime)
    from pa_agent.config.settings import AIProviderSettings


logger = logging.getLogger(__name__)

_WORKBUDDY_MODEL = "openclaw_wb"
_WORKBUDDY_DEFAULT_INTERNAL_MODEL = "auto"

# WorkBuddy known config paths
_WORKBUDDY_CONFIG_DIR = Path(
    os.environ.get("WORKBUDDY_CONFIG_DIR", "")
    or Path.home() / ".workbuddy"
)
_WORKBUDDY_SESSION_PATH = _WORKBUDDY_CONFIG_DIR / "app" / "session"
_WORKBUDDY_LOCAL_STATE = _WORKBUDDY_SESSION_PATH / "Local State"
_WORKBUDDY_TOKEN_FILE = _WORKBUDDY_CONFIG_DIR / ".wb_token"

# OpenAI-compatible chat API (same route WorkBuddy desktop uses internally).
_DEFAULT_WORKBUDDY_ENDPOINT = "https://copilot.tencent.com"
_WORKBUDDY_API_PATH = "/v2"


def is_workbuddy_route(provider: Any) -> bool:
    """True when provider targets WorkBuddy / CodeBuddy copilot API."""
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import is_openclaw_model
    from pa_agent.ai.qoder_connector import is_openclaw_qc_model
    from pa_agent.ai.trae_connector import is_openclaw_twc_model

    model = str(getattr(provider, "model", "") or "").strip().lower()
    if (
        is_openclaw_model(model)
        or is_openclaw_cs_model(model)
        or is_openclaw_twc_model(model)
        or is_openclaw_qc_model(model)
    ):
        return False
    if is_openclaw_wb_model(model):
        return True
    base = str(getattr(provider, "base_url", "") or "").strip().lower()
    return "copilot.tencent.com" in base and "/v2" in base


def resolve_workbuddy_api_model(model: str | None) -> str:
    """Map settings alias (``openclaw_wb/...``) to WorkBuddy API model id."""
    return _resolve_workbuddy_model(model)


def _codebuddy_auth_dir() -> Path | None:
    """Directory where WorkBuddy desktop stores shared auth session files."""
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app:
        return None
    auth_dir = Path(local_app) / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    return auth_dir if auth_dir.is_dir() else None


def _auth_session_file_candidates() -> list[Path]:
    """Known FileAuthenticationStorage paths (workbuddy-desktop.info, etc.)."""
    auth_dir = _codebuddy_auth_dir()
    if auth_dir is None:
        return []
    names = (
        os.environ.get("WORKBUDDY_AUTH_FILE", "").strip(),
        "workbuddy-desktop.info",
        "auth.info",
    )
    out: list[Path] = []
    for name in names:
        if not name:
            continue
        path = auth_dir / name
        if path not in out:
            out.append(path)
    return out


def _read_auth_session_access_token(path: Path) -> str | None:
    """Read ``auth.accessToken`` from WorkBuddy's FileAuthenticationStorage JSON."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read WorkBuddy auth session %s: %s", path, exc)
        return None

    auth = payload.get("auth")
    if not isinstance(auth, dict):
        return None

    token = str(auth.get("accessToken") or auth.get("access_token") or "").strip()
    if not token:
        return None

    expires_at = auth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        import time

        if expires_at <= time.time() * 1000:
            logger.debug("WorkBuddy auth session expired: %s", path)
            return None

    return token


def is_openclaw_wb_model(model: str | None) -> bool:
    """True when the user selected WorkBuddy's model route.

    Accepts the bare alias ``openclaw_wb`` and variants such as
    ``openclaw_wb/auto`` (specific model under WorkBuddy route).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    return m == _WORKBUDDY_MODEL or m.startswith(f"{_WORKBUDDY_MODEL}/")


def should_use_workbuddy_provider(
    model: str | None,
    base_url: str | None = None,
) -> bool:
    """True when settings Save should auto-configure from WorkBuddy."""
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import is_openclaw_model
    from pa_agent.ai.qoder_connector import is_openclaw_qc_model
    from pa_agent.ai.trae_connector import is_openclaw_twc_model

    # ``openclaw`` / ``openclaw/*`` is QClaw's Agent alias — never WorkBuddy,
    # even if a stale base_url still points at copilot.tencent.com.
    if (
        is_openclaw_model(model)
        or is_openclaw_cs_model(model)
        or is_openclaw_twc_model(model)
        or is_openclaw_qc_model(model)
    ):
        return False
    if is_openclaw_wb_model(model):
        return True
    if not detect_workbuddy():
        return False
    info = _get_workbuddy_info()
    if info is None:
        return False
    _endpoint, _token = info
    base = (base_url or "").strip().lower()
    if not base:
        return False
    return (
        "copilot.tencent.com" in base
        or "codebuddy" in base
    )


def detect_workbuddy() -> bool:
    """Return True if running inside WorkBuddy environment.

    Detects WorkBuddy via:
    1. CLIENT_INFO_PRODUCT_NAME == "WorkBuddy" env var
    2. WORKBUDDY_CONFIG_DIR env var exists
    3. Known WorkBuddy session paths exist
    """
    # Primary: check the product name env var
    product_name = os.environ.get("CLIENT_INFO_PRODUCT_NAME", "")
    if product_name == "WorkBuddy":
        return True

    # Secondary: check for WorkBuddy config dir
    if os.environ.get("WORKBUDDY_CONFIG_DIR"):
        return True

    # Tertiary: WorkBuddy logged-in session file (Windows desktop)
    if _auth_session_file_candidates():
        return True

    # Quaternary: known ~/.workbuddy paths
    if _WORKBUDDY_CONFIG_DIR.exists():
        return True

    return False


def _read_workbuddy_local_state() -> dict | None:
    """Parse WorkBuddy's Local State JSON file; returns None on error."""
    if not _WORKBUDDY_LOCAL_STATE.exists():
        return None
    try:
        return json.loads(_WORKBUDDY_LOCAL_STATE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read WorkBuddy local state: %s", exc)
        return None


def _extract_workbuddy_token() -> str | None:
    """Try to extract WorkBuddy auth token from multiple sources.

    Uses a multi-layered discovery strategy, from most to least reliable:

    1. WorkBuddy desktop auth session (``CodeBuddyExtension/.../auth/*.info``)
    2. ``~/.workbuddy/.wb_token`` file — manual override
    3. ``WORKBUDDY_API_TOKEN`` / related env vars
    4. DPAPI-decrypted Electron session storage (Windows only)
    """
    # ── Layer 1: Desktop auth session (FileAuthenticationStorage) ─────────
    for auth_path in _auth_session_file_candidates():
        token = _read_auth_session_access_token(auth_path)
        if token:
            logger.debug("Using token from WorkBuddy auth session %s", auth_path)
            return token

    # ── Layer 2: Token file ──────────────────────────────────────────────
    if _WORKBUDDY_TOKEN_FILE.exists():
        try:
            token = _WORKBUDDY_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                logger.debug("Using token from %s", _WORKBUDDY_TOKEN_FILE)
                return token
        except OSError:
            pass

    # ── Layer 3: Environment variables ────────────────────────────────────
    for env_name in (
        "WORKBUDDY_API_TOKEN",
        "CODEBUDDY_AUTH_TOKEN",
        "ACC_AUTH_TOKEN",
    ):
        token = os.environ.get(env_name, "").strip()
        if token:
            logger.debug("Using token from env var %s", env_name)
            return token

    # ── Layer 4: DPAPI-decrypt Electron safeStorage (Windows) ────────────
    token = _decrypt_electron_token()
    if token:
        logger.debug("Using token from DPAPI-decrypted Electron storage")
        return token

    return None


def _decrypt_electron_token() -> str | None:
    """Try to extract auth token from Electron's DPAPI-encrypted storage.

    On Windows, Electron encrypts sensitive values with AES-GCM, where the
    AES key itself is DPAPI-protected.  We unwind both layers.

    Returns the token string, or None if decryption fails / no token found.
    """
    import sys
    if sys.platform != "win32":
        return None

    # ── Read the Electron Local State to get the DPAPI-encrypted AES key ──
    local_state = _read_workbuddy_local_state()
    if local_state is None:
        return None

    os_crypt = local_state.get("os_crypt", {})
    encrypted_key_b64 = os_crypt.get("encrypted_key", "")
    if not encrypted_key_b64:
        return None

    try:
        import base64
        encrypted_key = base64.b64decode(encrypted_key_b64)
    except Exception:
        return None

    if not encrypted_key.startswith(b"DPAPI"):
        return None

    aes_key = _dpapi_decrypt(encrypted_key[5:])
    if aes_key is None:
        return None

    # ── Search all LevelDB stores for encrypted values ────────────────────
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aesgcm = AESGCM(aes_key)
    except ImportError:
        logger.debug("cryptography not installed; skipping DPAPI decryption")
        return None

    # Directories that may contain encrypted Electron values
    search_dirs = [
        _WORKBUDDY_SESSION_PATH / "Local Storage" / "leveldb",
        _WORKBUDDY_SESSION_PATH / "Session Storage",
        _WORKBUDDY_SESSION_PATH / "Network",
    ]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for entry in search_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                data = entry.read_bytes()
            except OSError:
                continue

            for prefix in (b"v10", b"v11"):
                idx = 0
                while True:
                    idx = data.find(prefix, idx)
                    if idx == -1:
                        break
                    start = idx + 3
                    encrypted_val = data[start:start + 2048]
                    if len(encrypted_val) >= 27:
                        try:
                            nonce = encrypted_val[:12]
                            ciphertext = encrypted_val[12:]
                            plain = aesgcm.decrypt(nonce, ciphertext, None)
                            plain_str = plain.decode("utf-8", errors="replace")
                            # Auth tokens are typically JWTs (eyJ...) or
                            # long opaque strings (40+ alphanumeric chars).
                            # Also check for JSON snippets containing "token".
                            looks_like_token = (
                                plain_str.startswith("eyJ")
                                or (
                                    len(plain_str) >= 40
                                    and plain_str.strip().isascii()
                                    and not plain_str.startswith("{")
                                    and not plain_str.startswith("[")
                                    and "\x00" not in plain_str
                                )
                                or (
                                    "accessToken" in plain_str
                                    or "access_token" in plain_str
                                    or "bearerToken" in plain_str
                                )
                            )
                            if looks_like_token:
                                # If it's a JSON object, extract the actual token
                                if "accessToken" in plain_str or "access_token" in plain_str:
                                    try:
                                        obj = json.loads(plain_str)
                                        for k in ("accessToken", "access_token", "token", "bearerToken"):
                                            if k in obj:
                                                return str(obj[k])
                                    except json.JSONDecodeError:
                                        pass
                                return plain_str.strip("\x00").strip()
                        except Exception:
                            pass
                    idx += 1

    return None


def _dpapi_decrypt(blob: bytes) -> bytes | None:
    """Decrypt a DPAPI-protected blob on Windows. Returns plaintext or None."""
    import ctypes
    from ctypes import wintypes

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    buf_in = (ctypes.c_ubyte * len(blob))(*blob)
    blob_in = DATA_BLOB(len(blob), buf_in)
    blob_out = DATA_BLOB()

    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(blob_out),
    )
    if not ok:
        return None

    try:
        size = blob_out.cbData
        buf = ctypes.cast(
            blob_out.pbData,
            ctypes.POINTER(ctypes.c_ubyte * size),
        )
        return bytes(buf.contents)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _get_workbuddy_endpoint() -> str:
    """Get WorkBuddy API endpoint.

    Priority:
    1. WORKBUDDY_API_ENDPOINT env var
    2. WORKBUDDY_API_URL env var
    3. ACC_ENDPOINT from env config
    4. Default: https://copilot.tencent.com
    """
    # User-specified endpoint
    endpoint = os.environ.get("WORKBUDDY_API_ENDPOINT", "").strip()
    if endpoint:
        return endpoint

    endpoint = os.environ.get("WORKBUDDY_API_URL", "").strip()
    if endpoint:
        return endpoint

    # Parse from ACC_PRODUCT_CONFIG_V3
    acc_config = os.environ.get("ACC_PRODUCT_CONFIG_V3", "")
    if acc_config:
        try:
            config = json.loads(acc_config)
            acc_endpoint = config.get("endpoint", "")
            if acc_endpoint:
                return acc_endpoint
        except json.JSONDecodeError:
            pass

    return _DEFAULT_WORKBUDDY_ENDPOINT


def _get_workbuddy_info() -> tuple[str, str] | None:
    """Return (endpoint, token) for WorkBuddy, or None if unavailable."""
    token = _extract_workbuddy_token()
    if not token:
        return None
    endpoint = _get_workbuddy_endpoint()
    return endpoint, token


def _get_workbuddy_api_base() -> str:
    """Get the full API base URL for WorkBuddy's OpenAI-compatible endpoint."""
    endpoint = _get_workbuddy_endpoint()
    return f"{endpoint.rstrip('/')}{_WORKBUDDY_API_PATH}"


def workbuddy_provider_settings(
    model: str | None = None,
    thinking: bool = True,
    reasoning_effort: str = "high",
    context_window: int = 2_000_000,
) -> "AIProviderSettings | None":
    """Return AIProviderSettings for WorkBuddy's model route."""
    from pa_agent.config.settings import AIProviderSettings

    info = _get_workbuddy_info()
    if info is None:
        logger.debug(
            "WorkBuddy info unavailable; "
            "set WORKBUDDY_API_TOKEN or WORKBUDDY_API_ENDPOINT env vars. "
            "Detected env: CLIENT_INFO_PRODUCT_NAME=%s",
            os.environ.get("CLIENT_INFO_PRODUCT_NAME", ""),
        )
        return None

    endpoint, token = info
    base_url = f"{endpoint.rstrip('/')}{_WORKBUDDY_API_PATH}"

    route_model = (
        (model or "").strip()
        if is_openclaw_wb_model(model)
        else _WORKBUDDY_MODEL
    )
    api_model = _resolve_workbuddy_model(route_model)

    logger.info(
        "WorkBuddy detected at %s (route=%s api_model=%s)",
        base_url,
        route_model,
        api_model,
    )
    return AIProviderSettings(
        model=route_model,
        base_url=base_url,
        api_key=token,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        context_window=context_window,
    )


def _resolve_workbuddy_model(model: str | None) -> str:
    """Resolve the actual model name to send to WorkBuddy's API.

    If model is ``openclaw_wb/specific-model``, use ``specific-model``.
    If model is just ``openclaw_wb``, use WorkBuddy's default ``auto`` model.
    """
    if model and model.startswith(f"{_WORKBUDDY_MODEL}/"):
        suffix = model[len(_WORKBUDDY_MODEL) + 1:]
        return suffix.strip() or _WORKBUDDY_DEFAULT_INTERNAL_MODEL
    return _WORKBUDDY_DEFAULT_INTERNAL_MODEL


def apply_workbuddy_provider_to_settings(
    settings: Any,
    *,
    preferred_model: str | None = None,
) -> str | None:
    """Populate *settings.provider* from WorkBuddy environment.

    Returns None on success, or a user-facing error string.
    """
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import is_openclaw_model

    model_hint = (preferred_model or getattr(settings.provider, "model", "") or "").strip()
    if is_openclaw_model(model_hint):
        return (
            "模型 openclaw 属于 QClaw 路由，不应走 WorkBuddy。\n\n"
            "请在设置中将模型填 openclaw 并保存，程序会自动配置本地 QClaw Gateway。"
        )
    if is_openclaw_cs_model(model_hint):
        return (
            "模型 openclaw_cs 属于 Cursor 订阅路由，不应走 WorkBuddy。\n\n"
            "请在设置中将模型填 openclaw_cs 并保存。"
        )

    if not detect_workbuddy():
        return (
            "未检测到 WorkBuddy 环境。\n\n"
            "请确认：\n"
            "1. PA Agent 是在 WorkBuddy 中运行的\n"
            "2. 已配置 WorkBuddy Token（见下方说明）"
        )

    model_arg = (
        model_hint
        if is_openclaw_wb_model(model_hint)
        else _WORKBUDDY_DEFAULT_INTERNAL_MODEL
    )
    resolved = workbuddy_provider_settings(model=model_arg)
    if resolved is None:
        # Check which layer failed
        if _WORKBUDDY_TOKEN_FILE.exists():
            return (
                "WorkBuddy Token 文件存在但读取失败。\n"
                f"请检查 {_WORKBUDDY_TOKEN_FILE} 内容是否有效。"
            )
        return (
            "未找到 WorkBuddy API Token。\n\n"
            "请确认 WorkBuddy 已打开并完成登录，然后重试。\n\n"
            "程序已尝试以下来源：\n"
            "1. WorkBuddy 登录会话文件 (CodeBuddyExtension/.../auth/*.info)\n"
            f"2. Token 文件 ({_WORKBUDDY_TOKEN_FILE})\n"
            "3. 环境变量 (WORKBUDDY_API_TOKEN)\n\n"
            "也可手动配置 Token（任选其一）：\n"
            f"• echo \"你的Token\" > {_WORKBUDDY_TOKEN_FILE}\n"
            "• 设置环境变量：WORKBUDDY_API_TOKEN"
        )

    provider = settings.provider
    provider.model = resolved.model
    provider.base_url = resolved.base_url
    provider.api_key = resolved.api_key
    provider.context_window = resolved.context_window
    # Preserve user/GUI thinking prefs — only connector fields (url/key/model) are authoritative.

    ok, health_msg = workbuddy_health_check()
    if not ok:
        return f"WorkBuddy 连通性检查失败：\n\n{health_msg}"
    return None


def _probe_workbuddy_api(base_url: str, token: str, *, timeout: float = 5.0) -> bool:
    """Quick connectivity check via streaming chat (WorkBuddy requires stream)."""
    try:
        import httpx

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": _WORKBUDDY_DEFAULT_INTERNAL_MODEL,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": True,
        }
        with httpx.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
        ) as resp:
            if resp.status_code == 200:
                for _line in resp.iter_lines():
                    return True
                return False
            if resp.status_code in (401, 403):
                return True
            logger.debug(
                "WorkBuddy API probe HTTP %s: %s",
                resp.status_code,
                resp.read().decode("utf-8", "replace")[:200],
            )
            return False
    except Exception as exc:
        logger.debug("WorkBuddy API probe failed: %s", exc)
        return False


def workbuddy_health_check(*, timeout: float = 5.0) -> tuple[bool, str]:
    """Perform a quick health check against WorkBuddy's API.

    Returns a (ok, message) tuple.
    """
    info = _get_workbuddy_info()
    if info is None:
        if detect_workbuddy():
            return (
                False,
                "WorkBuddy 环境已检测到，但未找到 API Token。\n\n"
                "请通过以下方式配置 Token（任选其一）：\n"
                f"• 在终端执行：echo \"你的Token\" > {_WORKBUDDY_TOKEN_FILE}\n"
                "• 设置环境变量：export WORKBUDDY_API_TOKEN=你的Token"
            )
        return False, "WorkBuddy 环境未检测到"

    endpoint, token = info
    base_url = f"{endpoint.rstrip('/')}{_WORKBUDDY_API_PATH}"

    if _probe_workbuddy_api(base_url, token, timeout=timeout):
        return True, (
            f"WorkBuddy 连接正常 ({base_url})，"
            f"API 模型默认使用 {_WORKBUDDY_DEFAULT_INTERNAL_MODEL}"
        )

    try:
        import httpx

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": _WORKBUDDY_DEFAULT_INTERNAL_MODEL,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": True,
        }
        with httpx.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
        ) as resp:
            if resp.status_code in (401, 403):
                return (
                    False,
                    f"WorkBuddy API 认证失败 (HTTP {resp.status_code})。\n"
                    "请确认 WorkBuddy 仍处于登录状态。",
                )
            body = resp.read().decode("utf-8", "replace")[:200]
            return False, f"WorkBuddy 返回 HTTP {resp.status_code}: {body}"
    except Exception as exc:
        return False, f"无法连接 WorkBuddy API ({base_url}): {exc}"


def sync_workbuddy_provider_on_load(
    settings: Any,
    *,
    save_path: Path | None = None,
) -> None:
    """Refresh token/base_url for openclaw_wb routing on load."""
    if not detect_workbuddy():
        return
    provider = settings.provider
    if not is_workbuddy_route(provider):
        return

    before_url = str(getattr(provider, "base_url", "") or "")
    before_model = str(getattr(provider, "model", "") or "")
    err = apply_workbuddy_provider_to_settings(settings)
    if err:
        logger.warning("WorkBuddy provider sync failed: %s", err)
        return

    after_url = str(getattr(provider, "base_url", "") or "")
    after_model = str(getattr(provider, "model", "") or "")
    if save_path is not None and (
        before_url != after_url or before_model != after_model
    ):
        try:
            from pa_agent.config.settings import save_settings

            save_settings(settings, save_path)
            logger.info(
                "WorkBuddy provider synced on load: %s @ %s",
                after_model,
                after_url,
            )
        except Exception as exc:
            logger.warning(
                "Failed to persist synced WorkBuddy provider: %s", exc
            )
