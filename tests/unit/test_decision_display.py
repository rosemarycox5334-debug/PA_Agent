import copy
import json

from pa_agent.ai.decision_display import reconcile_lite_decision
from pa_agent.ai.json_validator import JsonValidator, Ok
from pa_agent.gui.stage2_payload import prepare_stage2_for_ui
from pa_agent.util.trade_metrics import validate_take_profit_2_geometry
from tests.unit.test_glm_flash_lite import payload


def test_single_target_exception_is_explicit_and_primary_remains_strict():
    d = {"order_direction": "做多", "entry_price": 100, "stop_loss_price": 98,
         "take_profit_price": 104, "take_profit_price_2": 104,
         "target_2_source": "same_as_target_1_compatibility"}
    assert validate_take_profit_2_geometry(d)
    assert not validate_take_profit_2_geometry(d, allow_single_target=True)
    d["stop_loss_price"] = 101
    assert validate_take_profit_2_geometry(d, allow_single_target=True)


def test_valid_lite_single_target_not_rejected_for_duplicate_target():
    p = payload()
    p["decision"].update(order_type="限价单", order_direction="做多", entry_price=2000,
        stop_loss_price=1980, take_profit_price=2040, take_profit_price_2=None,
        estimated_win_rate=60)
    out = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True)
    assert isinstance(out, Ok), out
    assert out.obj["decision"]["order_type"] != "不下单", out.obj
    assert out.obj["model_decision"]["take_profit_price_2"] is None


def test_postcheck_rejection_text_does_not_repeat_trade_instruction():
    original = {"order_type": "突破单", "reasoning": "突破单做多，立即入场。"}
    out = {"decision": {"order_type": "不下单", "reasoning": original["reasoning"]},
           "decision_adjustments": [{"reason": "decision prices: risk_reward is below minimum"}]}
    reconcile_lite_decision(out, original)
    assert out["decision"]["reasoning"].startswith("当前不下单")
    assert "突破单做多" not in out["decision"]["reasoning"]
    assert "盈亏比" in out["decision"]["reasoning"]
    assert out["model_decision"] == original
    assert out["decision"]["trade_confidence"] == 0


def test_old_record_ui_fixed_without_modifying_original_or_inventing_cause():
    original = {"trace_source": "local_rules", "decision": {"order_type": "不下单", "reasoning": "突破单做多，以当前收盘价作为入场价"}}
    before = copy.deepcopy(original)
    shown = prepare_stage2_for_ui(original)
    assert shown["order_type"] == "不下单"
    assert "突破单做多" not in shown["reasoning"]
    assert "未保留具体调整原因" in shown["reasoning"]
    assert shown["model_decision"]["reasoning"] == before["decision"]["reasoning"]
    assert original == before


_REAL_REFUSAL = (
    "周期为趋势型交易区间且偏空，价格已接近下沿支撑（收盘分位0.30），追空空间有限；"
    "唯一顺势方案是等反弹至上方阻力3877.57做空限价，但程序自动外扩止损后风险约2.59点，"
    "超过区间宽度10%（1.7755点），按§10.2判止损过大，放弃入场，等待价格反弹至阻力位或结构变化。"
)


def test_readable_refusal_reason_is_kept_verbatim():
    """A text that already explains the refusal must not be replaced."""
    original = {"decision": {"order_type": "不下单", "reasoning": _REAL_REFUSAL}}
    shown = prepare_stage2_for_ui(copy.deepcopy(original))
    assert shown["reasoning"] == _REAL_REFUSAL
    assert "未保留具体调整原因" not in shown["reasoning"]
    assert "止损过大" in shown["reasoning"]
    assert not shown.get("decision_adjustment")


def test_refusal_with_contradiction_wording_is_kept():
    text = (
        "价格位于区间下部，靠下边界；按§6.3 方向偏空时在下边界不做多。但做空止损仅 0.001 点，"
        "属噪音内止损无意义；若放到更远结构位则超过失效位，逻辑矛盾。因此当前无合理三价方案，等待价格反弹。"
    )
    original = {"decision": {"order_type": "不下单", "reasoning": text}}
    shown = prepare_stage2_for_ui(copy.deepcopy(original))
    assert shown["reasoning"] == text


def test_compact_decision_details_and_no_fabricated_default_score():
    from PyQt6.QtWidgets import QApplication
    from pa_agent.gui.decision_panel import DecisionPanel
    app = QApplication.instance() or QApplication([])
    panel = DecisionPanel()
    data = {"trace_source": "local_rules", "order_type": "不下单", "reasoning": "当前不下单，等待确认。",
        "diagnosis_confidence": 50, "trade_confidence": 50,
        "diagnosis_confidence_reasoning": "本地规则示意（非模型推理，非逐节点验证）",
        "model_decision": {"reasoning": "模型原文"}}
    panel.set_decision(data)
    assert panel._diag_conf_bar.isHidden()
    assert panel._trade_conf_inline_label.isHidden()
    assert panel._analysis_details.body.isHidden()
    panel._analysis_details.button.click()
    assert not panel._analysis_details.body.isHidden()
    assert "未经统计校准" in panel._analysis_details.body.text()
    assert "模型原文" in panel._analysis_details.body.text()
    panel.clear()
    assert panel._analysis_details.isHidden()
    panel.close()
    app.processEvents()
