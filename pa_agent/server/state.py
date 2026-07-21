"""服务端运行状态容器（线程安全，供调度器写入、API 读取）."""
from __future__ import annotations

import copy
import threading
import time
from collections import deque
from typing import Any

#: 事件日志环形缓冲容量
EVENT_CAPACITY = 200
#: 每品种每条实时推理流的最大字符数（超出丢头部保尾部）
LIVE_CAP = 16384
#: results / live 字典的品种条目上限（超出淘汰最早写入且不在分析中的）
SYMBOL_CAPACITY = 50

_LIVE_STREAM_KEYS = (
    "stage1_reasoning",
    "stage1_content",
    "stage2_reasoning",
    "stage2_content",
)


class ServerState:
    """轮巡调度器与分析编排的共享状态快照.

    所有写方法与 :meth:`snapshot` 均加锁；snapshot 返回深拷贝，
    调用方可任意修改而不影响内部状态。``current`` 为字典
    ``{symbol: {phase, round, started_ts}}``，支持并发多品种。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._error: str | None = None
        self._current: dict[str, dict[str, Any]] = {}
        self._round_wait_eta: float | None = None
        self._market_closed_until: float | None = None
        self._results: dict[str, dict[str, Any]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_CAPACITY)
        self._live: dict[str, dict[str, Any]] = {}

    # ── 调度器状态 ────────────────────────────────────────────────────────────

    def set_scheduler(self, running: bool, error: str | None = None) -> None:
        with self._lock:
            self._running = running
            self._error = error

    def set_round_wait(self, eta_epoch: float | None) -> None:
        with self._lock:
            self._round_wait_eta = eta_epoch

    def set_market_closed(self, until_epoch: float | None) -> None:
        with self._lock:
            self._market_closed_until = until_epoch

    # ── 进行中品种（并发字典）────────────────────────────────────────────────

    def set_symbol_phase(self, symbol: str, phase: str, round_num: int) -> None:
        with self._lock:
            entry = self._current.get(symbol)
            if entry is None:
                entry = {"phase": phase, "round": round_num, "started_ts": time.time()}
                self._current[symbol] = entry
            else:
                entry["phase"] = phase
                entry["round"] = round_num

    def clear_symbol(self, symbol: str) -> None:
        with self._lock:
            self._current.pop(symbol, None)

    def clear_all_current(self) -> None:
        with self._lock:
            self._current.clear()

    # ── 品种结果与事件 ────────────────────────────────────────────────────────

    def set_symbol_result(self, symbol: str, summary: dict[str, Any]) -> None:
        with self._lock:
            self._results[symbol] = dict(summary)
            self._evict_locked(self._results)

    def _evict_locked(self, store: dict[str, Any]) -> None:
        """容量淘汰：dict 保序，删最早写入且不在分析中的键（持锁调用）."""
        while len(store) > SYMBOL_CAPACITY:
            victim = next(
                (k for k in store if k not in self._current), next(iter(store))
            )
            store.pop(victim, None)

    def add_event(self, text: str) -> None:
        with self._lock:
            self._events.append({"ts": time.time(), "text": text})

    # ── 实时推理缓冲 ─────────────────────────────────────────────────────────

    def reset_live(self, symbol: str) -> None:
        with self._lock:
            self._live[symbol] = {
                "stage": "stage1",
                "seq": 0,
                **{k: "" for k in _LIVE_STREAM_KEYS},
            }
            self._evict_locked(self._live)

    def append_live(self, symbol: str, stage: str, kind: str, chunk: str) -> None:
        key = f"{stage}_{kind}"
        if key not in _LIVE_STREAM_KEYS or not chunk:
            return
        with self._lock:
            buf = self._live.get(symbol)
            if buf is None:
                buf = {"stage": stage, "seq": 0, **{k: "" for k in _LIVE_STREAM_KEYS}}
                self._live[symbol] = buf
            text = buf[key] + chunk
            if len(text) > LIVE_CAP:
                text = text[-LIVE_CAP:]
            buf[key] = text
            buf["stage"] = stage
            buf["seq"] += 1

    def get_live(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            buf = self._live.get(symbol)
            if buf is None:
                return None
            out = dict(buf)
            out["running"] = symbol in self._current
            return out

    # ── 读接口 ────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(
                {
                    "scheduler": {"running": self._running, "error": self._error},
                    "current": self._current,
                    "round_wait_eta": self._round_wait_eta,
                    "market_closed_until": self._market_closed_until,
                    "results": self._results,
                    "events": list(self._events),
                }
            )
