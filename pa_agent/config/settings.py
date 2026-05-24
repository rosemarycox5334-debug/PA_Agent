"""Pydantic settings models for PA Agent."""
from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]


class AIProviderSettings(BaseModel):
    """AI provider connection and behaviour settings."""
    model_config = ConfigDict(extra="ignore")

    model: str = "claude-sonnet-4-6"
    base_url: str = "https://www.packyapi.com/v1"
    api_key: str = ""
    api_key_encrypted: str = ""
    thinking: bool = True
    reasoning_effort: Literal["low", "medium", "high", "max"] = "max"
    context_window: int = 2_000_000


class GeneralSettings(BaseModel):
    """UI and data-feed general settings."""
    model_config = ConfigDict(extra="ignore")

    default_bar_count: int = 100
    refresh_interval_ms: int = 1000
    context_warning_threshold_pct: float = 80.0
    last_symbol: str = "XAUUSDm"
    last_timeframe: str = "15m"
    decision_flow_auto_play: bool = True
    decision_flow_play_seconds: int = 50
    incremental_max_new_bars: int = Field(default=10, ge=0, le=500)
    #: 增量分析时发送的历史 K 线数量上限；0 = 发送全部（默认，分析最全面）
    incremental_recent_bar_limit: int = Field(default=0, ge=0, le=500)
    #: 阶段二交易倾向：balanced=默认；conservative/aggressive 逐级调整下单意愿
    decision_stance: DecisionStance = "balanced"
    #: 决策树可视化：在「整图适配」基础上的缩放百分比（100=与适配一致；可任意放大，仅下限 10%）
    decision_flow_default_zoom_pct: int = Field(default=500, ge=10)
    #: 「实时」页思考过程/撰写回答框与追问输入框的等宽字体字号（pt）
    stream_pane_font_pt: int = Field(default=11, ge=8, le=28)
    #: K 线图上 #序号 标签的字号（pt）
    chart_seq_label_font_pt: int = Field(default=7, ge=6, le=24)

    @field_validator("decision_flow_default_zoom_pct", mode="before")
    @classmethod
    def _coerce_zoom_pct(cls, v: object) -> object:
        if v is None:
            return 50
        return v


class Settings(BaseModel):
    """Root settings object persisted to config/settings.json."""
    model_config = ConfigDict(extra="ignore")

    provider: AIProviderSettings = Field(default_factory=AIProviderSettings)
    general: GeneralSettings = Field(default_factory=GeneralSettings)


def provider_api_key_configured(settings: Settings | None) -> bool:
    """Return True when a non-empty API key is loaded in memory."""
    if settings is None:
        return False
    return bool((settings.provider.api_key or "").strip())


# ── Persistence ───────────────────────────────────────────────────────────────
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_settings(path: Path | None = None) -> "Settings":
    """Load settings from *path* (default: SETTINGS_JSON_PATH).

    Decrypts api_key_encrypted → api_key in memory.
    Returns default Settings and writes them to disk if the file is absent.
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.security.secret_store import SecretStore

    path = path or SETTINGS_JSON_PATH

    if not path.exists():
        defaults = Settings()
        save_settings(defaults, path)
        return defaults

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("settings.json unreadable (%s); using defaults", exc)
        return Settings()

    # Migrate legacy field names
    general = raw.get("general", {})
    if "cost_warning_threshold_pct" in general and "context_warning_threshold_pct" not in general:
        general["context_warning_threshold_pct"] = general.pop("cost_warning_threshold_pct")
    general.pop("last_htf_text", None)
    raw["general"] = general
    provider = raw.get("provider", {})
    provider.pop("pricing", None)
    raw["provider"] = provider

    encrypted = provider.get("api_key_encrypted", "")
    if encrypted:
        try:
            raw.setdefault("provider", {})["api_key"] = SecretStore.decrypt(encrypted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to decrypt api_key (%s); leaving blank", exc)
            raw.setdefault("provider", {})["api_key"] = ""

    return Settings.model_validate(raw)


def save_settings(settings: "Settings", path: Path | None = None) -> None:
    """Persist settings to *path* (default: SETTINGS_JSON_PATH).

    Encrypts api_key → api_key_encrypted; never writes plaintext api_key.
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.security.secret_store import SecretStore

    path = path or SETTINGS_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = settings.model_dump()

    plaintext = data.get("provider", {}).get("api_key", "")
    if plaintext:
        data["provider"]["api_key_encrypted"] = SecretStore.encrypt(plaintext)
    else:
        data["provider"]["api_key_encrypted"] = ""
    data["provider"].pop("api_key", None)

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
