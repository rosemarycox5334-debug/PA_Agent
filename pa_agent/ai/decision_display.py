"""Consistent final-decision text and compact, inspectable provenance."""
from __future__ import annotations

import copy
import re
from typing import Any


NOTICE = "本地规则示意（非模型推理，非逐节点验证）"
SOURCE_DETAILS = "决策与走势文字来自模型；最终计划经过应用检查。决策路径由应用根据最终计划绘制；周期概率为描述性规则权重，未经统计校准，并非模型原始概率或交易胜率。"


def public_text(value: Any) -> str:
    return str(value or "").replace(NOTICE + "：", "").replace(NOTICE, "").strip(" ：；")


def compact_trace(trace):
    return [{**item, "reason": public_text(item.get("reason")),
             "section": "决策" if item.get("source") == "local_rules" else item.get("section", "")}
            if isinstance(item, dict) else item for item in (trace or [])]


def compact_terminal(terminal):
    return {**terminal, "label": public_text(terminal.get("label"))} if isinstance(terminal, dict) else terminal


def guard_summary(reason: str) -> str:
    if "连续性" in reason or "§2.4" in reason:
        return "方向中性且未确认 Always In 方向，连续性规则不允许方向性入场"
    if "take_profit_price_2" in reason:
        return "止盈目标的先后关系未通过检查"
    if "risk_reward" in reason:
        return "盈亏比未达到当前设置要求"
    if "estimated_win_rate" in reason:
        return "缺少当前检查所需的胜率字段"
    if "trader equation" in reason:
        return "收益风险检查未通过"
    if "limit " in reason or "stale" in reason:
        return "挂单价与当前行情位置不匹配，需要重新确认"
    return public_text(reason) or "入场计划尚未满足当前检查条件"


def ensure_min_rr(decision: dict[str, Any], *, min_rr: float = 1.0) -> bool:
    """Lift the target so reward/risk reaches *min_rr* (long and short).

    Only widens the target outward; never moves entry or stop. Used for the
    lite route, where a sub-1R plan would otherwise be displayed as-is.
    """
    if decision.get("order_type") not in ("限价单", "突破单", "市价单"):
        return False
    try:
        entry = float(decision.get("entry_price"))
        stop = float(decision.get("stop_loss_price"))
        target = float(decision.get("take_profit_price"))
    except (TypeError, ValueError):
        return False
    risk = abs(entry - stop)
    if risk <= 0:
        return False
    reward = abs(target - entry)
    if reward + 1e-12 >= risk * min_rr:
        return False
    long = target > entry
    new_target = entry + risk * min_rr if long else entry - risk * min_rr
    decision["take_profit_price"] = round(new_target, 6)
    tp2 = decision.get("take_profit_price_2")
    try:
        tp2_val = float(tp2)
    except (TypeError, ValueError):
        tp2_val = None
    if tp2_val is not None:
        # Keep the second target beyond the (possibly widened) first one.
        decision["take_profit_price_2"] = round(
            max(tp2_val, new_target) if long else min(tp2_val, new_target), 6)
    decision["target_rr_source"] = "min_rr_1_0_local_adjustment"
    return True


def reconcile_lite_decision(out: dict[str, Any], original: dict[str, Any], *, frame=None, decision_stance=None) -> None:
    final = out.get("decision") or {}
    # The lite contract renders the model's plan without RR / trader-equation
    # gating (explicit user decision); only price finiteness and long/short
    # geometry are enforced earlier, in glm_flash_lite.plan_problem.
    if ensure_min_rr(final):
        out["decision_adjustment_tp"] = {
            "min_rr": 1.0,
            "from_take_profit": (original.get("take_profit_price")),
            "to_take_profit": final.get("take_profit_price"),
        }
    out["model_decision"] = copy.deepcopy(original)
    before, after = original.get("order_type"), final.get("order_type")
    if before == after:
        return
    adjustments = out.get("decision_adjustments") or []
    raw_reason = out.get("local_plan_guard") or (adjustments[-1].get("reason") if adjustments else "")
    if after == "不下单":
        explanation = guard_summary(str(raw_reason or "入场计划尚未满足当前检查条件"))
        final["reasoning"] = f"当前不下单：{explanation}。此前入场建议未生效，等待重新确认入场条件。"
        final["trade_confidence"] = 0
        final["trade_confidence_reasoning"] = "当前没有有效入场计划"
    else:
        explanation = "入场方式已按当前可用信息调整"
        final["reasoning"] = (
            f"当前方案：{after}{final.get('order_direction') or ''}。"
            f"入场价{final.get('entry_price')}，止损{final.get('stop_loss_price')}，"
            f"目标{final.get('take_profit_price')}。{explanation}。"
        )
    out["decision_adjustment"] = {"from": before, "to": after,
                                  "reason": explanation, "raw_reason": raw_reason}


_PLAN_WORDS = ("止损", "TP1", "TP2", "入场价", "目标位", "目标价", "作为入场")
_DIRECTION_WORDS = ("做多", "做空", "买入", "卖出", "限价", "突破单", "市价单", "多单", "空单")


def looks_like_trade_instructions(text: str) -> bool:
    """True when the text still describes a concrete entry plan."""
    if not text or "当前不下单" in text:
        return False
    if re.search(r"突破单做[多空]|限价单做[多空]|市价单做[多空]|作为入场价", text):
        return True
    return (any(w in text for w in _PLAN_WORDS)
            and any(w in text for w in _DIRECTION_WORDS))


def repair_legacy_display(out: dict[str, Any]) -> None:
    """Old saved records: remove stale instructions, never invent a cause."""
    if out.get("decision_adjustment"):
        return
    d = out.get("decision") or {}
    text = str(d.get("reasoning") or "")
    if d.get("order_type") == "不下单" and looks_like_trade_instructions(text):
        out.setdefault("model_decision", copy.deepcopy(d))
        d["reasoning"] = "当前不下单：此前入场建议未生效。该历史记录未保留具体调整原因，请重新分析确认入场条件。"
        d["trade_confidence"] = 0
        d["trade_confidence_reasoning"] = "当前没有有效入场计划"
        out["decision_adjustment"] = {"from": "历史入场建议", "to": "不下单", "reason": "旧记录未保存具体调整原因"}


def details_text(payload: dict[str, Any]) -> str:
    lines = [SOURCE_DETAILS]
    adjustment = payload.get("decision_adjustment") or {}
    if adjustment:
        lines.append(f"\n计划调整：{adjustment.get('from')} → {adjustment.get('to')}\n原因：{adjustment.get('reason')}")
    original = payload.get("model_decision") or {}
    if original.get("reasoning"):
        lines.append("\n原始文字（不等于最终入场指令）：\n" + str(original["reasoning"]))
    return "\n".join(lines)
