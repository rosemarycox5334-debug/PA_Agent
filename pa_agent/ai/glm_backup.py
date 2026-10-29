"""glm-4-flash backup channel for the SenseNova gateway.

Only active when the primary base_url is the SenseNova token gateway. The key
comes from the user's settings (`provider.backup_glm_api_key`), never from
source code or the repository.
"""
from __future__ import annotations

from typing import Any

GLM_MODEL = "glm-4-flash"
GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"


def _is_sensenova(base_url: str | None) -> bool:
    from pa_agent.ai.sensenova_fallback import is_sensenova_fallback_eligible

    return is_sensenova_fallback_eligible(base_url)


def ensure_glm_fallback(provider: Any) -> bool:
    """Attach the glm-4-flash fallback when SenseNova + a GLM key are present.

    Returns True when a fallback entry was appended. Idempotent: an existing
    glm entry (same model and base_url) is left untouched.
    """
    if provider is None or not _is_sensenova(getattr(provider, "base_url", "")):
        return False
    key = str(getattr(provider, "backup_glm_api_key", "") or "").strip()
    if not key:
        return False
    fallbacks = list(getattr(provider, "fallbacks", None) or [])
    for cand in fallbacks:
        if (str(getattr(cand, "model", "")).strip() == GLM_MODEL
                and str(getattr(cand, "base_url", "")).strip() == GLM_BASE_URL):
            if not str(getattr(cand, "api_key", "") or "").strip():
                cand.api_key = key
            return False
    from pa_agent.config.settings import AIProviderSettings

    fallbacks.append(AIProviderSettings(
        model=GLM_MODEL, base_url=GLM_BASE_URL, api_key=key,
        thinking=False, reasoning_effort="low",
    ))
    provider.fallbacks = fallbacks
    return True


def is_sensenova_base(base_url: str | None) -> bool:
    """Public helper for the settings UI."""
    return _is_sensenova(base_url)


def glm_fallback_available(provider: Any) -> bool:
    """True when the GLM key is set and the primary is the SenseNova gateway."""
    return (_is_sensenova(getattr(provider, "base_url", ""))
            and bool(str(getattr(provider, "backup_glm_api_key", "") or "").strip()))
