"""多品种轮巡调度器（服务端并发版）.

每轮把全部品种投入 ``ThreadPoolExecutor``（并发数 =
``general.watch_concurrency``），每个任务从数据源池借独立实例执行
:func:`pa_agent.server.service.run_symbol_analysis`，轮内全部完成后统一等待
``watch_round_interval_min`` 分钟再开始下一轮。

任一品种失败只记事件日志并跳过；stop() 会取消全部进行中分析并跳过未开始的。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
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
    """并发轮巡状态机；start/stop 线程安全."""

    def __init__(self, ctx: Any, state: ServerState, ds_pool: Any) -> None:
        self._ctx = ctx
        self._state = state
        self._ds_pool = ds_pool
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._tokens: dict[str, CancelToken] = {}

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
            concurrency = max(
                1,
                min(
                    int(getattr(settings.general, "watch_concurrency", 2) or 2),
                    len(symbols),
                    8,
                ),
            )
            self._stop_evt.clear()
            self._tokens.clear()
            self._thread = threading.Thread(
                target=self._loop,
                args=(symbols, timeframe, interval_s, concurrency),
                name="watch-scheduler",
                daemon=True,
            )
            self._state.set_scheduler(True)
            self._state.add_event(
                f"轮巡启动：{'、'.join(symbols)}（周期 {timeframe}，并发 {concurrency}，"
                f"轮间隔 {interval_s / 60:.0f} 分钟）"
            )
            self._thread.start()
            return None

    def stop(self, timeout: float = 35.0) -> None:
        """请求停止：取消全部进行中分析、跳过未开始的、打断轮间隔等待."""
        self._stop_evt.set()
        with self._lock:
            tokens = list(self._tokens.values())
        for token in tokens:
            token.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    # ── 主循环 ───────────────────────────────────────────────────────────────

    def _run_one(self, symbol: str, timeframe: str, round_num: int, total: int) -> None:
        """线程池任务：借数据源 → 分析 → 归还（异常已在 service 内消化）."""
        # 换 token 与 stop() 读 token 用同一把锁：要么 stop 看到 token 能取消，
        # 要么本任务看到 stop 位不再启动
        with self._lock:
            if self._stop_evt.is_set():
                return
            token = CancelToken()
            self._tokens[symbol] = token
        ds = None
        try:
            ds = self._ds_pool.acquire(timeout=30)
            run_symbol_analysis(
                self._ctx,
                self._state,
                symbol,
                timeframe,
                cancel_token=token,
                data_source=ds,
                round_num=round_num,
                total=total,
            )
        except Exception as exc:  # noqa: BLE001 — 兜底（如池借用超时）
            self._state.add_event(f"{symbol} 分析异常跳过：{exc}")
            self._state.clear_symbol(symbol)
        finally:
            if ds is not None:
                self._ds_pool.release(ds)
            with self._lock:
                self._tokens.pop(symbol, None)

    def _loop(
        self, symbols: list[str], timeframe: str, interval_s: float, concurrency: int
    ) -> None:
        round_num = 1
        try:
            with ThreadPoolExecutor(
                max_workers=concurrency, thread_name_prefix="watch-worker"
            ) as pool:
                while not self._stop_evt.is_set():
                    self._state.set_round_wait(None)
                    futures = [
                        pool.submit(
                            self._run_one, sym, timeframe, round_num, len(symbols)
                        )
                        for sym in symbols
                    ]
                    wait(futures)  # 轮 barrier：全部完成才进入下一步
                    if self._stop_evt.is_set():
                        break
                    if interval_s > 0:
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
            self._state.clear_all_current()
            self._state.add_event(f"轮巡异常终止：{exc}")
            return
        self._state.set_scheduler(False)
        self._state.clear_all_current()
        self._state.set_round_wait(None)
        self._state.add_event("轮巡已停止")
