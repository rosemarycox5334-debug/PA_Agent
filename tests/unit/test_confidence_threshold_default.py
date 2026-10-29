"""置信度阈值默认 0 = 关闭；界面上不再因阈值改写决策。"""
from __future__ import annotations

import pytest

from pa_agent.config.settings import Settings
from pa_agent.gui.decision_panel import DecisionPanel

PLAN = {
    "order_type": "限价单", "order_direction": "做多",
    "entry_price": 4042.2, "stop_loss_price": 4041.275,
    "take_profit_price": 4050.42, "take_profit_price_2": 4050.42,
    "reasoning": "宽通道顺势做多，RR≈1.19 通过方程。",
    "trade_confidence": 48, "trade_confidence_reasoning": "止损略宽",
    "diagnosis_confidence": 60, "estimated_win_rate": 48,
}


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_default_threshold_is_zero():
    assert Settings().general.decision_confidence_threshold == 0


def test_zero_threshold_keeps_low_confidence_plan(qapp):
    p = DecisionPanel()
    p.set_decision(dict(PLAN), confidence_threshold=0)
    assert "限价单" in p._conclusion_label.text()
    assert "不下单" not in p._conclusion_label.text()
    assert "低于阈值" not in p._reasoning_edit.toPlainText()
    p.close()


def test_main_window_reads_zero_from_settings():
    """主窗口取的就是 settings.general.decision_confidence_threshold。"""
    from types import SimpleNamespace
    from pa_agent.gui.main_window import MainWindow
    host = SimpleNamespace(_ctx=SimpleNamespace(settings=Settings()))
    assert MainWindow._confidence_threshold(host) == 0
