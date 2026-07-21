"""ServerState 线程安全状态容器测试."""
import threading
import time


def test_snapshot_roundtrip():
    from pa_agent.server.state import ServerState

    st = ServerState()
    st.set_scheduler(True)
    st.set_current("XAUUSD", "stage1", round_num=1, idx=0, total=2)
    st.set_symbol_result(
        "XAUUSD",
        {
            "ts": time.time(),
            "ok": True,
            "direction": "做多",
            "order_type": "限价单",
            "confidence": 70,
            "has_order": True,
            "error": None,
        },
    )
    st.add_event("测试事件")
    snap = st.snapshot()
    assert snap["scheduler"]["running"] is True
    assert snap["current"]["symbol"] == "XAUUSD"
    assert snap["current"]["phase"] == "stage1"
    assert snap["results"]["XAUUSD"]["has_order"] is True
    assert snap["events"][-1]["text"] == "测试事件"
    # snapshot 是拷贝：改动不回渗
    snap["results"]["XAUUSD"]["ok"] = False
    assert st.snapshot()["results"]["XAUUSD"]["ok"] is True


def test_round_wait_and_clear_current():
    from pa_agent.server.state import ServerState

    st = ServerState()
    st.set_current("BTCUSD", "stage2", round_num=3, idx=1, total=2)
    eta = time.time() + 600
    st.set_round_wait(eta)
    st.clear_current()
    snap = st.snapshot()
    assert snap["current"] is None
    assert snap["round_wait_eta"] == eta
    st.set_current("BTCUSD", "switching", round_num=4, idx=0, total=2)
    assert st.snapshot()["round_wait_eta"] is None  # 进入新品种后清除倒计时


def test_scheduler_error_state():
    from pa_agent.server.state import ServerState

    st = ServerState()
    st.set_scheduler(False, error="线程崩溃")
    snap = st.snapshot()
    assert snap["scheduler"]["running"] is False
    assert snap["scheduler"]["error"] == "线程崩溃"


def test_event_ring_capacity():
    from pa_agent.server.state import ServerState

    st = ServerState()
    for i in range(300):
        st.add_event(f"e{i}")
    events = st.snapshot()["events"]
    assert len(events) == 200 and events[-1]["text"] == "e299"


def test_thread_safety_smoke():
    from pa_agent.server.state import ServerState

    st = ServerState()

    def worker():
        for i in range(200):
            st.add_event(str(i))
            st.set_symbol_result("S", {"ts": 0, "ok": True})
            st.snapshot()

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(st.snapshot()["events"]) == 200
