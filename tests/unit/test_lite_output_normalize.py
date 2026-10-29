"""GLM spelling tolerance: unusable directions are recomputed, never displayed."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pa_agent.ai.json_validator import JsonValidator, Ok, ValidationError
from pa_agent.ai.lite_output_normalize import (
    normalize_lite_payload, is_local_stage1, visible_lite_result_text,
)
from pa_agent.ai.local_cycle_forecast import derive_forecast
from pa_agent.config.settings import AIProviderSettings
from pa_agent.util.threading import CancelToken
from tests.integration.conftest import make_frame
from tests.unit.test_glm_flash_lite import payload, orch, reply

UNUSABLE = ["unpredictable", "unknown", "null", "无法预测", "不确定", None, "", "other-value"]
CANONICAL = [(" BULLISH ", "bullish"), ("up", "bullish"), ("看涨", "bullish"),
             ("下跌", "bearish"), ("bear", "bearish"), ("sideways", "neutral"),
             ("中性", "neutral")]


@pytest.mark.parametrize("field", ["next_bar_prediction", "next_cycle_prediction"])
@pytest.mark.parametrize("raw", UNUSABLE)
def test_unusable_direction_is_recomputed_locally(raw, field):
    p = payload()
    p["decision"]["order_direction"] = "null"
    p[field]["direction"] = raw
    before = copy.deepcopy(p)
    result = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(result, Ok), result
    pred = result.obj[field]
    assert pred["direction"] in ("bullish", "bearish", "neutral")
    assert pred["unpredictable"] is False
    assert pred["direction_filled_by"] == "program_rules"
    assert pred["model_direction_raw"] == raw
    assert pred["reasoning"] and pred["reasoning"] != p[field]["reasoning"]
    assert "无法预测" not in visible_lite_result_text(result.obj)
    assert result.obj["decision"]["order_direction"] is None
    assert p == before


@pytest.mark.parametrize("field", ["next_bar_prediction", "next_cycle_prediction"])
@pytest.mark.parametrize("raw,expected", CANONICAL)
def test_known_synonyms_are_kept_not_recomputed(raw, expected, field):
    p = payload()
    p[field]["direction"] = raw
    result = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(result, Ok), result
    pred = result.obj[field]
    assert pred["direction"] == expected
    assert "direction_filled_by" not in pred
    assert pred["reasoning"] == p[field]["reasoning"]


def test_explicit_unpredictable_flag_is_replaced_too():
    p = payload()
    p["next_bar_prediction"].update(direction="bullish", unpredictable=True)
    out = normalize_lite_payload(p, frame=make_frame())
    pred = out["next_bar_prediction"]
    assert pred["unpredictable"] is False
    assert pred["direction_filled_by"] == "program_rules"
    assert pred["model_direction_raw"] == "bullish"


def test_neutral_text_saying_cannot_judge_is_replaced():
    p = payload()
    p["next_cycle_prediction"].update(
        direction="neutral", reasoning="未来走势难以预测，存在不确定性。")
    out = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(out, Ok), out
    pred = out.obj["next_cycle_prediction"]
    assert pred["direction_filled_by"] == "program_rules"
    assert "难以预测" not in pred["reasoning"]
    assert pred["model_reasoning"] == "未来走势难以预测，存在不确定性。"


def test_confident_direction_with_caveat_is_kept():
    p = payload()
    p["next_bar_prediction"].update(
        direction="bullish", reasoning="多头强势，但若跌破前低则存在不确定性。")
    out = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(out, Ok), out
    assert out.obj["next_bar_prediction"]["reasoning"].startswith("多头强势")
    assert "direction_filled_by" not in out.obj["next_bar_prediction"]


def test_derived_forecast_follows_project_rules():
    derived = derive_forecast(make_frame())
    assert derived["next_bar_prediction"]["direction"] in ("bullish", "bearish", "neutral")
    assert derived["next_cycle_prediction"]["direction"] == derived["next_bar_prediction"]["direction"]
    assert "当前结构判为" in derived["next_bar_prediction"]["reasoning"]
    assert "当前结构判为" in derived["next_cycle_prediction"]["reasoning"]


def test_missing_section_is_filled_and_true_content_errors_still_fail():
    p = payload()
    del p["next_cycle_prediction"]
    result = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(result, Ok), result
    assert result.obj["next_cycle_prediction"]["direction"] in ("bullish", "bearish", "neutral")
    assert isinstance(JsonValidator().validate("stage2", json.dumps(p)), ValidationError)


def test_invalid_order_type_downgrades_without_raising():
    p = payload()
    p["decision"]["order_type"] = "观察"
    result = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(result, Ok), result
    assert result.obj["decision"]["order_type"] == "不下单"


@pytest.mark.parametrize("fallback", [False, True])
def test_synthetic_stage1_never_sent_to_visible_stream(fallback):
    o = orch("primary" if fallback else "glm-4-flash",
             [RuntimeError("primary unavailable"), reply(payload())] if fallback else None)
    if fallback:
        o._settings.provider.fallbacks = [AIProviderSettings(model="glm-4-flash", api_key="test")]
        o._assembler.build_stage1.return_value = [{"role": "system", "content": "primary"}]
    content, reasoning = [], []
    result = o.submit(frame=make_frame(), cancel_token=CancelToken(), on_event=lambda e: None,
        on_stage1_content=content.append, on_stage1_reasoning=reasoning.append)
    assert result.exception is None, result.exception
    assert content == [] and reasoning == []
    assert is_local_stage1(result.stage1_diagnosis, result.stage1_response)
    assert result.stage1_diagnosis["gate_result"] == "proceed"  # internal audit retained


def test_both_stages_get_readable_text_never_blank():
    from PyQt6.QtWidgets import QApplication
    from pa_agent.gui.ai_stream_window import AIStreamPanel
    from pa_agent.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    o = orch(side_effect=[reply(payload())])
    record = o.submit(frame=make_frame(), cancel_token=CancelToken(), on_event=lambda e: None)
    assert record.exception is None, record.exception
    host = MagicMock()
    host._debug_widget = None
    host._prompt_files_panel = None
    host._stream_panel = AIStreamPanel()
    host._ctx = SimpleNamespace(settings=None)
    host._demo_mode = True
    # Lite route does not pre-write empty stage headers.
    prompts = []
    panel = host._stream_panel
    # Mimic the live progress callbacks that used to pre-write headers.
    panel.on_analysis_progress("阶段二分析中…")
    panel.on_analysis_progress("阶段二完成")
    MainWindow._on_record_ready_impl(host, record)
    text = panel._reasoning_edit.toPlainText()
    for section in ("【阶段一 · 市场诊断】", "【阶段二 · 交易决策】"):
        assert section in text
    # Each stage header must be immediately followed by its own content.
    assert text.index("【阶段一 · 市场诊断】") < text.index("市场诊断：当前结构判为")
    assert text.index("市场诊断：当前结构判为") < text.index("【阶段二 · 交易决策】")
    assert text.index("【阶段二 · 交易决策】") < text.index("最终决策：")
    assert text.count("【撰写回答】") == 2
    for hidden in ("gate_result", "local_compatibility", "本地规则示意",
                   "diagnosis_confidence", "无法预测", "unpredictable", "{"):
        assert hidden not in text, hidden
    panel.close()
    app.processEvents()


def test_lite_stage2_stream_does_not_push_raw_json():
    o = orch(side_effect=[reply(payload())])
    chunks = []
    record = o.submit(frame=make_frame(), cancel_token=CancelToken(), on_event=lambda e: None,
        on_stage2_content=chunks.append)
    assert record.exception is None, record.exception
    assert chunks == []
    assert record.stage2_decision["trace_source"] == "local_rules"


def test_main_window_record_ready_shows_readable_text_only():
    from PyQt6.QtWidgets import QApplication
    from pa_agent.gui.ai_stream_window import AIStreamPanel
    from pa_agent.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    p = payload()
    p["next_cycle_prediction"]["direction"] = "unpredictable"
    o = orch(side_effect=[reply(p)])
    record = o.submit(frame=make_frame(), cancel_token=CancelToken(), on_event=lambda e: None)
    assert record.exception is None, record.exception
    host = MagicMock()
    host._debug_widget = None
    host._prompt_files_panel = None
    host._stream_panel = AIStreamPanel()
    host._ctx = SimpleNamespace(settings=None)
    host._demo_mode = True  # stops before FreeChatSession; never network
    MainWindow._on_record_ready_impl(host, record)
    text = host._stream_panel._reasoning_edit.toPlainText()
    assert "最终决策" in text and "下一根K线预期" in text and "后续走势预期" in text
    for hidden in ("gate_result", "diagnosis_confidence", "本地规则示意",
                   "local_compatibility", "无法预测", "unpredictable"):
        assert hidden not in text, hidden
    assert record.stage2_decision["next_cycle_prediction"]["reasoning"] in text
    host._stream_panel.close()
    app.processEvents()
