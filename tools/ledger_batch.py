#!/usr/bin/env python3
"""批量回放：把"用这套信号交易能不能赚钱"变成一个可结算的样本集。

做法（与单条 `trading_ledger.py record` 完全同一条代码路径，只是批量 + 自动结算）：

  1. 在**过去**的时间里均匀取 N 个快照点（例如过去 8 天每 12 小时一个）；
  2. 每个快照点用 as-of 回放跑一次**真实两阶段分析**（真实币安历史行情 + 真实 API），
     物理上看不到未来数据（数据接口按 endTime 截断）；
  3. 因为快照点在 24 小时（= 96 根 15m）持仓上限之外，**跑完立刻就能用真实行情结算**；
  4. 汇总：净收益（扣双边成本）、胜率 vs 盈亏平衡胜率、与"永远做多/永远做空"同口径基线对比、
     弃单的机会成本、二项检验与自助法置信区间。

为什么这么做：单点回测/单次分析都回答不了盈亏问题。只有"多个不同市况的快照 + 真实结算"
才能给出带误差范围的结论，而且样本会随运行时间累积，越跑越可信。

用法：
  python tools/ledger_batch.py run --count 16 --step-h 12 --end-hours-ago 48 --tag b20261016
  python tools/ledger_batch.py report --tag b20261016      # 只汇总不跑分析
  python tools/ledger_batch.py report                      # 汇总全部条目
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import trading_ledger as L  # noqa: E402  （复用台账的全部实现）

TRADE_OUTCOMES = ("tp_hit", "sl_hit", "timeout")


# ------------------------------------------------------------------ 统计
def _breakeven_win_rate(row: dict) -> float | None:
    """扣成本后的盈亏平衡胜率（%）：(风险+成本) / (风险+回报)。"""
    entry, sl, tp = row.get("entry_price"), row.get("stop_loss_price"), row.get("take_profit_price")
    if not (entry and sl and tp):
        return None
    risk = abs(entry - sl) / entry
    reward = abs(tp - entry) / entry
    if risk <= 0 or reward <= 0:
        return None
    c = L.ROUND_TRIP_COST_PCT / 100.0
    return (risk + c) / (risk + reward) * 100.0


def _bootstrap_ci(values: list[float], n_iter: int = 10_000, seed: int = 42) -> tuple[float, float]:
    """均值差的自助法 95% 区间（样本量小时比正态近似诚实）。"""
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(n_iter):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return (means[int(0.025 * n_iter)], means[int(0.975 * n_iter)])


def _fmt(x: float, unit: str = "%", nd: int = 3) -> str:
    if x is None or x != x:  # None / NaN
        return "n/a"
    return f"{x:+.{nd}f}{unit}"


# ------------------------------------------------------------------ 汇总
def aggregate(rows: list[dict]) -> str:
    trades = [r for r in rows if r.get("outcome") in TRADE_OUTCOMES]
    abstained = [r for r in rows if r.get("outcome") == "abstained"]
    errored = [r for r in rows if r.get("status") == "resolve_error" or r.get("exception")]
    open_n = sum(1 for r in rows if r.get("status") == "open")

    out: list[str] = []
    out.append("=" * 78)
    out.append(f"样本汇总  （条目 {len(rows)} | 下单 {len(trades)} | 弃单 {len(abstained)}"
               f" | 未到期 {open_n} | 异常 {len(errored)}）")
    out.append("=" * 78)

    if not trades:
        out.append("尚无已结算的下单样本——任何收益结论都不成立。")
    else:
        nets = [r["net_pct"] for r in trades if r.get("net_pct") is not None]
        gross = [r["gross_pct"] for r in trades if r.get("gross_pct") is not None]
        wins = sum(1 for r in trades if r.get("direction_correct"))
        tp_n = sum(1 for r in trades if r["outcome"] == "tp_hit")
        sl_n = sum(1 for r in trades if r["outcome"] == "sl_hit")
        to_n = sum(1 for r in trades if r["outcome"] == "timeout")

        out.append(f"\n结局          : 止盈 {tp_n} / 止损 {sl_n} / 到期 {to_n}")
        out.append(f"方向命中率    : {wins}/{len(trades)} = {wins/len(trades)*100:.1f}%")
        out.append(f"单笔毛收益    : 均值 {_fmt(statistics.mean(gross))}  中位 {_fmt(statistics.median(gross))}")
        out.append(f"单笔净收益    : 均值 {_fmt(statistics.mean(nets))}  中位 {_fmt(statistics.median(nets))}")
        out.append(f"累计净收益    : {sum(nets):+.2f}%（等额下注、不含复利、含双边成本）")
        drag = len(trades) * L.ROUND_TRIP_COST_PCT
        out.append(f"成本拖累      : {drag:.2f}%（{len(trades)} 笔 × {L.ROUND_TRIP_COST_PCT:.2f}%）；"
                   f"成本 ÷ 毛收益绝对值 = "
                   f"{drag / max(0.01, abs(sum(gross))):.2f}×  → 成本{'吃掉' if drag > abs(sum(gross)) else '占'}了"
                   f"{min(999, drag / max(0.01, abs(sum(gross))) * 100):.0f}% 的毛收益")

        lo, hi = _bootstrap_ci(nets)
        if lo != lo or hi != hi:  # NaN：样本不足，自助法给不出区间
            out.append(f"均值 95% 区间 : n={len(nets)} < 2，自助法无法给出区间")
        else:
            out.append(f"均值 95% 区间 : [{_fmt(lo)}, {_fmt(hi)}]（自助法 1 万次重抽样）")
            if lo <= 0 <= hi:
                out.append("                ⚠ 区间跨 0 ⇒ 目前**无法**断言有正期望")
            else:
                out.append(f"                ✅ 区间不跨 0 ⇒ 在 {len(nets)} 个样本下方向性为正期望"
                           f"（样本仍小，需继续累积）")

        bes = [_breakeven_win_rate(r) for r in trades]
        bes = [b for b in bes if b is not None]
        if bes:
            out.append(f"扣成本盈亏平衡 : 平均需 {statistics.mean(bes):.1f}% 胜率（每笔按自身止盈止损距离算）")
            gap = statistics.mean(bes) - wins / len(trades) * 100
            out.append(f"                 实测命中率 {wins/len(trades)*100:.1f}% − 需要 {statistics.mean(bes):.1f}%"
                       f" = 缺口 {gap:+.1f} 个百分点")

        if len(trades) >= 2:
            p50 = L._binom_p_ge(wins, len(trades))
            if bes:
                need = statistics.mean(bes) / 100
                out.append(f"二项检验       : 胜率优于抛硬币 p = {p50:.3f}；"
                           f"胜率优于平衡点({need*100:.1f}%) p = {L._binom_p_ge(wins, len(trades), need):.3f}")

        bl = [r for r in trades if r.get("baseline_long")]
        if bl:
            blm = statistics.mean(r["baseline_long"]["net_pct"] for r in bl)
            bsm = statistics.mean(r["baseline_short"]["net_pct"] for r in bl)
            mm = statistics.mean(nets)
            out.append(f"\n同口径基线     : 永远做多 {_fmt(blm)} / 永远做空 {_fmt(bsm)}  （模型 {_fmt(mm)}）")
            rank = ["模型", "永远做多", "永远做空"][sorted([mm, blm, bsm], reverse=True).index(mm)]
            out.append(f"                 三者中模型排名第 {[mm, blm, bsm].index(max(mm, blm, bsm)) + 1}"
                       f"/3（最高者：{rank}）")

        wr = [r["model_estimated_win_rate"] for r in trades if r.get("model_estimated_win_rate") is not None]
        if wr:
            out.append(f"模型自评胜率   : 平均 {statistics.mean(wr):.1f}% vs 实测 {wins/len(trades)*100:.1f}%"
                       f"（{'高估' if statistics.mean(wr) > wins/len(trades)*100 else '低估'}）")

    if abstained:
        holds = [r["hold_return_pct"] for r in abstained if r.get("hold_return_pct") is not None]
        if holds:
            out.append(f"\n弃单 {len(abstained)} 次：同期持有收益均值 {_fmt(statistics.mean(holds))}"
                       f"（{'负=弃单避免了损失' if statistics.mean(holds) < 0 else '正=弃单错过机会'}）")
            pos = sum(1 for h in holds if h > 0)
            out.append(f"         其中 {pos}/{len(holds)} 次弃单后行情其实上涨（错过机会的比例）")

    if errored:
        out.append(f"\n异常 {len(errored)} 条：")
        for r in errored[:5]:
            why = r.get("resolve_error") or json.dumps(r.get("exception"), ensure_ascii=False)[:90]
            out.append(f"  {r['id']}: {why}")

    suppressed = [r for r in rows if r.get("suppressed_by_guard")]
    if suppressed:
        out.append(f"\n连续性守卫改写 {len(suppressed)} 次（模型给了方案，被程序改为「不下单」）")
        cfs = [
            r["counterfactual"]["net_pct"]
            for r in suppressed
            if isinstance(r.get("counterfactual"), dict) and r["counterfactual"].get("net_pct") is not None
        ]
        if cfs:
            m = statistics.mean(cfs)
            out.append(f"  反事实（若没拦下）: 均值 {_fmt(m)}  中位 {_fmt(statistics.median(cfs))}"
                       f"  最好 {_fmt(max(cfs))}  最差 {_fmt(min(cfs))}"
                       f"  事后盈利 {sum(1 for c in cfs if c > 0)}/{len(cfs)}")
            out.append("  判定: " + ("✅ 守卫总体拦对了（被拦方案事后平均亏损）"
                                  if m < 0 else "⚠ 守卫可能在错杀（被拦方案事后平均盈利）"))
        else:
            out.append("  （这些条目还没有反事实结算结果）")
        for r in suppressed[:5]:
            out.append(f"  · {r['id'][:20]}  {(r.get('suppressed_reason') or '')[:72]}")

    out.append("")
    if len(trades) < 30:
        out.append(f"⚠ 下单样本仅 {len(trades)} 笔（<30）：结论**不具备**统计意义，只能当作方向性线索。")
    return "\n".join(out)


def cmd_report(args: argparse.Namespace) -> int:
    rows = L.load_ledger()
    if getattr(args, "tag", None):
        rows = [r for r in rows if r.get("batch") == args.tag]
    print(aggregate(rows))
    return 0


# ------------------------------------------------------------------ 批量跑
def _find_near(expected_ts: int, rows: list[dict]) -> dict | None:
    """找到与预期时间戳最接近的台账条目（±30 分钟内）。

    容差要放宽：`trading_ledger._parse_as_of` 用的是**调用当刻**的时间，
    批量跑得越久，条目时间相对批启动时预算的 ts 漂移越大（实测十几分钟）。
    """
    best: tuple[int, dict] | None = None
    for r in rows:
        d = abs(int(r.get("created_ts_ms", 0)) - expected_ts)
        if d < 1_800_000 and (best is None or d < best[0]):
            best = (d, r)
    return best[1] if best else None


def _run_window(cmd: list[str], timeout_s: int) -> tuple[int | None, str, str, bool]:
    """跑一个窗口子进程，带**进程树**强杀的超时保护。

    为什么不能只用 subprocess.run(timeout=)：Windows 上 .venv\\Scripts\\python.exe 是
    启动器，会再起一个真解释器；超时只杀掉启动器，**孙进程仍持有 stdout 管道**，
    communicate 会永久阻塞（实测整批卡死 8 分钟以上）。所以这里用 Popen +
    taskkill /T 杀整棵树。
    """
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    try:
        out, err = p.communicate(timeout=timeout_s)
        return p.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(p.pid)],
            capture_output=True,
        )
        try:
            out, err = p.communicate(timeout=20)
        except Exception:  # noqa: BLE001 - 已尽力，残管道不再阻塞主流程
            out, err = "", ""
        return None, out or "", err or "", True


def cmd_run(args: argparse.Namespace) -> int:
    now_ms = L._now_ms()
    expected: list[tuple[int, str]] = []
    for i in range(args.count):
        hours = args.end_hours_ago + i * args.step_h
        expected.append((now_ms - hours * 3_600_000, f"-{hours}h"))

    # 已有条目（含此前跑过的窗口）补批次标记，并据此跳过，避免重复烧 token
    existing = L.load_ledger()
    todo: list[tuple[int, str]] = []
    skipped = 0
    changed = False
    for ts, spec in expected:
        row = _find_near(ts, existing)
        if row is not None:
            skipped += 1
            if row.get("batch") != args.tag:
                row["batch"] = args.tag
                changed = True
        else:
            todo.append((ts, spec))
    if changed:
        L.save_ledger(existing)
    print(f"计划 {len(expected)} 个窗口，已有 {skipped} 个（跳过并已补标记），"
          f"本次将跑 {len(todo)} 个：{', '.join(s for _, s in todo) or '无'}")
    if not todo:
        rows = [r for r in L.load_ledger() if r.get("batch") == args.tag]
        print(aggregate(rows))
        return 0

    # 每个窗口独立子进程 + 硬时限：单个请求卡死（客户端超时长达 600s）不会拖死整批
    ledger_py = ROOT / "tools" / "trading_ledger.py"
    ok = failed = 0
    t_start = time.time()
    for n, (ts, spec) in enumerate(todo, start=1):
        cmd = [
            sys.executable, str(ledger_py), "record",
            "--symbol", args.symbol, "--timeframe", args.timeframe,
            "--source", args.source, "--bars", str(args.bars),
            "--horizon-bars", str(args.horizon_bars),
            # 必须用 --as-of= 形式：值以负号开头，空格分隔会被 argparse 当成选项
            f"--as-of={spec}",
        ]
        t0 = time.time()
        ids_before = {r.get("id") for r in L.load_ledger()}
        rc, out_txt, err_txt, timed_out = _run_window(cmd, args.window_timeout)
        if timed_out:
            failed += 1
            print(f"[{n}/{len(todo)}] {spec} ⏱ 超 {args.window_timeout}s 未完成 → 杀进程树、丢弃该窗口继续")
            continue
        el = time.time() - t0
        rows = L.load_ledger()
        # 认领本条：优先用"运行前后新增的 id"（确定性），兼容漂移
        fresh = [r for r in rows if r.get("id") not in ids_before]
        row = fresh[-1] if len(fresh) == 1 else _find_near(ts, rows)
        if row is None:
            failed += 1
            err = (err_txt or "").strip().splitlines()
            print(f"[{n}/{len(todo)}] {spec} rc={rc} {el:.0f}s ⚠ 未写入台账"
                  f"{' | ' + err[-1][:110] if err else ''}")
            continue
        if row.get("batch") != args.tag:
            row["batch"] = args.tag
            L.save_ledger(rows)
        tail = [
            ln.strip() for ln in (out_txt or "").splitlines()
            if "持仓建议" in ln or "闸门" in ln or "连续性守卫" in ln or "被改写方案" in ln
        ]
        print(f"[{n}/{len(todo)}] {spec} rc={rc} {el:.0f}s  " + " | ".join(tail[:3]))
        ok += 1

    print(f"\n批量分析完成：成功 {ok} / 失败 {failed}，累计 {time.time() - t_start:.0f}s。开始结算…")
    with contextlib.redirect_stdout(io.StringIO()):
        L.cmd_resolve(argparse.Namespace(redo=False))

    rows = [r for r in L.load_ledger() if r.get("batch") == args.tag]
    print(aggregate(rows))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n批次 {args.tag} 于 {stamp} 完成；重看结果："
          f"python tools/ledger_batch.py report --tag {args.tag}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="批量回放并结算（回答：用这套信号交易能不能赚钱）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="跑一批 as-of 快照分析并结算")
    pr.add_argument("--count", type=int, default=16, help="窗口数")
    pr.add_argument("--step-h", type=int, default=12, help="窗口间隔（小时）")
    pr.add_argument(
        "--end-hours-ago", type=int, default=48,
        help="最早的窗口距现在多少小时（默认 48h，留出 24h 持仓上限让样本立即可结算）",
    )
    pr.add_argument("--tag", default=None, help="批次标记（默认按时间生成）")
    pr.add_argument("--symbol", default="BTCUSDT")
    pr.add_argument("--timeframe", default="15m")
    pr.add_argument("--source", default="binance")
    pr.add_argument("--bars", type=int, default=100, help="送模型的 bar 数（与 GUI 一致为 100）")
    pr.add_argument("--horizon-bars", type=int, default=L.DEFAULT_HORIZON_BARS)
    pr.add_argument(
        "--window-timeout", type=int, default=240,
        help="单个窗口的硬时限（秒）；超时则丢弃该窗口继续跑下一个",
    )
    pr.set_defaults(func=cmd_run)

    pp = sub.add_parser("report", help="只汇总（不跑分析）")
    pp.add_argument("--tag", default=None, help="批次标记；省略=全部条目")
    pp.set_defaults(func=cmd_report)

    args = ap.parse_args()
    if args.cmd == "run" and not args.tag:
        args.tag = "b" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
