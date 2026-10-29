"""Settings dialog: GLM backup key field appears only for the SenseNova URL."""
from __future__ import annotations

import pytest

from pa_agent.config.settings import Settings


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    return app


def _dialog(settings: Settings):
    from pa_agent.gui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(settings)
    dlg.show()
    return dlg


def test_field_hidden_for_non_sensenova(qapp):
    s = Settings()
    s.provider.base_url = "https://api.deepseek.com"
    dlg = _dialog(s)
    assert dlg._glm_key_edit.isVisibleTo(dlg) is False
    dlg._base_url_edit.setText("https://token.sensenova.cn/v1")
    qapp.processEvents()
    assert dlg._glm_key_edit.isVisibleTo(dlg) is True
    dlg.close()


def test_field_shown_and_loaded_for_sensenova(qapp):
    s = Settings()
    s.provider.base_url = "https://token.sensenova.cn/v1"
    s.provider.backup_glm_api_key = "glm-key-xyz"
    dlg = _dialog(s)
    assert dlg._glm_key_edit.isVisibleTo(dlg) is True
    assert dlg._glm_key_edit.text() == "glm-key-xyz"
    # 用户要求不显示说明性长句：界面只保留输入框本身。
    assert "主通道失败会自动切到" not in " ".join(
        w.text() for w in dlg.findChildren(type(dlg._glm_key_label))
    )
    dlg.close()


def test_saving_builds_fallback_chain(qapp, monkeypatch, tmp_path):
    import pa_agent.gui.settings_dialog as mod
    path = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "SETTINGS_JSON_PATH", path)
    from pa_agent.ai.glm_backup import GLM_BASE_URL, GLM_MODEL
    from pa_agent.config.settings import load_settings
    s = Settings()
    s.provider.base_url = "https://token.sensenova.cn/v1"
    s.provider.api_key = "sk-primary"
    s.provider.model = "deepseek-v4-flash"
    dlg = _dialog(s)
    dlg._glm_key_edit.setText("glm-key-abc")
    dlg._on_save()
    loaded = load_settings(path)
    assert loaded.provider.backup_glm_api_key == "glm-key-abc"
    entry = next(fb for fb in loaded.provider.fallbacks if fb.model == GLM_MODEL)
    assert entry.base_url == GLM_BASE_URL
    assert entry.api_key == "glm-key-abc"
    assert entry.thinking is False


def test_clearing_key_removes_glm_fallback(qapp, monkeypatch, tmp_path):
    import pa_agent.gui.settings_dialog as mod
    path = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "SETTINGS_JSON_PATH", path)
    from pa_agent.ai.glm_backup import GLM_MODEL, ensure_glm_fallback
    from pa_agent.config.settings import load_settings
    s = Settings()
    s.provider.base_url = "https://token.sensenova.cn/v1"
    s.provider.api_key = "sk-primary"
    s.provider.backup_glm_api_key = "glm-key-abc"
    ensure_glm_fallback(s.provider)
    assert s.provider.fallbacks
    dlg = _dialog(s)
    dlg._glm_key_edit.setText("")
    dlg._on_save()
    loaded = load_settings(path)
    assert loaded.provider.backup_glm_api_key == ""
    assert all(fb.model != GLM_MODEL for fb in loaded.provider.fallbacks)
