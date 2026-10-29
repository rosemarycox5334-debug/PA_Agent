"""Tests for decision continuity (flip cooldown, neutral+AIS, guard)."""
from __future__ import annotations

from datetime import datetime, timezone

from pa_agent.ai.decision_continuity import (
    apply_continuity_guard,
    assess_limit_order_triggered,
    assess_plan_invalidation,
    audit_relation_fields,
    bars_elapsed_between,
    build_continuity_context,
    continuity_violation_reason,
    entries_same_structure,
    order_direction_sign,
    render_continuity_prompt_block,
)
from pa_agent.data.base import KlineBar, KlineFrame, IndicatorBundle


def _ms(iso: str) -> int:
    # Records store *local* wall-clock ISO strings (see _build_empty_record,
    # which uses datetime.fromtimestamp -> naive local).  Parse the same way the
    # production _parse_local_iso_ms does: naive -> .timestamp() uses local tz.
    dt = datetime.strptime(iso, "%Y-%m-%d %H:%M:%S")
    return int(dt.timestamp() * 1000)


def _frame(
    *,
    close: float = 4193.0,
    high: float = 4194.0,
    low: float = 4190.0,
    snapshot_ts_local_ms: int | None = None,
) -> KlineFrame:
    bars = (
        KlineBar(
            seq=1,
            ts_open=1.0,
            open=4192.0,
            high=high,
            low=low,
            close=close,
            volume=1.0,
            closed=True,
        ),
    )
    return KlineFrame(
        symbol="XAUUSDm",
        timeframe="5m",
        bars=bars,
        indicators=IndicatorBundle(ema20=(4195.0,), atr14=(2.0,)),
        snapshot_ts_local_ms=(
            int(snapshot_ts_local_ms)
            if snapshot_ts_local_ms is not None
            else _ms("2026-06-30 14:25:00")
        ),
    )


def test_assess_plan_invalidation_short_stop_hit():
    dec = {"order_direction": "做空", "order_type": "限价单", "stop_loss_price": 4194.0}
    inv, reason = assess_plan_invalidation(dec, _frame(close=4194.5, high=4195.0))
    assert inv is True
    assert "止损" in reason


def test_assess_limit_order_triggered_long_touch():
    dec = {
        "order_direction": "做多",
        "order_type": "限价单",
        "entry_price": 7459.05,
        "stop_loss_price": 7456.08,
    }
    frame = _frame(close=7458.24, high=7463.72, low=7456.78)
    triggered, reason, seq = assess_limit_order_triggered(dec, frame, max_bars=3)
    assert triggered is True
    assert seq == 1
    assert "7459.05" in reason


def test_assess_limit_order_triggered_long_not_touched():
    dec = {
        "order_direction": "做多",
        "order_type": "限价单",
        "entry_price": 7450.0,
        "stop_loss_price": 7440.0,
    }
    triggered, reason, seq = assess_limit_order_triggered(
        dec, _frame(close=7458.24, high=7463.72, low=7456.78), max_bars=1
    )
    assert triggered is False
    assert reason == ""
    assert seq is None


def test_render_continuity_prompt_mentions_limit_triggered():
    ctx = {
        "has_previous_plan": True,
        "previous_decision": {
            "order_direction": "做多",
            "order_type": "限价单",
            "entry_price": 7459.05,
            "stop_loss_price": 7456.08,
        },
        "previous_time": "2026-06-30 14:38:44",
        "bars_since": 1,
        "cooldown_bars": 3,
        "invalidated": False,
        "limit_triggered": True,
        "limit_trigger_reason": "K1 low=7456.78 已触及",
        "direction": "neutral",
        "always_in_branch": "AIL",
        "timeframe": "15m",
    }
    text = render_continuity_prompt_block(ctx)
    assert "限价已触发" in text
    assert "禁止" in text and "仍等待限价触发" in text


def test_entries_same_structure_within_ticks():
    assert entries_same_structure(4196.79, 4196.791, tick=0.001) is True
    assert entries_same_structure(4196.79, 4190.0, tick=0.001) is False


def test_continuity_blocks_neutral_ais_long():
    ctx = {
        "direction": "neutral",
        "always_in_branch": "AIS",
        "has_previous_plan": False,
        "cooldown_bars": 3,
    }
    decision = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 4190.0,
        "stop_loss_price": 4185.0,
        "take_profit_price": 4196.0,
    }
    reason = continuity_violation_reason(ctx, decision)
    assert reason is not None
    assert "AIS" in reason


def test_continuity_blocks_flip_same_structure():
    ctx = {
        "direction": "bearish",
        "always_in_branch": "AIS",
        "has_previous_plan": True,
        "previous_decision": {
            "order_direction": "\u505a\u7a7a",
            "order_type": "\u9650\u4ef7\u5355",
            "entry_price": 4196.79,
            "stop_loss_price": 4200.0,
        },
        "previous_entry": 4196.79,
        "tick": 0.001,
        "bars_since": 1,
        "cooldown_bars": 3,
        "invalidated": False,
    }
    decision = {
        "order_type": "\u9650\u4ef7\u5355",
        "order_direction": "\u505a\u591a",
        "entry_price": 4196.791,
        "stop_loss_price": 4190.0,
        "take_profit_price": 4204.0,
    }
    reason = continuity_violation_reason(ctx, decision)
    assert reason is not None
    assert "反手" in reason


def test_apply_continuity_guard_forces_no_order():
    ctx = {
        "direction": "neutral",
        "always_in_branch": "AIS",
        "has_previous_plan": False,
        "cooldown_bars": 3,
    }
    stage2 = {
        "decision": {
            "order_type": "限价单",
            "order_direction": "做多",
            "entry_price": 4190.0,
            "stop_loss_price": 4185.0,
            "take_profit_price": 4196.0,
            "reasoning": "test",
        },
        "terminal": {"outcome": "trade", "node_id": "11.2"},
    }
    out = apply_continuity_guard(stage2, ctx)
    assert out["decision"]["order_type"] == "不下单"
    assert out["terminal"]["outcome"] == "wait"


def test_apply_continuity_guard_reasoning_within_max_len():
    ctx = {
        "direction": "neutral",
        "always_in_branch": None,
        "section_2_4_answer": "否",
        "has_previous_plan": False,
        "cooldown_bars": 3,
    }
    long_body = "x" * 300
    stage2 = {
        "decision": {
            "order_type": "限价单",
            "order_direction": "做空",
            "entry_price": 3990.0,
            "stop_loss_price": 4000.0,
            "take_profit_price": 3970.0,
            "reasoning": long_body,
        },
        "terminal": {"outcome": "trade", "node_id": "11.2"},
    }
    out = apply_continuity_guard(stage2, ctx)
    reasoning = out["decision"]["reasoning"]
    assert len(reasoning) <= 280
    assert reasoning.startswith("【程序连续性守卫】")


def test_missing_section_2_4_does_not_block_directional_plan():
    """Local-compatibility diagnosis has no gate_trace §2.4 — do not discard
    a legal plan just because the section was never evaluated."""
    ctx = {
        "direction": "neutral",
        "always_in_branch": None,
        "section_2_4_answer": None,
        "has_previous_plan": False,
        "cooldown_bars": 3,
    }
    decision = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 4028.155,
        "stop_loss_price": 4025.17,
        "take_profit_price": 4030.0,
    }
    assert continuity_violation_reason(ctx, decision) is None
    stage2 = {"decision": decision, "terminal": {"outcome": "trade", "node_id": "11.2"}}
    out = apply_continuity_guard(stage2, ctx)
    assert out["decision"]["order_type"] == "限价单"
    assert out["decision"]["entry_price"] == 4028.155


def test_explicit_section_2_4_no_still_blocks_directional_plan():
    """When §2.4 was evaluated and said 否, keep the original prohibition."""
    ctx = {
        "direction": "neutral",
        "always_in_branch": None,
        "section_2_4_answer": "否",
        "has_previous_plan": False,
        "cooldown_bars": 3,
    }
    decision = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 4028.155,
        "stop_loss_price": 4025.17,
        "take_profit_price": 4030.0,
    }
    reason = continuity_violation_reason(ctx, decision)
    assert reason is not None
    assert "§2.4" in reason


def test_apply_continuity_guard_records_reason_for_display():
    """The guard's specific reason must reach the display layer, not be
    swallowed by the generic fallback."""
    ctx = {
        "direction": "neutral",
        "always_in_branch": None,
        "section_2_4_answer": "否",
        "has_previous_plan": False,
        "cooldown_bars": 3,
    }
    stage2 = {
        "decision": {
            "order_type": "限价单",
            "order_direction": "做多",
            "entry_price": 4028.155,
            "stop_loss_price": 4025.17,
            "take_profit_price": 4030.0,
            "reasoning": "test",
        },
        "terminal": {"outcome": "trade", "node_id": "11.2"},
    }
    out = apply_continuity_guard(stage2, ctx)
    assert out["decision"]["order_type"] == "不下单"
    assert out["local_plan_guard"] and "§2.4" in out["local_plan_guard"]
    last = out["decision_adjustments"][-1]
    assert last["check"] == "continuity"
    assert last["from"] == "限价单"
    assert "§2.4" in last["reason"]


def test_render_prompt_mentions_neutral_ais():
    ctx = build_continuity_context(
        frame=_frame(),
        stage1_json={
            "direction": "neutral",
            "gate_trace": [
                {"node_id": "2.4", "answer": "是", "branch": "AIS"},
            ],
        },
        cooldown_bars=3,
    )
    block = render_continuity_prompt_block(ctx)
    assert "AIS" in block
    assert "做空" in block


def test_audit_relation_flip_label():
    prev = {
        # Keep record_time near the frame snapshot (1 bar apart): a multi-day
        # gap would exceed max_pending_bars and auto-cancel the resting limit,
        # masking the flip classification this test is checking.
        "record_time": "2026-06-30 14:20:00",
        "order_direction": "做空",
        "order_type": "限价单",
        "entry_price": "4196.79",
        "stop_loss_price": "4200.657",
    }
    curr = {
        "order_direction": "做多",
        "order_type": "限价单",
        "entry_price": 4196.55,
    }
    audit = audit_relation_fields(prev, curr, frame=_frame(), cooldown_bars=3)
    assert audit["prev_plan_relation"] == "反手"
    assert order_direction_sign("做空") == -1


def test_bars_elapsed_between_parses_iso_t_separator():
    prev = "2026-06-30T15:15:00.651"
    curr = _ms("2026-06-30 15:30:01")
    assert bars_elapsed_between(prev, curr, "15m") == 1


def test_build_continuity_context_auto_cancels_after_3_bars_unfilled_limit():
    prev_time = "2026-06-30 14:00:00"
    frame = _frame(snapshot_ts_local_ms=_ms("2026-06-30 14:25:00"))  # 5m * 5 bars
    ctx = build_continuity_context(
        frame=frame,
        stage1_json={"direction": "bullish", "cycle_position": "trending_tr"},
        previous_record={
            "meta": {"timestamp_local_iso": prev_time},
            "stage1_diagnosis": {"direction": "bullish", "cycle_position": "trending_tr"},
            "stage2_decision": {
                "decision": {
                    "order_direction": "做多",
                    "order_type": "限价单",
                    # Long limit must rest BELOW market to stay unfilled:
                    # _frame() low=4190, so 4185 is genuinely untouched.
                    "entry_price": 4185.0,
                    "stop_loss_price": 4170.0,
                    "take_profit_price": 4230.0,
                }
            },
        },
    )
    assert ctx["bars_since"] > 3
    assert ctx["limit_triggered"] is False
    assert ctx["invalidated"] is True
    assert "自动取消" in (ctx["invalidation_reason"] or "")


def test_build_continuity_context_auto_cancels_on_cycle_change_unfilled_limit():
    frame = _frame(snapshot_ts_local_ms=_ms("2026-06-30 14:05:00"))
    ctx = build_continuity_context(
        frame=frame,
        stage1_json={"direction": "bullish", "cycle_position": "trading_range"},
        previous_record={
            "meta": {"timestamp_local_iso": "2026-06-30 14:00:00"},
            "stage1_diagnosis": {"direction": "bullish", "cycle_position": "trending_tr"},
            "stage2_decision": {
                "decision": {
                    "order_direction": "做多",
                    "order_type": "限价单",
                    "entry_price": 4185.0,
                    "stop_loss_price": 4170.0,
                    "take_profit_price": 4230.0,
                }
            },
        },
    )
    assert ctx["limit_triggered"] is False
    assert ctx["invalidated"] is True
    assert "周期" in (ctx["invalidation_reason"] or "")


def test_build_continuity_context_auto_cancels_on_direction_change_unfilled_limit():
    frame = _frame(snapshot_ts_local_ms=_ms("2026-06-30 15:30:01"))
    ctx = build_continuity_context(
        frame=frame,
        stage1_json={"direction": "bullish", "cycle_position": "trending_tr"},
        previous_record={
            "meta": {"timestamp_local_iso": "2026-06-30T15:15:00.651"},
            "stage1_diagnosis": {"direction": "neutral", "cycle_position": "trending_tr"},
            "stage2_decision": {
                "decision": {
                    "order_direction": "做多",
                    "order_type": "限价单",
                    # Long limit must rest BELOW market: _frame() low=4190,
                    # so a 7459 entry would already be "triggered".
                    "entry_price": 4185.0,
                    "stop_loss_price": 4170.0,
                    "take_profit_price": 4230.0,
                }
            },
        },
    )
    assert ctx["limit_triggered"] is False
    assert ctx["invalidated"] is True
    assert "趋势方向" in (ctx["invalidation_reason"] or "")


def test_build_continuity_context_does_not_auto_cancel_when_limit_already_triggered():
    # entry=4192 is touched by _frame() low=4190
    frame = _frame(snapshot_ts_local_ms=_ms("2026-06-30 14:25:00"))
    ctx = build_continuity_context(
        frame=frame,
        stage1_json={"direction": "bullish", "cycle_position": "trending_tr"},
        previous_record={
            "meta": {"timestamp_local_iso": "2026-06-30 14:00:00"},
            "stage1_diagnosis": {"direction": "bullish", "cycle_position": "trending_tr"},
            "stage2_decision": {
                "decision": {
                    "order_direction": "做多",
                    "order_type": "限价单",
                    "entry_price": 4192.0,
                    "stop_loss_price": 4185.0,
                    "take_profit_price": 4200.0,
                }
            },
        },
    )
    assert ctx["limit_triggered"] is True
    # Triggered means we should not auto-cancel under the "unfilled" rule.
    assert ctx["invalidated"] is False
