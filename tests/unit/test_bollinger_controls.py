"""Tests for the BOLL controls shared by the chart and prompt settings."""
from __future__ import annotations

from unittest.mock import patch

from pa_agent.app_context import AppContext
from pa_agent.config.settings import Settings
from pa_agent.gui.main_window import MainWindow


def test_main_window_restores_and_applies_bollinger_controls(qtbot) -> None:
    settings = Settings()
    settings.prompt.boll_period = 34
    settings.prompt.boll_stddev = 2.5
    window = MainWindow(AppContext(settings=settings))
    qtbot.addWidget(window)

    assert window._boll_period_spin.value() == 34
    assert window._boll_stddev_spin.value() == 2.5
    assert window._chart_widget.bollinger_params() == (34, 2.5)

    with patch("pa_agent.config.settings.save_settings") as save_mock:
        window._boll_period_spin.setValue(21)
        window._boll_stddev_spin.setValue(1.8)

    assert settings.prompt.boll_period == 21
    assert settings.prompt.boll_stddev == 1.8
    assert window._chart_widget.bollinger_params() == (21, 1.8)
    assert save_mock.called


def test_bollinger_controls_lock_while_analysis_is_running(qtbot) -> None:
    window = MainWindow(AppContext(settings=Settings()))
    qtbot.addWidget(window)

    window._analysis_in_progress = True
    window._sync_submit_button_state()

    assert not window._boll_period_spin.isEnabled()
    assert not window._boll_stddev_spin.isEnabled()
