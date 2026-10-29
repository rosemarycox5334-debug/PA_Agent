"""Relaxed validation while running on a fallback provider.

When the primary provider fails and the orchestrator switches to GLM-4-flash,
the fallback model often writes free text into enum fields (decision_trace
answer, order_type, ...). Retrying cannot fix that. The orchestrator flips
_on_fallback_provider=True and the validator accepts any parsable JSON object
so the GUI can render, instead of looping forever.
"""
from __future__ import annotations

import json

import pytest

from pa_agent.ai.json_validator import JsonValidator, Ok, ValidationError
from pa_agent.config.settings import ValidationSettings


def _v() -> JsonValidator:
    return JsonValidator(ValidationSettings())


_STAGE2_BAD_ENUM = {
    "decision": {
        "order_type": "限价单",
        "entry_price": None,
        "take_profit_price": None,
        "take_profit_price_2": None,
        "stop_loss_price": None,
        "reasoning": "x",
        "diagnosis_confidence": 70,
        "diagnosis_confidence_reasoning": "x",
        "trade_confidence": 50,
        "trade_confidence_reasoning": "x",
        "estimated_win_rate": None,
        "estimated_win_rate_reasoning": "",
    },
    "diagnosis_summary": "x",
    "bar_analysis": {},
    "decision_trace": [
        {
            "node_id": "3",
            "question": "区间?",
            "answer": "普通交易区间",  # invalid enum
            "reason": "x",
            "bar_range": "K5-K1",
        }
    ],
    "terminal": {"node_id": "11.4", "outcome": "trade", "label": None},
}


def test_strict_mode_rejects_bad_enum():
    """Baseline: strict mode rejects the bad enum (this is what looped before)."""
    content = json.dumps(_STAGE2_BAD_ENUM, ensure_ascii=False)
    r = _v().validate("stage2", content)
    assert isinstance(r, ValidationError)


def test_relaxed_mode_accepts_bad_enum():
    """Relaxed mode accepts the same payload — GUI can render."""
    content = json.dumps(_STAGE2_BAD_ENUM, ensure_ascii=False)
    r = _v().validate("stage2", content, relaxed=True)
    assert isinstance(r, Ok)
    assert isinstance(r.obj, dict)
    assert "decision" in r.obj


def test_relaxed_mode_accepts_arbitrary_trace_answer():
    """Any free-text answer is fine in relaxed mode."""
    payload = dict(_STAGE2_BAD_ENUM)
    payload["decision_trace"] = [
        {"node_id": str(i), "question": "q", "answer": f"自由文本{i}",
         "reason": "r", "bar_range": "K1-K1"}
        for i in range(5)
    ]
    r = _v().validate("stage2", json.dumps(payload, ensure_ascii=False), relaxed=True)
    assert isinstance(r, Ok)


def test_relaxed_mode_salvages_non_json_stage2():
    """Stage 2 plain text is salvaged into a renderable minimal object.

    (Stage 1 keeps strict behaviour - it has no salvage path.)
    """
    r = _v().validate("stage2", "not json at all", relaxed=True)
    assert isinstance(r, Ok)
    assert r.obj["decision"]["order_type"] == "不下单"
    assert r.obj["terminal"]["outcome"] == "wait"


def test_relaxed_mode_salvages_json_array_stage2():
    """A bare array carries no decision, so salvage yields the neutral shell."""
    r = _v().validate("stage2", json.dumps([1, 2, 3]), relaxed=True)
    assert isinstance(r, Ok)
    assert r.obj["decision"]["order_type"] == "不下单"


def test_relaxed_mode_stage1_also_accepted():
    """Same relaxation applies to stage 1."""
    payload = {"cycle_position": "totally_unknown_enum", "direction": "??"}
    r = _v().validate("stage1", json.dumps(payload, ensure_ascii=False), relaxed=True)
    assert isinstance(r, Ok)
