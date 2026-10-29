"""Cycle is always determined by code; "未知" never reaches the UI."""
import json

import pytest

from pa_agent.ai.glm_flash_lite import local_diagnosis
from pa_agent.ai.json_validator import JsonValidator, Ok
from pa_agent.ai.lite_output_normalize import visible_local_diagnosis_text
from pa_agent.ai.local_cycle_forecast import classify_cycle
from tests.integration.conftest import make_frame
from tests.unit.test_glm_flash_lite import payload


def test_classify_never_returns_unknown():
    assert classify_cycle(make_frame()) not in ("unknown", "", None)
    assert classify_cycle(None) == "trading_range"


def test_local_diagnosis_carries_determined_cycle():
    diag = local_diagnosis(make_frame())
    assert diag["cycle_position"] != "unknown"
    assert "未知" not in visible_local_diagnosis_text(diag)


def test_validator_fills_diagnosis_cycle_from_structure():
    result = JsonValidator().validate("stage2", json.dumps(payload()), glm_flash_lite=True,
        kline_frame=make_frame(), stage1_json=local_diagnosis(make_frame()))
    assert isinstance(result, Ok), result
    summary = result.obj["diagnosis_summary"]
    assert summary["cycle_position"] not in ("unknown", "", None)
    assert summary["cycle_position"] == result.obj["next_cycle_prediction"]["probability_current_cycle"]
    out_text = json.dumps(result.obj, ensure_ascii=False)
    assert '"cycle_position": "unknown"' not in out_text


def test_panel_chip_has_no_unknown_cycle():
    from PyQt6.QtWidgets import QApplication
    from pa_agent.gui.decision_panel import DecisionPanel
    app = QApplication.instance() or QApplication([])
    panel = DecisionPanel()
    result = JsonValidator().validate("stage2", json.dumps(payload()), glm_flash_lite=True,
        kline_frame=make_frame(), stage1_json=local_diagnosis(make_frame()))
    panel.set_decision(result.obj["decision"], diagnosis_summary=result.obj["diagnosis_summary"])
    assert "未知" not in panel._cycle_label.text()
    assert "—" != panel._cycle_label.text()
    panel.close()
    app.processEvents()


def test_main_window_replaces_legacy_unknown_cycle():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from PyQt6.QtWidgets import QApplication
    from pa_agent.gui.main_window import MainWindow
    from pa_agent.config.settings import Settings
    from pa_agent.util.threading import CancelToken
    from tests.unit.test_glm_flash_lite import orch, reply
    app = QApplication.instance() or QApplication([])
    o = orch(side_effect=[reply(payload())])
    record = o.submit(frame=make_frame(), cancel_token=CancelToken(), on_event=lambda e: None)
    assert record.exception is None, record.exception
    # Simulate a legacy record saved with an undetermined cycle.
    legacy = record.model_copy(update={"stage2_decision": dict(
        record.stage2_decision,
        diagnosis_summary={"cycle_position": "unknown", "direction": "bullish", "key_signals": []})})
    host = MagicMock()
    host._debug_widget = None
    host._prompt_files_panel = None
    host._stream_panel = None
    host._decision_panel = MagicMock()
    host._future_trend_panel = MagicMock()
    host._decision_tree_panel = None
    host._decision_flow_viz_panel = None
    host._last_analysis_record = None
    host._ctx = SimpleNamespace(settings=Settings())
    host._confidence_threshold = lambda: 0
    host._bind_decision_tree = lambda *a, **k: None
    host._ui_is_alive = lambda: True
    host._last_analysis_had_error = False
    MainWindow._on_record_ready_impl(host, legacy)
    summary = host._decision_panel.set_decision.call_args.kwargs["diagnosis_summary"]
    assert summary["cycle_position"] not in ("unknown", "", None)
