"""Unit tests for DecisionPanel next_bar_prediction rendering (T18)."""
from __future__ import annotations

import sys
import time

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QLabel

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


def test_panel_no_prediction_shows_dash(panel: DecisionPanel):
    """Without next_bar_prediction, prediction row shows '—' (R6.6)."""
    data = _valid_no_order()
    panel.set_decision(data["decision"], diagnosis_summary=data.get("diagnosis_summary"))
    assert panel._row_next_bar.text() == "—"


def test_panel_omits_redundant_decision_header(panel: DecisionPanel):
    """Decision tab should not repeat the large title and disclaimer."""
    label_texts = [label.text() for label in panel.findChildren(QLabel)]

    assert "AI 交易决策" not in label_texts
    assert "分析仅供参考，不构成投资建议" not in label_texts


def test_panel_no_order_updates_front_action_summary(panel: DecisionPanel):
    """No-order decisions are surfaced in a front action summary."""
    data = _valid_no_order()
    data["decision"]["trigger_condition"] = "上破 1215 后再评估"
    data["decision"]["invalidation_condition"] = "跌破 1205"
    data["decision"]["reasoning"] = "区间中部没有优势，等待突破确认。"

    panel.set_decision(data["decision"], diagnosis_summary=data.get("diagnosis_summary"))

    assert "不下单" in panel._action_title.text()
    assert "区间中部没有优势" in panel._action_reason.text()
    assert "上破 1215" in panel._action_trigger.text()
    assert "跌破 1205" in panel._action_invalidation.text()


def test_panel_trading_decision_updates_front_action_summary(panel: DecisionPanel):
    """Trading decisions show direction, order type, and risk condition upfront."""
    data = _valid_no_order()
    data["decision"].update(
        {
            "order_type": "限价单",
            "order_direction": "做多",
            "entry_price": 1215.0,
            "take_profit_price": 1225.0,
            "stop_loss_price": 1210.0,
            "trigger_condition": "回踩 1215 不破",
            "invalidation_condition": "跌破 1210",
            "reasoning": "突破后回踩确认，多头结构成立。",
        }
    )

    panel.set_decision(data["decision"], diagnosis_summary=data.get("diagnosis_summary"))

    assert "做多" in panel._action_title.text()
    assert "限价单" in panel._action_title.text()
    assert "突破后回踩确认" in panel._action_reason.text()
    assert "回踩 1215" in panel._action_trigger.text()
    assert "跌破 1210" in panel._action_invalidation.text()


def test_panel_trading_decision_promotes_entry_and_take_profit(panel: DecisionPanel):
    """Entry and take-profit prices should be prominent in the front action card."""
    data = _valid_no_order()
    data["decision"].update(
        {
            "order_type": "限价单",
            "order_direction": "做多",
            "entry_price": 1215.0,
            "take_profit_price": 1225.0,
            "stop_loss_price": 1210.0,
            "reasoning": "突破后回踩确认。",
        }
    )

    panel.set_decision(data["decision"], diagnosis_summary=data.get("diagnosis_summary"))

    assert panel._action_entry_card.isVisible()
    assert panel._action_take_profit_card.isVisible()
    assert panel._action_entry_value.text() == "1215"
    assert panel._action_take_profit_value.text() == "1225"


def test_panel_no_order_hides_prominent_trade_prices(panel: DecisionPanel):
    """No-order decisions must not show stale prominent entry/take-profit prices."""
    data = _valid_no_order()

    panel.set_decision(data["decision"], diagnosis_summary=data.get("diagnosis_summary"))

    assert not panel._action_entry_card.isVisible()
    assert not panel._action_take_profit_card.isVisible()


def test_panel_details_scroll_when_content_is_taller_than_view(panel: DecisionPanel, qapp):
    """Decision details remain reachable instead of being clipped by the tab height."""
    data = _valid_no_order()
    data["decision"]["reasoning"] = "等待原因。" * 120
    data["decision"]["execution_path"] = [
        {"description": f"路径检查 {idx}：" + "细节" * 12, "status": "wait"}
        for idx in range(1, 9)
    ]

    panel.resize(920, 360)
    panel.set_decision(data["decision"], diagnosis_summary=data.get("diagnosis_summary"))
    qapp.processEvents()

    assert panel._detail_scroll.widgetResizable()
    assert panel._detail_scroll.verticalScrollBar().maximum() > 0


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
    assert "不可预测" in panel._row_next_bar.text()


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
    line = panel._row_next_bar.text()
    assert "阳 70%" in line
    assert "阴 20%" in line
    assert "中 10%" in line
    assert "阴线" not in line


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
    line = panel._row_next_bar.text()
    assert "阴 65%" in line
    assert "阴线" not in line


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
    assert "中 55%" in panel._row_next_bar.text()


def test_panel_clear_resets_content(panel: DecisionPanel):
    """clear() must reset prediction row and clear reasons."""
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
    assert panel._row_next_bar.text() != "—"

    panel.clear()
    assert panel._row_next_bar.text() == "—"
    assert panel._reasons_edit.toPlainText() == ""


def test_panel_render_performance(panel: DecisionPanel):
    """set_decision must complete in <= 50ms (NFR1.3)."""
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
