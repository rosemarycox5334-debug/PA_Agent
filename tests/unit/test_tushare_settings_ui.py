"""Tests for the TuShare token field in general settings."""
from __future__ import annotations

from PyQt6.QtWidgets import QLineEdit

from pa_agent.config.settings import Settings
from pa_agent.gui.general_settings_dialog import GeneralSettingsDialog


def test_tushare_token_is_loaded_and_masked(qtbot) -> None:
    settings = Settings(tushare={"token": "ts-secret"})
    dialog = GeneralSettingsDialog(settings)
    qtbot.addWidget(dialog)

    assert dialog._tushare_token_edit.text() == "ts-secret"
    assert dialog._tushare_token_edit.echoMode() == QLineEdit.EchoMode.Password


def test_tushare_token_visibility_toggle(qtbot) -> None:
    dialog = GeneralSettingsDialog(Settings())
    qtbot.addWidget(dialog)

    dialog._show_tushare_token_btn.setChecked(True)
    assert dialog._tushare_token_edit.echoMode() == QLineEdit.EchoMode.Normal
    assert dialog._show_tushare_token_btn.text() == "隐藏"

    dialog._show_tushare_token_btn.setChecked(False)
    assert dialog._tushare_token_edit.echoMode() == QLineEdit.EchoMode.Password
    assert dialog._show_tushare_token_btn.text() == "显示"


def test_tushare_token_is_saved(monkeypatch, qtbot) -> None:
    settings = Settings()
    dialog = GeneralSettingsDialog(settings)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        "pa_agent.gui.general_settings_dialog.save_settings",
        lambda *_args, **_kwargs: None,
    )
    dialog._tushare_token_edit.setText(" new-token ")

    dialog._on_save()

    assert settings.tushare.token == "new-token"
