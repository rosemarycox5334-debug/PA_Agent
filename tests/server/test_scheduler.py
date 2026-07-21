"""轮巡调度器状态机测试（fake 分析函数）."""
import logging
import time
from types import SimpleNamespace


def _ctx(symbols="AAA, BBB", interval=0):
    general = SimpleNamespace(
        watch_symbols=symbols,
        last_timeframe="15m",
        watch_round_interval_min=interval,
    )
    provider = SimpleNamespace(api_key="sk-x")
    return SimpleNamespace(
        settings=SimpleNamespace(general=general, provider=provider),
        logger=logging.getLogger("t"),
    )


def test_parse_watch_symbols():
    from pa_agent.server.scheduler import parse_watch_symbols

    assert parse_watch_symbols("XAUUSD，BTCUSD、eth  xauusd") == [
        "XAUUSD",
        "BTCUSD",
        "eth",
    ]
    assert parse_watch_symbols("") == []


def test_rotation_order_and_skip_on_error(monkeypatch):
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    calls = []

    def fake_run(ctx, state, symbol, timeframe, **kw):
        calls.append(symbol)
        if symbol == "AAA" and len(calls) <= 2:
            raise RuntimeError("boom")  # 异常也不能中断轮巡
        return {"ok": True}

    monkeypatch.setattr(sched_mod, "run_symbol_analysis", fake_run)

    state = ServerState()
    s = sched_mod.WatchScheduler(_ctx(), state)
    assert s.start() is None
    deadline = time.time() + 5
    while len(calls) < 4 and time.time() < deadline:
        time.sleep(0.05)
    s.stop()
    assert calls[:4] == ["AAA", "BBB", "AAA", "BBB"]  # interval=0 连续轮
    assert state.snapshot()["scheduler"]["running"] is False


def test_start_validation():
    from pa_agent.server.scheduler import WatchScheduler
    from pa_agent.server.state import ServerState

    # 空监控列表
    assert WatchScheduler(_ctx(symbols="  "), ServerState()).start() is not None
    # 未配置 API Key
    ctx = _ctx()
    ctx.settings.provider.api_key = ""
    assert WatchScheduler(ctx, ServerState()).start() is not None


def test_start_twice_rejected(monkeypatch):
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    monkeypatch.setattr(
        sched_mod, "run_symbol_analysis", lambda *a, **k: time.sleep(0.2) or {"ok": True}
    )
    s = sched_mod.WatchScheduler(_ctx(symbols="AAA", interval=60), ServerState())
    assert s.start() is None
    assert s.start() is not None  # 已在运行
    s.stop()


def test_stop_during_round_wait(monkeypatch):
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    monkeypatch.setattr(
        sched_mod, "run_symbol_analysis", lambda *a, **k: {"ok": True}
    )
    state = ServerState()
    s = sched_mod.WatchScheduler(_ctx(symbols="AAA", interval=60), state)
    s.start()
    deadline = time.time() + 5
    while state.snapshot()["round_wait_eta"] is None and time.time() < deadline:
        time.sleep(0.05)  # 等进入 round_wait（60 分钟间隔）
    t0 = time.time()
    s.stop()
    assert time.time() - t0 < 5  # stop 立即生效，而非等满一轮间隔
    assert not s.running
