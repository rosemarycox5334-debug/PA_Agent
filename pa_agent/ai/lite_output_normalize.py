"""Format-only normalization for the exact GLM lite contract.

Unexpected model wording is repaired locally instead of surfacing an error:
direction aliases are normalized, and an unusable ("unpredictable") direction
is replaced by a rule-based projection computed from the K-line snapshot.
Nothing here relaxes the trading risk checks.
"""
from __future__ import annotations

import copy
from typing import Any

DIRECTIONS = {
    "bullish": "bullish", "bull": "bullish", "up": "bullish", "long": "bullish",
    "看涨": "bullish", "上涨": "bullish", "偏多": "bullish", "做多": "bullish",
    "bearish": "bearish", "bear": "bearish", "down": "bearish", "short": "bearish",
    "看跌": "bearish", "下跌": "bearish", "偏空": "bearish", "做空": "bearish",
    "neutral": "neutral", "sideways": "neutral", "range": "neutral",
    "中性": "neutral", "震荡": "neutral", "横盘": "neutral",
}
ORDER_TYPES = ("限价单", "突破单", "市价单", "不下单")


def _canonical_direction(raw: Any) -> str | None:
    return DIRECTIONS.get(raw.strip().lower()) if isinstance(raw, str) else None


DECLINE_PHRASES = ("无法预测", "难以预测", "不可预测", "无法判断", "难以判断",
                   "不确定", "说不准", "难以确定")


def declines_to_judge(text: Any) -> bool:
    return isinstance(text, str) and any(p in text for p in DECLINE_PHRASES)


def _declined(pred: dict[str, Any]) -> bool:
    flag = pred.get("unpredictable")
    return flag is True or (isinstance(flag, str) and flag.strip().lower() in ("true", "1", "yes"))


def normalize_lite_payload(obj: dict[str, Any], *, frame: Any = None) -> dict[str, Any]:
    """Repair the lite payload in place (copy) so the UI always has a result."""
    out = copy.deepcopy(obj)
    derived = None

    def _derived() -> dict[str, Any]:
        nonlocal derived
        if derived is None:
            from pa_agent.ai.local_cycle_forecast import derive_forecast
            derived = derive_forecast(frame) if frame is not None else {}
        return derived

    decision = out.get("decision")
    if not isinstance(decision, dict):
        decision = {"order_type": "不下单", "reasoning": "", "order_direction": None}
        out["decision"] = decision
        out["lite_repaired"] = "decision_missing"
    if isinstance(decision.get("order_direction"), str):
        raw = decision["order_direction"].strip()
        if raw.lower() in ("null", "none", "", "n/a"):
            decision["order_direction"] = None

    for key, label in (("next_bar_prediction", "下一根K线预期"),
                       ("next_cycle_prediction", "后续走势预期")):
        pred = out.get(key)
        if not isinstance(pred, dict):
            pred = {}
            out[key] = pred
            out.setdefault("lite_repaired", f"{key}_missing")
        raw_direction = pred.get("direction")
        direction = _canonical_direction(raw_direction)
        # A valid direction with confident wording is kept as-is. A model that
        # says it cannot judge, while giving no directional edge, is replaced
        # by the rule-based projection instead of showing "无法预测" to the user.
        unusable = (_declined(pred) or direction is None
                    or (direction == "neutral" and declines_to_judge(pred.get("reasoning"))))
        if unusable:
            if raw_direction is not None or pred.get("reasoning"):
                pred.setdefault("model_direction_raw", copy.deepcopy(raw_direction))
                pred.setdefault("model_reasoning", copy.deepcopy(pred.get("reasoning")))
                out.setdefault("lite_repaired", f"{key}_direction")
            computed = _derived().get(key) or {}
            if not computed:
                computed = {"direction": "neutral", "unpredictable": False,
                    "reasoning": (f"{label}：当前没有可用的模型说明与K线快照，"
                                  "按教材的区间思路先按震荡处理，等结构明确后再评估方向。"),
                    "direction_source": "program_rules"}
            pred.update(computed)
            pred["direction_filled_by"] = "program_rules"
        else:
            if raw_direction != direction:
                pred["direction_raw"] = copy.deepcopy(raw_direction)
                pred["direction_normalized_by"] = "lite_format_adapter"
            pred["direction"] = direction
            pred["unpredictable"] = False
        if not isinstance(pred.get("reasoning"), str) or not pred["reasoning"].strip():
            pred["reasoning"] = (f"{label}：当前没有可用的模型说明，"
                                 "已按教材的周期结构规则给出方向判断。")
            out.setdefault("lite_repaired", f"{key}_reasoning")

    if decision.get("order_type") not in ORDER_TYPES:
        decision["order_type"] = "不下单"
        out["lite_repaired"] = "order_type"
    if not isinstance(decision.get("reasoning"), str) or not decision["reasoning"].strip():
        decision["reasoning"] = "当前不下单：模型未给出可用说明，已按结构规则保持观望。"
        out.setdefault("lite_repaired", "decision_reasoning")
    return out


def visible_local_diagnosis_text(diagnosis: dict[str, Any] | None) -> str:
    """Readable stage-1 summary for the app-computed diagnosis (no raw JSON)."""
    diag = diagnosis if isinstance(diagnosis, dict) else {}
    cycle = str(diag.get("cycle_position") or "")
    if cycle.lower() in ("", "unknown", "none"):
        # Last-resort display value: the model never sees an undetermined state.
        cycle = "trading_range"
    direction = str(diag.get("direction") or "neutral")
    from pa_agent.ai.cycle_enums import format_cycle_position

    dir_zh = {"bullish": "偏多", "bearish": "偏空"}.get(direction, "中性")
    return (
        f"市场诊断：当前结构判为{format_cycle_position(cycle)}，方向{dir_zh}。\n\n"
        "判断依据来自已收盘K线的结构特征（效率、K线重叠、摆动高低点与回撤比例）。"
        "后续按该结构选择对应的价格行为逻辑：趋势/通道中回撤视为顺势机会，"
        "区间中等待边界或收盘有效突破，不在中部追单。"
    )


def visible_lite_result_text(stage2: dict[str, Any]) -> str:
    """Human-readable final display; diagnostics remain in raw/debug records."""
    from pa_agent.ai.decision_display import repair_legacy_display
    shown = copy.deepcopy(stage2)
    repair_legacy_display(shown)
    decision = shown.get("decision") or {}
    lines = [f"最终决策：{decision.get('order_type') or '待确认'}",
             f"分析说明：{decision.get('reasoning') or ''}"]
    for key, label in (("next_bar_prediction", "下一根K线预期"),
                       ("next_cycle_prediction", "后续走势预期")):
        pred = shown.get(key)
        if isinstance(pred, dict) and pred.get("reasoning"):
            lines.append(f"{label}：{pred['reasoning']}")
    return "\n\n".join(lines)


def is_local_stage1(diagnosis: Any = None, raw: Any = None) -> bool:
    return ((isinstance(raw, dict) and raw.get("source") == "local_compatibility")
            or (isinstance(diagnosis, dict) and diagnosis.get("diagnosis_source") == "local_compatibility"))
