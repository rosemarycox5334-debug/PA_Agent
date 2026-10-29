"""Uncalibrated display-only cycle weights derived from project PA descriptions.

The source texts define qualitative transitions, NOT a fitted transition table.
All numerical weights and signal cutoffs below are engineering heuristics.
Never use these probabilities as trading gates, win rates or sizing inputs.
"""
from __future__ import annotations

import copy
import math
from dataclasses import replace
from typing import Any

from pa_agent.ai.cycle_enums import CYCLE_ORDER, CYCLE_POSITION_ZH

SOURCE = "program_rules"
VERSION = "pa-cycle-display-v1"
# Column order is explicitly independent of the UI's cycle ordering.
KEYS = ("spike", "micro_channel", "tight_channel", "normal_channel", "broad_channel", "trending_tr", "trading_range", "extreme_tr")
# Persistence + nearby-state transitions; channel/flag or range after spikes,
# wider channels on deceleration, range persistence without confirmed breakout.
TRANSITIONS = {
    "spike":          (12, 15, 30, 20,  8,  5,  8,  2),
    "micro_channel":  (10, 20, 30, 18,  8,  5,  7,  2),
    "tight_channel":  ( 8,  8, 30, 25, 15,  7,  5,  2),
    "normal_channel": ( 5,  5, 15, 30, 25, 10,  8,  2),
    "broad_channel":  ( 3,  2,  8, 18, 30, 20, 16,  3),
    "trending_tr":    ( 3,  2,  5, 10, 22, 28, 25,  5),
    "trading_range":  ( 5,  2,  3,  5, 10, 20, 45, 10),
    "extreme_tr":     ( 3,  1,  2,  3,  6, 10, 35, 40),
    "unknown":        ( 5,  3,  7, 15, 20, 20, 25,  5),
}
SOURCES = [
    {"path": "prompt_engineering/市场诊断框架.txt", "lines": "65-221,264-289,529-551", "rule": "八态定义、尖峰后延续、回撤分层、有序波段与末期衰竭"},
    {"path": "prompt_engineering/文件13-窄通道与宽通道策略.txt", "lines": "7-41,73-93", "rule": "窄通道减速变宽、宽通道加速变窄、失去方向变区间"},
]


def percentages(weights: dict[str, float]) -> dict[str, int]:
    """Largest-remainder rounding, deterministic ties, exact integer sum=100."""
    values = {k: max(0.0, float(weights.get(k, 0))) for k in CYCLE_ORDER}
    total = sum(values.values())
    if total <= 0 or not math.isfinite(total):
        values = dict(zip(KEYS, TRANSITIONS["unknown"]))
        total = sum(values.values())
    raw = {k: values[k] / total * 100 for k in CYCLE_ORDER}
    out = {k: int(raw[k]) for k in CYCLE_ORDER}
    order = sorted(CYCLE_ORDER, key=lambda k: -(raw[k] - out[k]))
    for k in order[:100 - sum(out.values())]:
        out[k] += 1
    return out


def _run(bars: tuple, *, strong: bool) -> int:
    if not bars:
        return 0
    sign = 1 if bars[0].close > bars[0].open else -1 if bars[0].close < bars[0].open else 0
    if not sign:
        return 0
    count = 0
    for i, b in enumerate(bars[:10]):
        width = b.high - b.low
        if strong and (width <= 0 or sign * (b.close - b.open) / width < 0.65):
            break
        if i:
            newer = bars[i - 1]
            if sign > 0 and not (newer.high > b.high and newer.low >= b.low):
                break
            if sign < 0 and not (newer.low < b.low and newer.high <= b.high):
                break
            if strong:
                lo = max(min(newer.open, newer.close), min(b.open, b.close))
                hi = min(max(newer.open, newer.close), max(b.open, b.close))
                denom = min(abs(newer.close - newer.open), abs(b.close - b.open))
                if denom <= 0 or max(0, hi - lo) / denom >= 0.30:
                    break
        count += 1
    return count


def classify_cycle(frame: Any) -> str:
    """Public: classify the current cycle from closed bars only.

    Falls back to a conservative state when there is not enough closed data,
    so callers never have to show an "unknown" cycle.
    """
    if frame is None:
        return "trading_range"
    cycle, _ = _structure(frame)
    return cycle if cycle != "unknown" else "trading_range"


def _structure(frame: Any) -> tuple[str, dict[str, Any]]:
    from pa_agent.ai.market_features import compute_simple_market_features
    from pa_agent.data.snapshot import compute_indicators

    # Discard forming bars BEFORE every calculation, then align indicators.
    bars = tuple(sorted((b for b in frame.bars if b.closed and b.seq > 0), key=lambda b: b.seq))[:40]
    if len(bars) < 5:
        return "unknown", {"closed_bars": len(bars), "note": "已收盘样本不足，使用保守先验"}
    clean = replace(frame, bars=bars, indicators=compute_indicators(list(bars)))
    f = compute_simple_market_features(clean, lookback=40)
    travel = sum(abs(a.close - b.close) for a, b in zip(bars, bars[1:]))
    efficiency = abs(bars[0].close - bars[-1].close) / travel if travel > 0 else 0.0
    overlap = float(f.overlap_mean_10 or 0)
    dojis = float(f.doji_inside_ratio_10 or 0)
    strong_run, micro_run = _run(bars, strong=True), _run(bars, strong=False)
    # The shared pivot detector also emits endpoints. Only interior pivots are
    # confirmed by both neighbouring closed bars, so exclude both endpoints.
    interior = {b.seq for b in bars[1:-1]}
    pivots = [p for p in f.swings if p.seq in interior]
    highs = sorted((p for p in pivots if p.kind == "high"), key=lambda p: p.seq)
    lows = sorted((p for p in pivots if p.kind == "low"), key=lambda p: p.seq)
    sign = 1 if bars[0].close > bars[-1].close else -1 if bars[0].close < bars[-1].close else 0
    def ordered_count(items):
        n = 1 if items else 0
        for a, b in zip(items, items[1:]):
            if sign * (a.price - b.price) <= 0:
                break
            n += 1
        return n
    groups = min(ordered_count(highs), ordered_count(lows)) if sign else 0
    parallel = False
    if groups >= 3:
        hs = (highs[0].price - highs[2].price) / (highs[2].seq - highs[0].seq)
        ls = (lows[0].price - lows[2].price) / (lows[2].seq - lows[0].seq)
        parallel = hs * ls > 0 and abs(hs - ls) <= 0.75 * max(abs(hs), abs(ls))
    depth = None
    if sign and highs and lows:
        extremes, anchors = (highs, lows) if sign > 0 else (lows, highs)
        peak = extremes[0]
        older = [p for p in anchors if p.seq > peak.seq]
        since = [b for b in bars if b.seq < peak.seq]
        if older and since:
            anchor = min(older, key=lambda p: p.seq)
            leg = sign * (peak.price - anchor.price)
            retrace_price = min(b.low for b in since) if sign > 0 else max(b.high for b in since)
            if leg > 0:
                depth = max(0.0, sign * (peak.price - retrace_price) / leg)
    if strong_run >= 2:
        cycle = "spike"
    elif 2 <= micro_run <= 10 and efficiency > 0.55:
        cycle = "micro_channel"
    elif groups >= 3 and parallel and depth is not None and depth <= 0.786:
        cycle = "tight_channel" if depth < 0.30 else "normal_channel" if depth <= 0.50 else "broad_channel"
    elif groups >= 2 or efficiency > 0.35:
        cycle = "trending_tr"
    elif overlap > 0.80 and dojis >= 0.70 and efficiency < 0.12:
        cycle = "extreme_tr"
    else:
        cycle = "trading_range"
    ema_slope = None
    ema = getattr(getattr(clean, "indicators", None), "ema20", None)
    atr = getattr(getattr(clean, "indicators", None), "atr14", None)
    if ema and atr:
        valid = [v for v in ema if v == v]
        if len(valid) >= 2 and atr and atr[0] == atr[0] and atr[0] > 0:
            ema_slope = round((valid[0] - valid[-1]) / (atr[0] * max(len(valid) - 1, 1)), 4)
    return cycle, {"closed_bars": len(bars), "efficiency": round(efficiency, 3),
        "overlap": round(overlap, 3), "doji_ratio": round(dojis, 3),
        "strong_run": strong_run, "micro_run": micro_run, "ordered_swing_groups": groups,
        "parallel": parallel, "pullback_ratio": round(depth, 3) if depth is not None else None,
        "net_change": round(bars[0].close - bars[-1].close, 6),
        "ema20_slope_atr_per_bar": ema_slope}


def fill_cycle_probabilities(prediction: dict[str, Any], *, frame: Any = None,
                             stage1_json: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return owned forecast, retaining the model's direction/text unchanged."""
    out = copy.deepcopy(prediction)
    if frame is not None:
        current, evidence = _structure(frame)
    else:
        current = str((stage1_json or {}).get("cycle_position") or "unknown")
        current = current if current in TRANSITIONS else "unknown"
        evidence = {"note": "无K线快照，使用已有周期/保守先验"}
    weights = dict(zip(KEYS, TRANSITIONS[current]))
    notes = []
    if evidence.get("overlap", 0) > 0.65 and current not in ("trading_range", "extreme_tr"):
        weights["trading_range"] += 12
        weights["trending_tr"] += 8
        notes.append("重叠增多，增加减速和区间权重")
    if evidence.get("strong_run", 0) >= 6:
        weights["spike"] *= 0.5
        weights["normal_channel"] += 10
        weights["trading_range"] += 10
        notes.append("连续强棒较多，增加后续整理权重，不据此确认高潮")
    probs = percentages(weights)
    out.update(probabilities=probs, cycle=max(probs, key=probs.get),
        probability_source=SOURCE, probability_version=VERSION,
        probability_notice="程序规则估算，非模型概率、非统计校准，不代表交易胜率",
        probability_current_cycle=current, probability_evidence=evidence,
        probability_basis="当前参考状态：" + CYCLE_POSITION_ZH.get(current, "未知") + "；按延续及相邻周期转换分配权重。" + "；".join(notes),
        probability_rule_sources=copy.deepcopy(SOURCES))
    # A model may decline to give direction while local state weights still
    # exist. Keep that uncertainty, but let the UI display the separate weights.
    out["probability_unpredictable"] = bool(prediction.get("unpredictable"))
    return out


# ── Local forecast used when the model declines to give a usable answer ──────
# Rules follow prompt_engineering/市场诊断框架.txt: trend-side cycles keep the
# trend direction; ranges carry no directional edge.
TREND_CYCLES = frozenset({
    "spike", "micro_channel", "tight_channel",
    "normal_channel", "broad_channel", "trending_tr",
})
_RANGE_CYCLES = frozenset({"trading_range", "extreme_tr"})
DIRECTION_ZH = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}


def _derived_direction(cycle: str, evidence: dict[str, Any]) -> str:
    if cycle in _RANGE_CYCLES or cycle == "unknown":
        return "neutral"
    net = float(evidence.get("net_change") or 0.0)
    slope = evidence.get("ema20_slope_atr_per_bar")
    votes = 0
    if net > 0:
        votes += 1
    elif net < 0:
        votes -= 1
    if isinstance(slope, (int, float)) and not isinstance(slope, bool):
        if slope > 0.01:
            votes += 1
        elif slope < -0.01:
            votes -= 1
    if votes > 0:
        return "bullish"
    if votes < 0:
        return "bearish"
    # Net move and EMA disagree, or both flat: no directional edge is claimed.
    return "neutral"


def _evidence_phrase(cycle: str, evidence: dict[str, Any]) -> str:
    parts = [f"当前结构判为{CYCLE_POSITION_ZH.get(cycle, '未知状态')}"]
    if evidence.get("efficiency") is not None:
        parts.append(f"近{evidence.get('closed_bars', 40)}根效率约{evidence.get('efficiency')}")
    if evidence.get("overlap") is not None:
        parts.append(f"K线重叠约{evidence.get('overlap')}")
    slope = evidence.get("ema20_slope_atr_per_bar")
    if isinstance(slope, (int, float)) and not isinstance(slope, bool):
        trend = "向上" if slope > 0.01 else "向下" if slope < -0.01 else "走平"
        parts.append(f"20EMA{trend}")
    if evidence.get("pullback_ratio") is not None:
        parts.append(f"最近回撤约{evidence.get('pullback_ratio')}")
    return "，".join(parts)


def derive_forecast(frame: Any) -> dict[str, Any]:
    """Compute next-bar / next-cycle expectations from the project's PA rules."""
    current, evidence = _structure(frame)
    direction = _derived_direction(current, evidence)
    phrase = _evidence_phrase(current, evidence)
    if direction == "neutral":
        bar_text = (f"{phrase}。按教材的区间判定，方向偏好很弱、边界内来回震荡的概率更高，"
                    "因此下一根K线按震荡处理：不追单，等价格走到区间边界或出现有效突破后再评估。")
        cycle_text = (f"{phrase}。区间状态通常延续，只有收盘有效突破边界并得到跟随确认后，"
                      "才把下一周期视为新趋势；在此之前维持区间看待。")
    else:
        trend_zh = "上涨" if direction == "bullish" else "下跌"
        bar_text = (f"{phrase}。按教材的通道/趋势规则，趋势中第一次回撤多为顺势机会而不是反转，"
                    f"因此下一根K线倾向继续{trend_zh}，回撤视为{trend_zh}方向的机会。")
        cycle_text = (f"{phrase}。趋势通常由尖峰进入通道、再逐步变宽或转为区间，"
                      f"方向以{trend_zh}为主；若出现连续反向收盘并跌破结构极值，才重新评估是否转为区间或反转。")
    probs = {"neutral": 40, "bearish": 30, "bullish": 30}
    if direction == "bullish":
        probs = {"bullish": 45, "bearish": 30, "neutral": 25}
    elif direction == "bearish":
        probs = {"bearish": 45, "bullish": 30, "neutral": 25}
    return {
        "current_cycle": current,
        "evidence": evidence,
        "next_bar_prediction": {
            "direction": direction, "reasoning": bar_text,
            "probabilities": probs, "unpredictable": False,
            "direction_source": SOURCE,
        },
        "next_cycle_prediction": {
            "direction": direction, "reasoning": cycle_text,
            "unpredictable": False, "direction_source": SOURCE,
        },
    }
