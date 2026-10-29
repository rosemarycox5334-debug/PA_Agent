"""Sub-1R targets are lifted to 1R on the lite route; other routes untouched."""
import copy
import json

import pytest

from pa_agent.ai.decision_display import ensure_min_rr
from pa_agent.ai.json_validator import JsonValidator, Ok
from pa_agent.ai.stage2_normalizer import normalize_stage2
from tests.integration.conftest import make_frame
from tests.unit.test_glm_flash_lite import payload


def _plan(direction="做多", entry=4048.7, stop=4045.4, tp=4050.0, tp2=None):
    return {"order_type": "限价单", "order_direction": direction,
            "entry_price": entry, "stop_loss_price": stop,
            "take_profit_price": tp, "take_profit_price_2": tp2,
            "reasoning": "模型原文"}


def test_long_target_lifted_to_one_r():
    d = _plan()
    assert ensure_min_rr(d)
    assert d["entry_price"] == 4048.7 and d["stop_loss_price"] == 4045.4
    assert d["take_profit_price"] == pytest.approx(4052.0)
    risk = d["entry_price"] - d["stop_loss_price"]
    assert d["take_profit_price"] - d["entry_price"] == pytest.approx(risk)


def test_short_target_lifted_to_one_r():
    d = _plan("做空", entry=4000.0, stop=4004.0, tp=3998.0)
    assert ensure_min_rr(d)
    assert d["take_profit_price"] == pytest.approx(3996.0)


@pytest.mark.parametrize("tp", [4052.0, 4060.0])
def test_compliant_target_untouched(tp):
    d = _plan(tp=tp)
    assert ensure_min_rr(d) is False
    assert d["take_profit_price"] == tp


def test_second_target_kept_beyond_first():
    d = _plan(tp=4050.0, tp2=4050.0)
    assert ensure_min_rr(d)
    assert d["take_profit_price_2"] == pytest.approx(d["take_profit_price"])
    d2 = _plan(tp=4050.0, tp2=4099.0)
    assert ensure_min_rr(d2)
    assert d2["take_profit_price_2"] == 4099.0


@pytest.mark.parametrize("bad", [None, 4048.7])
def test_no_or_zero_risk_untouched(bad):
    d = _plan(stop=bad)
    assert ensure_min_rr(d) is False


def test_no_order_untouched():
    d = {"order_type": "不下单", "entry_price": None, "stop_loss_price": None,
         "take_profit_price": None}
    assert ensure_min_rr(d) is False


def test_lite_validation_lifts_target_and_records_adjustment():
    p = payload()
    p["decision"].update(order_type="限价单", order_direction="做多", entry_price=4048.7,
        stop_loss_price=4045.4, take_profit_price=4050.0, take_profit_price_2=None)
    before = copy.deepcopy(p["decision"])
    result = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(result, Ok), result
    d = result.obj["decision"]
    assert d["order_type"] == "限价单"
    assert d["take_profit_price"] == pytest.approx(4052.0)
    assert d["reasoning"] == before["reasoning"]
    assert result.obj["model_decision"]["take_profit_price"] == 4050.0
    assert result.obj["decision_adjustment_tp"]["min_rr"] == 1.0


def test_main_route_does_not_apply_lift():
    p = payload()
    p["decision"].update(order_type="限价单", order_direction="做多", entry_price=4048.7,
        stop_loss_price=4045.4, take_profit_price=4050.0, take_profit_price_2=4060.0,
        estimated_win_rate=60)
    out = normalize_stage2(copy.deepcopy(p), kline_frame=make_frame())
    assert out["decision"].get("target_rr_source") != "min_rr_1_0_local_adjustment"


def test_ui_payload_and_panel_show_one_r_target():
    from PyQt6.QtWidgets import QApplication
    from pa_agent.gui.decision_panel import DecisionPanel
    from pa_agent.gui.stage2_payload import prepare_stage2_for_ui
    app = QApplication.instance() or QApplication([])
    legacy = {"trace_source": "local_rules", "decision": _plan()}
    shown = prepare_stage2_for_ui(legacy)
    assert shown["take_profit_price"] == pytest.approx(4052.0)
    assert legacy["decision"]["take_profit_price"] == 4050.0  # never mutated
    panel = DecisionPanel()
    panel.set_decision(shown)
    assert "1.00" in panel._rr_inline_label.text()
    assert "TP1  4052" in panel._tp_label.text()
    panel.close()
    app.processEvents()
