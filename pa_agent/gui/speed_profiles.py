"""GUI speed profile presets for analysis submissions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pa_agent.config.settings import Settings


@dataclass(frozen=True)
class GuiSpeedProfile:
    """A small preset that trades analysis depth for turnaround time."""

    key: str
    label: str
    analysis_bar_count: int
    thinking: bool
    reasoning_effort: str
    description: str


_PROFILES: tuple[GuiSpeedProfile, ...] = (
    GuiSpeedProfile(
        key="fast",
        label="极速扫描",
        analysis_bar_count=50,
        thinking=False,
        reasoning_effort="low",
        description="少量K线，关闭思考，适合快速扫方向。",
    ),
    GuiSpeedProfile(
        key="standard",
        label="标准分析",
        analysis_bar_count=100,
        thinking=True,
        reasoning_effort="high",
        description="日常盯盘推荐，速度和质量相对平衡。",
    ),
    GuiSpeedProfile(
        key="deep",
        label="深度复盘",
        analysis_bar_count=200,
        thinking=True,
        reasoning_effort="max",
        description="更多上下文和最深推理，适合关键位置或复盘。",
    ),
)

_PROFILE_BY_KEY = {profile.key: profile for profile in _PROFILES}


def speed_profile_choices() -> list[tuple[str, str]]:
    """Return toolbar choices in display order."""
    return [(profile.key, profile.label) for profile in _PROFILES]


def get_speed_profile(key: str) -> GuiSpeedProfile:
    """Return a profile by key, raising KeyError for unknown keys."""
    return _PROFILE_BY_KEY[key]


def apply_speed_profile(settings: "Settings", key: str) -> GuiSpeedProfile:
    """Apply *key* to in-memory settings used by the next GUI analysis."""
    profile = get_speed_profile(key)
    settings.general.analysis_bar_count = profile.analysis_bar_count
    settings.provider.thinking = profile.thinking
    settings.provider.reasoning_effort = profile.reasoning_effort  # type: ignore[assignment]
    return profile


def infer_speed_profile_key(settings: "Settings | None") -> str:
    """Best-effort profile match for current settings."""
    if settings is None:
        return "standard"
    bar_count = int(getattr(settings.general, "analysis_bar_count", 100) or 100)
    thinking = bool(getattr(settings.provider, "thinking", True))
    effort = str(getattr(settings.provider, "reasoning_effort", "high") or "high")
    for profile in _PROFILES:
        if (
            profile.analysis_bar_count == bar_count
            and profile.thinking == thinking
            and profile.reasoning_effort == effort
        ):
            return profile.key
    return "standard"
