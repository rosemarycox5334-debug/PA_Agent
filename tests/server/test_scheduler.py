"""轮巡调度器测试（并发池版，fake 分析函数与数据源池）."""
import logging
import threading
import time
from types import SimpleNamespace


def _ctx(symbols="AAA, BBB", interval=0, concurrency=2):
    general = SimpleNamespace(
        watch_symbols=symbols,
        last_timeframe="15m",
        watch_round_interval_min=interval,
        watch_concurrency=concurrency,
    )
    provider = SimpleNamespace(api_key="sk-x")
    return SimpleNamespace(
        settings=SimpleNamespace(general=general, provider=provider),
        logger=logging.getLogger("t"),
    )


def _fake_pool():
    return SimpleNamespace(
        acquire=lambda timeout=30: SimpleNamespace(),
        release=lambda ds: None,
    )


def test_parse_watch_symbols():
    from pa_agent.server.scheduler import parse_watch_symbols

    assert parse_watch_symbols("XAUUSD，BTCUSD、eth  xauusd") == [
        "XAUUSD",
        "BTCUSD",
        "eth",
    ]
    assert parse_watch_symbols("") == []


def test_rotation_rounds_and_skip_on_error(monkeypatch):
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    calls = []
    lock = threading.Lock()

    def fake_run(ctx, state, symbol, timeframe, **kw):
        with lock:
            calls.append(symbol)
        if symbol == "AAA" and len(calls) <= 2:
            raise RuntimeError("boom")  # 异常也不能中断轮巡
        return {"ok": True}

    monkeypatch.setattr(sched_mod, "run_symbol_analysis", fake_run)

    state = ServerState()
    s = sched_mod.WatchScheduler(_ctx(), state, _fake_pool())
    assert s.start() is None
    deadline = time.time() + 5
    while len(calls) < 4 and time.time() < deadline:
        time.sleep(0.05)
    s.stop()
    # interval=0 连续轮：两轮内 AAA/BBB 各出现两次（轮内并发无固定顺序）
    assert sorted(calls[:4]) == ["AAA", "AAA", "BBB", "BBB"]
    assert state.snapshot()["scheduler"]["running"] is False


def test_concurrency_cap(monkeypatch):
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    peak = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def fake_run(ctx, state, symbol, timeframe, **kw):
        with lock:
            peak["cur"] += 1
            peak["max"] = max(peak["max"], peak["cur"])
        time.sleep(0.2)
        with lock:
            peak["cur"] -= 1
        return {"ok": True}

    monkeypatch.setattr(sched_mod, "run_symbol_analysis", fake_run)
    s = sched_mod.WatchScheduler(
        _ctx(symbols="A,B,C,D", interval=60, concurrency=2),
        ServerState(),
        _fake_pool(),
    )
    s.start()
    time.sleep(1.0)
    s.stop()
    assert peak["max"] == 2  # 确实并发、且不超配置


def test_stop_cancels_all_active(monkeypatch):
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    cancelled = []
    lock = threading.Lock()

    def fake_run(ctx, state, symbol, timeframe, cancel_token=None, **kw):
        cancel_token.wait(10)
        with lock:
            cancelled.append(symbol)
        return {"ok": False}

    monkeypatch.setattr(sched_mod, "run_symbol_analysis", fake_run)
    s = sched_mod.WatchScheduler(
        _ctx(symbols="A,B", interval=0, concurrency=2), ServerState(), _fake_pool()
    )
    s.start()
    time.sleep(0.3)
    t0 = time.time()
    s.stop()
    assert time.time() - t0 < 5
    assert set(cancelled) == {"A", "B"}


def test_start_validation():
    from pa_agent.server.scheduler import WatchScheduler
    from pa_agent.server.state import ServerState

    # 空监控列表
    assert (
        WatchScheduler(_ctx(symbols="  "), ServerState(), _fake_pool()).start()
        is not None
    )
    # 未配置 API Key
    ctx = _ctx()
    ctx.settings.provider.api_key = ""
    assert WatchScheduler(ctx, ServerState(), _fake_pool()).start() is not None


def test_start_twice_rejected(monkeypatch):
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    monkeypatch.setattr(
        sched_mod,
        "run_symbol_analysis",
        lambda *a, **k: time.sleep(0.2) or {"ok": True},
    )
    s = sched_mod.WatchScheduler(
        _ctx(symbols="AAA", interval=60), ServerState(), _fake_pool()
    )
    assert s.start() is None
    assert s.start() is not None  # 已在运行
    s.stop()


def test_stop_during_round_wait(monkeypatch):
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    monkeypatch.setattr(sched_mod, "run_symbol_analysis", lambda *a, **k: {"ok": True})
    state = ServerState()
    s = sched_mod.WatchScheduler(_ctx(symbols="AAA", interval=60), state, _fake_pool())
    s.start()
    deadline = time.time() + 5
    while state.snapshot()["round_wait_eta"] is None and time.time() < deadline:
        time.sleep(0.05)  # 等进入 round_wait（60 分钟间隔）
    t0 = time.time()
    s.stop()
    assert time.time() - t0 < 5  # stop 立即生效，而非等满一轮间隔
    assert not s.running
