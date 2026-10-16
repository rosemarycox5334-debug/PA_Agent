#!/usr/bin/env python3
"""预测台账（paper-trading ledger）——把每次分析变成可证伪的预测，到期用真实行情结算。

为什么需要它：回测回答不了"下个月还能不能行"。唯一诚实的做法是把每个预测
（方向 / 入场 / 止损 / 止盈 / 自评胜率）连同**当时的快照价**写下来，到期后用
真实行情逐根判定命中与否，并把结果与基线对照。台账会自己给出结论，不需要谁去相信。

子命令：
  record    运行真实两阶段分析（真实行情 + 真实 API），把预测写入台账
  resolve   结算已到期预测（真实行情逐根判定 TP/SL 先后，含手续费与滑点）
  status    记分板：命中率、净收益、成本拖累、与"永远做多/做空"基线对比

用法：
  python tools/trading_ledger.py record  --symbol BTCUSDT --timeframe 15m
  python tools/trading_ledger.py resolve
  python tools/trading_ledger.py status

台账文件：records/predictions/predictions.jsonl（运行时数据，不入库）
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pa_agent.ai.deepseek_client import DeepSeekClient
from pa_agent.ai.json_validator import JsonValidator
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.router import route_strategy_files
from pa_agent.config.paths import EXPERIENCE_DIR, PROMPT_DIR, SETTINGS_JSON_PATH
from pa_agent.config.settings import load_settings, provider_api_key_configured
from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.data.factory import create_data_source
from pa_agent.data.snapshot import compute_indicators
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.util.atomic_write import atomic_write_text
from pa_agent.util.event_bus import EventBus
from pa_agent.util.threading import CancelToken, OrchestratorEvent
from pa_agent.util.trade_metrics import cost_viability

LEDGER_DIR = ROOT / "records" / "predictions"
LEDGER_PATH = LEDGER_DIR / "predictions.jsonl"
RECORD_DIR = LEDGER_DIR / "records"

#: 单边 taker 手续费（%），币安现货默认 0.1%
TAKER_FEE_PCT = 0.10
#: 单边滑点估计（%）
SLIPPAGE_PCT = 0.05
#: 双边总成本（%）：手续费 + 滑点，各计一次
ROUND_TRIP_COST_PCT = 2 * (TAKER_FEE_PCT + SLIPPAGE_PCT)
#: 止盈距离至少要是双边成本的多少倍（低于此值的方案扣成本后不切实际）
MIN_REWARD_COST_RATIO = 3.0
#: 默认持仓上限（根 bar），15m × 96 = 24 小时
DEFAULT_HORIZON_BARS = 96

log = logging.getLogger("ledger")


# ---------------------------------------------------------------- 行情与台账 IO
def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_as_of(spec: str | None) -> int | None:
    """'2026-10-14T08:00'（本地时间）或 '-24h' / '-3d' -> 毫秒时间戳。"""
    if not spec:
        return None
    s = spec.strip()
    m = re.fullmatch(r"-(\d+)([mhd])", s)
    if m:
        unit = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[m.group(2)]
        return _now_ms() - int(m.group(1)) * unit
    dt = datetime.fromisoformat(s)
    # naive 时间按本机本地时间理解（datetime.timestamp() 对 naive 即本地时区）
    return int(dt.timestamp() * 1000)


def fetch_bars(
    kind: str, symbol: str, timeframe: str, count: int, as_of_ms: int | None = None
) -> list[KlineBar]:
    """拉取真实 K 线（index 0 = 最新，含未收盘根）。

    *as_of_ms* 不为空时走历史快照（``endTime``），只取该时刻之前已存在的 bar。
    """
    src = create_data_source(kind)
    src.connect()
    try:
        src.subscribe(symbol, timeframe)
        if as_of_ms is None:
            bars = list(src.latest_snapshot(count))
        else:
            hist = getattr(src, "historical_snapshot", None)
            if hist is None:
                raise RuntimeError(f"数据源 {kind} 不支持历史快照，无法做 as-of 回放")
            bars = list(hist(count, as_of_ms))
        src.unsubscribe()
    finally:
        src.disconnect()
    return bars


def build_frame(
    kind: str, symbol: str, timeframe: str, count: int, as_of_ms: int | None = None
) -> tuple[KlineFrame, float]:
    """构造 KlineFrame，并返回「最近一根已收盘 bar 的收盘价」作为快照价。"""
    bars = fetch_bars(kind, symbol, timeframe, count, as_of_ms=as_of_ms)
    if len(bars) < 3:
        raise RuntimeError(f"只取到 {len(bars)} 根 bar，数据不足")
    frame = KlineFrame(
        symbol=symbol,
        timeframe=timeframe,
        bars=tuple(bars),
        snapshot_ts_local_ms=_now_ms(),
        indicators=compute_indicators(bars),
    )
    closed = next((b for b in bars if b.closed), None)
    snapshot_price = float((closed or bars[0]).close)
    return frame, snapshot_price


def load_ledger() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    rows: list[dict] = []
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def save_ledger(rows: list[dict]) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    atomic_write_text(LEDGER_PATH, body)


def append_ledger(row: dict) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_ledger()
    rows.append(row)
    save_ledger(rows)


# ---------------------------------------------------------------- record
def _num(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f


def cmd_record(args: argparse.Namespace) -> int:
    settings = load_settings(SETTINGS_JSON_PATH)
    if not provider_api_key_configured(settings):
        log.error("未配置 API 密钥：%s", SETTINGS_JSON_PATH)
        return 2

    kind = args.source or settings.general.last_data_source
    as_of_ms = _parse_as_of(args.as_of)
    frame, snapshot_price = build_frame(
        kind, args.symbol, args.timeframe, args.bars, as_of_ms=as_of_ms
    )
    log.info(
        "快照: %s %s %d 根 (源=%s) 快照价=%.2f 最新bar=%s%s",
        args.symbol,
        args.timeframe,
        len(frame.bars),
        kind,
        snapshot_price,
        frame.bars[0].ts_open,
        "（as-of 回放）" if as_of_ms else "",
    )

    exp_reader = ExperienceReader(experience_dir=EXPERIENCE_DIR, logger=log)
    assembler = PromptAssembler(
        prompt_dir=PROMPT_DIR,
        experience_reader=exp_reader,
        prompt_settings=settings.prompt,
    )
    client = DeepSeekClient(settings=settings.provider, logger_=log)
    orchestrator = TwoStageOrchestrator(
        client=client,
        assembler=assembler,
        router=route_strategy_files,
        validator=JsonValidator(settings.validation),
        pending_writer=PendingWriter(
            pending_dir=RECORD_DIR,
            event_bus=EventBus(),
            api_key=settings.provider.api_key,
        ),
        exp_reader=exp_reader,
        settings=settings,
    )

    event_names: list[str] = []
    t0 = time.time()
    record = orchestrator.submit(
        frame=frame,
        cancel_token=CancelToken(),
        on_event=lambda e: event_names.append(e.name),
    )
    elapsed = time.time() - t0

    s1 = record.stage1_diagnosis or {}
    s2 = record.stage2_decision or {}
    dec = (s2 or {}).get("decision") or {}
    terminal = (s2 or {}).get("terminal") or {}
    gate_result = str(s1.get("gate_result") or "unknown").lower()
    terminal_outcome = str(terminal.get("outcome") or "").lower() or None

    horizon = args.horizon_bars
    step_ms = _timeframe_ms(args.timeframe)
    created = as_of_ms if as_of_ms is not None else _now_ms()
    created_dt = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
    pred_id = created_dt.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]

    pending_files = sorted(RECORD_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    viability = cost_viability(dec, round_trip_cost_pct=ROUND_TRIP_COST_PCT) if dec else None
    cg = (s2 or {}).get("continuity_guard") if isinstance(s2, dict) else None
    if not (isinstance(cg, dict) and cg.get("applied")):
        cg_cost = (s2 or {}).get("cost_guard") if isinstance(s2, dict) else None
        if isinstance(cg_cost, dict) and cg_cost.get("applied"):
            cg = cg_cost
    cg = cg if isinstance(cg, dict) else {}
    row = {
        "id": pred_id,
        "created_utc": created_dt.isoformat(timespec="seconds"),
        "created_ts_ms": created,
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "source": kind,
        "snapshot_price": snapshot_price,
        "horizon_bars": horizon,
        "expiry_ts_ms": created + horizon * step_ms,
        "gate_result": gate_result,
        "stage2_reached": bool(s2),
        "terminal_outcome": terminal_outcome,
        "terminal_node": terminal.get("node_id"),
        "order_direction": dec.get("order_direction"),
        "order_type": dec.get("order_type"),
        "entry_price": _num(dec.get("entry_price")),
        "stop_loss_price": _num(dec.get("stop_loss_price")),
        "take_profit_price": _num(dec.get("take_profit_price")),
        "take_profit_price_2": _num(dec.get("take_profit_price_2")),
        "model_estimated_win_rate": _num(dec.get("estimated_win_rate")),
        "trade_confidence": _num(dec.get("trade_confidence")),
        "cost_viability": viability,
        "cost_pct": ROUND_TRIP_COST_PCT,
        # 连续性守卫是否把模型给的方案改写成了「不下单」，以及被改写的原始方案
        # （用于评估“守卫这次拦得对不对”——反事实结算在 resolve 里做）
        "suppressed_by_guard": bool(cg.get("applied")),
        "suppressed_reason": cg.get("reason"),
        "suppressed_plan": cg.get("original_plan"),
        "reasoning": (dec.get("reasoning") or "")[:400],
        "events": event_names,
        "exception": record.exception,
        "model": settings.provider.model,
        "elapsed_s": round(elapsed, 1),
        "record_path": str(pending_files[-1]) if pending_files else None,
        "status": "open",
    }
    append_ledger(row)

    trades = (
        row["order_direction"] in ("做多", "做空")
        and row["entry_price"] is not None
        and terminal_outcome == "trade"
    )
    print("\n=== record 完成 ===")
    print(f"台账条目        : {pred_id}")
    print(f"闸门 gate_result: {gate_result}")
    print(f"持仓建议        : {row['order_direction']} / {row['order_type']}" if trades else "持仓建议        : 不下单（wait/无决策）")
    if trades:
        print(f"入场/止损/止盈  : {row['entry_price']} / {row['stop_loss_price']} / {row['take_profit_price']}")
        print(f"模型自评胜率    : {row['model_estimated_win_rate']}%  信心={row['trade_confidence']}")
    if cg.get("applied"):
        print(f"连续性守卫      : ⚠ 已把模型方案改写为「不下单」—— {cg.get('reason')}")
        print(f"  被改写方案    : {json.dumps(cg.get('original_plan'), ensure_ascii=False)}")
    if viability:
        flag = "⚠ 成本不划算" if viability["reward_cost_ratio"] < MIN_REWARD_COST_RATIO else "成本可行"
        print(f"成本可行性      : [{flag}] {viability['text']}")
    print(f"耗时            : {elapsed:.1f}s   事件={event_names}")
    if row["exception"]:
        print(f"异常            : {json.dumps(row['exception'], ensure_ascii=False)[:300]}")
    print(f"台账            : {LEDGER_PATH}")
    return 0 if trades or gate_result != "unknown" else 1


# ---------------------------------------------------------------- resolve
def _timeframe_ms(tf: str) -> int:
    unit = tf[-1]
    n = int(tf[:-1] or 1)
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}[unit]


def _fetch_range(kind: str, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> list[KlineBar]:
    """取 [start, end] 区间内的已收盘 bar，按时间正序返回。

    以 *end_ms* 为 endTime 拉历史快照——窗口可能在很久以前，用「最新 N 根」
    会取不到该区间（实测：30 小时前的窗口全部落空）。
    """
    total = max(2, int((end_ms - start_ms) / _timeframe_ms(timeframe)) + 8)
    bars = fetch_bars(kind, symbol, timeframe, min(total, 1000), as_of_ms=end_ms)
    win = [b for b in bars if start_ms <= b.ts_open <= end_ms]
    return sorted(win, key=lambda b: b.ts_open)


def _simulate(
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    bars: list[KlineBar],
) -> tuple[str, float, float, int, float]:
    """逐根判定结果。同一根内同时触及 SL 与 TP 时按**最坏情况**（先止损）处理。

    返回 (outcome, exit_price, mfe_pct, bars_held, 触发时价格)
    """
    long = direction == "做多"
    mfe = 0.0
    for i, b in enumerate(bars, start=1):
        hi, lo = float(b.high), float(b.low)
        exc = (hi - entry) / entry * 100 if long else (entry - lo) / entry * 100
        mfe = max(mfe, exc)
        hit_sl = lo <= sl if long else hi >= sl
        hit_tp = hi >= tp if long else lo <= tp
        if hit_sl:  # 最坏情况优先
            return "sl_hit", sl, mfe, i, float(b.ts_open)
        if hit_tp:
            return "tp_hit", tp, mfe, i, float(b.ts_open)
    last = bars[-1] if bars else None
    return ("timeout", float(last.close) if last else entry, mfe, len(bars), float(last.ts_open) if last else 0.0)


def cmd_resolve(args: argparse.Namespace) -> int:
    rows = load_ledger()
    now = _now_ms()
    retryable = {"open", "resolve_error"}
    if getattr(args, "redo", False):
        retryable = retryable | {"resolved"}
    due = [
        r
        for r in rows
        if r.get("status") in retryable and r.get("expiry_ts_ms", 0) <= now
    ]
    if not due:
        open_n = sum(1 for r in rows if r.get("status") == "open")
        print(f"没有到期预测（未到期 {open_n} 条 / 共 {len(rows)} 条）")
        return 0

    cost_pct = ROUND_TRIP_COST_PCT
    for r in due:
        try:
            bars = _fetch_range(
                r["source"], r["symbol"], r["timeframe"], r["created_ts_ms"], r["expiry_ts_ms"]
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不应中断整批结算
            r["status"] = "resolve_error"
            r["resolve_error"] = f"{type(exc).__name__}: {exc}"
            continue
        if not bars:
            r["status"] = "resolve_error"
            r["resolve_error"] = "区间内无行情"
            continue

        entry, sl, tp = r.get("entry_price"), r.get("stop_loss_price"), r.get("take_profit_price")
        if r.get("order_direction") in ("做多", "做空") and entry and sl and tp:
            outcome, exit_px, mfe, bars_held, trig = _simulate(
                r["order_direction"], entry, sl, tp, bars
            )
            r["outcome"] = outcome
            r["exit_price"] = exit_px
            r["mfe_pct"] = round(mfe, 3)
            r["bars_held"] = bars_held
            r["trigger_ts_ms"] = int(trig)
            gross = (exit_px - entry) / entry * 100 if r["order_direction"] == "做多" else (entry - exit_px) / entry * 100
            r["gross_pct"] = round(gross, 4)
            r["net_pct"] = round(gross - cost_pct, 4)  # 扣双边手续费+滑点
            r["direction_correct"] = gross > 0

            # --- 同口径基线：同样的 SL/TP 距离，永远做多 / 永远做空 ---
            d_sl, d_tp = abs(entry - sl), abs(tp - entry)
            for tag, dr in (("long", "做多"), ("short", "做空")):
                o2, x2, _, _, _ = _simulate(
                    dr,
                    r["snapshot_price"],
                    r["snapshot_price"] - d_sl if dr == "做多" else r["snapshot_price"] + d_sl,
                    r["snapshot_price"] + d_tp if dr == "做多" else r["snapshot_price"] - d_tp,
                    bars,
                )
                g2 = (x2 - r["snapshot_price"]) / r["snapshot_price"] * 100 if dr == "做多" else (r["snapshot_price"] - x2) / r["snapshot_price"] * 100
                r[f"baseline_{tag}"] = {"outcome": o2, "net_pct": round(g2 - cost_pct, 4)}
        else:
            # 未下单：记录"若按快照价持有到期"的走势，用于衡量"放弃"是否避免了损失
            last = float(bars[-1].close)
            r["outcome"] = "abstained"
            r["hold_return_pct"] = round((last - r["snapshot_price"]) / r["snapshot_price"] * 100, 4)

            # 反事实：若连续性守卫没拦下那个方案，按同样的止损/止盈会怎样？
            sp = r.get("suppressed_plan") or {}
            d, e, s, t = (
                sp.get("order_direction"),
                sp.get("entry_price"),
                sp.get("stop_loss_price"),
                sp.get("take_profit_price"),
            )
            if d in ("做多", "做空") and e and s and t:
                try:
                    o3, x3, _, bh3, _ = _simulate(d, float(e), float(s), float(t), bars)
                    g3 = (
                        (x3 - float(e)) / float(e) * 100
                        if d == "做多"
                        else (float(e) - x3) / float(e) * 100
                    )
                    r["counterfactual"] = {
                        "outcome": o3,
                        "net_pct": round(g3 - cost_pct, 4),
                        "bars_held": bh3,
                    }
                except (TypeError, ValueError) as exc:  # 单条反事实失败不影响结算
                    r["counterfactual"] = {"error": f"{type(exc).__name__}: {exc}"}
        r["status"] = "resolved"
        r["resolved_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    save_ledger(rows)
    print(f"已结算 {len(due)} 条 -> {LEDGER_PATH}")
    for r in due:
        if r.get("outcome") in ("tp_hit", "sl_hit", "timeout"):
            print(
                f"  {r['id']}  {r['order_direction']}{r['order_type']}  {r['outcome']:8s} "
                f"净收益 {r.get('net_pct'):+.3f}%  持仓 {r.get('bars_held')} 根  "
                f"（永远做多基线 {r.get('baseline_long', {}).get('net_pct', 0):+.3f}% / "
                f"永远做空 {r.get('baseline_short', {}).get('net_pct', 0):+.3f}%）"
            )
        elif r.get("outcome") == "abstained":
            print(f"  {r['id']}  弃单 同期持有收益 {r.get('hold_return_pct'):+.3f}%")
        else:
            print(f"  {r['id']}  结算失败: {r.get('resolve_error')}")
    return 0


# ---------------------------------------------------------------- status
def _binom_p_ge(k: int, n: int, p: float = 0.5) -> float:
    """P(X >= k) —— 单尾二项检验，样本很小时用来提醒结论不显著。"""
    from math import comb

    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


def cmd_status(args: argparse.Namespace) -> int:
    rows = load_ledger()
    if not rows:
        print("台账为空：先跑 `python tools/trading_ledger.py record`")
        return 0

    graded = [r for r in rows if r.get("outcome") in ("tp_hit", "sl_hit", "timeout")]
    abstained = [r for r in rows if r.get("outcome") == "abstained"]
    open_n = sum(1 for r in rows if r.get("status") == "open")

    print("=== 预测台账记分板 ===")
    print(f"条目总数 {len(rows)} | 已结算 {len(graded)} | 未到期 {open_n} | 弃单 {len(abstained)}")

    if graded:
        wins = [r for r in graded if r.get("direction_correct")]
        nets = [r["net_pct"] for r in graded if r.get("net_pct") is not None]
        tp_n = sum(1 for r in graded if r["outcome"] == "tp_hit")
        sl_n = sum(1 for r in graded if r["outcome"] == "sl_hit")
        to_n = sum(1 for r in graded if r["outcome"] == "timeout")
        print(f"\n结局分布: 止盈 {tp_n} / 止损 {sl_n} / 到期平仓 {to_n}")
        print(f"方向命中率: {len(wins)}/{len(graded)} = {len(wins)/len(graded)*100:.1f}%")
        print(f"单笔净收益: 均值 {statistics.mean(nets):+.3f}%  中位 {statistics.median(nets):+.3f}%")
        print(f"累计净收益: {sum(nets):+.2f}%（假设每笔等额、不含复利）")
        fees = len(graded) * 2 * (TAKER_FEE_PCT + SLIPPAGE_PCT)
        print(f"成本拖累  : {fees:.2f}%（{len(graded)} 笔 × 双边 {2*(TAKER_FEE_PCT+SLIPPAGE_PCT):.2f}%）")
        if len(graded) >= 2:
            p = _binom_p_ge(len(wins), len(graded))
            print(f"显著性与 50% 抛硬币对比: 单尾 p = {p:.3f}", "（不显著，样本太小）" if p > 0.05 else "（显著）")
        bl = [r for r in graded if r.get("baseline_long")]
        if bl:
            blm = statistics.mean(r["baseline_long"]["net_pct"] for r in bl)
            bsm = statistics.mean(r["baseline_short"]["net_pct"] for r in bl)
            print(f"同口径基线: 永远做多 {blm:+.3f}% / 永远做空 {bsm:+.3f}%  （模型 {statistics.mean(nets):+.3f}%）")
        model_wr = [r["model_estimated_win_rate"] for r in graded if r.get("model_estimated_win_rate") is not None]
        if model_wr:
            print(f"模型自评胜率均值 {statistics.mean(model_wr):.1f}%  vs 实测命中率 {len(wins)/len(graded)*100:.1f}%")
    else:
        print("\n尚无结算样本——任何收益结论都还不成立。")

    if abstained:
        holds = [r["hold_return_pct"] for r in abstained if r.get("hold_return_pct") is not None]
        if holds:
            print(f"\n弃单 {len(abstained)} 次，同期持有收益均值 {statistics.mean(holds):+.3f}%"
                  f"（负值=弃单避免了损失，正值=错过了机会）")

    viabs = [r["cost_viability"] for r in rows if isinstance(r.get("cost_viability"), dict)]
    if viabs:
        bad = [v for v in viabs if v["reward_cost_ratio"] < MIN_REWARD_COST_RATIO]
        print(f"\n--- 成本可行性（{len(viabs)} 条下单方案，双边成本 {ROUND_TRIP_COST_PCT:.2f}%）---")
        print(f"  止盈距离 < {MIN_REWARD_COST_RATIO:.1f}× 成本（扣成本后不切实际）: "
              f"{len(bad)}/{len(viabs)} 条 = {len(bad)/len(viabs)*100:.0f}%")
        print(f"  止盈/成本倍数均值 {statistics.mean(v['reward_cost_ratio'] for v in viabs):.2f}×"
              f"  |  平均盈亏平衡胜率 {statistics.mean(v['breakeven_win_rate_pct'] for v in viabs):.1f}%")
        wr = [v["win_rate_pct"] for v in viabs if v.get("win_rate_pct") is not None]
        if wr:
            print(f"  模型自评胜率均值 {statistics.mean(wr):.1f}%"
                  f"  -  平均盈亏平衡胜率 {statistics.mean(v['breakeven_win_rate_pct'] for v in viabs):.1f}%"
                  f"  =  缺口 {statistics.mean(v['breakeven_win_rate_pct'] for v in viabs) - statistics.mean(wr):+.1f} 个百分点")

    if len(graded) < 30:
        print(f"\n⚠ 已结算样本仅 {len(graded)} 条：统计上毫无意义，不要据此下任何结论。")
    print(f"\n台账文件: {LEDGER_PATH}")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """补齐早期条目缺失的派生字段（成本可行性等），不改动原始记录。"""
    rows = load_ledger()
    filled = 0
    for r in rows:
        if r.get("order_direction") in ("做多", "做空") and r.get("entry_price"):
            if not isinstance(r.get("cost_viability"), dict):
                v = cost_viability(
                    {
                        "order_direction": r.get("order_direction"),
                        "order_type": r.get("order_type"),
                        "entry_price": r.get("entry_price"),
                        "stop_loss_price": r.get("stop_loss_price"),
                        "take_profit_price": r.get("take_profit_price"),
                        "take_profit_price_2": r.get("take_profit_price_2"),
                        "estimated_win_rate": r.get("model_estimated_win_rate"),
                    },
                    round_trip_cost_pct=ROUND_TRIP_COST_PCT,
                )
                if v:
                    r["cost_viability"] = v
                    r.setdefault("cost_pct", ROUND_TRIP_COST_PCT)
                    filled += 1
    if filled:
        save_ledger(rows)
    print(f"backfill: 补齐 {filled} 条（共 {len(rows)} 条）")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("record", help="运行真实分析并写入一条预测")
    pr.add_argument("--symbol", default=None)
    pr.add_argument("--timeframe", default=None)
    pr.add_argument("--source", default=None, help="数据源（默认取 settings.json）")
    pr.add_argument("--bars", type=int, default=120, help="送模型的 bar 数")
    pr.add_argument("--horizon-bars", type=int, default=DEFAULT_HORIZON_BARS, help="持仓上限（根）")
    pr.add_argument(
        "--as-of",
        default=None,
        help="历史回放：ISO 时间（如 2026-10-10T12:00）或 -24h / -3d，只用到该时刻为止的行情",
    )
    pr.set_defaults(func=cmd_record)

    sub.add_parser("resolve", help="结算到期预测").set_defaults(func=cmd_resolve)
    sub.add_parser("status", help="记分板").set_defaults(func=cmd_status)
    sub.add_parser("backfill", help="补齐早期条目缺失的派生字段").set_defaults(func=cmd_backfill)
    redo = sub.add_parser("resolve-redo", help="重算全部已结算条目（代码修复后使用）")
    redo.set_defaults(func=cmd_resolve, redo=True)

    args = p.parse_args()
    if args.cmd == "record":
        s = load_settings(SETTINGS_JSON_PATH)
        args.symbol = args.symbol or s.general.last_symbol
        args.timeframe = args.timeframe or s.general.last_timeframe
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
