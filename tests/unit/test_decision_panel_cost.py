"""决策面板的成本口径提示：预估胜率 vs 扣成本后所需胜率。

用两条**真实分析记录**的价格做回归：
- 20:10 那条（成本闸门关闭时代）：止盈/成本 = 2.35×，扣成本需 63.4% 胜率 → 必须标红；
- 21:08 那条（成本闸门开启时代）：止盈/成本 = 4.30×，扣成本需 46.3% 胜率 → 必须打勾。
"""

from __future__ import annotations

import pytest

from pa_agent.config.settings import ValidationSettings
from pa_agent.gui.decision_panel import DecisionPanel

#: 真实记录 2026-10-15_20-10-08_BTCUSDT_15m.json
DOOMED = {
    "order_type": "限价单",
    "order_direction": "做空",
    "entry_price": 81071.21,
    "stop_loss_price": 81397.05,
    "take_profit_price": 80745.37,
    "take_profit_price_2": 80500.01,
    "estimated_win_rate": 50,
}

#: 真实记录（成本闸门开启后的 21:08 实盘分析）
VIABLE = {
    "order_type": "限价单",
    "order_direction": "做空",
    "entry_price": 80976.0,
    "stop_loss_price": 81424.73999999999,
    "take_profit_price": 80527.26,
    "take_profit_price_2": 79931.71,
    "estimated_win_rate": 52,
}


@pytest.fixture
def panel(qtbot):
    p = DecisionPanel()
    qtbot.addWidget(p)
    return p


@pytest.mark.unit
def test_cost_doomed_plan_is_flagged_red(panel) -> None:
    panel._render_cost_note(DOOMED)
    text = panel._win_rate_inline_label.text()
    assert "预估胜率" in text
    assert "63.4" in text  # 扣成本后所需的盈亏平衡胜率
    assert "成本不达标" in text
    assert "#f85149" in panel._win_rate_inline_label.styleSheet()
    assert "2.35" in panel._win_rate_inline_label.toolTip()


@pytest.mark.unit
def test_viable_plan_shows_check_mark(panel) -> None:
    panel._render_cost_note(VIABLE)
    text = panel._win_rate_inline_label.text()
    assert "46.3" in text
    assert "✓" in text
    assert "成本不达标" not in text
    assert "#3fb950" in panel._win_rate_inline_label.styleSheet()


#: 真实 GUI 运行（2026-10-15 21:51 提交分析）产生的方案：止盈/成本 3.34× 达标，
#: 但扣成本需 53.1% 而模型自评 52% —— 差 1.1pt，落在 5pt 余量内。
MARGINAL = {
    "order_type": "限价单",
    "order_direction": "做空",
    "entry_price": 80902.0,
    "stop_loss_price": 81304.0,
    "take_profit_price": 80500.0,
    "take_profit_price_2": 80091.0,
    "estimated_win_rate": 52,
}


@pytest.mark.unit
def test_marginal_plan_is_amber_not_green(panel) -> None:
    """略低于盈亏平衡（差 1.1pt）不能显示成通过——实测遇到的真实情况。"""
    panel._render_cost_note(MARGINAL)
    text = panel._win_rate_inline_label.text()
    assert "53.1" in text
    assert "基本持平" in text and "1.1pt" in text
    assert "✓" not in text
    assert "#d29922" in panel._win_rate_inline_label.styleSheet()


@pytest.mark.unit
def test_decision_without_prices_has_no_cost_clause(panel) -> None:
    panel._render_cost_note({"order_type": "不下单", "estimated_win_rate": None})
    text = panel._win_rate_inline_label.text()
    assert "成本" not in text


@pytest.mark.unit
def test_bind_settings_reads_thresholds_from_validation(panel) -> None:
    """配置里的阈值要生效：放宽倍数与胜率余量后，2.35× 的方案可做。"""
    panel._render_cost_note(DOOMED)
    assert "成本不达标" in panel._win_rate_inline_label.text()

    panel.bind_settings(
        ValidationSettings(
            min_reward_cost_ratio=2.0,
            assumed_round_trip_cost_pct=0.30,
            min_win_rate_margin_pct=20.0,  # 缺口 13.4pt < 20pt
        )
    )
    panel._render_cost_note(DOOMED)
    text = panel._win_rate_inline_label.text()
    assert "成本不达标" not in text
    assert "基本持平" in text  # 缺口 13.4pt 落在放宽后的 20pt 余量内


@pytest.mark.unit
def test_win_rate_gap_is_flagged_even_when_cost_ratio_ok(panel) -> None:
    """止盈/成本达标但自评胜率低于门槛 → 标红并说明是胜率缺口。"""
    plan = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.0,
        "stop_loss_price": 99.0,
        "take_profit_price": 101.5,
        "estimated_win_rate": 45,
    }
    panel._render_cost_note(plan)
    text = panel._win_rate_inline_label.text()
    assert "自评胜率低" in text and "7.0pt" in text
    assert "#f85149" in panel._win_rate_inline_label.styleSheet()


@pytest.mark.unit
def test_bind_settings_accepts_full_settings_object(panel) -> None:
    class _Cfg:
        validation = ValidationSettings(
            min_reward_cost_ratio=5.0, assumed_round_trip_cost_pct=0.30
        )

    panel.bind_settings(_Cfg())
    panel._render_cost_note(VIABLE)  # 4.30× < 5.0× → 标红
    assert "成本不达标" in panel._win_rate_inline_label.text()
