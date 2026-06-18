"""Unit tests for settings load/save round-trip (task 2.4)."""
from __future__ import annotations
import json
import pytest
from pathlib import Path
from pa_agent.config.settings import Settings, load_settings, save_settings


def test_defaults(tmp_path):
    """load_settings on a missing file returns defaults and creates the file."""
    p = tmp_path / "settings.json"
    s = load_settings(p)
    assert s.provider.model == "deepseek-v4-flash"
    assert s.provider.base_url == "https://api.deepseek.com"
    assert s.provider.thinking is True
    assert s.provider.reasoning_effort == "max"
    assert s.provider.context_window == 2_000_000
    assert s.general.analysis_bar_count == 100
    assert s.general.last_symbol == "XAUUSDm"
    assert s.general.last_timeframe == "15m"
    assert s.general.decision_stance == "balanced"
    assert s.general.decision_flow_auto_play is True
    assert s.general.auto_resume_chart_after_analysis is False
    assert p.exists(), "defaults should be written to disk"


def test_round_trip(tmp_path):
    """save → load preserves all fields."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.provider.api_key = "sk-test-1234"
    original.general.last_symbol = "BTCUSDT"
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.provider.api_key == "sk-test-1234"
    # Crypto symbols migrate to gold defaults on load
    assert loaded.general.last_symbol == "XAUUSDm"
    assert loaded.provider.model == original.provider.model


def test_api_key_present_on_disk(tmp_path):
    """The saved JSON contains the plaintext API key."""
    p = tmp_path / "settings.json"
    s = Settings()
    s.provider.api_key = "sk-super-secret-key"
    save_settings(s, p)
    raw = p.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["provider"]["api_key"] == "sk-super-secret-key"


def test_corrupt_json_returns_defaults(tmp_path):
    """Corrupt settings.json falls back to defaults without raising."""
    p = tmp_path / "settings.json"
    p.write_text("{not valid json", encoding="utf-8")
    s = load_settings(p)
    assert s.provider.model == "deepseek-v4-flash"


def test_missing_api_key_leaves_api_key_blank(tmp_path):
    """If api_key is absent, api_key stays empty string."""
    p = tmp_path / "settings.json"
    data = Settings().model_dump()
    data["provider"].pop("api_key", None)
    data["provider"].pop("api_key_encrypted", None)
    p.write_text(json.dumps(data), encoding="utf-8")
    s = load_settings(p)
    assert s.provider.api_key == ""


def test_feishu_round_trip(tmp_path):
    """save → load preserves feishu settings."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.feishu.webhook_url = "https://example.com/hook"
    original.feishu.secret = "sec"
    original.feishu.app_id = "cli_test"
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.feishu.webhook_url == "https://example.com/hook"
    assert loaded.feishu.secret == "sec"
    assert loaded.feishu.app_id == "cli_test"


def test_tushare_round_trip(tmp_path):
    """save/load preserves the locally configured Tushare token."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.tushare.token = "ts-test-token"
    save_settings(original, p)

    loaded = load_settings(p)

    assert loaded.tushare.token == "ts-test-token"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["tushare"]["token"] == "ts-test-token"


def test_migrate_legacy_feishu_json(tmp_path):
    """Legacy config/feishu.json is merged into settings.json on load."""
    p = tmp_path / "settings.json"
    legacy = tmp_path / "feishu.json"
    save_settings(Settings(), p)
    legacy.write_text(
        json.dumps(
            {
                "enabled": True,
                "webhook_url": "https://example.com/legacy-hook",
                "secret": "legacy-secret",
                "app_id": "cli_legacy",
                "app_secret": "legacy-app-secret",
                "notify_on_order_only": True,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings(p)
    assert loaded.feishu.webhook_url == "https://example.com/legacy-hook"
    assert loaded.feishu.secret == "legacy-secret"
    assert loaded.feishu.app_id == "cli_legacy"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["feishu"]["webhook_url"] == "https://example.com/legacy-hook"
