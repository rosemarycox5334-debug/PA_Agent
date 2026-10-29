"""Small GLM-4-flash contract; all displayed paths are local illustrations.

The illustration is added AFTER risk normalization and must never be fed back
into the order checks as evidence that a market/entry condition passed.
"""
from __future__ import annotations

import copy
import json
import math
from typing import Any


NOTICE = "本地规则示意（非模型推理，非逐节点验证）"


def is_glm_flash(provider: Any) -> bool:
    # Exact model match: do not change GLM-4-flashx / GLM-5 / other routes.
    return str(getattr(provider, "model", "") or "").strip().lower() == "glm-4-flash"


def local_diagnosis(frame: Any) -> dict[str, Any]:
    """Compatibility context only, not an AI diagnosis or a passed safety gate."""
    from pa_agent.ai.local_cycle_forecast import classify_cycle, derive_forecast

    bars = [b for b in getattr(frame, "bars", ()) if b.closed and b.seq > 0]
    bars.sort(key=lambda b: b.seq)
    # Same rule set as the forecast cards, so the diagnosis chip and the
    # prediction panel can never point in opposite directions.
    try:
        direction = derive_forecast(frame)["next_cycle_prediction"]["direction"]
    except Exception:  # noqa: BLE001
        delta = bars[0].close - bars[-1].close if len(bars) > 1 else 0
        direction = "bullish" if delta > 0 else "bearish" if delta < 0 else "neutral"
    return {
        "cycle_position": classify_cycle(frame), "direction": direction,
        "diagnosis_confidence": 0, "market_phase": "stable",
        "detected_patterns": [], "key_signals": [],
        "htf_context": NOTICE, "entry_setup": "由决策阶段独立判断",
        "strategy_files_needed": [], "bar_by_bar_summary": [],
        "gate_trace": [], "gate_result": "proceed",
        "trace_source": "local_rules", "diagnosis_source": "local_compatibility",
        "diagnosis_notice": NOTICE + "；proceed仅表示继续请求决策，不代表风控通过",
    }


def build_messages(frame: Any, stage: str) -> list[dict[str, str]]:
    if stage == "stage1":
        return [{"role": "system", "content": "备用通道：阶段一由本地代码提供兼容上下文，不请求模型。"}]
    bars = [
        {"bar": f"K{b.seq}", "open": b.open, "high": b.high,
         "low": b.low, "close": b.close, "volume": b.volume}
        for b in getattr(frame, "bars", ()) if b.closed and b.seq > 0
    ]
    # Chronological order puts the current bar next to the answer, rather than
    # letting a small model confuse the last (oldest) array item with 'now'.
    bars.sort(key=lambda bar: int(bar["bar"][1:]), reverse=True)
    context = {"symbol": getattr(frame, "symbol", ""),
               "timeframe": getattr(frame, "timeframe", ""), "bars_oldest_to_newest": bars,
               "latest_closed_bar": bars[-1] if bars else None}
    system = (
        "你是价格行为分析助手。仅依据给定已收盘K线，返回一个JSON对象，使用中文解释。"
        "只输出决策和未来走势预期，不输出决策树、逐节点推理或其他诊断。"
        "K编号越小越新，K1为最新K线（若K1未收盘则不提供），编号越大越早；以最新已收盘K线为当前时点。"
        "顶层仅三个字段：decision、next_bar_prediction、next_cycle_prediction。"
        "decision包含order_type(限价单/突破单/市价单/不下单)、order_direction(做多/做空/null)、"
        "entry_price、stop_loss_price、take_profit_price、take_profit_price_2、reasoning(280字内)。"
        "没有可靠入场则不下单，方向及所有价格填null；有入场必须提供完整合理入场/止损/目标三价，第二目标可为null。"
        "做多stop<entry<target，做空target<entry<stop。"
        "next_bar_prediction和next_cycle_prediction各包含direction(bullish/bearish/neutral)、"
        "reasoning(未来怎样走、什么条件下失效)。无法判断时direction=null、unpredictable=true。"
        "不要编造概率、置信度或未提供的行情。"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, allow_nan=False)}]


def check_contract(obj: dict[str, Any]) -> str | None:
    decision = obj.get("decision")
    if not isinstance(decision, dict):
        return "备用通道未返回有效决策"
    if decision.get("order_type") not in ("限价单", "突破单", "市价单", "不下单"):
        return "备用通道决策的下单类型无效"
    if not isinstance(decision.get("reasoning"), str) or not decision["reasoning"].strip():
        return "备用通道未返回决策说明"
    for key in ("next_bar_prediction", "next_cycle_prediction"):
        pred = obj.get(key)
        if not isinstance(pred, dict) or not isinstance(pred.get("reasoning"), str) or not pred["reasoning"].strip():
            return "备用通道未返回完整未来走势预期"
        if pred.get("direction") not in ("bullish", "bearish", "neutral", None):
            return "备用通道走势预期方向无效"
    return None


def _valid_price(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def fill_missing_tp1_from_rr(decision: dict[str, Any]) -> bool:
    """Give a missing TP1 a 1R target when entry and stop are otherwise valid.

    The lite model sometimes returns an entry and a stop but no take-profit.
    Rather than discard the plan, widen a target outward from the risk distance
    (1R, matching MIN_RISK_REWARD_RATIO) and tag the source so the invented
    level is visible instead of being passed off as the model's own number.
    """
    if decision.get("order_type") == "不下单":
        return False
    if _valid_price(decision.get("take_profit_price")):
        return False
    entry = decision.get("entry_price")
    stop = decision.get("stop_loss_price")
    if not (_valid_price(entry) and _valid_price(stop)):
        return False
    risk = abs(float(entry) - float(stop))
    if risk <= 0:
        return False
    text = str(decision.get("order_direction") or "").strip().lower()
    if "多" in text or text in ("long", "buy", "bull"):
        long = True
    elif "空" in text or text in ("short", "sell", "bear"):
        long = False
    else:
        return False
    target = float(entry) + risk if long else float(entry) - risk
    decision["take_profit_price"] = round(target, 6)
    decision["target_1_source"] = "rr_floor_local_adjustment"
    tp2 = decision.get("take_profit_price_2")
    if not (_valid_price(tp2) and ((long and float(tp2) >= target) or (not long and float(tp2) <= target))):
        decision["take_profit_price_2"] = decision["take_profit_price"]
    return True


def plan_problem(decision: dict[str, Any]) -> str | None:
    if decision.get("order_type") == "不下单":
        return None
    prices = [decision.get(k) for k in ("entry_price", "stop_loss_price", "take_profit_price")]
    prices.append(decision.get("take_profit_price_2") if decision.get("take_profit_price_2") is not None else decision.get("take_profit_price"))
    if any(isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or p <= 0 for p in prices):
        return "交易计划价格缺失或无效"
    entry, stop, target, target2 = prices
    direction = decision.get("order_direction")
    if not ((direction == "做多" and stop < entry < target <= target2)
            or (direction == "做空" and target2 <= target < entry < stop)):
        return "交易方向与止损止盈不一致"
    return None


def model_fields(obj: dict[str, Any]) -> dict[str, Any]:
    """Ignore even malformed model trees; they have no part in this contract."""
    out = {k: copy.deepcopy(obj[k]) for k in
           ("decision", "next_bar_prediction", "next_cycle_prediction", "lite_repaired") if k in obj}
    out["analysis_contract"] = "glm_flash_lite"
    decision = out.get("decision")
    model_original = copy.deepcopy(decision) if isinstance(decision, dict) else None
    if isinstance(decision, dict):
        decision.pop("target_2_source", None)
        # A missing TP1 with a valid entry/stop is recoverable on this route:
        # fill a 1R target instead of discarding the whole plan.
        if fill_missing_tp1_from_rr(decision):
            out["target_1_source"] = decision.get("target_1_source")
    problem = plan_problem(decision) if isinstance(decision, dict) else None
    if problem:
        out["model_decision"] = model_original
        out["local_plan_guard"] = problem
        decision["order_type"] = "不下单"
        decision["reasoning"] = f"本地检查：{problem}，已改为观望。模型意见：" + decision.get("reasoning", "")
        for key in ("order_direction", "entry_price", "stop_loss_price", "take_profit_price", "take_profit_price_2"):
            decision[key] = None
    if isinstance(decision, dict) and decision.get("order_type") != "不下单" and decision.get("take_profit_price_2") is None:
        # One-target plans remain one-target plans; duplicate the existing
        # target only for compatibility with the legacy two-target renderer.
        decision["take_profit_price_2"] = decision.get("take_profit_price")
        decision["target_2_source"] = "same_as_target_1_compatibility"
    # Unknown probabilities remain unknown rather than inventing certainty.
    for key in ("next_bar_prediction", "next_cycle_prediction"):
        pred = out.get(key)
        if isinstance(pred, dict):
            pred.setdefault("probabilities", None)
            pred.setdefault("unpredictable", pred.get("direction") is None)
            pred.setdefault("features_used", ["kline_features"])
    return out


def add_local_tree(out: dict[str, Any]) -> dict[str, Any]:
    """Map final (post-risk-check) decision onto existing visualizer node IDs."""
    decision = out.get("decision") or {}
    trade = decision.get("order_type") in {"限价单", "突破单", "市价单"}
    reason = str(decision.get("reasoning") or "暂无说明")
    def node(node_id: str, question: str, answer: str, detail: str) -> dict[str, Any]:
        return {"node_id": node_id, "section": "本地规则示意", "question": question,
                "answer": answer, "reason": NOTICE + "：" + detail,
                "bar_range": "不适用", "skipped": False, "source": "local_rules"}
    terminal_id = "11.2" if trade else "9.0"
    out["decision_trace"] = [
        node("9.0", "最终决策是否包含入场计划？", "是" if trade else "否", reason),
    ]
    if trade:
        out["decision_trace"].append(node("11.2", "最终计划摘要（非条件逐项验证）", "是",
            f"{decision.get('order_direction')} / {decision.get('order_type')}；"
            f"入场={decision.get('entry_price')}，止损={decision.get('stop_loss_price')}，"
            f"目标={decision.get('take_profit_price')}"))
    out["terminal"] = {"node_id": terminal_id, "outcome": "trade" if trade else "wait",
                       "label": NOTICE + "：" + ("有入场计划" if trade else "观望")}
    out["trace_source"] = "local_rules"
    out["trace_notice"] = NOTICE
    out["local_gate_trace"] = []
    return out
