"""Unit tests for JSON repair in json_validator."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa_agent.ai.json_validator import (
    JsonValidator,
    Ok,
    ValidationError,
    _repair_unclosed_string_before_brace,
    _repair_unescaped_quotes,
    _strip_fences,
)

_SAMPLE = Path(__file__).resolve().parents[2] / "tools" / "stage2_raw_sample.txt"
from tests.fixtures.validators import schema_test_validator

_validator = schema_test_validator()


@pytest.mark.skipif(
    not _SAMPLE.exists(),
    reason="fixture tools/stage2_raw_sample.txt is not committed to the repo "
    "(tracked in docs/源码分析报告.md)",
)
def test_stage2_raw_sample_repair_then_parse():
    """Broken stage-2 sample with inner quotes must parse after repair."""
    raw = _SAMPLE.read_text(encoding="utf-8")
    stripped = _strip_fences(raw)
    repaired = _repair_unescaped_quotes(stripped)
    obj = json.loads(repaired)
    assert obj["decision"]["order_type"] == "不下单"
    assert "在区间中部入场" in obj["decision"]["reasoning"]


@pytest.mark.skipif(
    not _SAMPLE.exists(),
    reason="fixture tools/stage2_raw_sample.txt is not committed to the repo "
    "(tracked in docs/源码分析报告.md)",
)
def test_strip_fences_includes_repair():
    """_strip_fences applies quote repair so json.loads succeeds directly."""
    raw = _SAMPLE.read_text(encoding="utf-8")
    obj = json.loads(_strip_fences(raw))
    assert isinstance(obj["decision_trace"], list)


def test_repair_unclosed_string_before_brace_fixes_summary_newline() -> None:
    broken = '{"summary":"unfinished\n}'
    repaired = _repair_unclosed_string_before_brace(broken)
    obj = json.loads(repaired)
    assert obj["summary"] == "unfinished"


# ── T2: Schema backward-compatibility tests ──────────────────────────────────


def _valid_stage2_no_prediction() -> dict:
    """Minimal valid Stage 2 JSON without next_bar_prediction (legacy format)."""
    return {
        "decision": {
            "order_type": "不下单",
            "order_direction": None,
            "entry_price": None,
            "take_profit_price": None,
            "stop_loss_price": None,
            "reasoning": "Market unclear",
            "diagnosis_confidence": 40,
            "diagnosis_confidence_reasoning": "test",
            "trade_confidence": 30,
            "trade_confidence_reasoning": "test",
            "estimated_win_rate": None,
            "estimated_win_rate_reasoning": "test",
            "key_factors": [],
            "watch_points": [],
            "risk_assessment": "high",
            "invalidation_condition": "test",
        },
        "diagnosis_summary": {
            "cycle_position": "normal_channel",
            "direction": "bullish",
            "key_signals": [],
        },
        "decision_trace": [
            {
                "node_id": "10.3",
                "question": "q",
                "answer": "否",
                "reason": "r",
                "bar_range": "K1",
            }
        ],
        "terminal": {"node_id": "10.3", "outcome": "wait", "label": "test"},
    }


def _valid_prediction() -> dict:
    return {
        "direction": "bullish",
        "probabilities": {"bullish": 50, "bearish": 30, "neutral": 20},
        "reasoning": "阳线概率最高，趋势明确，结构清晰。",
        "unpredictable": False,
        "features_used": ["stage1_diagnosis"],
    }


def test_stage2_schema_backward_compatible_without_prediction():
    """Legacy Stage 2 JSON without predictions is auto-filled in normalizer."""
    obj = _valid_stage2_no_prediction()
    result = _validator.validate("stage2", json.dumps(obj, ensure_ascii=False))
    assert isinstance(result, Ok), f"Expected Ok, got {result}"
    assert isinstance(result.obj.get("next_bar_prediction"), dict)
    assert isinstance(result.obj.get("next_cycle_prediction"), dict)


def _valid_cycle_prediction() -> dict:
    return {
        "cycle": "normal_channel",
        "direction": "bullish",
        "probabilities": {
            "spike": 5,
            "micro_channel": 5,
            "tight_channel": 10,
            "normal_channel": 40,
            "broad_channel": 15,
            "trending_tr": 10,
            "trading_range": 10,
            "extreme_tr": 5,
        },
        "reasoning": "通道延续概率最高。",
        "unpredictable": False,
        "features_used": ["stage1_diagnosis"],
    }


def test_stage2_schema_accepts_valid_prediction():
    """Stage 2 JSON with valid predictions must validate OK."""
    obj = _valid_stage2_no_prediction()
    obj["next_bar_prediction"] = _valid_prediction()
    obj["next_cycle_prediction"] = _valid_cycle_prediction()
    result = _validator.validate("stage2", json.dumps(obj, ensure_ascii=False))
    assert isinstance(result, Ok), f"Expected Ok, got {result}"


def test_stage2_normalizer_aligns_prediction_direction_to_argmax():
    """Normalizer fixes direction when it disagrees with probability argmax."""
    obj = _valid_stage2_no_prediction()
    obj["next_cycle_prediction"] = _valid_cycle_prediction()
    obj["next_bar_prediction"] = {
        "direction": "bearish",
        "probabilities": {"bullish": 50, "bearish": 30, "neutral": 20},
        "reasoning": "x" * 30,
        "unpredictable": False,
        "features_used": ["stage1_diagnosis"],
    }
    result = _validator.validate("stage2", json.dumps(obj, ensure_ascii=False))
    assert isinstance(result, Ok), f"Expected Ok, got {result}"
    assert result.obj["next_bar_prediction"]["direction"] == "bullish"


# ── T6: Validator unit tests for _check_next_bar_prediction ──────────────────


def test_check_next_bar_prediction_absent_passes():
    """Missing next_bar_prediction must not cause any error."""
    errors = JsonValidator._check_next_bar_prediction({})
    assert errors == []


def test_check_next_bar_prediction_unpredictable_null_consistency():
    """unpredictable=true with null direction/probabilities must pass."""
    obj = {
        "next_bar_prediction": {
            "direction": None,
            "probabilities": None,
            "reasoning": "数据不足，无法预测方向",
            "unpredictable": True,
            "features_used": ["stage1_diagnosis"],
        }
    }
    errors = JsonValidator._check_next_bar_prediction(obj)
    assert errors == [], f"Expected no errors, got {errors}"


def test_check_next_bar_prediction_sum_out_of_tolerance():
    """Probabilities sum outside [99, 101] must error."""
    # sum=98: 50+30+18
    obj98 = {
        "next_bar_prediction": {
            "direction": "bullish",
            "probabilities": {"bullish": 50, "bearish": 30, "neutral": 18},
            "reasoning": "x" * 30,
            "unpredictable": False,
            "features_used": ["stage1_diagnosis"],
        }
    }
    errors = JsonValidator._check_next_bar_prediction(obj98)
    assert any("sum" in e for e in errors), f"sum=98 should fail, got {errors}"

    # sum=102: 50+30+22
    obj102 = {
        "next_bar_prediction": {
            "direction": "bullish",
            "probabilities": {"bullish": 50, "bearish": 30, "neutral": 22},
            "reasoning": "x" * 30,
            "unpredictable": False,
            "features_used": ["stage1_diagnosis"],
        }
    }
    errors = JsonValidator._check_next_bar_prediction(obj102)
    assert any("sum" in e for e in errors), f"sum=102 should fail, got {errors}"

    # sum=99: 50+30+19 → pass
    obj99 = {
        "next_bar_prediction": {
            "direction": "bullish",
            "probabilities": {"bullish": 50, "bearish": 30, "neutral": 19},
            "reasoning": "x" * 30,
            "unpredictable": False,
            "features_used": ["stage1_diagnosis"],
        }
    }
    errors = JsonValidator._check_next_bar_prediction(obj99)
    assert not any("sum" in e for e in errors), f"sum=99 should pass, got {errors}"

    # sum=101: 50+30+21 → pass
    obj101 = {
        "next_bar_prediction": {
            "direction": "bullish",
            "probabilities": {"bullish": 50, "bearish": 30, "neutral": 21},
            "reasoning": "x" * 30,
            "unpredictable": False,
            "features_used": ["stage1_diagnosis"],
        }
    }
    errors = JsonValidator._check_next_bar_prediction(obj101)
    assert not any("sum" in e for e in errors), f"sum=101 should pass, got {errors}"


def test_check_next_bar_prediction_direction_mismatch():
    """direction != argmax of probabilities must error."""
    obj = {
        "next_bar_prediction": {
            "direction": "bearish",
            "probabilities": {"bullish": 60, "bearish": 30, "neutral": 10},
            "reasoning": "x" * 30,
            "unpredictable": False,
            "features_used": ["stage1_diagnosis"],
        }
    }
    errors = JsonValidator._check_next_bar_prediction(obj)
    assert any("direction" in e and "argmax" in e for e in errors), (
        f"Expected direction-argmax error, got {errors}"
    )


def test_check_next_bar_prediction_invalid_fields_prefix():
    """All error messages for next_bar_prediction must start with the prefix."""
    obj = {
        "next_bar_prediction": {
            "direction": "bearish",  # mismatch
            "probabilities": {"bullish": 60, "bearish": 30, "neutral": 10},
            "reasoning": "x" * 30,
            "unpredictable": False,
            "features_used": ["stage1_diagnosis"],
        }
    }
    errors = JsonValidator._check_next_bar_prediction(obj)
    assert all(e.startswith("next_bar_prediction.") for e in errors), (
        f"Not all errors have prefix: {errors}"
    )
