"""GUI analysis mode presets for Stage 2 prompt construction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pa_agent.config.settings import Settings

AnalysisModeKey = Literal["original", "optimized"]


@dataclass(frozen=True)
class GuiAnalysisMode:
    """A selectable analysis process mode."""

    key: AnalysisModeKey
    label: str
    description: str


_MODES: tuple[GuiAnalysisMode, ...] = (
    GuiAnalysisMode(
        key="original",
        label="原始分析过程",
        description="保留当前完整二阶段续写上下文，适合复盘和最大可追溯性。",
    ),
    GuiAnalysisMode(
        key="optimized",
        label="优化分析过程",
        description="减少二阶段重复上下文和冗长思考，适合日常盯盘提速。",
    ),
)

_MODE_BY_KEY = {mode.key: mode for mode in _MODES}


def analysis_mode_choices() -> list[tuple[str, str]]:
    """Return toolbar choices in display order."""
    return [(mode.key, mode.label) for mode in _MODES]


def get_analysis_mode(key: str) -> GuiAnalysisMode:
    """Return an analysis mode by key, raising KeyError for unknown keys."""
    return _MODE_BY_KEY[key]  # type: ignore[index]


def apply_analysis_mode(settings: "Settings", key: str) -> GuiAnalysisMode:
    """Apply *key* to in-memory settings used by the next GUI analysis."""
    mode = get_analysis_mode(key)
    settings.general.analysis_mode = mode.key
    return mode


def infer_analysis_mode_key(settings: "Settings | None") -> str:
    """Best-effort mode match for current settings."""
    if settings is None:
        return "original"
    mode = str(getattr(settings.general, "analysis_mode", "original") or "original")
    if mode in _MODE_BY_KEY:
        return mode
    return "original"
