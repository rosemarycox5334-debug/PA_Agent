"""Unit tests for PromptAssembler (task 7.3)."""
from __future__ import annotations

import json
import math
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.data.base import KlineBar, KlineFrame, IndicatorBundle


def _make_frame(n: int = 5) -> KlineFrame:
    bars = tuple(
        KlineBar(
            seq=i + 1,
            ts_open=float(1_700_000_000 - i * 3600),
            open=2600.0 + i,
            high=2610.0 + i,
            low=2590.0 + i,
            close=2605.0 + i,
            volume=1000.0,
            closed=(i != 0),
        )
        for i in range(n)
    )
    indicators = IndicatorBundle(
        ema20=tuple(2600.0 + i for i in range(n)),
        atr14=tuple(5.0 for _ in range(n)),
    )
    return KlineFrame(
        symbol="XAUUSD",
        timeframe="1h",
        bars=bars,
        indicators=indicators,
        snapshot_ts_local_ms=1_700_000_000_000,
    )


@pytest.fixture()
def assembler(tmp_path: Path) -> PromptAssembler:
    """PromptAssembler with fake prompt files."""
    for fname in [
        "提示词大纲_人设与思维方式.txt",
        "市场诊断框架.txt",
        "二元决策.txt",
        "二元决策_闸门.txt",
        "文件16-K线信号识别.txt",
        "逐棒分析检查单.txt",
        "文件17-止损和止盈与仓位管理.txt",
        "文件18-突破失败与突破测试.txt",
        "文件19-H1H2-L1L2计数.txt",
        "文件20-AlwaysIn与20GB.txt",
        "文件21-铁丝网与无交易环境.txt",
        "文件22-信号失败后的磁力位.txt",
        "上涨通道分析识别.txt",
        "上涨通道交易策略.txt",
        "下跌通道分析识别.txt",
        "下跌通道交易策略.txt",
        "极速上涨分析识别.txt",
        "极速上涨交易策略.txt",
        "极速下跌分析识别.txt",
        "极速下跌交易策略.txt",
        "震荡区间分析识别.txt",
        "震荡区间交易策略.txt",
        "文件13-窄通道与宽通道策略.txt",
        "文件14-楔形形态分析交易.txt",
        "文件15-二次入场机会.txt",
    ]:
        (tmp_path / fname).write_text(f"[CONTENT OF {fname}]", encoding="utf-8")
    return PromptAssembler(prompt_dir=tmp_path)


def test_stage1_system_prompt_order(assembler: PromptAssembler):
    """Stage 1 system: shared persona + full binary tree; user: framework + signals."""
    frame = _make_frame()
    messages = assembler.build_stage1(frame)
    system = messages[0]["content"]
    user = messages[1]["content"]
    pos_persona = system.find("提示词大纲_人设与思维方式")
    pos_binary_sys = system.find("二元决策")
    assert pos_persona >= 0
    assert 0 <= pos_persona < pos_binary_sys, "Binary decision tree should follow persona in system"
    # Stage 1 now uses the FULL binary tree (same as Stage 2) for prefix caching
    assert "市场诊断框架" not in system

    pos_diag = user.find("市场诊断框架")
    pos_signal = user.find("文件16-K线信号识别")
    pos_bar_by_bar = user.find("逐棒分析检查单")
    assert "是否为尖峰 / 极速行情" not in system
    assert "[CONTENT OF 二元决策.txt]" in system
    assert "[CONTENT OF 二元决策_闸门.txt]" not in system
    assert 0 <= pos_diag < pos_signal, "Stage 1 user task files are out of order"
    assert 0 <= pos_signal < pos_bar_by_bar, "Bar-by-bar checklist should follow signal file"
    assert "文件18-突破失败与突破测试" not in user
    assert "文件13-窄通道与宽通道策略" not in user


def test_stage1_user_prompt_contains_required_fields(assembler: PromptAssembler):
    """Stage 1 user prompt must contain symbol, timeframe, bar count."""
    frame = _make_frame()
    messages = assembler.build_stage1(frame)
    user = messages[1]["content"]
    assert "XAUUSD" in user
    assert "1h" in user
    assert "序号" in user
    assert "K线几何特征" in user
    assert "doji" in user
    assert "更高时间框架" not in user


def test_stage2_user_prompt_includes_gate_trace(assembler: PromptAssembler):
    frame = _make_frame()
    stage1_json = {
        "cycle_position": "normal_channel",
        "direction": "bullish",
        "gate_result": "proceed",
        "gate_trace": [{"node_id": "0.1", "question": "q", "answer": "是", "reason": "r"}],
    }
    messages = assembler.build_stage2(frame, stage1_json, [], [])
    user = messages[1]["content"]
    # gate_trace and gate_result are embedded inside the compact stage1 JSON block
    assert "gate_result" in user
    assert "gate_trace" in user or "0.1" in user
    # The redundant separate gate_block section should no longer exist
    assert "## 阶段一闸门路径" not in user


def test_stage2_system_prompt_order(assembler: PromptAssembler):
    """Stage 2 system reuses stage-1 system (persona + binary); user: strategy → risk."""
    frame = _make_frame()
    stage1_json = {"cycle_position": "normal_channel", "direction": "bullish"}
    strategy_files = ["上涨通道分析识别.txt", "上涨通道交易策略.txt", "文件13-窄通道与宽通道策略.txt"]
    messages = assembler.build_stage2(frame, stage1_json, strategy_files, [])
    system = messages[0]["content"]
    user = messages[1]["content"]

    pos_persona = system.find("提示词大纲_人设与思维方式")
    pos_binary_sys = system.find("二元决策")
    assert pos_persona >= 0
    assert 0 <= pos_persona < pos_binary_sys

    assert "[CONTENT OF 二元决策.txt]" not in user, (
        "Full binary tree file is not duplicated in stage 2 user turn"
    )
    assert "[CONTENT OF 二元决策.txt]" in system
    pos_strategy = user.find("上涨通道分析识别")
    pos_bar_by_bar = user.find("逐棒分析检查单")
    pos_signal = user.find("文件16-K线信号识别")
    pos_risk = user.find("文件17-止损和止盈与仓位管理")
    assert 0 <= pos_strategy < pos_risk, "Stage 2 user task files are out of order"
    assert 0 <= pos_bar_by_bar < pos_signal < pos_risk


def test_stage2_user_prompt_contains_stage1_json(assembler: PromptAssembler):
    """Stage 2 user prompt must embed the Stage 1 JSON."""
    frame = _make_frame()
    stage1_json = {"cycle_position": "spike", "direction": "bearish"}
    messages = assembler.build_stage2(frame, stage1_json, [], [])
    user = messages[1]["content"]
    assert "spike" in user
    assert "bearish" in user


def test_stage2_user_prompt_uses_routed_strategy_only_by_default(
    assembler: PromptAssembler,
):
    """Default Stage 2 loads router output + base files, not the full strategy pack."""
    frame = _make_frame()
    routed = ["上涨通道分析识别.txt", "上涨通道交易策略.txt"]
    messages = assembler.build_stage2(
        frame,
        {"cycle_position": "normal_channel", "direction": "bullish", "gate_result": "proceed"},
        routed,
        [],
    )
    user = messages[1]["content"]
    assert "上涨通道分析识别" in user
    assert "文件17-止损和止盈与仓位管理" in user
    assert "下跌通道分析识别" not in user
    assert "极速下跌分析识别" not in user


def test_stage1_output_reminder_present(assembler: PromptAssembler):
    """Stage 1 user turn must contain the output format reminder."""
    frame = _make_frame()
    messages = assembler.build_stage1(frame)
    user = messages[1]["content"]
    assert "cycle_position" in user
    assert "diagnosis_confidence" in user
    assert "bar_by_bar_summary" in user
    assert "逐K摘要硬规则" in user
    assert "gate_trace" in user
    assert "gate_result" in user


def test_stage1_original_mode_requires_full_gate_trace(assembler: PromptAssembler):
    """Original mode must not tell the model to omit program-prefilled gates."""
    frame = _make_frame()
    messages = assembler.build_stage1(frame, analysis_mode="original")
    user = messages[1]["content"]

    assert "原始分析过程闸门硬规则" in user
    assert "0.1" in user
    assert "0.2" in user
    assert "1.1" in user
    assert "2.3" in user
    assert "2.4" in user
    assert "不要使用“程序已经判定 / 由程序填充 / AI 不输出”" in user
    assert "程序预填充节点判断依据" not in user


def test_stage1_optimized_mode_keeps_program_prefill_hint(assembler: PromptAssembler):
    """Optimized mode keeps the local deterministic prefill path."""
    frame = _make_frame()
    messages = assembler.build_stage1(frame, analysis_mode="optimized")
    user = messages[1]["content"]

    assert "程序预填充节点判断依据" in user
    assert "原始分析过程闸门硬规则" not in user


def test_stage2_output_contract_present(assembler: PromptAssembler):
    """Stage 2 user turn must contain the output contract with null rule."""
    frame = _make_frame()
    messages = assembler.build_stage2(frame, {}, [], [])
    user = messages[1]["content"]
    assert "不下单" in user
    assert "order_direction" in user
    assert "bar_analysis" in user
    assert "entry_basis_bar" in user
    assert "突破单 entry_price 硬规则" in user
    assert "§9 逐K信号链与新鲜度硬规则" in user
    assert "K线几何特征" in user
    assert "EMA缺口数" in user
    assert "decision_trace" in user
    assert "terminal" in user


def test_stage2_experience_entries_included(assembler: PromptAssembler):
    """Stage 2 user turn must include experience entries when provided."""
    frame = _make_frame()
    entries = [{"cycle_position": "spike", "outcome": "success"}]
    messages = assembler.build_stage2(frame, {}, [], entries)
    user = messages[1]["content"]
    assert "经验库" in user
    assert "案例 1" in user


def test_stage2_system_prompt_only_matches_build_stage2(assembler: PromptAssembler):
    """stage2_system_prompt_only must return the same system content as build_stage2."""
    frame = _make_frame()
    strategy_files = ["上涨通道分析识别.txt"]
    entries = [{"note": "test"}]
    messages = assembler.build_stage2(frame, {}, strategy_files, entries)
    system_from_build = messages[0]["content"]
    system_only = assembler.stage2_system_prompt_only(strategy_files, entries)
    assert system_from_build == system_only


def test_kline_table_contains_nan_as_na(assembler: PromptAssembler):
    """K-line table renders NaN indicator values as 'N/A'."""
    bars = (
        KlineBar(seq=1, ts_open=1_700_000_000.0, open=2600.0, high=2610.0,
                 low=2590.0, close=2605.0, volume=1000.0, closed=False),
    )
    indicators = IndicatorBundle(
        ema20=(float("nan"),),
        atr14=(float("nan"),),
    )
    frame = KlineFrame(
        symbol="XAUUSD", timeframe="1h", bars=bars,
        indicators=indicators, snapshot_ts_local_ms=1_700_000_000_000,
    )
    messages = assembler.build_stage1(frame)
    user = messages[1]["content"]
    assert "N/A" in user


def test_stage1_message_roles(assembler: PromptAssembler):
    """build_stage1 must return exactly [system, user] messages."""
    frame = _make_frame()
    messages = assembler.build_stage1(frame)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


def test_stage2_message_roles(assembler: PromptAssembler):
    """build_stage2 must return exactly [system, user] messages."""
    frame = _make_frame()
    messages = assembler.build_stage2(frame, {}, [], [])
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


def test_stage2_continuation_omits_stage1_user_prompt(assembler: PromptAssembler):
    """Stage 2 must not duplicate the huge Stage 1 user turn; K-line table lives in Stage 2 user."""
    frame = _make_frame()
    stage1_messages = assembler.build_stage1(frame)
    stage1_json = {"cycle_position": "spike", "direction": "bearish", "gate_result": "proceed"}

    messages = assembler.build_stage2_continuation(
        frame=frame,
        stage1_messages=stage1_messages,
        stage1_reply_content='{"cycle_position":"spike","direction":"bearish"}',
        stage1_json=stage1_json,
        strategy_files=["下跌通道分析识别.txt", "下跌通道交易策略.txt"],
        experience_entries=[],
    )

    # New 4-message structure: system, user(S1), assistant(S1), user(S2)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    # System prompt includes full binary tree (same as Stage 1 now)
    assert "二元决策.txt" in messages[0]["content"] or "## 3." in messages[0]["content"]
    # Message [1] is Stage 1 user prompt (with K-line table)
    assert "K线数据" in messages[1]["content"]
    assert "序号 | 时间" in messages[1]["content"]  # K-line table present
    # Message [2] is Stage 1 assistant reply
    assert "cycle_position" in messages[2]["content"]
    # Message [3] is Stage 2 user prompt (without K-line table)
    assert "沿用上一轮阶段一用户消息中的同一份 K线数据" in messages[3]["content"]
    assert "下跌通道分析识别" in messages[3]["content"]
    assert "上涨通道分析识别" not in messages[3]["content"]
    assert "【最后一步·必做】" in messages[3]["content"]


def test_stage2_prompt_includes_balanced_stance_guidance(assembler: PromptAssembler):
    frame = _make_frame()
    messages = assembler.build_stage2(frame, {}, [], [], decision_stance="balanced")
    user = messages[1]["content"]
    assert "交易倾向" in user
    assert "均衡" in user
    assert "次优但可执行" in user


def test_stage2_prompt_conservative_omits_balanced_only_hints(assembler: PromptAssembler):
    frame = _make_frame()
    messages = assembler.build_stage2(frame, {}, [], [], decision_stance="conservative")
    user = messages[1]["content"]
    assert "当前系统默认" in user
    assert "次优但可执行" not in user


def test_incremental_stage1_prompt_includes_previous_record_and_new_bars(
    assembler: PromptAssembler,
):
    """Incremental Stage 1 with full previous record uses 4-message continuation."""
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    frame = _make_frame(5)
    # Build a full Stage 1 to get realistic messages/response for the previous record
    full_s1_messages = assembler.build_stage1(frame)
    prev_user = next(m["content"] for m in full_s1_messages if m["role"] == "user")
    prev_assistant = '{"cycle_position":"normal_channel","gate_result":"proceed"}'

    previous = AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-01-01T00:00:00.000",
            timestamp_local_ms=1,
            symbol="XAUUSD",
            timeframe="1h",
            bar_count=5,
            ai_provider={},
        ),
        kline_data=[],
        htf_text="",
        stage1_messages=full_s1_messages,
        stage1_response={"content": prev_assistant},
        stage1_diagnosis={"cycle_position": "normal_channel"},
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={"decision": {"order_type": "不下单"}},
        strategy_files_used=["上涨通道分析识别.txt"],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )

    messages = assembler.build_incremental_stage1(frame, previous, 2)

    # 4-message continuation structure: system, user(prev S1), assistant(prev S1 reply), user(incremental)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    # Message [1] is the previous full Stage 1 user prompt (with K-line table)
    assert messages[1]["content"] == prev_user
    # Message [2] is the previous Stage 1 reply
    assert messages[2]["content"] == prev_assistant
    # Message [3] is the incremental task
    incremental_user = messages[3]["content"]
    assert "阶段一增量更新任务" in incremental_user
    assert "新增已收盘K线:2" in incremental_user
    assert "上一轮已完成分析" in incremental_user
    assert "normal_channel" in incremental_user
    # Anti-anchoring directives must be present
    assert "反锚定要求" in incremental_user
    assert "不要因为上一轮已得出结论就倾向于延续它" in incremental_user
    assert "宁可过度更新，不可锚定延续" in incremental_user
    assert "先独立审视完整 K 线数据" in incremental_user
    # No full K-line table in the incremental user message
    assert "当前完整 K线数据" not in incremental_user
    assert "当前完整 K线几何特征" not in incremental_user
    # But new K-line data is present
    assert "新增 K线数据" in incremental_user
    assert "完整窗口计算" in incremental_user


def test_incremental_stage1_raises_without_previous_messages(
    assembler: PromptAssembler,
):
    """Incremental Stage 1 raises ValueError when previous record lacks messages."""
    import pytest
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    frame = _make_frame(5)
    previous = AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-01-01T00:00:00.000",
            timestamp_local_ms=1,
            symbol="XAUUSD",
            timeframe="1h",
            bar_count=5,
            ai_provider={},
        ),
        kline_data=[],
        htf_text="",
        stage1_messages=[],  # empty → must raise
        stage1_response=None,  # None → must raise
        stage1_diagnosis={"cycle_position": "normal_channel"},
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={"decision": {"order_type": "不下单"}},
        strategy_files_used=["上涨通道分析识别.txt"],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )

    with pytest.raises(ValueError, match="stage1_messages contains no user message"):
        assembler.build_incremental_stage1(frame, previous, 2)


def test_incremental_stage1_raises_without_previous_response(
    assembler: PromptAssembler,
):
    """Incremental Stage 1 raises ValueError when previous record lacks response content."""
    import pytest
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    frame = _make_frame(5)
    # Build full Stage 1 messages so stage1_messages is populated
    full_s1 = assembler.build_stage1(frame)
    previous = AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-01-01T00:00:00.000",
            timestamp_local_ms=1,
            symbol="XAUUSD",
            timeframe="1h",
            bar_count=5,
            ai_provider={},
        ),
        kline_data=[],
        htf_text="",
        stage1_messages=full_s1,  # has user message
        stage1_response={},  # empty dict → no 'content' key → must raise
        stage1_diagnosis={"cycle_position": "normal_channel"},
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={"decision": {"order_type": "不下单"}},
        strategy_files_used=["上涨通道分析识别.txt"],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )

    with pytest.raises(ValueError, match="stage1_response has no 'content' field"):
        assembler.build_incremental_stage1(frame, previous, 2)


def test_stage1_prompt_has_kline_indicator_disclaimer(assembler: PromptAssembler) -> None:
    """Stage 1 user prompt and tables warn that indicators are window-recomputed."""
    frame = _make_frame()
    messages = assembler.build_stage1(frame)
    user = messages[1]["content"]
    assert "指标非全历史延续" in user
    assert "勿逐点对比" in user


def test_stage2_prompt_has_kline_indicator_disclaimer(assembler: PromptAssembler) -> None:
    frame = _make_frame()
    messages = assembler.build_stage2(frame, {}, [], [])
    user = messages[1]["content"]
    assert "指标非全历史延续" in user


def test_render_kline_feature_table_limit_uses_full_window_header(
    assembler: PromptAssembler,
) -> None:
    """Incremental «新增» feature table: header notes full-window computation."""
    frame = _make_frame(5)
    table = assembler._render_kline_feature_table(frame, limit=2)
    assert "完整窗口计算" in table
    assert "勿逐点对比" in table
    assert table.count("序号 | 类型") == 1
    assert "K2" in table or " 2 " in table


# ── T13: Prompt assembler tests for next_bar_prediction ──────────────────────


def test_stage2_prompt_contains_prediction_instruction(assembler: PromptAssembler):
    """Stage 2 prompt must contain next_bar_prediction instruction (R4.1)."""
    frame = _make_frame()
    messages = assembler.build_stage2(frame, {}, [], [])
    user = messages[1]["content"]
    assert "next_bar_prediction" in user
    assert "probabilities" in user
    assert "direction" in user


def test_previous_prediction_rendered_in_incremental_mode(assembler: PromptAssembler):
    """With previous_record containing prediction, prompt must show summary (R5.2)."""
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    frame = _make_frame()
    stage1_messages = assembler.build_stage1(frame)
    stage1_json = {"cycle_position": "normal_channel", "direction": "bullish", "gate_result": "proceed"}

    previous = AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-01-01T00:00:00.000",
            timestamp_local_ms=1,
            symbol="XAUUSD",
            timeframe="1h",
            bar_count=5,
            ai_provider={},
        ),
        kline_data=[],
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis={"cycle_position": "normal_channel"},
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={
            "decision": {"order_type": "不下单"},
            "next_bar_prediction": {
                "direction": "bullish",
                "probabilities": {"bullish": 60, "bearish": 30, "neutral": 10},
                "reasoning": "test",
                "unpredictable": False,
            },
        },
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )

    messages = assembler.build_stage2_continuation(
        frame=frame,
        stage1_messages=stage1_messages,
        stage1_reply_content='{"cycle_position":"normal_channel","direction":"bullish"}',
        stage1_json=stage1_json,
        strategy_files=["上涨通道分析识别.txt"],
        experience_entries=[],
        previous_record=previous,
    )

    user = messages[3]["content"]  # Stage 2 user prompt is now at index 3
    assert "上一轮下一根K线预测" in user
    assert "阳线" in user
    assert "60%" in user


def test_no_previous_prediction_no_summary(assembler: PromptAssembler):
    """Without previous_record, prompt must not contain prediction summary."""
    frame = _make_frame()
    messages = assembler.build_stage2(frame, {}, [], [])
    user = messages[1]["content"]
    assert "上一轮下一根K线预测" not in user


def test_unpredictable_previous_prediction_renders_note(assembler: PromptAssembler):
    """Unpredictable previous prediction must render note (R5.2)."""
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    frame = _make_frame()
    stage1_messages = assembler.build_stage1(frame)
    stage1_json = {"cycle_position": "normal_channel", "direction": "bullish", "gate_result": "proceed"}

    previous = AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-01-01T00:00:00.000",
            timestamp_local_ms=1,
            symbol="XAUUSD",
            timeframe="1h",
            bar_count=5,
            ai_provider={},
        ),
        kline_data=[],
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis={},
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={
            "next_bar_prediction": {
                "direction": None,
                "probabilities": None,
                "reasoning": "test",
                "unpredictable": True,
            },
        },
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )

    messages = assembler.build_stage2_continuation(
        frame=frame,
        stage1_messages=stage1_messages,
        stage1_reply_content='{"cycle_position":"normal_channel"}',
        stage1_json=stage1_json,
        strategy_files=["上涨通道分析识别.txt"],
        experience_entries=[],
        previous_record=previous,
    )

    user = messages[3]["content"]  # Stage 2 user prompt is now at index 3
    assert "不可预测" in user
