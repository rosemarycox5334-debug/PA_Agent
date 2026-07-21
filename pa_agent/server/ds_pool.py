"""数据源实例池：并发分析的实例隔离.

TradingView/东财等数据源均为单订阅设计（subscribe 覆盖当前品种），
共享实例并发拉数会数据串号。池为每个并发槽位提供独立实例，
借还语义复用连接，配置变更时整池重建。
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from pa_agent.server.bootstrap import _create_data_source_from_settings

logger = logging.getLogger(__name__)


class DataSourcePool:
    """惰性创建、借还复用的固定容量数据源实例池（线程安全）."""

    def __init__(self, settings: Any, size: int) -> None:
        self._settings = settings
        self._size = max(1, int(size))
        self._idle: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._created = 0

    def acquire(self, timeout: float = 30.0) -> Any:
        """借出一个数据源实例；池空且未达上限则新建，超时抛 TimeoutError."""
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self._size:
                ds = _create_data_source_from_settings(self._settings)
                self._created += 1
                return ds
        try:
            return self._idle.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"数据源池繁忙（{self._size} 个实例全部占用超过 {timeout}s）"
            ) from None

    def release(self, ds: Any) -> None:
        """归还实例（不做健康检查；下次借出使用时自愈重连）."""
        if ds is not None:
            self._idle.put(ds)

    def close_all(self) -> None:
        """断开并丢弃全部空闲实例（借出未还的由持有者用完即弃）."""
        with self._lock:
            while True:
                try:
                    ds = self._idle.get_nowait()
                except queue.Empty:
                    break
                self._created -= 1
                try:
                    ds.disconnect()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("数据源断开失败（忽略）: %s", exc)

    def rebuild(self, settings: Any) -> None:
        """配置变更后整池重建：关掉空闲实例，后续按新配置惰性新建."""
        self.close_all()
        with self._lock:
            self._settings = settings
