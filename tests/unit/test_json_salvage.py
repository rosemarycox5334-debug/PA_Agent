"""Stage 2 JSON salvage for fallback providers.

GLM-4-flash sometimes emits JSON no repair can fix. In relaxed mode the
validator rebuilds a minimal Stage 2 object from the raw text instead of
failing the whole analysis, so the GUI still shows the decision and the
forward-looking prediction. The decision tree and terminal node are not
reconstructed - they are explicitly empty.
"""
from __future__ import annotations

import json

import pytest

from pa_agent.ai.json_salvage import salvage_stage2
from pa_agent.ai.json_validator import JsonValidator, Ok
from pa_agent.config.settings import ValidationSettings

FENCE = chr(96) * 3


def _v() -> JsonValidator:
    return JsonValidator(ValidationSettings())


# ── salvage_stage2 unit tests ────────────────────────────────────────────────

def test_salvage_recovers_order_type_and_confidence():
    raw = '{"decision": {"order_type": "不下单", "diagnosis_confidence": 65}}'
    out = salvage_stage2(raw)
    assert out["decision"]["order_type"] == "不下单"
    assert out["decision"]["diagnosis_confidence"] == 65


def test_salvage_recovers_direction():
    raw = '{"next_bar_prediction": {"direction": "bullish"}}'
    out = salvage_stage2(raw)
    assert out["next_bar_prediction"]["direction"] == "bullish"
    assert out["diagnosis_summary"]["direction"] == "bullish"


def test_salvage_maps_chinese_direction_tokens():
    for token, expected in (("上涨", "bullish"), ("做空", "bearish"), ("中性", "neutral")):
        raw = '{"direction": "' + token + '"}'
        assert salvage_stage2(raw)["next_bar_prediction"]["direction"] == expected


def test_salvage_defaults_to_no_order_when_nothing_usable():
    out = salvage_stage2("完全不是 JSON 的一段说明")
    assert out["decision"]["order_type"] == "不下单"
    assert out["next_bar_prediction"]["direction"] == "neutral"
    assert out["decision_trace"] == []


def test_salvage_downgrades_half_filled_trade_to_no_order():
    """A trade order missing any of entry / TP / SL must become 不下单."""
    raw = '{"order_type": "限价单", "entry_price": 3000}'
    out = salvage_stage2(raw)
    assert out["decision"]["order_type"] == "不下单"
    assert out["decision"]["entry_price"] is None


def test_salvage_keeps_complete_trade_order():
    raw = (
        '{"order_type": "限价单", "order_direction": "做多",'
        ' "entry_price": 3000, "take_profit_price": 3060, "stop_loss_price": 2970,'
        ' "direction": "bullish"}'
    )
    out = salvage_stage2(raw)
    assert out["decision"]["order_type"] == "限价单"
    assert out["decision"]["entry_price"] == 3000
    assert out["decision"]["take_profit_price"] == 3060
    assert out["decision"]["stop_loss_price"] == 2970
    assert out["decision"]["order_direction"] == "做多"


def test_salvage_terminal_is_wait_and_trace_empty():
    out = salvage_stage2("garbage")
    assert out["terminal"]["outcome"] == "wait"
    assert out["decision_trace"] == []


def test_salvage_next_bar_prediction_survives_normalizer():
    """unpredictable=False + matching probabilities keeps the direction."""
    out = salvage_stage2('{"direction": "bullish"}')
    assert out["next_bar_prediction"]["unpredictable"] is False
    probs = out["next_bar_prediction"]["probabilities"]
    assert probs["bullish"] > probs["bearish"]


# ── validator integration ────────────────────────────────────────────────────

def test_strict_mode_does_not_salvage():
    r = _v().validate("stage2", "plain text, not JSON", relaxed=False)
    assert not isinstance(r, Ok)


def test_relaxed_mode_salvages_plain_text_stage2():
    r = _v().validate("stage2", "这是一段说明文字", relaxed=True)
    assert isinstance(r, Ok)
    assert r.obj["decision"]["order_type"] == "不下单"
    assert r.obj["decision_trace"] == []
    assert r.obj["terminal"]["outcome"] == "wait"


def test_relaxed_mode_salvages_json_array_stage2():
    r = _v().validate("stage2", "[1, 2, 3]", relaxed=True)
    assert isinstance(r, Ok)
    assert r.obj["decision"]["order_type"] == "不下单"


def test_relaxed_mode_does_not_salvage_stage1():
    """Stage 1 has no salvage path: a plain-text reply still fails."""
    r = _v().validate("stage1", "这是一段说明文字", relaxed=True)
    assert not isinstance(r, Ok)


def test_relaxed_mode_fills_missing_top_level_keys():
    """Renderer reads decision_trace / terminal unconditionally."""
    r = _v().validate("stage2", "garbage", relaxed=True)
    assert isinstance(r, Ok)
    for key in ("decision_trace", "terminal", "diagnosis_summary",
                "next_bar_prediction", "next_cycle_prediction"):
        assert key in r.obj, f"missing {key}"


def test_relaxed_salvage_direction_survives_full_pipeline():
    raw = (
        '{"decision": {"order_type": "不下单", "reasoning": "区间中部"'
        ', "diagnosis_confidence": 65}'
        ', "next_bar_prediction": {"direction": "bullish"'
        '  "reasoning": "K1 阳线"'
    )
    r = _v().validate("stage2", raw, relaxed=True)
    assert isinstance(r, Ok)
    assert r.obj["next_bar_prediction"]["direction"] == "bullish"


@pytest.mark.parametrize("bad_payload", [
    FENCE + "json" + chr(10) + '{"decision": {"order_type": "不下单"' + chr(10),
    "not json at all",
    '{"decision": {"order_type": "不下单", "decision_trace": [{"answer": 普通交易区间"}]}',
    '[]',
])
def test_relaxed_never_crashes_on_any_garbage(bad_payload):
    r = _v().validate("stage2", bad_payload, relaxed=True)
    assert isinstance(r, Ok)
    assert isinstance(r.obj.get("decision"), dict)
