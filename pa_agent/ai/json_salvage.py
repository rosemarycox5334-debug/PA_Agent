# -*- coding: utf-8 -*-
"""Regex salvage for response text that no JSON repair can rescue.

GLM-4-flash occasionally emits JSON so mangled that every structural repair
fails: unbalanced quotes nested inside arrays, markdown fences injected
mid-token in several places, prose interleaved with the object. Retrying does
not help because the next attempt mangles it differently.

When the orchestrator is on a fallback provider it only needs the GUI to show
something useful: the decision and the forward-looking prediction. This module
pulls those fields straight out of the raw text and rebuilds a minimal,
schema-valid Stage 2 object. The decision tree and terminal node are left
empty on purpose - they are not needed for display.
"""
from __future__ import annotations

import json
import re

_SALVAGE_STR_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"((?:[^"\\]|\\.)*)"')
_SALVAGE_NUM_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*(-?\d+(?:\.\d+)?)')
_SALVAGE_BOOL_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*(true|false)')
_SALVAGE_NULL_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*null')

ORDER_TYPES = ("限价单", "突破单", "市价单", "不下单")


def _flat_fields(raw: str) -> dict:
    """Regex-extract every top-level-looking "key": value pair."""
    out: dict = {}
    for m in _SALVAGE_STR_RE.finditer(raw):
        k, v = m.group(1), m.group(2)
        if k in out:
            continue
        try:
            out[k] = json.loads('"' + v + '"')
        except Exception:
            out[k] = v
    for m in _SALVAGE_NUM_RE.finditer(raw):
        k, v = m.group(1), m.group(2)
        if k in out:
            continue
        try:
            out[k] = float(v) if "." in v else int(v)
        except (TypeError, ValueError):
            pass
    for m in _SALVAGE_BOOL_RE.finditer(raw):
        k, v = m.group(1), m.group(2)
        if k not in out:
            out[k] = v == "true"
    for m in _SALVAGE_NULL_RE.finditer(raw):
        if m.group(1) not in out:
            out[m.group(1)] = None
    return out


def _direction(value: object) -> str:
    """Normalise any direction token to bullish / bearish / neutral."""
    text = str(value or "").strip().lower()
    if text in ("bullish", "long", "up", "看涨", "上涨", "做多"):
        return "bullish"
    if text in ("bearish", "short", "down", "看跌", "下跌", "做空"):
        return "bearish"
    return "neutral"


def _int(value: object, default: int, lo: int = 0, hi: int = 100) -> int:
    try:
        n = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def salvage_stage2(raw: str) -> dict:
    """Rebuild a minimal, schema-valid Stage 2 object from mangled text."""
    flat = _flat_fields(raw)

    order_type = flat.get("order_type")
    if order_type not in ORDER_TYPES:
        order_type = "不下单"

    direction = _direction(
        flat.get("next_bar_direction") or flat.get("direction")
    )

    def _s(key: str, default: str, limit: int = 500) -> str:
        value = flat.get(key)
        if not isinstance(value, str) or not value.strip():
            value = default
        return value[:limit]

    decision: dict = {
        "order_type": order_type,
        "reasoning": _s(
            "reasoning",
            "模型返回的 JSON 已损坏，仅从原文恢复了核心字段（文本恢复模式）。",
            280,
        ),
        "diagnosis_confidence": _int(flat.get("diagnosis_confidence"), 50),
        "diagnosis_confidence_reasoning": _s(
            "diagnosis_confidence_reasoning", "文本恢复：无详细说明"
        ),
        "trade_confidence": _int(flat.get("trade_confidence"), 0),
        "trade_confidence_reasoning": _s(
            "trade_confidence_reasoning", "文本恢复：无详细说明"
        ),
        "estimated_win_rate": None,
        "estimated_win_rate_reasoning": None,
        "key_factors": [],
        "watch_points": [],
        "risk_assessment": _s("risk_assessment", "文本恢复：无风险评估"),
        "invalidation_condition": None,
    }

    # A trade order needs a coherent price set. If salvage could not recover
    # all of it, downgrade to 不下单 rather than emit a half-filled trade.
    if order_type != "不下单":
        needed = ("entry_price", "take_profit_price", "stop_loss_price")
        if not all(isinstance(flat.get(k), (int, float)) for k in needed):
            decision["order_type"] = "不下单"
            order_type = "不下单"

    if order_type == "不下单":
        decision.update({
            "order_direction": None,
            "entry_price": None,
            "entry_basis_bar": None,
            "entry_basis_extreme": None,
            "entry_rule": None,
            "take_profit_price": None,
            "take_profit_price_2": None,
            "stop_loss_price": None,
        })
    else:
        decision.update({
            "order_direction": (
                "做空" if direction == "bearish" else "做多"
            ),
            "entry_price": flat.get("entry_price"),
            "entry_basis_bar": flat.get("entry_basis_bar"),
            "entry_basis_extreme": flat.get("entry_basis_extreme"),
            "entry_rule": flat.get("entry_rule"),
            "take_profit_price": flat.get("take_profit_price"),
            "take_profit_price_2": (
                flat.get("take_profit_price_2") or flat.get("take_profit_price")
            ),
            "stop_loss_price": flat.get("stop_loss_price"),
            "estimated_win_rate": _int(flat.get("estimated_win_rate"), 50),
        })

    return {
        "decision": decision,
        "decision_trace": [],
        "terminal": {
            "node_id": "salvage",
            "outcome": "wait",
            "label": "模型 JSON 解析失败，已从原文恢复核心字段",
        },
        "diagnosis_summary": {
            "cycle_position": str(flat.get("cycle_position") or "unknown"),
            "direction": direction,
            "key_signals": [],
        },
        "next_bar_prediction": {
            "direction": direction,
            # A 3-way split that agrees with `direction` keeps the normalizer
            # from clearing it: with unpredictable=False and a valid
            # probabilities dict, direction survives normalization.
            "probabilities": (
                {"bullish": 60, "bearish": 20, "neutral": 20}
                if direction == "bullish"
                else {"bullish": 20, "bearish": 60, "neutral": 20}
                if direction == "bearish"
                else {"bullish": 33, "bearish": 33, "neutral": 34}
            ),
            "reasoning": "文本恢复：仅保留方向",
            "unpredictable": False,
            "features_used": ["kline_features"],
        },
        # Cycle prediction stays explicitly unpredictable: the salvaged text
        # carries no cycle enum, and inventing one would be worse than saying
        # "unknown". This field is not shown as a direction anywhere.
        "next_cycle_prediction": {
            "cycle": None,
            "direction": None,
            "probabilities": None,
            "reasoning": "文本恢复：周期预测不可用",
            "unpredictable": True,
            "features_used": ["kline_features"],
        },
    }
