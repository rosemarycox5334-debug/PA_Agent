"""AI 模型设置（主界面那个窗口）必须提供 GLM 备用 Key 输入。"""
from __future__ import annotations

import pytest

from pa_agent.config.settings import Settings

SENSENOVA = "https://token.sensenova.cn/v1"


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _dialog(settings: Settings):
    from pa_agent.gui.ai_model_settings_dialog import AIModelSettingsDialog
    dlg = AIModelSettingsDialog(settings)
    dlg.show()
    return dlg


def test_glm_row_visible_only_for_sensenova(qapp):
    s = Settings()
    s.provider.base_url = SENSENOVA
    dlg = _dialog(s)
    assert dlg._glm_key_edit.isVisibleTo(dlg) is True
    dlg._base_url_edit.setText("https://api.deepseek.com")
    qapp.processEvents()
    assert dlg._glm_key_edit.isVisibleTo(dlg) is False
    dlg._base_url_edit.setText(SENSENOVA)
    qapp.processEvents()
    assert dlg._glm_key_edit.isVisibleTo(dlg) is True
    dlg.close()


def test_glm_row_loaded_from_settings(qapp):
    s = Settings()
    s.provider.base_url = SENSENOVA
    s.provider.backup_glm_api_key = "glm-key-loaded"
    dlg = _dialog(s)
    assert dlg._glm_key_edit.text() == "glm-key-loaded"
    dlg.close()


def test_save_writes_key_and_builds_fallback(qapp, monkeypatch, tmp_path):
    import pa_agent.gui.ai_model_settings_dialog as mod
    from pa_agent.ai.glm_backup import GLM_BASE_URL, GLM_MODEL
    from pa_agent.config.settings import load_settings
    path = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "SETTINGS_JSON_PATH", path)
    s = Settings()
    s.provider.base_url = SENSENOVA
    s.provider.model = "deepseek-v4-flash"
    s.provider.api_key = "sk-primary"
    dlg = _dialog(s)
    dlg._glm_key_edit.setText("glm-key-新")
    dlg._base_url_edit.setText(SENSENOVA)
    dlg._on_save()
    loaded = load_settings(path)
    assert loaded.provider.backup_glm_api_key == "glm-key-新"
    entry = next(fb for fb in loaded.provider.fallbacks if fb.model == GLM_MODEL)
    assert entry.base_url == GLM_BASE_URL
    assert entry.api_key == "glm-key-新"
    assert entry.thinking is False


def test_clearing_key_drops_glm_fallback(qapp, monkeypatch, tmp_path):
    import pa_agent.gui.ai_model_settings_dialog as mod
    from pa_agent.ai.glm_backup import GLM_MODEL, ensure_glm_fallback
    from pa_agent.config.settings import load_settings
    path = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "SETTINGS_JSON_PATH", path)
    s = Settings()
    s.provider.base_url = SENSENOVA
    s.provider.api_key = "sk-primary"
    s.provider.backup_glm_api_key = "glm-key-abc"
    ensure_glm_fallback(s.provider)
    dlg = _dialog(s)
    dlg._glm_key_edit.setText("")
    dlg._on_save()
    loaded = load_settings(path)
    assert loaded.provider.backup_glm_api_key == ""
    assert all(fb.model != GLM_MODEL for fb in loaded.provider.fallbacks)
