"""TRAE Work CN connector for PA Agent.

Detects the local TRAE Work CN (TRAE SOLO CN) installation, reads its auth
JWT token and device info, and routes PA Agent through TRAE's cloud
model infrastructure (glm-5.2 / glm-5.1 via llm_utils_chat API).

Usage::

    from pa_agent.ai.trae_connector import (
        detect_trae_cn,
        trae_cn_provider_settings,
    )

    if detect_trae_cn():
        settings.provider = trae_cn_provider_settings()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import base64
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

if TYPE_CHECKING:  # type-checker-only import (annotations stay lazy at runtime)
    from pa_agent.config.settings import AIProviderSettings


logger = logging.getLogger(__name__)

_TRAE_MODEL = "openclaw_twc"
# Default model for /api/ide/v1/llm_raw_chat. Verified working models include:
#   seed_m8, Doubao_1_5_thinking_pro, deepseek-R1, deepseek-V3, deepseek-V3-0324
# seed_m8 is a balanced general-purpose chat model (non-reasoning, fast).
_TRAE_DEFAULT_INTERNAL_MODEL = "seed_m8"

# TRAE Work CN known data directories (Windows).
# The running app is "TRAE SOLO CN"; older installs may use "Trae CN".
_APPDATA = os.environ.get("APPDATA", "").strip()

def _candidate_data_dirs() -> list[Path]:
    """Return candidate TRAE data dirs, most-likely-first."""
    dirs: list[Path] = []
    env_override = os.environ.get("TRAE_CN_DATA_DIR", "").strip()
    if env_override:
        dirs.append(Path(env_override))
    if _APPDATA:
        for name in ("TRAE SOLO CN", "Trae CN"):
            dirs.append(Path(_APPDATA) / name)
    dirs.append(Path.home() / ".trae-cn")
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _find_data_dir() -> Path | None:
    """Return the first candidate data dir that has storage.json or local_env.json."""
    for d in _candidate_data_dirs():
        env = d / "ModularData" / "ckg_server" / "local_env.json"
        storage = d / "User" / "GlobalStorage" / "storage.json"
        # Also check legacy paths (lowercase globalStorage).
        storage_alt = d / "User" / "globalStorage" / "storage.json"
        if env.exists() or storage.exists() or storage_alt.exists():
            return d
    return None


_TRAE_CN_DATA_DIR = _find_data_dir()

# Resolve concrete paths from the detected data dir.
def _storage_path() -> Path | None:
    if _TRAE_CN_DATA_DIR is None:
        return None
    for rel in ("User/GlobalStorage/storage.json", "User/globalStorage/storage.json"):
        p = _TRAE_CN_DATA_DIR / rel
        if p.exists():
            return p
    return None


def _env_path() -> Path | None:
    if _TRAE_CN_DATA_DIR is None:
        return None
    p = _TRAE_CN_DATA_DIR / "ModularData" / "ckg_server" / "local_env.json"
    return p if p.exists() else None


def _logs_path() -> Path | None:
    if _TRAE_CN_DATA_DIR is None:
        return None
    p = _TRAE_CN_DATA_DIR / "logs"
    return p if p.exists() else None


_TRAE_CN_TOKEN_FILE = Path.home() / ".trae_cn_token"

# TRAE cloud API endpoint (from local_env.json host_map / log analysis).
# The working chat endpoint is /api/ide/v1/llm_raw_chat (verified by live test).
# It accepts {messages, model_name} and streams SSE with `output` events whose
# payload has `response` (content) and `reasoning_content` (thinking) fields.
_DEFAULT_TRAE_API_HOST = "https://trae-api-cn.mchost.guru"
_TRAE_API_CHAT_PATH = "/api/ide/v1/llm_raw_chat"
# Legacy endpoint kept for reference; returns "param is invalid" for chat use.
_TRAE_API_UTILS_CHAT_PATH = "/api/agent/v3/llm_utils_chat"

# App identity headers (from TRAE log analysis — constant across sessions).
_TRAE_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"

# JWT regex: eyJ...eyJ...sig
_JWT_RE = re.compile(
    rb"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"
)

# ── AES-128-CBC decryption for storage.json "tc\x05" prefixed values ──────────

# Hardcoded salt arrays from TRAE SOLO CN main.js (byteCrypto module).
# salt = Rte XOR $te
_CRYPTO_SALT_A = bytes([
    82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
    124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
    84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78, 8,
    46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37,
])
_CRYPTO_SALT_B = bytes([
    31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
    96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
    160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
    23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125,
])
_CRYPTO_SALT = bytes(a ^ b for a, b in zip(_CRYPTO_SALT_A, _CRYPTO_SALT_B))

# Header magic: tc\x05\x10\x00\x00
_CRYPTO_HEADER = bytes([116, 99, 5, 16, 0, 0])
_CRYPTO_KEY_MATERIAL_LEN = 32


def _decrypt_trae_value(encrypted: str) -> str | None:
    """Decrypt a "tc\\x05"-prefixed value from TRAE storage.json.

    The value is base64-encoded. After decoding:
    - bytes[0:6]   = header magic [116,99,5,16,0,0]
    - bytes[6:38]  = 32-byte random key material
    - bytes[38:]   = AES-128-CBC ciphertext

    Key derivation: SHA-512(SHA-512(key_material) + salt), first 16 bytes = AES key,
    next 16 bytes = IV. Decrypted payload = SHA-512(body) + body (UTF-8 text).
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError:
        logger.debug("cryptography library not available for TRAE storage decryption")
        return None

    try:
        raw = base64.b64decode(encrypted)
    except Exception:
        return None

    if len(raw) < 38 + 16:
        return None
    if raw[:6] != _CRYPTO_HEADER:
        return None

    key_material = raw[6:6 + _CRYPTO_KEY_MATERIAL_LEN]
    ciphertext = raw[6 + _CRYPTO_KEY_MATERIAL_LEN:]

    inner_hash = hashlib.sha512(key_material).digest()
    derived = hashlib.sha512(inner_hash + _CRYPTO_SALT).digest()
    aes_key = derived[0:16]
    iv = derived[16:32]

    try:
        decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
    except Exception as exc:
        logger.debug("TRAE storage decryption failed: %s", exc)
        return None

    if len(plain) < 64:
        return None
    stored_hash = plain[:64]
    body = plain[64:]
    if hashlib.sha512(body).digest() != stored_hash:
        logger.debug("TRAE storage hash check failed")
        return None

    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ── Model alias detection ────────────────────────────────────────────────────

def is_openclaw_twc_model(model: str | None) -> bool:
    """True when the user selected TRAE Work CN's model route.

    Accepts the bare alias ``openclaw_twc`` and variants such as
    ``openclaw_twc/glm-5.2`` (specific model under TRAE route).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    return m == _TRAE_MODEL or m.startswith(f"{_TRAE_MODEL}/")


def should_use_trae_cn_provider(
    model: str | None,
    base_url: str | None = None,
) -> bool:
    """True when settings Save should auto-configure from TRAE Work CN."""
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import is_openclaw_model
    from pa_agent.ai.qoder_connector import is_openclaw_qc_model
    from pa_agent.ai.workbuddy_connector import is_openclaw_wb_model

    # Other openclaw routes take precedence.
    if (
        is_openclaw_model(model)
        or is_openclaw_cs_model(model)
        or is_openclaw_wb_model(model)
        or is_openclaw_qc_model(model)
    ):
        return False
    if is_openclaw_twc_model(model):
        return True
    if not detect_trae_cn():
        return False
    base = (base_url or "").strip().lower()
    if not base:
        return False
    return "trae-api-cn.mchost.guru" in base


def is_trae_cn_route(provider: Any) -> bool:
    """True when provider targets TRAE Work CN's cloud API."""
    model = str(getattr(provider, "model", "") or "").strip().lower()
    if is_openclaw_twc_model(model):
        return True
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import is_openclaw_model
    from pa_agent.ai.qoder_connector import is_openclaw_qc_model
    from pa_agent.ai.workbuddy_connector import is_openclaw_wb_model

    if (
        is_openclaw_model(model)
        or is_openclaw_cs_model(model)
        or is_openclaw_wb_model(model)
        or is_openclaw_qc_model(model)
    ):
        return False
    base = str(getattr(provider, "base_url", "") or "").strip().lower()
    return "trae-api-cn.mchost.guru" in base and _TRAE_API_CHAT_PATH in base


def resolve_trae_cn_api_model(model: str | None) -> str:
    """Resolve the actual model name to send to TRAE's API.

    ``openclaw_twc/glm-5.1`` -> ``glm-5.1``
    ``openclaw_twc`` -> ``glm-5.2`` (default)
    """
    raw = (model or "").strip()
    if raw.lower().startswith(f"{_TRAE_MODEL}/"):
        suffix = raw[len(_TRAE_MODEL) + 1:]
        return suffix.strip() or _TRAE_DEFAULT_INTERNAL_MODEL
    return _TRAE_DEFAULT_INTERNAL_MODEL


# ── Detection ─────────────────────────────────────────────────────────────────

def detect_trae_cn() -> bool:
    """Return True if TRAE Work CN is installed and has auth data."""
    if os.environ.get("TRAE_CN_API_TOKEN"):
        return True
    if _TRAE_CN_TOKEN_FILE.exists():
        return True
    if _TRAE_CN_DATA_DIR is not None:
        return True
    return False


# ── Token extraction ──────────────────────────────────────────────────────────

def _extract_trae_cn_token() -> str | None:
    """Try to extract TRAE CN auth JWT from multiple sources.

    Priority:
    1. ``TRAE_CN_API_TOKEN`` env var — manual override
    2. ``~/.trae_cn_token`` file — manual override
    3. Decrypted from storage.json ``iCubeAuthInfo://icube.cloudide`` (primary)
    4. Most recent JWT found in TRAE log files (fallback)
    """
    # ── Layer 1: Environment variable ─────────────────────────────────────
    token = os.environ.get("TRAE_CN_API_TOKEN", "").strip()
    if token:
        logger.debug("Using token from env var TRAE_CN_API_TOKEN")
        return token

    # ── Layer 2: Token file ───────────────────────────────────────────────
    if _TRAE_CN_TOKEN_FILE.exists():
        try:
            token = _TRAE_CN_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                logger.debug("Using token from %s", _TRAE_CN_TOKEN_FILE)
                return token
        except OSError:
            pass

    # ── Layer 3: Decrypt from storage.json (primary, always fresh) ───────
    token = _extract_token_from_storage()
    if token:
        logger.debug("Using token from storage.json (decrypted)")
        return token

    # ── Layer 4: Scan log files for the most recent valid JWT ─────────────
    token = _scan_logs_for_jwt()
    if token:
        logger.debug("Using token from TRAE log files (fallback)")
        return token

    return None


def _extract_token_from_storage() -> str | None:
    """Decrypt the auth JWT from TRAE's storage.json.

    Looks for ``iCubeAuthInfo://icube.cloudide`` key, whose value is an
    AES-128-CBC encrypted blob containing JSON with a ``token`` field.
    """
    sp = _storage_path()
    if sp is None:
        return None

    try:
        storage = json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read storage.json: %s", exc)
        return None

    # Try the known auth key. The suffix may vary by authProviderId.
    auth_keys = [
        k for k in storage
        if k.startswith("iCubeAuthInfo://") and k != "iCubeAuthInfo://usertag"
    ]
    # Prefer "icube.cloudide" first.
    auth_keys.sort(key=lambda k: 0 if "icube.cloudide" in k else 1)

    for key in auth_keys:
        value = storage[key]
        if not isinstance(value, str) or len(value) < 50:
            continue
        plain = _decrypt_trae_value(value)
        if plain is None:
            continue
        try:
            obj = json.loads(plain)
        except json.JSONDecodeError:
            continue
        token = obj.get("token", "")
        if token and isinstance(token, str) and token.startswith("eyJ"):
            return token

    return None


def _scan_logs_for_jwt() -> str | None:
    """Scan TRAE log files for the most recent (non-expired) JWT token."""
    lp = _logs_path()
    if lp is None:
        return None

    best_token: str | None = None
    best_iat = 0

    for log_dir in sorted(lp.iterdir(), reverse=True):
        if not log_dir.is_dir():
            continue
        for log_file in log_dir.rglob("*.log"):
            try:
                data = log_file.read_bytes()
                if len(data) > 50 * 1024 * 1024:
                    continue
            except OSError:
                continue
            for m in _JWT_RE.finditer(data):
                jwt = m.group().decode("ascii", errors="replace")
                iat = _jwt_issued_at(jwt)
                if iat is not None and iat > best_iat:
                    best_iat = iat
                    best_token = jwt

        # If we found a token in this dir, check if it's still valid
        if best_token:
            if not _is_jwt_expired(best_token):
                return best_token
            # Keep scanning older dirs — but the most recent is already expired,
            # so older ones will be too. Break to avoid wasting time.
            break

    return best_token


def _jwt_issued_at(jwt: str) -> int | None:
    """Extract 'iat' (issued-at timestamp) from a JWT, or None."""
    parts = jwt.split(".")
    if len(parts) != 3:
        return None
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad))
        return int(payload.get("iat", 0)) or None
    except Exception:
        return None


def _is_jwt_expired(jwt: str) -> bool:
    """Check if a JWT token has expired."""
    parts = jwt.split(".")
    if len(parts) != 3:
        return True
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad))
        exp = int(payload.get("exp", 0))
        if exp <= 0:
            return True
        return exp <= time.time()
    except Exception:
        return True


# ── Device info ───────────────────────────────────────────────────────────────

def _read_device_info() -> dict[str, str]:
    """Read device_id and machine_id from TRAE local config files."""
    info: dict[str, str] = {"device_id": "", "machine_id": ""}

    # device_id from local_env.json
    ep = _env_path()
    if ep is not None:
        try:
            env = json.loads(ep.read_text(encoding="utf-8"))
            info["device_id"] = str(env.get("device_id", ""))
        except (json.JSONDecodeError, OSError):
            pass

    # machine_id from storage.json (telemetry.machineId)
    sp = _storage_path()
    if sp is not None:
        try:
            storage = json.loads(sp.read_text(encoding="utf-8"))
            info["machine_id"] = str(storage.get("telemetry.machineId", ""))
        except (json.JSONDecodeError, OSError):
            pass

    return info


def _get_api_host() -> str:
    """Get the TRAE API host from local_env.json or default."""
    ep = _env_path()
    if ep is not None:
        try:
            env = json.loads(ep.read_text(encoding="utf-8"))
            host_map = env.get("host_map", {})
            for _key, host in host_map.items():
                if host and host.startswith("https://"):
                    return host.rstrip("/")
        except (json.JSONDecodeError, OSError):
            pass
    return _DEFAULT_TRAE_API_HOST


# ── Provider settings ─────────────────────────────────────────────────────────

def _get_trae_cn_info() -> tuple[str, str, dict[str, str]] | None:
    """Return (api_host, token, device_info) for TRAE CN, or None."""
    token = _extract_trae_cn_token()
    if not token:
        return None
    api_host = _get_api_host()
    device_info = _read_device_info()
    return api_host, token, device_info


def trae_cn_provider_settings(
    model: str | None = None,
    thinking: bool = True,
    reasoning_effort: str = "high",
    context_window: int = 128_000,
) -> "AIProviderSettings | None":
    """Return AIProviderSettings for TRAE Work CN's model route."""
    from pa_agent.config.settings import AIProviderSettings

    info = _get_trae_cn_info()
    if info is None:
        logger.debug(
            "TRAE CN info unavailable; set TRAE_CN_API_TOKEN env var or "
            "ensure TRAE Work CN is installed and has been launched at least once."
        )
        return None

    api_host, token, device_info = info
    base_url = f"{api_host}{_TRAE_API_CHAT_PATH}"

    route_model = (
        (model or "").strip()
        if is_openclaw_twc_model(model)
        else _TRAE_MODEL
    )
    api_model = resolve_trae_cn_api_model(route_model)

    logger.info(
        "TRAE CN detected at %s (route=%s api_model=%s device=%s data_dir=%s)",
        api_host,
        route_model,
        api_model,
        device_info.get("device_id", "?"),
        _TRAE_CN_DATA_DIR,
    )
    settings = AIProviderSettings(
        model=route_model,
        base_url=base_url,
        api_key=token,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        context_window=context_window,
    )
    return settings


# ── Apply to settings ─────────────────────────────────────────────────────────

def apply_trae_cn_provider_to_settings(
    settings: Any,
    *,
    preferred_model: str | None = None,
) -> str | None:
    """Populate *settings.provider* from TRAE Work CN environment.

    Returns None on success, or a user-facing error string.
    """
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import is_openclaw_model
    from pa_agent.ai.workbuddy_connector import is_openclaw_wb_model

    model_hint = (preferred_model or getattr(settings.provider, "model", "") or "").strip()
    if is_openclaw_model(model_hint):
        return "模型 openclaw 属于 QClaw 路由，不应走 TRAE Work CN。"
    if is_openclaw_cs_model(model_hint):
        return "模型 openclaw_cs 属于 Cursor 订阅路由，不应走 TRAE Work CN。"
    if is_openclaw_wb_model(model_hint):
        return "模型 openclaw_wb 属于 WorkBuddy 路由，不应走 TRAE Work CN。"

    if not detect_trae_cn():
        return (
            "未检测到 TRAE Work CN 环境。\n\n"
            "请确认：\n"
            "1. TRAE Work CN 已安装在本机\n"
            "2. TRAE Work CN 已启动并完成登录（至少一次）\n\n"
            "也可手动配置 Token（任选其一）：\n"
            f"• 在终端执行：echo \"你的JWT Token\" > {_TRAE_CN_TOKEN_FILE}\n"
            "• 设置环境变量：TRAE_CN_API_TOKEN"
        )

    model_arg = (
        model_hint
        if is_openclaw_twc_model(model_hint)
        else _TRAE_MODEL
    )
    resolved = trae_cn_provider_settings(model=model_arg)
    if resolved is None:
        return (
            "未找到 TRAE Work CN 的 API Token。\n\n"
            "请确认 TRAE Work CN 已启动并完成登录，然后重试。\n\n"
            "也可手动配置 Token（任选其一）：\n"
            f"• echo \"你的JWT Token\" > {_TRAE_CN_TOKEN_FILE}\n"
            "• 设置环境变量：TRAE_CN_API_TOKEN"
        )

    provider = settings.provider
    provider.model = resolved.model
    provider.base_url = resolved.base_url
    provider.api_key = resolved.api_key
    provider.context_window = resolved.context_window

    # Verify token is not expired
    token = resolved.api_key
    if _is_jwt_expired(token):
        return (
            "TRAE Work CN 的 JWT Token 已过期。\n\n"
            "请在 TRAE Work CN 中重新登录，然后重新保存设置。\n"
            "程序会自动从本地存储中提取最新的 Token。"
        )

    return None


# ── Sync on load ──────────────────────────────────────────────────────────────

def sync_trae_cn_provider_on_load(
    settings: Any,
    *,
    save_path: Path | None = None,
) -> None:
    """Refresh token for openclaw_twc routing on load."""
    if not detect_trae_cn():
        return
    provider = settings.provider
    if not is_trae_cn_route(provider):
        return

    before_token = str(getattr(provider, "api_key", "") or "")
    err = apply_trae_cn_provider_to_settings(settings)
    if err:
        logger.warning("TRAE CN provider sync failed: %s", err)
        return

    after_token = str(getattr(provider, "api_key", "") or "")
    if save_path is not None and before_token != after_token:
        try:
            from pa_agent.config.settings import save_settings

            save_settings(settings, save_path)
            logger.info("TRAE CN provider synced on load (token refreshed)")
        except Exception as exc:
            logger.warning("Failed to persist synced TRAE CN provider: %s", exc)


# ── Health check ──────────────────────────────────────────────────────────────

def trae_cn_health_check(*, timeout: float = 10.0) -> tuple[bool, str]:
    """Perform a quick health check against TRAE CN's API.

    Returns a (ok, message) tuple.
    """
    info = _get_trae_cn_info()
    if info is None:
        if detect_trae_cn():
            return (
                False,
                "TRAE Work CN 环境已检测到，但未找到 API Token。\n\n"
                "请启动 TRAE Work CN 并登录，然后重试。\n"
                "也可手动配置：设置环境变量 TRAE_CN_API_TOKEN",
            )
        return False, "TRAE Work CN 环境未检测到"

    api_host, token, device_info = info
    base_url = f"{api_host}{_TRAE_API_CHAT_PATH}"

    if _is_jwt_expired(token):
        return (
            False,
            "TRAE Work CN 的 JWT Token 已过期。\n"
            "请在 TRAE Work CN 中重新登录，然后重试。",
        )

    return True, (
        f"TRAE Work CN 连接配置就绪 ({api_host})，"
        f"模型默认使用 {_TRAE_DEFAULT_INTERNAL_MODEL}"
    )
