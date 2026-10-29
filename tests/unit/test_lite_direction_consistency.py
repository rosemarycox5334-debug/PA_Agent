"""Diagnosis chip and forecast cards must never show opposite directions."""
import json

import pytest

from pa_agent.ai.json_validator import JsonValidator, Ok
from tests.integration.conftest import make_frame
from tests.unit.test_glm_flash_lite import payload

DIRS = ("bullish", "bearish", "neutral")


@pytest.mark.parametrize("summary_dir", ["bullish", "bearish", "neutral", None, "unknown"])
@pytest.mark.parametrize("model_dir", ["bullish", "bearish", "neutral", "unpredictable"])
def test_single_direction_source_across_panels(summary_dir, model_dir):
    p = payload()
    p["next_cycle_prediction"]["direction"] = model_dir
    p["next_bar_prediction"]["direction"] = model_dir
    p["diagnosis_summary"] = {"cycle_position": "unknown", "direction": summary_dir,
                              "key_signals": []}
    out = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame())
    assert isinstance(out, Ok), out
    summary = out.obj["diagnosis_summary"]
    forecast = out.obj["next_cycle_prediction"]
    assert summary["direction"] == forecast["direction"]
    assert summary["direction"] in DIRS
    assert summary["cycle_position"] not in ("unknown", "", None)


def test_mismatch_is_recorded_not_silently_swallowed():
    p = payload()
    p["next_cycle_prediction"]["direction"] = "bearish"
    p["diagnosis_summary"] = {"cycle_position": "trending_tr", "direction": "bullish",
                              "key_signals": []}
    out = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame(),
        stage1_json={"cycle_position": "trending_tr", "direction": "bullish"})
    summary = out.obj["diagnosis_summary"]
    assert summary["direction"] == "bearish"
    assert summary["direction_before_unify"] == "bullish"


def test_panels_show_same_direction():
    from PyQt6.QtWidgets import QApplication
    from pa_agent.gui.decision_panel import DecisionPanel
    from pa_agent.gui.future_trend_panel import FutureTrendPanel
    app = QApplication.instance() or QApplication([])
    p = payload()
    p["next_cycle_prediction"]["direction"] = "bearish"
    p["next_bar_prediction"]["direction"] = "bearish"
    p["diagnosis_summary"] = {"cycle_position": "unknown", "direction": "bullish",
                              "key_signals": []}
    out = JsonValidator().validate("stage2", json.dumps(p), glm_flash_lite=True,
        kline_frame=make_frame(), stage1_json=None)
    obj = out.obj
    decision = DecisionPanel()
    decision.set_decision(obj["decision"], diagnosis_summary=obj["diagnosis_summary"])
    trend = FutureTrendPanel()
    from pa_agent.gui.stage2_payload import prepare_stage2_for_ui
    trend.set_prediction(prepare_stage2_for_ui(obj))
    chip = decision._trend_label.text()
    chips = " ".join(lbl.text() for lbl in trend._chip_labels)
    if "看跌" in chip:
        assert "看涨" not in chips
    if "看涨" in chip:
        assert "看跌" not in chips
    assert "未知" not in decision._cycle_label.text()
    decision.close()
    trend.close()
    app.processEvents()
