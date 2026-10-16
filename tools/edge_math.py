#!/usr/bin/env python3
"""edge 数学核算：盈亏平衡胜率 / 成本拖累 / 杠杆爆仓概率。

所有数字来自币安真实 K 线（本脚本可复现，不依赖任何假设性输入）。
用法：python tools/edge_math.py [--symbol BTCUSDT]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pa_agent.data.base import KlineBar
from pa_agent.data.factory import create_data_source

#: 双边总成本（taker 手续费 + 滑点），%
ROUND_TRIP_COST_PCT = 2 * (0.10 + 0.05)


def fetch(kind: str, symbol: str, timeframe: str, count: int) -> list[KlineBar]:
    src = create_data_source(kind)
    src.connect()
    try:
        src.subscribe(symbol, timeframe)
        bars = list(src.latest_snapshot(count))
        src.unsubscribe()
    finally:
        src.disconnect()
    return sorted(bars, key=lambda b: b.ts_open)  # 时间正序


def abs_move_stats(bars: list[KlineBar], horizon: int) -> tuple[float, float, float]:
    """返回 (平均绝对涨跌幅, 中位, 90 分位) %，horizon 以 bar 数计。"""
    moves = [
        abs(bars[i + horizon].close - bars[i].close) / bars[i].close * 100
        for i in range(len(bars) - horizon)
    ]
    moves.sort()
    return (
        statistics.mean(moves),
        statistics.median(moves),
        moves[int(len(moves) * 0.9)],
    )


def breakeven_wr(risk_pct: float, reward_pct: float, cost_pct: float = ROUND_TRIP_COST_PCT) -> float:
    """含成本的盈亏平衡胜率：p*(T-c) = (1-p)*(R+c)。"""
    return (risk_pct + cost_pct) / (risk_pct + reward_pct) * 100


def ruin_stats(bars: list[KlineBar], window: int, hold_windows: int, lev: float, mmr: float = 0.004) -> tuple[int, int, float, float]:
    """滚动窗口的最不利偏移(MAE)分布 -> 单窗口爆仓率 -> 持有期内生存率。

    多头杠杆 L 的强平阈值 ≈ 1/L - 维持保证金率。
    返回 (窗口数, 爆仓窗口数, 单窗口爆仓率, 持有期生存率)。
    """
    thr = 1 / lev - mmr
    maes: list[float] = []
    for i in range(0, len(bars) - window, max(1, window // 4)):
        seg = bars[i : i + window]
        maes.append(min(b.low for b in seg) / seg[0].open - 1)
    ruined = sum(1 for m in maes if m <= -thr)
    p_win = ruined / len(maes) if maes else 0.0
    return len(maes), ruined, p_win, (1 - p_win) ** hold_windows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--source", default="binance")
    args = ap.parse_args()

    print(f"=== {args.symbol} 真实数据 edge 核算（源={args.source}）===")
    t0 = time.time()
    h1 = fetch(args.source, args.symbol, "1h", 8760)   # 约 1 年
    m15 = fetch(args.source, args.symbol, "15m", 4000)  # 约 41 天
    print(f"取到 1h {len(h1)} 根（{time.strftime('%Y-%m-%d', time.localtime(h1[0].ts_open/1000))} ~ "
          f"{time.strftime('%Y-%m-%d', time.localtime(h1[-1].ts_open/1000))}）、15m {len(m15)} 根，耗时 {time.time()-t0:.1f}s")
    print(f"双边总成本假设: {ROUND_TRIP_COST_PCT:.2f}% （taker 0.10% + 滑点 0.05%，单边）")

    print("\n--- 1) 各持仓期平均绝对波动（真实）---")
    for tf, bars, horizons in (("15m", m15, {"4小时": 16, "24小时": 96}), ("1h", h1, {"4小时": 4, "24小时": 24, "7天": 168})):
        for label, h in horizons.items():
            if len(bars) > h:
                mean_, med, p90 = abs_move_stats(bars, h)
                print(f"  {tf:>4s} 持仓 {label:>5s}: 平均 {mean_:.3f}%  中位 {med:.3f}%  P90 {p90:.3f}%")

    print("\n--- 2) 只做方向（不设 TP/SL）的盈亏平衡胜率：0.5 + 成本/(2×平均波动) ---")
    for tf, bars, horizons in (("15m", m15, {"24小时": 96}), ("1h", h1, {"24小时": 24, "7天": 168})):
        for label, h in horizons.items():
            if len(bars) > h:
                mean_, _, _ = abs_move_stats(bars, h)
                p = 0.5 + ROUND_TRIP_COST_PCT / (2 * mean_) * 100 * 0.01
                print(f"  {tf:>4s} / {label:>5s}: 需要胜率 ≥ {p*100:.2f}%")

    print("\n--- 3) 有 TP/SL 结构时的盈亏平衡胜率 p = (风险%+成本)/(风险%+止盈%) ---")
    for risk, reward in ((1.0, 1.0), (1.0, 2.0), (0.5, 1.0), (1.5, 3.0), (2.0, 2.0)):
        rr = reward / risk
        print(f"  风险 {risk:.1f}% / 止盈 {reward:.1f}%（R:R={rr:.1f}）: 不计成本 {1/(1+rr)*100:.1f}% → "
              f"含 {ROUND_TRIP_COST_PCT:.2f}% 成本 需 {breakeven_wr(risk, reward):.1f}%")

    print("\n--- 4) 成本拖累 vs 交易频率（每笔双边成本 %.2f%%）---" % ROUND_TRIP_COST_PCT)
    for per_day in (1, 2, 5, 10):
        daily = per_day * ROUND_TRIP_COST_PCT
        print(f"  {per_day:>2d} 笔/天: {daily:.2f}%/天   月 {daily*21:.1f}%   年 {daily*252:.1f}%")

    print("\n--- 5) 杠杆爆仓概率（真实滚动最不利偏移 MAE，维持保证金率 0.4%）---")
    print("  持有期内生存率：")
    print(f"  {'杠杆':>6s} {'强平阈值':>9s} {'单24h窗口爆仓率':>16s} {'持有30天生存率':>14s} {'年化收益需达':>12s}")
    for lev in (1, 2, 3, 5, 10, 20):
        n, ruined, p_win, surv_30d = ruin_stats(h1, 24, 30, lev)
        # 若要在杠杆下月赚 5%，标的需要动的幅度
        need = 5.0 / lev
        thr = 1 / lev - 0.004
        print(f"  {lev:>5d}x {thr*100:>8.1f}% {p_win*100:>15.2f}% {surv_30d*100:>13.2f}% {need:>11.2f}%")

    print(f"\n注：MAE 用窗口内最低价/首根开盘价估算（多头），未考虑资金费率与插针；"
          f"实际强平常因交易所保证金率分档而更早发生。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
