"""Helpers to merge stage-2 JSON for UI panels."""
from __future__ import annotations

import copy
from typing import Any


def merge_stage2_for_panels(s2_full: dict[str, Any] | None) -> dict[str, Any]:
    """Merge top-level prediction fields into the inner decision dict for panels."""
    if not isinstance(s2_full, dict):
        return {}
    inner = s2_full.get("decision")
    payload: dict[str, Any] = dict(inner) if isinstance(inner, dict) else {}
    for key in ("next_bar_prediction", "next_cycle_prediction", "trace_source", "diagnosis_summary", "model_decision", "decision_adjustment"):
        if key in s2_full:
            payload[key] = s2_full[key]
    return payload


def prepare_stage2_for_ui(
    s2_full: dict[str, Any] | None,
    *,
    stage1_json: dict[str, Any] | None = None,
    skip_next_bar: bool = False,
) -> dict[str, Any]:
    """Normalize / synthesize predictions for display (e.g. demo replay of old records)."""
    if not isinstance(s2_full, dict):
        return {}
    out = copy.deepcopy(s2_full)
    from pa_agent.ai.decision_display import ensure_min_rr, repair_legacy_display
    repair_legacy_display(out)
    if out.get("trace_source") == "local_rules" and isinstance(out.get("decision"), dict):
        # Old saved lite records may still carry a sub-1R target.
        ensure_min_rr(out["decision"])
    repair_legacy_display(out)
    from pa_agent.ai.stage2_normalizer import ensure_stage2_predictions

    if skip_next_bar:
        out.pop("next_bar_prediction", None)

    ensure_stage2_predictions(out, stage1_json=stage1_json, skip_next_bar=skip_next_bar)
    return merge_stage2_for_panels(out)
