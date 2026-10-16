"""成本可行性 + 盈亏平衡胜率 + 方程容差（含实测记录回归）。

实测来源：2026-10-15 BTCUSDT 15m 真实分析记录
（entry 81071.21 / SL 81397.05 / TP1 80745.37 / TP2 80500.01，自评胜率 50%）。
"""
from __future__ import annotations

import pytest

from pa_agent.ai.json_validator import JsonValidator
from pa_agent.config.settings import ValidationSettings
from pa_agent.util.trade_metrics import (
    breakeven_win_rate_pct,
    cost_viability,
    passes_trader_equation,
    round_trip_cost_price,
    validate_cost_viability,
)

#: 实测记录的关键价格
REAL_DECISION = {
    "order_direction": "做空",
    "order_type": "限价单",
    "entry_price": 81071.21,
    "stop_loss_price": 81397.05,
    "take_profit_price": 80745.37,
    "take_profit_price_2": 80500.01,
    "estimated_win_rate": 50,
}


# --------------------------------------------------------------- 方程容差
def test_breakeven_no_longer_passes_on_float_noise() -> None:
    """风险 = 止盈 = 325.84（零期望）时，方程不得因浮点噪声判为通过。"""
    risk = 325.8399999999965  # 实测值：risk 与 reward 只差 1e-11
    reward = 325.84000000001106
    assert not passes_trader_equation(50, risk, reward)
    # 同一组数字在修复前是 True —— 用不带容差的原始比较复现旧行为
    assert 0.5 * reward > 0.5 * risk


def test_equation_unchanged_for_clear_cases() -> None:
    assert passes_trader_equation(70, risk=100.0, reward=200.0)
    assert not passes_trader_equation(30, risk=100.0, reward=200.0)
    assert not passes_trader_equation(50, risk=100.0, reward=100.0)


def test_equation_is_cost_aware() -> None:
    """R:R=1 且胜率 50% 的方案，加上成本后必须失败。"""
    assert passes_trader_equation(50, risk=100.0, reward=100.0, cost=0.0) is False
    # 胜率 70%、风险 100 / 止盈 200：成本 10 时仍通过，成本 120 时失败
    assert passes_trader_equation(70, risk=100.0, reward=200.0, cost=10.0)
    assert not passes_trader_equation(70, risk=100.0, reward=200.0, cost=120.0)


def test_cost_price_conversion() -> None:
    assert round_trip_cost_price(1000.0, 0.30) == pytest.approx(3.0)
    assert round_trip_cost_price(None, 0.30) == 0.0


# --------------------------------------------------------------- 盈亏平衡
def test_breakeven_formula() -> None:
    # 风险 1% / 止盈 1% / 成本 0.3% -> 1.3/2 = 65%
    assert breakeven_win_rate_pct(1.0, 1.0, 0.3) == pytest.approx(65.0)
    # 风险 1% / 止盈 2% / 成本 0.3% -> 1.3/3 = 43.33%
    assert breakeven_win_rate_pct(1.0, 2.0, 0.3) == pytest.approx(43.3333, abs=1e-3)
    # 无成本时就是 1/(1+RR)
    assert breakeven_win_rate_pct(1.0, 2.0, 0.0) == pytest.approx(33.3333, abs=1e-3)


# --------------------------------------------------------------- 实测记录
def test_real_record_uses_tp2_and_flags_cost() -> None:
    v = cost_viability(REAL_DECISION, round_trip_cost_pct=0.30)
    assert v is not None
    # 用更远的目标（TP2）时：止盈 571.2 / 成本 243.2 ≈ 2.35 倍，仍需 63.4% 胜率
    assert float(v["reward_cost_ratio"]) == pytest.approx(2.349, abs=0.01)
    assert float(v["breakeven_win_rate_pct"]) == pytest.approx(63.44, abs=0.05)
    assert float(v["win_rate_pct"]) == pytest.approx(50.0)
    assert "止盈距离" in str(v["text"])


def test_real_record_tp1_only_is_absurd() -> None:
    """只看第一目标（TP1）时，成本占止盈 75%，需要 87% 胜率。"""
    d = dict(REAL_DECISION)
    d.pop("take_profit_price_2")
    v = cost_viability(d, round_trip_cost_pct=0.30)
    assert v is not None
    assert float(v["reward_cost_ratio"]) == pytest.approx(1.340, abs=0.01)
    assert float(v["breakeven_win_rate_pct"]) == pytest.approx(87.32, abs=0.05)
    assert float(v["cost_share_of_reward_pct"]) == pytest.approx(74.6, abs=0.2)


def test_cost_viability_none_without_prices() -> None:
    assert cost_viability({"entry_price": 100}) is None
    assert cost_viability({"entry_price": 100, "stop_loss_price": 100, "take_profit_price": 100}) is None


# --------------------------------------------------------------- 校验拦截
def test_validate_blocks_target_below_cost_multiple() -> None:
    """两条规则同时命中：止盈相对成本太小 + 自评胜率低于扣成本门槛。"""
    msgs = validate_cost_viability(REAL_DECISION)
    assert len(msgs) == 2
    assert "2.35×" in msgs[0] and "63.4%" in msgs[0]
    assert "estimated_win_rate 50.0%" in msgs[1]
    assert "13.4 points" in msgs[1]


def test_win_rate_gap_rule_fires_even_when_ratio_is_fine() -> None:
    """止盈/成本 5 倍（达标）但自评胜率低于盈亏平衡 → 仍要拒。"""
    plan = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.0,
        "stop_loss_price": 99.0,
        "take_profit_price": 101.5,
        "estimated_win_rate": 45,
    }
    # 风险 1% / 止盈 1.5% / 成本 0.3% → 倍数 5.0、盈亏平衡 (1+0.3)/(1+1.5)=52%
    msgs = validate_cost_viability(plan)
    assert len(msgs) == 1
    assert "estimated_win_rate 45.0%" in msgs[0] and "52.0%" in msgs[0]


def test_win_rate_gap_ignores_small_deficit_within_margin() -> None:
    """缺口 4pt（< 默认 5pt 余量）时不拒——避免在盈亏平衡点附近误杀。"""
    plan = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.0,
        "stop_loss_price": 99.0,
        "take_profit_price": 101.5,
        "estimated_win_rate": 48,
    }
    assert validate_cost_viability(plan) == []


def test_win_rate_gap_skipped_when_win_rate_absent() -> None:
    plan = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.0,
        "stop_loss_price": 99.0,
        "take_profit_price": 101.5,
    }
    assert validate_cost_viability(plan) == []


def test_validate_allows_wide_target() -> None:
    wide = dict(REAL_DECISION, stop_loss_price=81000.0, take_profit_price_2=79000.0)
    assert validate_cost_viability(wide) == []


def test_validate_skips_non_order_decisions() -> None:
    assert validate_cost_viability({"order_type": "不下单"}) == []
    assert validate_cost_viability({**REAL_DECISION, "order_type": "不下单"}) == []


def test_validator_gate_is_off_by_default() -> None:
    validator = JsonValidator(ValidationSettings())
    assert validator._check_cost_viability({"decision": REAL_DECISION}) == []


def test_validator_no_longer_blocks_on_cost() -> None:
    """语义已变更：成本可行性不再作为校验错误（否则整轮分析会以 validation_error 中止，
    用户看到"分析失败"）。现在由 stage2_normalizer.apply_cost_guard() 降级为「不下单」。

    数学本身仍由 validate_cost_viability 提供（见上面各用例），这里只锁定"不再阻断"。
    """
    validator = JsonValidator(ValidationSettings(cost_viability_check=True))
    assert validator._check_cost_viability({"decision": REAL_DECISION}) == []
    # 原始数学仍然判它不达标
    assert len(validate_cost_viability(REAL_DECISION)) == 2


def test_validator_gate_respects_custom_threshold() -> None:
    validator = JsonValidator(
        ValidationSettings(
            cost_viability_check=True,
            min_reward_cost_ratio=2.0,
            assumed_round_trip_cost_pct=0.30,
        )
    )
    # 2.35× > 2.0× 且自评胜率 70% > 所需 63.4% → 按数学本应通过；校验器一律不阻断
    decision = dict(REAL_DECISION, estimated_win_rate=70)
    assert validator._check_cost_viability({"decision": decision}) == []
    assert validate_cost_viability(decision, min_reward_cost_ratio=2.0) == []
    # 用默认门槛（3.0×）时数学仍判不达标 —— 说明只是"不阻断"，判定标准没被削弱
    assert len(validate_cost_viability(decision)) == 1


# ------------------------------------------------- TP1 盈亏比上限放宽止损的后果
def test_tp1_rr_cap_widening_destroys_edge() -> None:
    """程序为满足 TP1 盈亏比上限而放宽止损，会把正期望变成零期望。

    实测：模型原始止损 81191（风险 119.79 / 回报 325.84，50% 胜率下期望 +103 点）；
    程序放宽止损到 81397.05 后风险 == 回报，50% 胜率期望恰好为 0 —— 修复前靠浮点噪声
    判为「方程通过」并被当作可执行方案。
    """
    from pa_agent.util.trade_metrics import compute_risk_reward, widen_stop_for_tp1_rr_cap

    entry, tp1, model_stop = 81071.21, 80745.37, 81191.0
    orig = compute_risk_reward(entry, tp1, model_stop, "做空")
    assert orig is not None
    assert passes_trader_equation(50, float(orig["risk"]), float(orig["reward"])) is True

    widened = widen_stop_for_tp1_rr_cap(entry, tp1, model_stop, "做空")
    assert widened is not None and widened > model_stop  # 做空：止损上移
    after = compute_risk_reward(entry, tp1, widened, "做空")
    assert after is not None
    # 放宽后风险 == 回报（构造上如此），零期望 → 必须判为不通过
    assert float(after["risk"]) == pytest.approx(float(after["reward"]), rel=1e-6)
    assert passes_trader_equation(50, float(after["risk"]), float(after["reward"])) is False
    # 叠加成本后，该结构需要 87% 以上的胜率
    cost = round_trip_cost_price(entry, 0.30)
    assert breakeven_win_rate_pct(float(after["risk"]), float(after["reward"]), cost) > 85.0
