"""Exact-route contract, risk regression, and real Qt rendering (no API)."""
from __future__ import annotations

import copy
import json
from unittest.mock import MagicMock

import pytest

from pa_agent.ai.deepseek_client import AIReply, AIUsage
from pa_agent.ai.glm_flash_lite import (
    NOTICE, add_local_tree, build_messages, check_contract, is_glm_flash, local_diagnosis,
)
from pa_agent.ai.json_validator import JsonValidator, Ok, ValidationError
from pa_agent.config.settings import AIProviderSettings, Settings
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.util.threading import CancelToken
from tests.integration.conftest import make_frame


def payload():
    return {
        "decision": {"order_type": "不下单", "reasoning": "目前在区间中部，先等突破。"},
        "next_bar_prediction": {"direction": "neutral", "reasoning": "下一根可能继续震荡。"},
        "next_cycle_prediction": {"direction": "bullish", "reasoning": "后续若突破上沿，有望转向上涨。"},
        "decision_trace": [{"node_id": "2.3", "answer": "普通交易区间"}],
        "terminal": {"outcome": "trade"},
    }


def reply(obj):
    return AIReply(json.dumps(obj, ensure_ascii=False), "", {"content": obj}, AIUsage(), "test", 0)


def orch(model="glm-4-flash", side_effect=None):
    settings = Settings()
    settings.provider = AIProviderSettings(model=model, api_key="test", base_url="https://test.invalid/v1")
    settings.general.enable_next_bar_prediction = True
    settings.validation.retry_enabled = False
    client = MagicMock()
    client.stream_chat.side_effect = side_effect or [reply(payload())]
    o = TwoStageOrchestrator(client=client, assembler=MagicMock(), router=lambda _: [],
        validator=JsonValidator(), pending_writer=MagicMock(), exp_reader=MagicMock(), settings=settings)
    return o


@pytest.mark.parametrize("model,expected", [("glm-4-flash", True), ("GLM-4-FLASH", True),
    ("glm-4-flashx", False), ("glm-5", False), ("deepseek-v4-flash", False)])
def test_exact_route(model, expected):
    assert is_glm_flash(AIProviderSettings(model=model)) is expected


def test_small_prompt_has_only_requested_contract_and_closed_bars():
    msgs = build_messages(make_frame(), "stage2")
    text = json.dumps(msgs, ensure_ascii=False)
    assert "decision_trace" not in text and "gate_trace" not in text
    assert "next_cycle_prediction" in text
    context = json.loads(msgs[-1]["content"])
    assert len(context["bars_oldest_to_newest"]) == 19
    assert all(b["bar"] != "K1" for b in context["bars_oldest_to_newest"])
    assert context["latest_closed_bar"]["bar"] == "K2"


def test_bad_model_tree_ignored_and_predictions_preserved():
    p = payload()
    before = copy.deepcopy(p)
    out = JsonValidator().validate("stage2", json.dumps(p, ensure_ascii=False),
        glm_flash_lite=True, kline_frame=make_frame(), stage1_json=local_diagnosis(make_frame()))
    assert isinstance(out, Ok), out
    assert out.obj["decision"]["order_type"] == "不下单"
    assert out.obj["decision"]["reasoning"] == p["decision"]["reasoning"]
    assert out.obj["next_cycle_prediction"]["reasoning"] == p["next_cycle_prediction"]["reasoning"]
    assert out.obj["next_bar_prediction"]["reasoning"] == p["next_bar_prediction"]["reasoning"]
    assert out.obj["trace_source"] == "local_rules"
    assert all(NOTICE in n["reason"] for n in out.obj["decision_trace"])
    assert "普通交易区间" not in str(out.obj["decision_trace"])
    assert p == before


@pytest.mark.parametrize("change", ["empty", "missing_forecast"])
def test_unexpected_payload_is_repaired_locally_not_raised(change):
    p = payload()
    if change == "empty":
        p = {}
    elif change == "missing_forecast":
        del p["next_cycle_prediction"]
    out = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True)
    assert isinstance(out, Ok), out
    assert out.obj["decision"]["order_type"] == "不下单"
    assert out.obj["decision"]["reasoning"]
    for key in ("next_bar_prediction", "next_cycle_prediction"):
        assert out.obj[key]["direction"] in ("bullish", "bearish", "neutral")
        assert out.obj[key]["reasoning"]
    assert out.obj.get("lite_repaired")


@pytest.mark.parametrize("bad_price", [None, True, -1, float('nan'), 2100])
def test_invalid_plan_downgraded_without_losing_forecast(bad_price):
    p = payload()
    p["decision"].update(order_type="限价单", order_direction="做多", entry_price=2000,
        stop_loss_price=bad_price, take_profit_price=2050, take_profit_price_2=None)
    out = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True)
    assert isinstance(out, Ok), out
    assert out.obj["decision"]["order_type"] == "不下单"
    assert "当前不下单" in out.obj["decision"]["reasoning"]
    assert "限价单做多" not in out.obj["decision"]["reasoning"]
    assert out.obj["decision"]["entry_price"] is None
    assert out.obj["next_cycle_prediction"]["direction"] == "bullish"
    assert sum(out.obj["next_cycle_prediction"]["probabilities"].values()) == 100
    assert out.obj["next_cycle_prediction"]["probability_source"] == "program_rules"
    assert out.obj["terminal"]["outcome"] == "wait"


def test_optional_second_target_compatible():
    p = payload()
    p["decision"].update(order_type="限价单", order_direction="做多", entry_price=2000,
        stop_loss_price=1980, take_profit_price=2040, take_profit_price_2=None)
    assert check_contract(p) is None
    from pa_agent.ai.glm_flash_lite import model_fields
    assert model_fields(p)["decision"]["take_profit_price_2"] == 2040


def test_non_glm_still_checks_model_trace():
    assert isinstance(JsonValidator().validate("stage2", json.dumps(payload())), ValidationError)


def test_full_pipeline_calls_model_once_no_diagnosis_or_tree_request(caplog):
    o = orch()
    events = []
    record = o.submit(frame=make_frame(), cancel_token=CancelToken(), on_event=events.append)
    assert record.exception is None, record.exception
    assert record.stage2_decision["trace_source"] == "local_rules"
    assert o._client.stream_chat.call_count == 1
    assert o._client.stream_chat.call_args.kwargs["thinking"] is False
    o._pending_writer.save_full.assert_called_once()
    o._assembler.build_stage1.assert_not_called()
    o._assembler.build_stage2_continuation.assert_not_called()
    assert "Stage 2 validation failed" not in caplog.text
    assert "reasoning_content is very short" not in caplog.text


@pytest.mark.parametrize("stage", ["Stage 1", "Stage 2"])
def test_midcall_primary_failure_changes_contract(stage):
    o = orch("primary", [RuntimeError("429"), reply(payload())])
    o._settings.provider.fallbacks = [AIProviderSettings(model="glm-4-flash", api_key="test")]
    messages = [{"role": "system", "content": "original complex tree prompt"}]
    r = o._stream_chat_resilient(messages, on_reasoning_token=None, on_content_token=None,
        cancel_token=CancelToken(), thinking=True, reasoning_effort="high", stage_label=stage, frame=make_frame())
    assert o._glm_flash_lite()
    assert "original complex" not in str(messages)
    if stage == "Stage 1":
        assert r.raw["source"] == "local_compatibility"
        assert o._client.stream_chat.call_count == 1
    else:
        assert o._client.stream_chat.call_count == 2
        assert o._client.stream_chat.call_args.kwargs["thinking"] is False
        assert "decision_trace" not in str(messages)


@pytest.mark.parametrize("model,should_warn", [("glm-4-flash", False), ("other-thinking-model", True)])
def test_missing_reasoning_warning_is_route_specific(model, should_warn, caplog):
    import logging
    from types import SimpleNamespace as NS
    from unittest.mock import patch
    from pa_agent.ai.deepseek_client import DeepSeekClient
    client = DeepSeekClient(AIProviderSettings(model=model, api_key="test", base_url="https://test.invalid/v1"))
    chunk = NS(id="test", model=model, usage=None,
        choices=[NS(delta=NS(content='{}', reasoning_content=None, reasoning_details=None))])
    api = MagicMock()
    api.return_value.chat.completions.create.return_value = iter([chunk])
    with patch("pa_agent.ai.deepseek_client._OpenAI", api), caplog.at_level(logging.WARNING):
        client.stream_chat([{"role": "user", "content": "test"}], thinking=True)
    assert ("reasoning_content is very short" in caplog.text) is should_warn


def test_console_neutral_and_file_details_unchanged():
    import logging
    from pa_agent.util.logging import ConsoleFormatter, MaskingFormatter
    text = "Stage 2 provider error (RuntimeError); switching to fallback model=glm-4-flash base_url=https://example.test"
    record = logging.LogRecord("pa_agent", logging.WARNING, "", 1, text, (), None)
    public = ConsoleFormatter("%(message)s").format(record)
    assert "备用通道" in public and "glm-4-flash" not in public
    assert MaskingFormatter("%(message)s").format(record) == text


def test_whole_pipeline_switch_at_stage2():
    from tests.integration.conftest import VALID_STAGE1
    from tests.fixtures.validators import schema_test_validator
    o = orch("primary", [reply(VALID_STAGE1), RuntimeError("primary unavailable"), reply(payload())])
    o._validator = schema_test_validator()
    o._settings.provider.fallbacks = [AIProviderSettings(model="glm-4-flash", api_key="test")]
    o._assembler.build_stage1.return_value = [{"role": "system", "content": "primary stage1"}]
    o._assembler.build_stage2_continuation.return_value = [{"role": "system", "content": "complex tree"}]
    r = o.submit(frame=make_frame(), cancel_token=CancelToken(), on_event=lambda e: None)
    assert r.exception is None, r.exception
    assert r.stage2_decision["trace_source"] == "local_rules"
    assert "complex tree" not in str(r.stage2_messages)
    assert o._client.stream_chat.call_count == 3


def test_tree_binding_and_both_qt_widgets(tmp_path):
    from PyQt6.QtWidgets import QApplication
    from pa_agent.gui.decision_tree_panel import DecisionTreePanel
    from pa_agent.gui.decision_flow_viz import DecisionFlowVizPanel
    from pa_agent.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    out = add_local_tree(payload())
    tree = DecisionTreePanel()
    flow = DecisionFlowVizPanel()
    host = MagicMock()
    host._decision_tree_panel = tree
    host._decision_flow_viz_panel = flow
    host._demo_mode = True
    try:
        MainWindow._bind_decision_tree(host, out, {"gate_trace": [{"node_id": "0.1", "answer": "是"}]})
        tree.resize(1100, 750)
        flow.resize(1400, 900)
        tree.show()
        flow.show()
        app.processEvents()
        flow.refit_view()
        assert flow._default_zoom_factor() == 1.0
        assert "本地规则示意" not in flow._legend_label.text()
        assert "AI 实际路径" not in flow._legend_label.text()
        assert "路径展示" in flow._hud_label.text()
        assert "本地规则示意" not in tree._terminal_banner.text()
        assert tree._gate_hint.text() == ""
        assert "周期概率" in tree._analysis_details.body.text()
        assert tree._analysis_details.body.isHidden()
        tree._analysis_details.button.click()
        assert not tree._analysis_details.body.isHidden()
        assert not tree.grab().isNull()
        assert not flow.grab().isNull()
    finally:
        tree.close()
        flow.close()
