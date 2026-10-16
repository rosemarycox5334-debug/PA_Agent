"""程序改写提示：连续性守卫把模型方案改成「不下单」时，面板必须显眼说出来。

真缺陷复现路径：`apply_continuity_guard` 静默改写 `order_type=不下单` 并清空价格字段，
界面只显示「不下单」——用户无法区分"模型自己不想下单"和"模型给了方案但被程序拦掉"。

本测试锁定两件事：
1. 面板能显示改写的告警行（含原因与原始方案）；
2. 没有改写时该行必须隐藏（不能一直挂着告警）。
"""

from __future__ import annotations

import pytest

from pa_agent.gui.decision_panel import DecisionPanel

#: 真实场景形态：direction=neutral 却给出做多限价单 ⇒ 被守卫改写
SUPPRESSED_STAGE2 = {
    "decision": {
        "order_type": "不下单",
        "order_direction": None,
        "entry_price": None,
        "stop_loss_price": None,
        "take_profit_price": None,
        "reasoning": "【程序连续性守卫】direction=neutral 且 §2.4 非 Always In：禁止无强信号的方向性方案；改为不下单。",
    },
    "terminal": {"outcome": "wait", "node_id": "continuity"},
    "continuity_guard": {
        "applied": True,
        "reason": "direction=neutral 且 §2.4 非 Always In：禁止无强信号的方向性方案",
        "original_plan": {
            "order_direction": "做多",
            "order_type": "限价单",
            "entry_price": 80100.0,
            "stop_loss_price": 79900.0,
            "take_profit_price": 80500.0,
            "estimated_win_rate": 54.0,
        },
    },
}


@pytest.fixture
def panel(qtbot):
    p = DecisionPanel()
    qtbot.addWidget(p)
    return p


def _note(stage2: dict) -> str | None:
    from pa_agent.gui.main_window import MainWindow

    return MainWindow._continuity_override_note(stage2)


@pytest.mark.unit
def test_note_mentions_reason_and_original_plan() -> None:
    note = _note(SUPPRESSED_STAGE2)
    assert note is not None
    assert "连续性守卫" in note
    assert "不下单" in note
    assert "neutral" in note  # 改写原因
    assert "做多" in note and "80100" in note  # 原始方案


@pytest.mark.unit
def test_note_is_none_without_rewrite() -> None:
    assert _note({"decision": {"order_type": "限价单"}}) is None
    assert _note({}) is None
    assert _note({"continuity_guard": {"applied": False}}) is None


@pytest.mark.unit
def test_panel_shows_and_hides_the_note(panel) -> None:
    inner = SUPPRESSED_STAGE2["decision"]
    # 注意：面板本身没被 show()，isVisible() 恒为 False；用 isVisibleTo 判断显式显隐
    panel.set_decision(inner, suppressed_note=_note(SUPPRESSED_STAGE2))
    label = panel._suppressed_note_label
    assert label.isVisibleTo(panel) is True
    assert "连续性守卫" in label.text()

    # 清空后不能残留告警
    panel.clear()
    assert label.text() == ""
    assert label.isVisibleTo(panel) is False

    # 正常一轮分析（无改写）必须隐藏
    panel.set_decision({"order_type": "不下单", "reasoning": "模型自己判断等待"})
    assert label.isVisibleTo(panel) is False
