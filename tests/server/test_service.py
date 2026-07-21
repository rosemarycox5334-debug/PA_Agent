"""run_symbol_analysis 编排测试：fake 数据源与 orchestrator."""
from types import SimpleNamespace
from unittest.mock import patch


def _fake_ctx(bars):
    class FakeDS:
        def __init__(self):
            self.subscribed = None
            self.connected = False

        def connect(self):
            self.connected = True

        def subscribe(self, s, tf):
            self.subscribed = (s, tf)

        def latest_snapshot(self, n):
            return bars[:n]

    general = SimpleNamespace(
        analysis_bar_count=5,
        decision_confidence_threshold=0,
        decision_stance="",
        structure_flip_cooldown_bars=3,
        last_tradingview_exchange="",
    )
    settings = SimpleNamespace(
        general=general,
        provider=SimpleNamespace(model="m"),
        feishu=SimpleNamespace(enabled=False),
        pushplus=SimpleNamespace(enabled=False),
    )
    return SimpleNamespace(
        settings=settings,
        logger=__import__("logging").getLogger("t"),
        data_source=FakeDS(),
        client=None,
        assembler=None,
        router=None,
        validator=None,
        pending_writer=None,
        exp_reader=None,
    )


def _mk_bars(n):
    from pa_agent.data.base import KlineBar

    return [
        KlineBar(
            seq=i,
            ts_open=1700000000.0 + 900 * (n - i),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10.0,
            closed=True,
        )
        for i in range(n)
    ]


def test_success_flow_with_order(monkeypatch):
    from pa_agent.server import service
    from pa_agent.server.state import ServerState

    ctx = _fake_ctx(_mk_bars(60))
    state = ServerState()
    record = SimpleNamespace(
        stage2_decision={
            "decision": {
                "order_type": "限价单",
                "order_direction": "做多",
                "trade_confidence": 66,
            }
        }
    )
    fake_orch = SimpleNamespace(submit=lambda *a, **k: record)
    notified = {}
    monkeypatch.setattr(
        service, "_notify_order", lambda *a, **k: notified.setdefault("called", True)
    )
    with patch("pa_agent.server.service.build_orchestrator", return_value=fake_orch):
        summary = service.run_symbol_analysis(ctx, state, "XAUUSD", "15m")
    assert summary["ok"] and summary["has_order"] and summary["direction"] == "做多"
    assert notified.get("called") is True
    assert state.snapshot()["results"]["XAUUSD"]["ok"] is True
    assert ctx.data_source.subscribed == ("XAUUSD", "15m")


def test_data_timeout_returns_error(monkeypatch):
    from pa_agent.server import service
    from pa_agent.server.state import ServerState

    ctx = _fake_ctx([])  # 无数据
    monkeypatch.setattr(service, "DATA_READY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(service, "_WAIT_POLL_S", 0.05)
    state = ServerState()
    summary = service.run_symbol_analysis(ctx, state, "XAUUSD", "15m")
    assert summary["ok"] is False and summary["error"]
    assert state.snapshot()["results"]["XAUUSD"]["ok"] is False


def test_no_order_skips_notify(monkeypatch):
    from pa_agent.server import service
    from pa_agent.server.state import ServerState

    ctx = _fake_ctx(_mk_bars(60))
    record = SimpleNamespace(stage2_decision={"decision": {"order_type": "观望"}})
    fake_orch = SimpleNamespace(submit=lambda *a, **k: record)
    called = {}
    monkeypatch.setattr(
        service, "_notify_order", lambda *a, **k: called.setdefault("x", True)
    )
    with patch("pa_agent.server.service.build_orchestrator", return_value=fake_orch):
        summary = service.run_symbol_analysis(ctx, ServerState(), "XAUUSD", "15m")
    assert summary["ok"] and not summary["has_order"] and "x" not in called


def test_confidence_threshold_blocks_notify(monkeypatch):
    from pa_agent.server import service
    from pa_agent.server.state import ServerState

    ctx = _fake_ctx(_mk_bars(60))
    ctx.settings.general.decision_confidence_threshold = 80
    record = SimpleNamespace(
        stage2_decision={
            "decision": {"order_type": "限价单", "trade_confidence": 50}
        }
    )
    fake_orch = SimpleNamespace(submit=lambda *a, **k: record)
    called = {}
    monkeypatch.setattr(
        service, "_notify_order", lambda *a, **k: called.setdefault("x", True)
    )
    with patch("pa_agent.server.service.build_orchestrator", return_value=fake_orch):
        summary = service.run_symbol_analysis(ctx, ServerState(), "XAUUSD", "15m")
    assert summary["ok"] and not summary["has_order"] and "x" not in called


def test_cancel_interrupts_data_wait(monkeypatch):
    """stop 期间的数据等待必须立即被 cancel_token 打断，而非等满 120s."""
    import threading
    import time

    from pa_agent.server import service
    from pa_agent.server.state import ServerState
    from pa_agent.util.threading import CancelToken

    ctx = _fake_ctx([])  # 永远无数据
    monkeypatch.setattr(service, "DATA_READY_TIMEOUT_S", 60)
    token = CancelToken()
    result = {}

    def _run():
        result["summary"] = service.run_symbol_analysis(
            ctx, ServerState(), "XAUUSD", "15m", cancel_token=token
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(0.3)
    token.set()
    t.join(5)
    assert not t.is_alive()
    assert result["summary"]["ok"] is False and "停止" in result["summary"]["error"]


def test_record_exception_marks_failure(monkeypatch):
    """record.exception 非空（如阶段一失败提前返回）必须记为失败而非成功."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from pa_agent.server import service
    from pa_agent.server.state import ServerState

    ctx = _fake_ctx(_mk_bars(60))
    record = SimpleNamespace(
        stage2_decision=None,
        exception={"type": "network_error", "message": "连接超时"},
    )
    fake_orch = SimpleNamespace(submit=lambda *a, **k: record)
    with patch("pa_agent.server.service.build_orchestrator", return_value=fake_orch):
        summary = service.run_symbol_analysis(ctx, ServerState(), "XAUUSD", "15m")
    assert summary["ok"] is False and "连接超时" in summary["error"]


def test_orchestrator_exception_captured(monkeypatch):
    from pa_agent.server import service
    from pa_agent.server.state import ServerState

    ctx = _fake_ctx(_mk_bars(60))

    def boom(*a, **k):
        raise RuntimeError("LLM 网络错误")

    fake_orch = SimpleNamespace(submit=boom)
    with patch("pa_agent.server.service.build_orchestrator", return_value=fake_orch):
        summary = service.run_symbol_analysis(ctx, ServerState(), "XAUUSD", "15m")
    assert summary["ok"] is False and "LLM 网络错误" in summary["error"]
