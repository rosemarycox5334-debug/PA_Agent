"""headless 装配测试：不依赖 Qt、组件齐全."""
import json
import sys


def _write_settings(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps(
            {
                "provider": {
                    "model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com",
                    "api_key": "sk-test",
                },
                "general": {
                    "last_data_source": "tradingview",
                    "last_symbol": "XAUUSD",
                    "last_timeframe": "15m",
                    "watch_symbols": "XAUUSD, BTCUSD",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


def test_bootstrap_headless_no_qt(tmp_path):
    for mod in list(sys.modules):
        if mod.startswith("PyQt6") or mod.startswith("pa_agent.gui"):
            del sys.modules[mod]

    from pa_agent.server.bootstrap import bootstrap_headless

    ctx = bootstrap_headless(settings_path=_write_settings(tmp_path))
    assert ctx.settings.general.last_symbol == "XAUUSD"
    for name in (
        "client",
        "assembler",
        "router",
        "validator",
        "pending_writer",
        "exp_reader",
        "data_source",
    ):
        assert getattr(ctx, name) is not None, name
    # 装配过程不得引入 Qt 或 GUI 模块
    assert not any(m.startswith("PyQt6") for m in sys.modules)
    assert not any(m.startswith("pa_agent.gui") for m in sys.modules)


def test_mt5_falls_back_to_tradingview(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps({"general": {"last_data_source": "mt5"}}), encoding="utf-8"
    )
    from pa_agent.server.bootstrap import bootstrap_headless

    ctx = bootstrap_headless(settings_path=p)
    assert type(ctx.data_source).__name__ == "TradingViewSource"


def test_build_orchestrator(tmp_path):
    from pa_agent.server.bootstrap import bootstrap_headless, build_orchestrator

    ctx = bootstrap_headless(settings_path=_write_settings(tmp_path))
    orch = build_orchestrator(ctx)
    assert orch is not None


def test_rebuild_client_and_data_source(tmp_path):
    from pa_agent.server.bootstrap import (
        bootstrap_headless,
        rebuild_client,
        rebuild_data_source,
    )

    ctx = bootstrap_headless(settings_path=_write_settings(tmp_path))
    old_client, old_ds = ctx.client, ctx.data_source
    rebuild_client(ctx)
    rebuild_data_source(ctx)
    assert ctx.client is not old_client
    assert ctx.data_source is not old_ds
