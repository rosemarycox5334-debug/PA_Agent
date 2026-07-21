"""ServerState 线程安全状态容器测试."""
import threading
import time


def test_snapshot_roundtrip():
    from pa_agent.server.state import ServerState

    st = ServerState()
    st.set_scheduler(True)
    st.set_symbol_phase("XAUUSD", "stage1", 1)
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
    assert snap["current"]["XAUUSD"]["phase"] == "stage1"
    assert snap["results"]["XAUUSD"]["has_order"] is True
    assert snap["events"][-1]["text"] == "测试事件"
    # snapshot 是拷贝：改动不回渗
    snap["results"]["XAUUSD"]["ok"] = False
    assert st.snapshot()["results"]["XAUUSD"]["ok"] is True


def test_multi_current_and_live():
    from pa_agent.server.state import ServerState

    st = ServerState()
    st.set_symbol_phase("AAA", "stage1", 1)
    st.set_symbol_phase("BBB", "waiting_data", 1)
    snap = st.snapshot()
    assert set(snap["current"]) == {"AAA", "BBB"}
    assert snap["current"]["AAA"]["phase"] == "stage1"
    started = snap["current"]["AAA"]["started_ts"]
    st.set_symbol_phase("AAA", "stage2", 1)
    assert st.snapshot()["current"]["AAA"]["started_ts"] == started  # 不重置
    st.clear_symbol("AAA")
    assert "AAA" not in st.snapshot()["current"]
    st.clear_all_current()
    assert st.snapshot()["current"] == {}


def test_live_buffer_cap_and_seq():
    from pa_agent.server import state as state_mod
    from pa_agent.server.state import ServerState

    st = ServerState()
    st.reset_live("AAA")
    st.append_live("AAA", "stage1", "reasoning", "abc")
    st.append_live("AAA", "stage2", "content", "x" * 20000)
    live = st.get_live("AAA")
    assert live["stage"] == "stage2"
    assert live["stage1_reasoning"] == "abc"
    assert len(live["stage2_content"]) == state_mod.LIVE_CAP  # 丢头部保尾部
    assert live["stage2_content"].endswith("x")
    assert live["seq"] == 2
    assert live["running"] is False
    assert st.get_live("NOPE") is None


def test_live_running_flag_follows_current():
    from pa_agent.server.state import ServerState

    st = ServerState()
    st.reset_live("AAA")
    st.append_live("AAA", "stage1", "content", "hi")
    st.set_symbol_phase("AAA", "stage1", 1)
    assert st.get_live("AAA")["running"] is True
    st.clear_symbol("AAA")
    assert st.get_live("AAA")["running"] is False


def test_round_wait_none_clears():
    from pa_agent.server.state import ServerState

    st = ServerState()
    eta = time.time() + 600
    st.set_round_wait(eta)
    assert st.snapshot()["round_wait_eta"] == eta
    st.set_round_wait(None)
    assert st.snapshot()["round_wait_eta"] is None


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
