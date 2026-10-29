"""Bounded real-API + production-orchestrator + Qt-rendering smoke test.

Any directory: python <absolute-script-path> --live
Re-render saved evidence without an API call: ... --render-record <record.json>
Never writes settings, places orders, sends notifications, or writes into
production records/pending. Each --live run saves its own complete record.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def render_screenshots(data, output, *, stream_record=None):
    import copy
    from pa_agent.ai.decision_display import repair_legacy_display
    from pa_agent.ai.glm_flash_lite import add_local_tree
    data = copy.deepcopy(data)
    repair_legacy_display(data)
    if data.get("trace_source") == "local_rules" and data.get("decision_adjustment"):
        add_local_tree(data)
    from PyQt6.QtWidgets import QApplication, QWidget, QHBoxLayout, QVBoxLayout, QLabel
    from PyQt6.QtGui import QFontDatabase, QFont
    from pa_agent.gui.decision_tree_panel import DecisionTreePanel
    from pa_agent.gui.decision_flow_viz import DecisionFlowVizPanel
    from pa_agent.gui.future_trend_panel import FutureTrendPanel
    from pa_agent.gui.decision_panel import DecisionPanel
    app = QApplication.instance() or QApplication([])
    # Offscreen Qt does not discover Windows fonts itself; load an existing one.
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
    if font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QFont(families[0], 10))
    app.setStyleSheet((ROOT / "pa_agent/gui/theme/dark.qss").read_text(encoding="utf-8"))
    root = QWidget()
    root.setWindowTitle("备用通道精简分析实测")
    layout = QVBoxLayout(root)
    layout.addWidget(QLabel("交易决策与未来走势"))
    row = QHBoxLayout()
    layout.addLayout(row)
    from pa_agent.gui.stage2_payload import prepare_stage2_for_ui
    prepared = prepare_stage2_for_ui(data)
    decision = DecisionPanel()
    decision.set_decision(prepared, diagnosis_summary=data.get("diagnosis_summary"))
    trend = FutureTrendPanel()
    trend.set_prediction(prepared)
    row.addWidget(decision)
    row.addWidget(trend)
    root.resize(1600, 1050)
    root.show()
    app.processEvents()
    assert root.grab().save(str(output / "decision_and_forecast.png"))
    trees = []
    for cls, name in [(DecisionTreePanel, "tree"), (DecisionFlowVizPanel, "flow")]:
        widget = cls()
        trees.append(widget)
        widget.set_trace(gate_trace=[], decision_trace=data["decision_trace"], terminal=data["terminal"],
            gate_result="proceed", gate_shortcircuited=False)
        widget.resize(1500, 1000)
        widget.show()
        app.processEvents()
        if name == "flow":
            widget.refit_view()
        app.processEvents()
        assert widget.grab().save(str(output / f"{name}.png"))
    assert data["next_bar_prediction"]["reasoning"] in trend._bar_reasoning_edit.toPlainText()
    assert data["next_cycle_prediction"]["reasoning"] in trend._cycle_reasoning_edit.toPlainText()
    if stream_record is not None:
        import json
        from pa_agent.gui.ai_stream_window import AIStreamPanel
        from pa_agent.ai.lite_output_normalize import (
            is_local_stage1, visible_lite_result_text, visible_local_diagnosis_text,
        )
        panel = AIStreamPanel()
        s1 = stream_record.get("stage1_diagnosis")
        if is_local_stage1(s1, stream_record.get("stage1_response") or {}):
            panel.show_stage_result("阶段一：市场诊断", visible_local_diagnosis_text(s1), "")
        elif s1:
            panel.show_stage_result("阶段一：市场诊断",
                json.dumps(s1, ensure_ascii=False, indent=2), "")
        panel.show_stage_result("阶段二：交易决策", visible_lite_result_text(data), "")
        trees.append(panel)
        panel.resize(1100, 820)
        panel.show()
        app.processEvents()
        assert panel.grab().save(str(output / "stream_output.png"))
        text = panel._reasoning_edit.toPlainText()
        for hidden in ("gate_result", "local_compatibility", "本地规则示意",
                       "diagnosis_confidence", "无法预测"):
            assert hidden not in text, hidden
        assert "市场诊断：当前结构判为" in text
        assert "最终决策：" in text
        assert data["next_cycle_prediction"]["reasoning"] in text
    for widget in trees:
        widget.close()
    root.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Allow one configured GLM API call")
    mode.add_argument("--render-record", type=Path, help="Re-render saved result without API access")
    mode.add_argument("--cycle-preview", type=Path, help="Add local cycle weights to saved result; no API; new evidence directory")
    args = parser.parse_args()
    if args.cycle_preview:
        import copy
        import hashlib
        from pa_agent.ai.local_cycle_forecast import fill_cycle_probabilities
        from pa_agent.demo.record_loader import frame_from_record_klines
        raw = args.cycle_preview.read_bytes()
        sample = json.loads(raw)
        frame = frame_from_record_klines(sample["kline_data"], symbol=sample["meta"]["symbol"],
            timeframe=sample["meta"]["timeframe"], snapshot_ts_local_ms=sample["meta"]["timestamp_local_ms"])
        data = copy.deepcopy(sample["stage2_decision"])
        before = copy.deepcopy(data["decision"])
        data["next_cycle_prediction"] = fill_cycle_probabilities(
            data["next_cycle_prediction"], frame=frame, stage1_json=sample.get("stage1_diagnosis"))
        assert data["decision"] == before
        output = ROOT / "docs/glm_flash_simplification" / datetime.now().strftime("cycle_preview_%Y%m%d_%H%M%S")
        output.mkdir(parents=True, exist_ok=True)
        render_screenshots(data, output)
        (output / "cycle_probability_preview.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        probs = data["next_cycle_prediction"]["probabilities"]
        summary = {"source_record_sha256": hashlib.sha256(raw).hexdigest(),
            "source_unchanged": raw == args.cycle_preview.read_bytes(), "api_calls": 0,
            "decision_unchanged": data["decision"] == before,
            "top3": sorted(probs.items(), key=lambda item: -item[1])[:3],
            "total": sum(probs.values()), "output": str(output)}
        (output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.render_record:
        data = json.loads(args.render_record.read_text(encoding="utf-8"))
        render_screenshots(data["stage2_decision"], args.render_record.parent)
        print("Re-rendered existing real response; API calls=0", flush=True)
        return 0

    from pa_agent.config.settings import Settings
    from pa_agent.ai.glm_flash_lite import is_glm_flash
    from pa_agent.ai.deepseek_client import DeepSeekClient
    from pa_agent.ai.json_validator import JsonValidator
    from pa_agent.demo.record_loader import frame_from_record_klines
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    from pa_agent.util.threading import CancelToken

    settings = Settings.model_validate(json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8")))
    candidates = [settings.provider, *settings.provider.fallbacks]
    candidate = next((p for p in candidates if is_glm_flash(p) and p.api_key.strip()), None)
    if candidate is None:
        raise RuntimeError("No configured GLM-4-flash credential; settings unchanged")
    settings = settings.model_copy(deep=True)
    settings.provider = candidate.model_copy(deep=True, update={"fallbacks": []})
    settings.general.enable_next_bar_prediction = True
    source = max((ROOT / "records/pending").glob("*.json"), key=lambda p: p.stat().st_mtime)
    sample = json.loads(source.read_text(encoding="utf-8"))
    frame = frame_from_record_klines(sample["kline_data"], symbol=sample["meta"]["symbol"],
        timeframe=sample["meta"]["timeframe"], snapshot_ts_local_ms=sample["meta"]["timestamp_local_ms"])
    output = ROOT / "docs/glm_flash_simplification" / datetime.now().strftime("live_%Y%m%d_%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    warnings = []
    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                warnings.append(record.getMessage())
    handler = Capture()
    logging.getLogger().addHandler(handler)

    class Writer:
        def save_full(self, record):
            (output / "record.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
        def save_partial(self, record, *args):
            self.save_full(record)
    class Unused:
        def __getattr__(self, name):
            raise AssertionError("Lite route unexpectedly used " + name)
    client = DeepSeekClient(settings.provider)
    orchestrator = TwoStageOrchestrator(client=client, assembler=Unused(), router=lambda _: [],
        validator=JsonValidator(settings.validation), pending_writer=Writer(), exp_reader=Unused(), settings=settings)
    print("正在通过备用通道执行一次真实分析，请稍等；不下单、不推送。", flush=True)
    try:
        record = orchestrator.submit(frame=frame, cancel_token=CancelToken(),
            on_event=lambda event: print(event.name, flush=True))
    finally:
        logging.getLogger().removeHandler(handler)
    assert record.exception is None, record.exception
    data = record.stage2_decision
    assert data and data["trace_source"] == "local_rules"
    assert not any("reasoning_content is very short" in w or "Stage 2 validation failed" in w for w in warnings), warnings
    assert "decision_trace" not in json.dumps(record.stage2_messages[:2], ensure_ascii=False)
    render_screenshots(data, output)
    result = {"source_record": source.name, "bars": len(frame.bars), "api_calls": 1,
        "exception": record.exception, "order_type": data["decision"]["order_type"],
        "trace_source": data["trace_source"], "warnings": warnings,
        "screenshots": ["decision_and_forecast.png", "tree.png", "flow.png"]}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    print("OUTPUT=" + str(output), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
