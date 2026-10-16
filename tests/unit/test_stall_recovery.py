"""行情卡死自愈：数据源已连接但长时间没有新帧时，自动重启 RefreshLoop。

实测背景（2026-10-15 22:09）：切到 OKX 失败再切回币安后，刷新循环被停掉却没人再启动，
之后 7 分钟零次行情请求，界面继续显示旧价格（差 0.4%）且状态格仍显示「已就绪」。
"""

from __future__ import annotations

import time

import pytest

from pa_agent.gui.main_window import MainWindow


class _FakeSource:
    """最小数据源替身：只提供自愈逻辑读取的字段。"""

    _connected = True
    _symbol = "BTCUSDT"
    _timeframe = "15m"

    def list_symbols(self) -> list[str]:
        return ["BTCUSDT"]

    def supported_timeframes(self) -> list[str]:
        return ["15m"]


@pytest.fixture
def window(qtbot, tmp_path):
    from tests.e2e.test_smoke_happy_path import _make_ctx

    ctx, _pending = _make_ctx(tmp_path)
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win._ctx.data_source = _FakeSource()
    return win


def _record_restarts(window: MainWindow) -> list[str]:
    calls: list[str] = []
    window._stop_refresh_loop = lambda: calls.append("stop")  # type: ignore[method-assign]
    window._start_refresh_loop = lambda: calls.append("start")  # type: ignore[method-assign]
    return calls


@pytest.mark.unit
def test_no_recovery_for_short_stall(window: MainWindow) -> None:
    calls = _record_restarts(window)
    window._ctx.data_source._connected = True
    window._maybe_recover_stalled_feed(5.0)
    assert calls == []


@pytest.mark.unit
def test_recovery_restarts_loop_after_long_stall(window: MainWindow) -> None:
    calls = _record_restarts(window)
    window._ctx.data_source._connected = True
    window._maybe_recover_stalled_feed(60.0)
    assert calls == ["stop", "start"]
    assert "自动重启数据刷新" in window._status_bar.currentMessage()
    # 自愈会解除冻结，避免"恢复"后图表仍不动
    assert window._chart_refresh_paused is False


@pytest.mark.unit
def test_recovery_respects_cooldown(window: MainWindow) -> None:
    calls = _record_restarts(window)
    window._ctx.data_source._connected = True
    window._maybe_recover_stalled_feed(60.0)
    window._maybe_recover_stalled_feed(90.0)  # 距上次自愈不足冷却时间
    assert calls == ["stop", "start"]


@pytest.mark.unit
def test_recovery_skipped_when_source_disconnected(window: MainWindow) -> None:
    calls = _record_restarts(window)
    window._ctx.data_source._connected = False
    window._maybe_recover_stalled_feed(120.0)
    assert calls == []


@pytest.mark.unit
def test_recovery_skipped_during_analysis_or_switch(window: MainWindow) -> None:
    calls = _record_restarts(window)
    window._ctx.data_source._connected = True
    window._analysis_in_progress = True
    window._maybe_recover_stalled_feed(120.0)
    window._analysis_in_progress = False
    window._switching = True
    window._maybe_recover_stalled_feed(120.0)
    assert calls == []


@pytest.mark.unit
def test_elapsed_label_turns_red_and_calls_recovery(window: MainWindow) -> None:
    calls = _record_restarts(window)
    window._ctx.data_source._connected = True
    window._last_refresh_ts = time.monotonic() - 60.0
    window._update_refresh_elapsed()
    assert "1m00s" in window._refresh_elapsed_label.text()
    assert "#f85149" in window._refresh_elapsed_label.styleSheet()
    assert calls == ["stop", "start"]
