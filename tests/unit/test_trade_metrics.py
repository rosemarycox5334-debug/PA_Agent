"""Unit tests for trade_metrics helpers."""
from __future__ import annotations

from pa_agent.util.trade_metrics import (
    adjust_decision_stop_for_tp1_rr_cap,
    apply_decision_kelly_metrics,
    compute_decision_kelly,
    compute_kelly_fraction,
    compute_risk_reward,
    format_estimated_win_rate,
    format_estimated_win_rate_reasoning,
    is_long_direction,
    max_risk_reward_ratio,
    min_risk_reward_ratio,
    widen_stop_for_tp1_rr_cap,
)


def test_is_long_direction():
    assert is_long_direction("做多") is True
    assert is_long_direction("做空") is False


def test_compute_risk_reward_short():
    rr = compute_risk_reward(4541, 4510, 4553, "做空")
    assert rr is not None
    assert rr["risk"] == 12
    assert rr["reward"] == 31


def test_rr_bounds_all_stances_share_one_floor() -> None:
    for stance in ("conservative", "balanced", "aggressive", "extreme_aggressive", None):
        assert min_risk_reward_ratio(stance) == 1.0
    assert max_risk_reward_ratio() == 1.0


def test_widen_stop_for_tp1_rr_cap_long():
    # entry=100, tp=110, stop=99 -> risk=1, reward=10, RR=10
    widened = widen_stop_for_tp1_rr_cap(100.0, 110.0, 99.0, "做多", tick=0.01)
    assert widened is not None
    assert widened < 99.0
    rr = compute_risk_reward(100.0, 110.0, widened, "做多")
    assert rr is not None
    assert rr["ratio"] <= 1.0 + 1e-9


def test_widen_stop_for_tp1_rr_cap_short():
    widened = widen_stop_for_tp1_rr_cap(100.0, 90.0, 101.0, "做空", tick=0.01)
    assert widened is not None
    assert widened > 101.0
    rr = compute_risk_reward(100.0, 90.0, widened, "做空")
    assert rr is not None
    assert rr["ratio"] <= 1.0 + 1e-9


def test_adjust_decision_stop_for_tp1_rr_cap_mutates_decision():
    decision = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.0,
        "take_profit_price": 110.0,
        "stop_loss_price": 99.0,
    }
    assert adjust_decision_stop_for_tp1_rr_cap(decision, tick=0.01)
    rr = compute_risk_reward(
        decision["entry_price"],
        decision["take_profit_price"],
        decision["stop_loss_price"],
        decision["order_direction"],
    )
    assert rr is not None
    assert rr["ratio"] <= 1.0


def test_format_estimated_win_rate_from_model_field():
    decision = {
        "estimated_win_rate": 47,
        "estimated_win_rate_reasoning": "宽通道顺势，方程用 47%",
    }
    assert format_estimated_win_rate(decision) == "47%"
    assert "47" in format_estimated_win_rate_reasoning(decision)


def test_compute_kelly_fraction_for_even_money_trade():
    kelly = compute_kelly_fraction(60, 1.0)
    assert kelly is not None
    assert kelly["raw_fraction"] == 0.2
    assert kelly["full_percent"] == 20.0
    assert kelly["half_percent"] == 10.0
    assert kelly["positive_edge"] is True


def test_compute_kelly_fraction_floors_negative_edge_at_zero():
    kelly = compute_kelly_fraction(40, 1.0)
    assert kelly is not None
    assert kelly["raw_fraction"] == -0.2
    assert kelly["full_percent"] == 0.0
    assert kelly["half_percent"] == 0.0
    assert kelly["positive_edge"] is False


def test_apply_decision_kelly_metrics_uses_final_trade_geometry():
    decision = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.0,
        "take_profit_price": 110.0,
        "stop_loss_price": 90.0,
        "estimated_win_rate": 60,
    }
    assert compute_decision_kelly(decision) is not None
    assert apply_decision_kelly_metrics(decision) is True
    assert decision["kelly"]["full_percent"] == 20.0


def test_apply_decision_kelly_metrics_clears_no_order_value():
    decision = {"order_type": "不下单", "kelly": {"full_percent": 50}}
    assert apply_decision_kelly_metrics(decision) is True
    assert decision["kelly"] is None
