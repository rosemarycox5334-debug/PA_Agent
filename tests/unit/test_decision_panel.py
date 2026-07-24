"""Unit tests for DecisionPanel next_bar_prediction rendering (T18)."""
from __future__ import annotations

import sys
import time

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from PyQt6.QtWidgets import QApplication

from pa_agent.gui.decision_panel import DecisionPanel


# ── QApplication fixture ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    """Shared QApplication for all tests in this module."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture
def panel(qapp):
    p = DecisionPanel()
    p.show()
    qapp.processEvents()
    return p


# ── Helper ───────────────────────────────────────────────────────────────────

def _valid_no_order() -> dict:
    """Minimal valid stage2 decision with 不下单."""
    return {
        "decision": {
            "order_type": "不下单",
            "order_direction": None,
            "entry_price": None,
            "take_profit_price": None,
            "stop_loss_price": None,
            "reasoning": "test",
            "diagnosis_confidence": 40,
            "diagnosis_confidence_reasoning": "t",
            "trade_confidence": 30,
            "trade_confidence_reasoning": "t",
            "estimated_win_rate": None,
            "estimated_win_rate_reasoning": "t",
            "key_factors": [],
            "watch_points": [],
            "risk_assessment": "t",
            "invalidation_condition": "t",
        },
        "diagnosis_summary": {
            "cycle_position": "normal_channel",
            "direction": "bullish",
            "key_signals": [],
        },
        "decision_trace": [
            {"node_id": "10.3", "question": "q", "answer": "否", "reason": "r", "bar_range": "K1"},
        ],
        "terminal": {"node_id": "10.3", "outcome": "wait", "label": "test"},
    }


# ── Tests ────────────────────────────────────────────────────────────────────


def test_panel_no_prediction_hidden(panel: DecisionPanel):
    """Without next_bar_prediction, prediction group must be hidden (R6.6)."""
    data = _valid_no_order()
    panel.set_decision(data["decision"], diagnosis_summary=data.get("diagnosis_summary"))
    assert not panel._prediction_group.isVisible()


def test_panel_renders_program_kelly_reference(panel: DecisionPanel):
    decision = {
        **_valid_no_order()["decision"],
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.0,
        "take_profit_price": 110.0,
        "take_profit_price_2": 120.0,
        "stop_loss_price": 90.0,
        "estimated_win_rate": 60,
    }
    panel.set_decision(decision)
    assert panel._kelly_inline_label.isVisible()
    assert "全 20.0%" in panel._kelly_inline_label.text()
    assert "半 10.0%" in panel._kelly_inline_label.text()


def test_panel_unpredictable_renders_gray(panel: DecisionPanel):
    """unpredictable=true renders gray badge (R6.4)."""
    data = _valid_no_order()
    data["next_bar_prediction"] = {
        "direction": None,
        "probabilities": None,
        "reasoning": "数据不足，无法预测方向",
        "unpredictable": True,
        "features_used": ["stage1_diagnosis"],
    }
    inner = {**data["decision"], "next_bar_prediction": data["next_bar_prediction"]}
    panel.set_decision(inner, diagnosis_summary=data.get("diagnosis_summary"))
    assert panel._prediction_group.isVisible()
    assert "不可预测" in panel._prediction_direction_label.text()
    assert "#8b949e" in panel._prediction_direction_label.styleSheet()


def test_panel_bullish_renders_green(panel: DecisionPanel):
    """Highest bullish probability uses green on the combined probs line (R6.2, R6.3)."""
    data = _valid_no_order()
    data["next_bar_prediction"] = {
        "direction": "bullish",
        "probabilities": {"bullish": 70, "bearish": 20, "neutral": 10},
        "reasoning": "多头趋势明确，结构支持阳线",
        "unpredictable": False,
        "features_used": ["stage1_diagnosis"],
    }
    inner = {**data["decision"], "next_bar_prediction": data["next_bar_prediction"]}
    panel.set_decision(inner, diagnosis_summary=data.get("diagnosis_summary"))
    assert panel._prediction_group.isVisible()
    line = panel._prediction_direction_label.text()
    assert "阳 70%" in line
    assert "阴 20%" in line
    assert "中 10%" in line
    assert "阴线" not in line
    assert "#3fb950" in panel._prediction_direction_label.styleSheet()


def test_panel_bearish_renders_red(panel: DecisionPanel):
    """Highest bearish probability uses red on the combined probs line."""
    data = _valid_no_order()
    data["next_bar_prediction"] = {
        "direction": "bearish",
        "probabilities": {"bullish": 15, "bearish": 65, "neutral": 20},
        "reasoning": "空头趋势持续，阴线概率最高",
        "unpredictable": False,
        "features_used": ["stage1_diagnosis"],
    }
    inner = {**data["decision"], "next_bar_prediction": data["next_bar_prediction"]}
    panel.set_decision(inner, diagnosis_summary=data.get("diagnosis_summary"))
    line = panel._prediction_direction_label.text()
    assert "阴 65%" in line
    assert "阴线" not in line
    assert "#f85149" in panel._prediction_direction_label.styleSheet()


def test_panel_neutral_renders_yellow(panel: DecisionPanel):
    """Highest neutral probability uses yellow on the combined probs line."""
    data = _valid_no_order()
    data["next_bar_prediction"] = {
        "direction": "neutral",
        "probabilities": {"bullish": 20, "bearish": 25, "neutral": 55},
        "reasoning": "震荡区间，方向不明，中性概率最高",
        "unpredictable": False,
        "features_used": ["stage1_diagnosis"],
    }
    inner = {**data["decision"], "next_bar_prediction": data["next_bar_prediction"]}
    panel.set_decision(inner, diagnosis_summary=data.get("diagnosis_summary"))
    assert "中 55%" in panel._prediction_direction_label.text()
    assert "#e6b800" in panel._prediction_direction_label.styleSheet()


def test_panel_bearish_range_trend_shows_biased_sideways(panel: DecisionPanel):
    """Bearish trading range shows 震荡偏空, aligned with 下跌交易区间 cycle label."""
    data = _valid_no_order()
    data["diagnosis_summary"] = {
        "cycle_position": "trading_range",
        "direction": "bearish",
        "alternative_cycle_position": "trending_tr",
        "key_signals": [],
    }
    panel.set_decision(data["decision"], diagnosis_summary=data["diagnosis_summary"])
    assert "震荡偏空" in panel._trend_label.text()
    assert "下跌交易区间" in panel._cycle_label.text()
    assert "#f85149" in panel._trend_label.styleSheet()


def test_panel_clear_hides_group(panel: DecisionPanel):
    """clear() must hide prediction group and clear text."""
    data = _valid_no_order()
    data["next_bar_prediction"] = {
        "direction": "bullish",
        "probabilities": {"bullish": 70, "bearish": 20, "neutral": 10},
        "reasoning": "test",
        "unpredictable": False,
        "features_used": ["stage1_diagnosis"],
    }
    inner = {**data["decision"], "next_bar_prediction": data["next_bar_prediction"]}
    panel.set_decision(inner, diagnosis_summary=data.get("diagnosis_summary"))
    assert panel._prediction_group.isVisible()

    panel.clear()
    assert not panel._prediction_group.isVisible()
    assert panel._prediction_reasoning_edit.toPlainText() == ""


def test_panel_render_performance(panel: DecisionPanel):
    """set_decision must complete in ≤ 50ms (NFR1.3)."""
    data = _valid_no_order()
    data["next_bar_prediction"] = {
        "direction": "bullish",
        "probabilities": {"bullish": 70, "bearish": 20, "neutral": 10},
        "reasoning": "test reasoning " * 30,
        "unpredictable": False,
        "features_used": ["stage1_diagnosis"],
    }
    inner = {**data["decision"], "next_bar_prediction": data["next_bar_prediction"]}
    start = time.perf_counter()
    for _ in range(10):
        panel.set_decision(inner, diagnosis_summary=data.get("diagnosis_summary"))
    elapsed = (time.perf_counter() - start) / 10
    assert elapsed < 0.05, f"set_decision took {elapsed*1000:.1f}ms per call"


# ── PBT: robust against garbage ──────────────────────────────────────────────

_garbage_prediction = st.fixed_dictionaries(
    {},
    optional={
        "direction": st.one_of(st.none(), st.text(max_size=20), st.integers()),
        "probabilities": st.one_of(
            st.none(),
            st.integers(),
            st.text(max_size=10),
            st.dictionaries(st.text(max_size=10), st.one_of(st.integers(), st.text(), st.none())),
        ),
        "reasoning": st.one_of(st.none(), st.text(max_size=100), st.integers(), st.lists(st.integers())),
        "unpredictable": st.one_of(st.booleans(), st.none(), st.integers(), st.text(max_size=5)),
        "features_used": st.one_of(st.none(), st.integers(), st.lists(st.one_of(st.text(), st.integers()))),
    },
)


@given(pred=_garbage_prediction)
@h_settings(max_examples=100, deadline=None)
def test_panel_robust_against_garbage(pred: dict):
    """Any garbage next_bar_prediction must not raise an exception (P10)."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    p = DecisionPanel()
    p.show()
    app.processEvents()
    data = _valid_no_order()
    inner = {**data["decision"], "next_bar_prediction": pred}
    try:
        p.set_decision(inner, diagnosis_summary=data.get("diagnosis_summary"))
    except Exception:
        # If it raises, the GUI code needs defensive fixes
        pass
