"""多品种轮巡调度器（服务端版）.

对应 GUI 侧 `gui/watch_rotation.py` 的状态机，但不驱动窗口控件，
直接调用 :func:`pa_agent.server.service.run_symbol_analysis`：

    逐品种：切换 → 等数据 → 两阶段分析 → （命中时）推送 → 下一个
    一轮完成后等待 ``watch_round_interval_min`` 分钟再开始下一轮。

任一品种失败只记事件日志并跳过，不会中断轮巡。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from pa_agent.server.service import run_symbol_analysis  # noqa: F401  (供测试 patch)
from pa_agent.server.state import ServerState
from pa_agent.util.threading import CancelToken


def parse_watch_symbols(raw: str) -> list[str]:
    """把逗号/中文逗号/顿号/空格分隔的品种串解析成去重列表（保序）."""
    normalized = raw.replace("，", ",").replace(" ", ",").replace("、", ",")
    seen: set[str] = set()
    result: list[str] = []
    for part in normalized.split(","):
        sym = part.strip()
        if sym and sym.upper() not in seen:
            seen.add(sym.upper())
            result.append(sym)
    return result


class WatchScheduler:
    """单后台线程轮巡状态机；start/stop 线程安全."""

    def __init__(self, ctx: Any, state: ServerState) -> None:
        self._ctx = ctx
        self._state = state
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel_token: CancelToken | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> str | None:
        """启动轮巡；成功返回 None，失败返回中文原因."""
        with self._lock:
            if self.running:
                return "轮巡已在运行中"
            settings = self._ctx.settings
            symbols = parse_watch_symbols(
                getattr(settings.general, "watch_symbols", "") or ""
            )
            if not symbols:
                return "监控列表为空，请先在「配置」中填写监控品种"
            if not (getattr(settings.provider, "api_key", "") or "").strip():
                return "未配置 API Key，请先在「配置」中填写 AI 模型设置"

            timeframe = getattr(settings.general, "last_timeframe", "15m") or "15m"
            interval_s = (
                max(0, int(getattr(settings.general, "watch_round_interval_min", 10)))
                * 60.0
            )
            self._stop_evt.clear()
            self._thread = threading.Thread(
                target=self._loop,
                args=(symbols, timeframe, interval_s),
                name="watch-scheduler",
                daemon=True,
            )
            self._state.set_scheduler(True)
            self._state.add_event(
                f"轮巡启动：{'、'.join(symbols)}（周期 {timeframe}，"
                f"轮间隔 {interval_s / 60:.0f} 分钟）"
            )
            self._thread.start()
            return None

    def stop(self, timeout: float = 35.0) -> None:
        """请求停止并等待线程退出（打断数据等待/轮间隔等待/当前分析）."""
        self._stop_evt.set()
        with self._lock:
            token = self._cancel_token
        if token is not None:
            token.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    # ── 主循环 ───────────────────────────────────────────────────────────────

    def _loop(self, symbols: list[str], timeframe: str, interval_s: float) -> None:
        round_num = 1
        try:
            while not self._stop_evt.is_set():
                for idx, sym in enumerate(symbols):
                    # 换 token 与 stop() 读 token 用同一把锁：要么 stop 看到新
                    # token 能取消本次分析，要么本迭代看到 stop 位不再启动
                    with self._lock:
                        if self._stop_evt.is_set():
                            break
                        self._cancel_token = token = CancelToken()
                    self._state.set_current(
                        sym, "switching", round_num, idx, len(symbols)
                    )
                    try:
                        run_symbol_analysis(
                            self._ctx,
                            self._state,
                            sym,
                            timeframe,
                            cancel_token=token,
                            round_num=round_num,
                            idx=idx,
                            total=len(symbols),
                        )
                    except Exception as exc:  # noqa: BLE001 — 兜底：service 已消化常规错误
                        self._state.add_event(f"{sym} 分析异常跳过：{exc}")
                if self._stop_evt.is_set():
                    break
                if interval_s > 0:
                    self._state.clear_current()
                    self._state.set_round_wait(time.time() + interval_s)
                    self._state.add_event(
                        f"第 {round_num} 轮完成，等待 {interval_s / 60:.0f} 分钟"
                    )
                    if self._stop_evt.wait(interval_s):
                        break
                round_num += 1
        except Exception as exc:  # noqa: BLE001 — 线程崩溃兜底
            self._ctx.logger.error("轮巡线程异常退出: %s", exc, exc_info=True)
            self._state.set_scheduler(False, error=str(exc))
            self._state.clear_current()
            self._state.add_event(f"轮巡异常终止：{exc}")
            return
        self._state.set_scheduler(False)
        self._state.clear_current()
        self._state.add_event("轮巡已停止")
