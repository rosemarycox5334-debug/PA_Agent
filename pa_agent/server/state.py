"""服务端运行状态容器（线程安全，供调度器写入、API 读取）."""
from __future__ import annotations

import copy
import threading
import time
from collections import deque
from typing import Any

#: 事件日志环形缓冲容量
EVENT_CAPACITY = 200


class ServerState:
    """轮巡调度器与分析编排的共享状态快照.

    所有写方法与 :meth:`snapshot` 均加锁；snapshot 返回深拷贝，
    调用方可任意修改而不影响内部状态。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._error: str | None = None
        self._current: dict[str, Any] | None = None
        self._round_wait_eta: float | None = None
        self._results: dict[str, dict[str, Any]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_CAPACITY)

    # ── 写接口 ────────────────────────────────────────────────────────────────

    def set_scheduler(self, running: bool, error: str | None = None) -> None:
        with self._lock:
            self._running = running
            self._error = error

    def set_current(
        self, symbol: str, phase: str, round_num: int, idx: int, total: int
    ) -> None:
        with self._lock:
            self._current = {
                "symbol": symbol,
                "phase": phase,
                "round": round_num,
                "idx": idx,
                "total": total,
            }
            self._round_wait_eta = None

    def clear_current(self) -> None:
        with self._lock:
            self._current = None

    def set_round_wait(self, eta_epoch: float) -> None:
        with self._lock:
            self._round_wait_eta = eta_epoch

    def set_symbol_result(self, symbol: str, summary: dict[str, Any]) -> None:
        with self._lock:
            self._results[symbol] = dict(summary)

    def add_event(self, text: str) -> None:
        with self._lock:
            self._events.append({"ts": time.time(), "text": text})

    # ── 读接口 ────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(
                {
                    "scheduler": {"running": self._running, "error": self._error},
                    "current": self._current,
                    "round_wait_eta": self._round_wait_eta,
                    "results": self._results,
                    "events": list(self._events),
                }
            )
