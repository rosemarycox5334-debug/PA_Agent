"""Unit tests for GUI speed profile presets."""
from __future__ import annotations

import pytest

from pa_agent.config.settings import Settings
from pa_agent.gui.analysis_modes import (
    analysis_mode_choices,
    apply_analysis_mode,
    infer_analysis_mode_key,
)
from pa_agent.gui.speed_profiles import apply_speed_profile, speed_profile_choices


def test_speed_profile_choices_are_ordered_for_toolbar():
    """Toolbar choices should keep the recommended daily profile in the middle."""
    choices = speed_profile_choices()

    assert [key for key, _ in choices] == ["fast", "standard", "deep"]
    assert choices[1][1] == "标准分析"


@pytest.mark.parametrize(
    ("key", "bar_count", "thinking", "effort"),
    [
        ("fast", 50, False, "low"),
        ("standard", 100, True, "high"),
        ("deep", 200, True, "max"),
    ],
)
def test_apply_speed_profile_updates_analysis_settings(
    key: str,
    bar_count: int,
    thinking: bool,
    effort: str,
):
    """Applying a profile changes the settings used by the next GUI submission."""
    settings = Settings()
    settings.general.analysis_bar_count = 123
    settings.provider.thinking = not thinking
    settings.provider.reasoning_effort = "medium"

    profile = apply_speed_profile(settings, key)

    assert profile.key == key
    assert settings.general.analysis_bar_count == bar_count
    assert settings.provider.thinking is thinking
    assert settings.provider.reasoning_effort == effort


def test_apply_speed_profile_rejects_unknown_key():
    """Unknown profile keys fail loudly instead of silently changing settings."""
    with pytest.raises(KeyError):
        apply_speed_profile(Settings(), "turbo")


def test_analysis_mode_choices_keep_original_first():
    """Toolbar analysis modes must preserve the legacy path as the first choice."""
    choices = analysis_mode_choices()

    assert choices == [
        ("original", "原始分析过程"),
        ("optimized", "优化分析过程"),
    ]


def test_apply_analysis_mode_updates_general_settings():
    settings = Settings()

    mode = apply_analysis_mode(settings, "optimized")

    assert mode.key == "optimized"
    assert settings.general.analysis_mode == "optimized"
    assert infer_analysis_mode_key(settings) == "optimized"


def test_apply_analysis_mode_rejects_unknown_key():
    with pytest.raises(KeyError):
        apply_analysis_mode(Settings(), "unknown")
