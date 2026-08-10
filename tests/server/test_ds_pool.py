"""数据源实例池测试（fake 数据源工厂）."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _settings():
    return SimpleNamespace(
        general=SimpleNamespace(
            last_data_source="tradingview", last_tradingview_exchange=""
        )
    )


class _FakeDS:
    n = 0

    def __init__(self):
        _FakeDS.n += 1
        self.id = _FakeDS.n
        self.closed = False

    def disconnect(self):
        self.closed = True


def test_acquire_distinct_and_reuse():
    from pa_agent.server.ds_pool import DataSourcePool

    with patch(
        "pa_agent.server.ds_pool._create_data_source_from_settings",
        side_effect=lambda s: _FakeDS(),
    ):
        pool = DataSourcePool(_settings(), size=2)
        a, b = pool.acquire(), pool.acquire()
        assert a is not b
        pool.release(a)
        assert pool.acquire() is a  # 复用而非新建
        with pytest.raises(TimeoutError):
            pool.acquire(timeout=0.1)  # 池空且已达上限


def test_rebuild_closes_and_recreates():
    from pa_agent.server.ds_pool import DataSourcePool

    with patch(
        "pa_agent.server.ds_pool._create_data_source_from_settings",
        side_effect=lambda s: _FakeDS(),
    ):
        pool = DataSourcePool(_settings(), size=1)
        a = pool.acquire()
        pool.release(a)
        pool.rebuild(_settings())
        assert a.closed
        b = pool.acquire()
        assert b is not a and not b.closed


def test_stale_release_after_rebuild_discarded():
    """rebuild 前借出的实例归还时必须被丢弃，不得污染新池."""
    from pa_agent.server.ds_pool import DataSourcePool

    with patch(
        "pa_agent.server.ds_pool._create_data_source_from_settings",
        side_effect=lambda s: _FakeDS(),
    ):
        pool = DataSourcePool(_settings(), size=1)
        old = pool.acquire()  # 借出中
        pool.rebuild(_settings())
        pool.release(old)  # 跨代际归还
        assert old.closed  # 被断开丢弃
        fresh = pool.acquire()  # 新代际实例，而非旧实例
        assert fresh is not old and not fresh.closed


def test_close_all_swallow_errors():
    from pa_agent.server.ds_pool import DataSourcePool

    class _BadDS(_FakeDS):
        def disconnect(self):
            raise RuntimeError("断开失败")

    with patch(
        "pa_agent.server.ds_pool._create_data_source_from_settings",
        side_effect=lambda s: _BadDS(),
    ):
        pool = DataSourcePool(_settings(), size=1)
        a = pool.acquire()
        pool.release(a)
        pool.close_all()  # 不抛异常
