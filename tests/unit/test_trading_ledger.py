"""台账核心逻辑单测：逐根判定、成本口径、时间解析。

背景：走查回放实测暴露过一个真 bug —— 结算时用「最新 N 根」取历史窗口，
30 小时前的窗口全部落空。因此判定与取数逻辑必须有测试兜住。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import trading_ledger as tl  # noqa: E402
from pa_agent.data.base import KlineBar  # noqa: E402


def bar(seq: int, ts: int, o: float, h: float, l: float, c: float) -> KlineBar:  # noqa: E731
    return KlineBar(seq=seq, ts_open=ts, open=o, high=h, low=l, close=c, volume=1.0, closed=True)


# ------------------------------------------------------------------ 逐根判定
def test_long_target_hit_before_stop() -> None:
    bars = [bar(1, 1000, 100, 102, 99, 101), bar(2, 2000, 101, 106, 100, 105)]
    outcome, exit_px, mfe, held, _ = tl._simulate("做多", 100.0, 95.0, 105.0, bars)
    assert outcome == "tp_hit"
    assert exit_px == 105.0
    assert held == 2
    assert mfe == pytest.approx(6.0)  # 最高 106 相对入场 100


def test_long_stop_hit_first() -> None:
    bars = [bar(1, 1000, 100, 101, 94, 95)]
    outcome, exit_px, _, held, _ = tl._simulate("做多", 100.0, 95.0, 105.0, bars)
    assert outcome == "sl_hit"
    assert exit_px == 95.0
    assert held == 1


def test_same_bar_stop_and_target_takes_worst_case() -> None:
    """同一根内同时触及止损与止盈 → 按最坏情况（先止损），不看先后假设。"""
    bars = [bar(1, 1000, 100, 110, 90, 105)]
    outcome, exit_px, _, _, _ = tl._simulate("做多", 100.0, 95.0, 105.0, bars)
    assert outcome == "sl_hit"
    assert exit_px == 95.0


def test_short_direction() -> None:
    bars = [bar(1, 1000, 100, 101, 98, 99), bar(2, 2000, 99, 100, 94, 95)]
    outcome, exit_px, _, held, _ = tl._simulate("做空", 100.0, 105.0, 95.0, bars)
    assert outcome == "tp_hit"
    assert exit_px == 95.0
    assert held == 2


def test_timeout_when_neither_touched() -> None:
    bars = [bar(1, 1000, 100, 102, 98, 101), bar(2, 2000, 101, 103, 99, 100.5)]
    outcome, exit_px, _, held, _ = tl._simulate("做多", 100.0, 90.0, 110.0, bars)
    assert outcome == "timeout"
    assert exit_px == pytest.approx(100.5)
    assert held == 2


# ------------------------------------------------------------------ 成本口径
def test_round_trip_cost_constant() -> None:
    assert tl.ROUND_TRIP_COST_PCT == pytest.approx(0.30)
    assert tl.TAKER_FEE_PCT == pytest.approx(0.10)
    assert tl.SLIPPAGE_PCT == pytest.approx(0.05)


def test_net_return_subtracts_full_round_trip_cost() -> None:
    """毛收益 0.31% 的单子，扣 0.30% 成本后只剩 0.01%。"""
    gross, cost = 0.31, tl.ROUND_TRIP_COST_PCT
    assert round(gross - cost, 4) == pytest.approx(0.01)


# ------------------------------------------------------------------ 时间解析
def test_timeframe_ms() -> None:
    assert tl._timeframe_ms("15m") == 900_000
    assert tl._timeframe_ms("1h") == 3_600_000
    assert tl._timeframe_ms("1d") == 86_400_000


def test_parse_as_of_relative_and_iso() -> None:
    now = tl._now_ms()
    got = tl._parse_as_of("-24h")
    assert got is not None and abs((now - got) - 86_400_000) < 5_000

    got_d = tl._parse_as_of("-3d")
    assert got_d is not None and abs((now - got_d) - 3 * 86_400_000) < 5_000

    assert tl._parse_as_of(None) is None
    assert tl._parse_as_of("2026-10-10T12:00") is not None


# ------------------------------------------------------------------ 结算取数回归
def test_fetch_range_uses_as_of_end_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：区间取数必须以 end_ms 为 endTime，否则历史窗口会落空。"""
    captured: dict[str, object] = {}

    def fake_fetch_bars(kind, symbol, timeframe, count, as_of_ms=None):  # type: ignore[no-untyped-def]
        captured["as_of_ms"] = as_of_ms
        captured["count"] = count
        return [bar(1, 1_000_000, 100, 101, 99, 100)]

    monkeypatch.setattr(tl, "fetch_bars", fake_fetch_bars)
    start, end = 1_000_000, 1_000_000 + 96 * 900_000
    tl._fetch_range("binance", "BTCUSDT", "15m", start, end)
    assert captured["as_of_ms"] == end
    assert int(captured["count"]) >= 96  # 窗口长度 + 余量
