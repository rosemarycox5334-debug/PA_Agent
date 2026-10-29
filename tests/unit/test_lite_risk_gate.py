"""Lite route renders the model plan without RR / trader-equation gating."""
import copy
import json

import pytest

from pa_agent.ai.json_validator import JsonValidator, Ok
from pa_agent.ai.decision_display import reconcile_lite_decision
from pa_agent.util.trade_metrics import validate_order_trade_metrics
from tests.integration.conftest import make_frame
from tests.unit.test_glm_flash_lite import payload


def _rr_failing_plan() -> dict:
    p = payload()
    p["decision"].update(order_type="限价单", order_direction="做多", entry_price=4043.17,
        stop_loss_price=4039.795, take_profit_price=4050.0, take_profit_price_2=None,
        estimated_win_rate=None,
        reasoning="K1收盘价处于近期高位，做多，止损在K1最低价附近，目标在收盘价上方。")
    return p


def test_plan_really_fails_standard_metrics():
    """Guard: the fixture is genuinely non-compliant for the normal route."""
    d = _rr_failing_plan()["decision"]
    assert validate_order_trade_metrics(d, kline_frame=make_frame())


def test_lite_route_keeps_trade_plan():
    p = _rr_failing_plan()
    result = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(result, Ok), result
    d = result.obj["decision"]
    assert d["order_type"] == "限价单"
    assert d["order_direction"] == "做多"
    assert d["entry_price"] == 4043.17
    assert d["stop_loss_price"] == 4039.795
    assert d["take_profit_price"] == 4050.0
    assert "当前不下单" not in d["reasoning"]
    assert d["reasoning"].startswith("K1收盘价")
    assert not result.obj.get("decision_adjustment")


def test_geometry_and_finiteness_still_required():
    for bad in (None, float("nan"), -1, True):
        p = payload()
        p["decision"].update(order_type="限价单", order_direction="做多", entry_price=4043.17,
            stop_loss_price=bad, take_profit_price=4050.0, take_profit_price_2=None)
        result = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
            kline_frame=make_frame())
        assert isinstance(result, Ok), result
        assert result.obj["decision"]["order_type"] == "不下单"
    p = payload()
    p["decision"].update(order_type="限价单", order_direction="做多", entry_price=4000,
        stop_loss_price=4010, take_profit_price=4050.0, take_profit_price_2=None)
    result = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert result.obj["decision"]["order_type"] == "不下单"


def test_main_route_still_coerces_failing_plan():
    from pa_agent.ai.json_validator import ValidationError
    p = _rr_failing_plan()
    result = JsonValidator().validate("stage2", json.dumps(p), kline_frame=make_frame())
    if isinstance(result, ValidationError):
        return  # strict schema/coherence rejects it outright
    assert result.obj["decision"]["order_type"] != "限价单"


def test_missing_tp1_is_filled_with_1r_instead_of_rejected():
    """Lite model gave entry+stop but no take-profit: fill 1R, do not go flat."""
    p = payload()
    p["decision"].update(order_type="限价单", order_direction="做空", entry_price=4027.545,
        stop_loss_price=4030.59, take_profit_price=None, take_profit_price_2=None,
        reasoning="从K1的收盘价4026.235可以看出，选择做空，止损4030.59，但未设置具体止盈价。")
    result = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(result, Ok), result
    d = result.obj["decision"]
    assert d["order_type"] == "限价单"
    assert d["target_1_source"] == "rr_floor_local_adjustment"
    # short: 1R below entry = 4027.545 - (4030.59 - 4027.545)
    assert d["take_profit_price"] == pytest.approx(4024.5, abs=1e-6)
    assert d["take_profit_price_2"] == d["take_profit_price"]
    assert not result.obj.get("local_plan_guard")
    # the model's own raw payload stays untouched
    assert result.obj["model_decision"]["take_profit_price"] is None


def test_missing_tp1_without_valid_stop_still_rejected():
    p = payload()
    p["decision"].update(order_type="限价单", order_direction="做多", entry_price=4027.545,
        stop_loss_price=None, take_profit_price=None, take_profit_price_2=None)
    result = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(result, Ok), result
    assert result.obj["decision"]["order_type"] == "不下单"
    assert result.obj["local_plan_guard"] == "交易计划价格缺失或无效"


def test_panel_shows_neutral_rr_without_failure_claim():
    from PyQt6.QtWidgets import QApplication
    from pa_agent.gui.decision_panel import DecisionPanel
    app = QApplication.instance() or QApplication([])
    panel = DecisionPanel()
    d = _rr_failing_plan()["decision"]
    d.update(trace_source="local_rules", estimated_win_rate=50)
    panel.set_decision(d)
    text = panel._rr_inline_label.text()
    assert "盈亏比" in text
    assert "方程不通过" not in text
    assert "#8b949e" in panel._rr_inline_label.styleSheet()
    panel.clear()
    panel.close()
    app.processEvents()
