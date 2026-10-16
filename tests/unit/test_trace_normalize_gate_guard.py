"""Regression tests: the gate_result repair must require positive evidence.

2026-10 code review (finding H4): a stage-1 response truncated before its
``gate_trace`` gets an ``AUTO`` stub node plus ``gate_result="unknown"`` injected
by ``json_validator._inject_stage1_missing_tail``; ``_repair_gate_result`` then
found "no blocking §1.2/§1.3 node" and forced ``proceed``, promoting an
un-diagnosed chart into stage-2 order evaluation.
"""

from __future__ import annotations

import pytest

from pa_agent.ai.trace_normalize import _repair_gate_result


@pytest.mark.unit
def test_truncated_stub_trace_does_not_become_proceed() -> None:
    obj = {
        "gate_result": "unknown",
        # Exactly what _inject_stage1_missing_tail writes.
        "gate_trace": [
            {
                "node_id": "AUTO",
                "question": "输出是否在gate_trace前被截断？",
                "answer": "否",
                "reason": "JSON在gate_trace前截断，程序已补全最小闸门记录",
                "bar_range": "K1",
            }
        ],
    }
    _repair_gate_result(obj)
    assert obj["gate_result"] == "unknown"


@pytest.mark.unit
def test_trace_without_gate_nodes_stays_unknown() -> None:
    obj = {
        "gate_result": "wait",
        "gate_trace": [{"node_id": "2.5", "answer": "否"}],
    }
    _repair_gate_result(obj)
    assert obj["gate_result"] == "wait"


@pytest.mark.unit
def test_complete_trace_still_repairs_to_proceed() -> None:
    """Existing behaviour is preserved when the gate nodes really are present."""
    obj = {
        "gate_result": "unknown",
        "gate_trace": [
            {"node_id": "1.2", "answer": "是"},
            {"node_id": "1.3", "answer": "否"},
            {"node_id": "2.5", "answer": "中性"},
        ],
    }
    _repair_gate_result(obj)
    assert obj["gate_result"] == "proceed"


@pytest.mark.unit
def test_chaotic_market_keeps_unknown() -> None:
    obj = {
        "gate_result": "unknown",
        "gate_trace": [
            {"node_id": "1.2", "answer": "是"},
            {"node_id": "1.3", "answer": "是"},
        ],
    }
    _repair_gate_result(obj)
    assert obj["gate_result"] == "unknown"
