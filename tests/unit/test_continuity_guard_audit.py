"""连续性守卫的审计信息测试。

背景（真缺陷）：`apply_continuity_guard` 判定违规时会把订单改写为「不下单」并**清空**
全部价格字段。这在产品上是对的（拒单），但原始方案随之永久丢失——事后无法回答
"守卫这次拦得对不对、模型本来想做什么"。本测试锁定修复后的行为：

1. 改写成「不下单」的价格字段清空（原行为不能变）；
2. 同时在 stage2 根节点保留 `continuity_guard.original_plan`（新增的审计信息）；
3. 不违规时不产生该字段（不会污染正常记录）。
"""
from __future__ import annotations

from pa_agent.ai.decision_continuity import apply_continuity_guard

PLAN = {
    "order_type": "限价单",
    "order_direction": "做多",
    "entry_price": 80000.0,
    "stop_loss_price": 79800.0,
    "take_profit_price": 80400.0,
    "take_profit_price_2": 80800.0,
    "estimated_win_rate": 55.0,
    "reasoning": "区间下边界限价做多",
}
# direction=neutral 且 §2.4 非 Always In ⇒ 禁止无强信号的方向性方案（最简违规路径）
VIOLATING_CTX = {"direction": "neutral", "always_in_branch": None, "has_previous_plan": False}


def _stage2() -> dict:
    return {"decision": dict(PLAN), "terminal": {"outcome": "trade", "node_id": "11.1"}}


def test_violation_rewrites_to_no_order_and_clears_prices() -> None:
    out = apply_continuity_guard(_stage2(), VIOLATING_CTX)
    dec = out["decision"]
    assert dec["order_type"] == "不下单"
    for k in ("order_direction", "entry_price", "stop_loss_price", "take_profit_price"):
        assert dec[k] is None, f"{k} 应被清空"
    assert out["terminal"]["outcome"] == "wait"
    assert out["terminal"]["node_id"] == "continuity"


def test_violation_preserves_original_plan_for_audit() -> None:
    out = apply_continuity_guard(_stage2(), VIOLATING_CTX)
    cg = out["continuity_guard"]
    assert cg["applied"] is True
    assert "neutral" in cg["reason"]
    # 原始方案必须完整保留（这是本次修复的核心）
    assert cg["original_plan"]["order_direction"] == "做多"
    assert cg["original_plan"]["entry_price"] == 80000.0
    assert cg["original_plan"]["stop_loss_price"] == 79800.0
    assert cg["original_plan"]["take_profit_price"] == 80400.0
    assert cg["original_plan"]["estimated_win_rate"] == 55.0


def test_no_violation_leaves_no_audit_field() -> None:
    ok_ctx = {"direction": "bullish", "always_in_branch": "AIL", "has_previous_plan": False}
    out = apply_continuity_guard(_stage2(), ok_ctx)
    assert "continuity_guard" not in out
    assert out["decision"]["order_type"] == "限价单"
    assert out["decision"]["entry_price"] == 80000.0


def test_original_plan_omits_none_fields() -> None:
    stage2 = _stage2()
    stage2["decision"]["take_profit_price_2"] = None
    out = apply_continuity_guard(stage2, VIOLATING_CTX)
    assert "take_profit_price_2" not in out["continuity_guard"]["original_plan"]
