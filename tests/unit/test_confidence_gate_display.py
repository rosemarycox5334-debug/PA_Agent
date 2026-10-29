"""置信度阈值不得让"理由说做多、决策说观望"同时出现。"""
from __future__ import annotations

import pytest

from pa_agent.gui.decision_panel import DecisionPanel

PLAN = {
    "order_type": "限价单",
    "order_direction": "做多",
    "entry_price": 4042.2,
    "stop_loss_price": 4041.275,
    "take_profit_price": 4050.42,
    "take_profit_price_2": 4050.42,
    "reasoning": "宽通道顺势做多，结构止损 4042.2，目标 4050.42，RR≈1.19>1.0 通过 10.3 方程。",
    "trade_confidence": 48,
    "trade_confidence_reasoning": "止损距离相对宽",
    "diagnosis_confidence": 60,
    "estimated_win_rate": 48,
}


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _panel():
    return DecisionPanel()


def test_lite_route_ignores_confidence_threshold(qapp):
    p = _panel()
    p.set_decision(dict(PLAN, trace_source="local_rules"), confidence_threshold=50)
    assert "不下单" not in p._conclusion_label.text()
    assert "限价单" in p._conclusion_label.text()
    assert "置信度" not in p._reasoning_edit.toPlainText()
    p.close()


def test_other_route_downgrade_is_explained(qapp):
    p = _panel()
    p.set_decision(dict(PLAN), confidence_threshold=50)
    assert "不下单" in p._conclusion_label.text()
    text = p._reasoning_edit.toPlainText()
    assert "置信度 48 低于阈值 50" in text
    assert text.index("置信度") < text.index("宽通道顺势做多")
    p.close()


def test_threshold_not_applied_when_confident(qapp):
    p = _panel()
    p.set_decision(dict(PLAN, trade_confidence=72), confidence_threshold=50)
    assert "限价单" in p._conclusion_label.text()
    p.close()


def test_no_threshold_configured_keeps_plan(qapp):
    p = _panel()
    p.set_decision(dict(PLAN))
    assert "限价单" in p._conclusion_label.text()
    p.close()
