"""火山方舟 (Ark) 网关适配：模型别名、模型池与自动降级链。

设计目标（与 SenseNova 主通道的处理方式保持一致）：

* 用户只填 ``base_url`` + 一个习惯的模型名（例如 ``deepseek-flash``），
  由本模块翻译成方舟真实的模型 ID；
* 方舟只接受 ``reasoning_effort`` low/medium/high，项目统一用 ``low``；
* 某个模型额度用完或未开通时，自动在方舟模型池里换下一个；
* 方舟模型池全部不可用时，交给 ``glm_backup`` / 免费备用通道。

本模块只做纯函数式的模型选择，不发网络请求；实际切换由
``TwoStageOrchestrator._try_next_provider_fallback`` 完成。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

#: 用户输入的习惯名 -> 方舟真实模型 ID。仅在 Ark 网关下生效。
ARK_MODEL_ALIASES: dict[str, str] = {
    "deepseek-flash": "deepseek-v4-1-flash-economy-261017",
    "deepseek": "deepseek-v4-1-flash-261017",
    "deepseek-chat": "deepseek-v4-1-flash-261017",
    "glm-flash": "glm-5-3-flash-260828",
    "doubao-flash": "doubao-seed-2-1-lite-260915",
}

#: 额度耗尽 / 未开通时依次尝试的方舟模型（按实测：额度充足、速度可接受优先）。
ARK_MODEL_POOL: tuple[str, ...] = (
    "doubao-seed-evolving",
    "glm-5-3-flash-260828",
    "deepseek-v4-pro-ga-260813",
    "deepseek-v4-flash-ga-260731",
    "glm-5-2-260617",
    "doubao-seed-2-1-turbo-260628",
    "doubao-seed-2-1-pro-260915",
    "doubao-seed-2-0-lite-260428",
    "doubao-seed-2-0-mini-260428",
    "kimi-k3-1-pro-261024",
    "doubao-seed-2-1-lite-260915",
    "deepseek-v4-1-flash-261017",
    "deepseek-v4-1-flash-economy-261017",
)

#: 方舟统一使用的思考强度（上游只支持 low/medium/high）。
ARK_THINKING_EFFORT = "low"

#: 每次分析轮换一次的模型顺序（用户 2026-11-07 指定：Kimi → 经济版 → 2.1-lite → 其余）。
#: 不论设置里填的是哪个模型，方舟下都按这个循环取下一个。
ARK_ROTATION_POOL: tuple[str, ...] = (
    "kimi-k3-1-pro-261024",
    "deepseek-v4-1-flash-economy-261017",
    "doubao-seed-2-1-lite-260915",
    "glm-5-3-flash-260828",
    "doubao-seed-evolving",
    "deepseek-v4-flash-ga-260731",
    "deepseek-v4-pro-ga-260813",
    "glm-5-2-260617",
    "doubao-seed-2-1-turbo-260628",
    "doubao-seed-2-1-pro-260915",
    "doubao-seed-2-0-lite-260428",
    "doubao-seed-2-0-mini-260428",
    "deepseek-v4-1-flash-261017",
)

_ROTATION_LOCK = threading.Lock()


def rotation_state_path() -> Path:
    from pa_agent.config.paths import CONFIG_DIR

    return CONFIG_DIR / "ark_rotation_state.json"


def next_rotation_model(state_path: Path | None = None) -> str:
    """Return the model for this analysis and advance the persisted cursor.

    The cursor lives in ``config/ark_rotation_state.json`` so restarts continue
    where the previous run stopped. Any read/write problem falls back to the
    first model instead of blocking the analysis.
    """
    path = state_path or rotation_state_path()
    with _ROTATION_LOCK:
        cursor = 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cursor = int(raw.get("cursor", 0))
        except (OSError, ValueError, TypeError):
            cursor = 0
        model = ARK_ROTATION_POOL[cursor % len(ARK_ROTATION_POOL)]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"cursor": (cursor + 1) % len(ARK_ROTATION_POOL), "last_model": model},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        return model


def is_account_level_error(exc: BaseException | str | None) -> bool:
    """True for账号级封禁（欠费/冻结），换同一网关的模型也没用。

    火山方舟欠费时对**所有**模型返回 403 ``AccountOverdueError``；此时逐个换
    模型只会白跑十几秒，应直接跳到免费池 / 备用通道。
    """
    msg = (str(exc) if exc is not None else "").lower()
    return any(
        marker in msg
        for marker in (
            "accountoverdue",
            "overdue balance",
            "account has been suspended",
            "arrear",
        )
    )


def is_ark_base(base_url: str | None) -> bool:
    """True when *base_url* points at the Ark OpenAI-compatible gateway."""
    url = (base_url or "").strip().lower()
    return "volces.com" in url or "volcengine" in url or "ark.cn-beijing" in url


def resolve_ark_model(model: str) -> str:
    """Translate a friendly alias to the real Ark model ID (identity otherwise)."""
    raw = (model or "").strip()
    return ARK_MODEL_ALIASES.get(raw.lower(), raw)


def ark_fallback_models(primary_model: str) -> list[str]:
    """Ark pool minus whichever model is already primary, order preserved."""
    current = resolve_ark_model(primary_model).strip()
    return [m for m in ARK_MODEL_POOL if m != current]


def ensure_ark_fallbacks(provider: Any) -> int:
    """Append other Ark models to ``provider.fallbacks``; return how many were added.

    Idempotent: candidates already present (same base_url + model) are skipped, so
    repeated analyses never duplicate the chain. Only active for the Ark gateway.
    """
    if provider is None or not is_ark_base(getattr(provider, "base_url", "")):
        return 0
    api_key = str(getattr(provider, "api_key", "") or "").strip()
    if not api_key:
        return 0
    fallbacks = list(getattr(provider, "fallbacks", None) or [])
    existing = {
        (str(getattr(c, "model", "")).strip(), str(getattr(c, "base_url", "")).strip())
        for c in fallbacks
    }
    from pa_agent.config.settings import AIProviderSettings

    added = 0
    for model in ark_fallback_models(getattr(provider, "model", "")):
        if (model, ARK_BASE_URL) in existing:
            continue
        fallbacks.append(
            AIProviderSettings(
                model=model,
                base_url=ARK_BASE_URL,
                api_key=api_key,
                thinking=True,
                reasoning_effort=ARK_THINKING_EFFORT,
            )
        )
        existing.add((model, ARK_BASE_URL))
        added += 1
    if added:
        provider.fallbacks = fallbacks
    return added
