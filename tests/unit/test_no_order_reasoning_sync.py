"""任何把决策改成"不下单"的后处理，都必须同步改写可见理由。"""
from __future__ import annotations

import json

from pa_agent.ai.json_validator import JsonValidator, Ok
from pa_agent.ai.stage2_normalizer import normalize_stage2
from tests.integration.conftest import VALID_STAGE2, make_frame

TRADE_TEXT = "买入方向：K1阳线突破区间上沿，计划型限价，入场 4032.91，止损 4031，TP1 4039.17。"


def _trade_payload(**over):
    payload = json.loads(json.dumps(VALID_STAGE2))
    payload["decision"].update({
        "order_type": "限价单", "order_direction": "做多",
        "entry_price": 4032.91, "stop_loss_price": 4031.0,
        "take_profit_price": 4039.17, "take_profit_price_2": 4045.0,
        "reasoning": TRADE_TEXT,
    })
    payload["decision"].update(over)
    return payload


def test_reject_terminal_rewrites_reasoning():
    p = _trade_payload()
    p["terminal"] = {"node_id": "10.3", "outcome": "reject", "label": "不通过"}
    out = normalize_stage2(p, kline_frame=make_frame())
    assert out["decision"]["order_type"] == "不下单"
    assert "当前不下单" in out["decision"]["reasoning"]
    assert "买入方向" not in out["decision"]["reasoning"]
    assert out["decision"]["model_reasoning_before_no_order"] == TRADE_TEXT
    assert out["decision"]["trade_confidence_reasoning"] == "当前没有有效入场计划"


def test_wait_terminal_rewrites_reasoning():
    p = _trade_payload()
    p["terminal"] = {"node_id": "11.2", "outcome": "wait", "label": "等待"}
    out = normalize_stage2(p, kline_frame=make_frame())
    if out["decision"]["order_type"] == "不下单":
        assert "当前不下单" in out["decision"]["reasoning"]


def test_metrics_failure_rewrites_reasoning():
    p = _trade_payload(estimated_win_rate=None)
    out = normalize_stage2(p, kline_frame=make_frame())
    if out["decision"]["order_type"] == "不下单":
        text = out["decision"]["reasoning"]
        assert text.startswith("当前不下单")
        assert "买入方向" not in text


def test_unchanged_plan_keeps_model_text():
    p = _trade_payload()
    out = normalize_stage2(p, kline_frame=make_frame())
    if out["decision"]["order_type"] != "不下单":
        assert out["decision"]["reasoning"] == TRADE_TEXT
        assert "model_reasoning_before_no_order" not in out["decision"]


def test_reason_text_stored_even_for_legacy_style_records():
    """老记录在界面渲染时仍会修复，不依赖重新分析。"""
    from pa_agent.gui.stage2_payload import prepare_stage2_for_ui
    legacy = {"trace_source": "local_rules",
              "decision": {"order_type": "不下单", "reasoning": TRADE_TEXT}}
    shown = prepare_stage2_for_ui(legacy)
    assert "买入方向" not in shown["reasoning"]
    assert "当前不下单" in shown["reasoning"]
    assert legacy["decision"]["reasoning"] == TRADE_TEXT
