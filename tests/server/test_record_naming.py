"""记录文件名时间格式回归：分钟位必须是分钟而非月份."""
from datetime import datetime


def test_basename_minute_not_month():
    from types import SimpleNamespace

    from pa_agent.records.pending_writer import _build_basename

    # 2026-01-05 14:37:09 本地时间 —— 分钟 37 与月份 01 不同，能暴露 %m/%M 笔误
    ts_ms = int(datetime(2026, 1, 5, 14, 37, 9).timestamp() * 1000)
    record = SimpleNamespace(
        meta=SimpleNamespace(timestamp_local_ms=ts_ms, symbol="XAUUSD", timeframe="15m")
    )
    name = _build_basename(record)
    assert "14-37-09" in name, name
    assert name.endswith("_XAUUSD_15m")
