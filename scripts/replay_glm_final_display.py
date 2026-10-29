"""Revalidate a saved GLM reply and render the final UI; zero network calls."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    args = parser.parse_args()
    from pa_agent.ai.json_validator import JsonValidator, Ok
    from pa_agent.demo.record_loader import frame_from_record_klines
    from scripts.smoke_glm_flash_lite import render_screenshots
    raw = args.record.read_bytes()
    r = json.loads(raw)
    frame = frame_from_record_klines(r["kline_data"], symbol=r["meta"]["symbol"],
        timeframe=r["meta"]["timeframe"], snapshot_ts_local_ms=r["meta"]["timestamp_local_ms"])
    reply = r["stage2_response"]["content"]
    result = JsonValidator().validate("stage2", reply, glm_flash_lite=True,
        kline_frame=frame, stage1_json=r.get("stage1_diagnosis"),
        decision_stance=r["meta"].get("decision_stance"))
    assert isinstance(result, Ok), result
    out = ROOT / "docs/glm_flash_simplification" / datetime.now().strftime("final_display_%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    payload = result.obj
    (out / "stage2_final.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    render_screenshots(payload, out, stream_record={
        "stage1_diagnosis": r.get("stage1_diagnosis"),
        "stage1_response": r.get("stage1_response") or {}})
    summary = {"api_calls": 0, "source_record": args.record.name,
        "source_sha256": hashlib.sha256(raw).hexdigest(), "source_unchanged": raw == args.record.read_bytes(),
        "original_order_type": payload.get("model_decision", {}).get("order_type"),
        "final_order_type": payload["decision"]["order_type"],
        "final_reasoning": payload["decision"]["reasoning"],
        "adjustment": payload.get("decision_adjustment"),
        "next_bar": payload["next_bar_prediction"],
        "next_cycle": payload["next_cycle_prediction"], "output": str(out)}
    (out / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
