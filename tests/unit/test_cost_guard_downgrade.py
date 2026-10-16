"""成本闸门降级：不达标的方案 → 「不下单」，而不是让整轮分析中止。

背景（实验暴露的真缺陷）：早期把"成本不达标"做成**校验错误**，模型改不出方案时整轮
分析以 `validation_error` 中止——16 个真实回放窗口里有 1 个如此，用户看到的是
"分析失败"，而不是"这一单不做"。

修复后的语义：
1. `stage2_normalizer.apply_cost_guard()` 在归一化阶段把这类方案降级为「不下单」；
2. 保留被改写的原始方案（`cost_guard.original_plan`）供事后复盘；
3. 界面据此显示告警行；台账记录 suppressed_by_guard / suppressed_plan；
4. 数学不变，仍由 `util.trade_metrics.validate_cost_viability` 提供；
5. 校验器不再因为成本可行性而中断分析。
"""

from __future__ import annotations

import pytest

from pa_agent.ai.json_validator import JsonValidator
from pa_agent.ai.stage2_normalizer import apply_cost_guard
from pa_agent.config.settings import ValidationSettings

#: 复刻实验里那条被中止的方案：止盈距离只有成本的 1.29×，自评胜率 55%
#: （真实数据：entry 入口价附近，tp 距离 0.387%，成本 0.30%）
DOOMED = {
    "order_type": "限价单",
    "order_direction": "做多",
    "entry_price": 80000.0,
    "stop_loss_price": 79600.0,   # 风险 0.5%
    "take_profit_price": 80310.0,  # 回报 0.3875% → 1.29× 成本
    "estimated_win_rate": 55.0,
    "reasoning": "区间下边界限价做多",
}
#: 达标方案：止盈 1.2% → 4.0× 成本，胜率 55% ≥ 平衡点 (0.5+0.3)/(0.5+1.2)=47.1%
VIABLE = {
    "order_type": "限价单",
    "order_direction": "做多",
    "entry_price": 80000.0,
    "stop_loss_price": 79600.0,
    "take_profit_price": 80960.0,
    "estimated_win_rate": 55.0,
}


def _stage2(decision: dict) -> dict:
    return {"decision": dict(decision), "terminal": {"outcome": "trade", "node_id": "11.1"}}


def _settings(on: bool = True) -> ValidationSettings:
    return ValidationSettings(
        cost_viability_check=on,
        assumed_round_trip_cost_pct=0.30,
        min_reward_cost_ratio=3.0,
        min_win_rate_margin_pct=5.0,
    )


@pytest.mark.unit
def test_doomed_plan_is_downgraded_not_aborted() -> None:
    out = apply_cost_guard(_stage2(DOOMED), _settings())
    dec = out["decision"]
    assert dec["order_type"] == "不下单"
    for k in ("order_direction", "entry_price", "stop_loss_price", "take_profit_price"):
        assert dec[k] is None
    assert out["terminal"]["node_id"] == "cost"
    assert out["terminal"]["outcome"] == "wait"
    assert "【程序成本闸门】" in dec["reasoning"]
    # 原因里应包含可核对的数字（倍数 / 倍数门槛）
    assert "1.29" in out["cost_guard"]["reason"] and "3.00" in out["cost_guard"]["reason"]


@pytest.mark.unit
def test_original_plan_is_preserved_for_audit() -> None:
    out = apply_cost_guard(_stage2(DOOMED), _settings())
    plan = out["cost_guard"]["original_plan"]
    assert plan["order_direction"] == "做多"
    assert plan["entry_price"] == 80000.0
    assert plan["take_profit_price"] == 80310.0
    assert plan["estimated_win_rate"] == 55.0


@pytest.mark.unit
def test_viable_plan_untouched() -> None:
    out = apply_cost_guard(_stage2(VIABLE), _settings())
    assert out["decision"]["order_type"] == "限价单"
    assert out["decision"]["entry_price"] == 80000.0
    assert "cost_guard" not in out


@pytest.mark.unit
def test_guard_disabled_setting_is_noop() -> None:
    out = apply_cost_guard(_stage2(DOOMED), _settings(on=False))
    assert out["decision"]["order_type"] == "限价单"
    assert "cost_guard" not in out


@pytest.mark.unit
def test_already_no_order_is_noop() -> None:
    out = apply_cost_guard(_stage2({"order_type": "不下单"}), _settings())
    assert "cost_guard" not in out


@pytest.mark.unit
def test_validator_no_longer_blocks_on_cost() -> None:
    """校验器不得再因成本可行性把分析打断（否则用户看到"分析失败"）。"""
    v = JsonValidator(_settings())
    assert v._check_cost_viability(DOOMED) == []


@pytest.mark.unit
def test_gui_note_mentions_cost_guard() -> None:
    from pa_agent.gui.main_window import MainWindow

    out = apply_cost_guard(_stage2(DOOMED), _settings())
    note = MainWindow._continuity_override_note(out)
    assert note is not None
    assert "成本闸门" in note and "不下单" in note
    assert "1.29" in note  # 原因
    assert "做多" in note and "80310" in note  # 被改写的原方案
