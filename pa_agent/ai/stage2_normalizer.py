"""Normalize common Stage 2 AI JSON variants before schema validation."""
from __future__ import annotations

import copy
import logging
from typing import Any

from pa_agent.ai.trace_normalize import normalize_stage2_traces
from pa_agent.util.price_tick import (
    normalize_breakout_basis_extreme,
    normalize_breakout_entry_price,
    parse_k_seq,
)

logger = logging.getLogger(__name__)

# Max length for decision.reasoning (stage-2 trade rationale paragraph).
DECISION_REASONING_MAX_LEN = 280

# ── Model alias mappings (Stage 1 normalizer has the same; keep in sync) ──

_SIGNAL_BAR_QUALITY_ALIASES: dict[str, str] = {
    "low": "weak",
    "high": "strong",
    "moderate": "medium",
    "poor": "weak",
    "good": "strong",
    "bad": "invalid",
    # 中文 synonyms
    "弱": "weak",
    "中": "medium",
    "强": "strong",
    "无效": "invalid",
}

_ORDER_DIRECTION_ALIASES: dict[str, str] = {
    "bearish": "做空",
    "bullish": "做多",
    "short": "做空",
    "long": "做多",
    "sell": "做空",
    "buy": "做多",
    "空头": "做空",
    "多头": "做多",
    "做空": "做空",
    "做多": "做多",
}

_ENTRY_BAR_STRENGTH_ALIASES: dict[str, str] = {
    "pending": "not_triggered",
    "waiting": "not_triggered",
    "triggered": "strong",
    "not_triggered": "not_triggered",
    "strong": "strong",
    "weak": "weak",
}

_TERMINAL_OUTCOME_ALIASES: dict[str, str] = {
    "action": "trade",
    "execute": "trade",
    "execution": "trade",
    "place_order": "trade",
    "breakout_entry": "trade",
    "breakout": "trade",
    "limit_entry": "trade",
    "market_entry": "trade",
    "entry": "trade",
    "trade_entry": "trade",
    "no_trade": "wait",
    "no_order": "wait",
    "wait": "wait",
    "reject": "reject",
    "trade": "trade",
    "proceed": "proceed",
}

_ENTRY_BAR_FRESHNESS_ALIASES: dict[str, str] = {
    "expired": "stale",
    "old": "stale",
    "aged": "stale",
    "too_old": "stale",
    # Freshness middle-grounds
    "active": "fresh",
    "ready": "fresh",
    "new": "fresh",
    "waiting": "pending",
    # "K0_trigger" / "k0_trigger" means "awaiting entry trigger at K0" — effectively pending
    "trigger": "pending",
    "k0_trigger": "pending",
    "limit_order_pending": "pending",
    "limit_pending": "pending",
    "order_pending": "pending",
    "awaiting_fill": "pending",
    "awaiting_trigger": "pending",
}

_BAR_TYPE_ENUM = frozenset({
    "trend_bull", "trend_bear", "doji", "inside",
    "outside_bull", "outside_bear", "flat", "other",
})
_ENTRY_BAR_FRESHNESS_ENUM = frozenset({"fresh", "pending", "stale", "invalid"})
_ENTRY_BAR_STRENGTH_ENUM = frozenset({"strong", "weak", "not_triggered"})
_SIGNAL_BAR_QUALITY_ENUM = frozenset({"strong", "medium", "weak", "invalid"})


_TRADE_ORDER_TYPES = frozenset({"限价单", "突破单", "市价单"})

_ORDER_TYPE_ALIASES: dict[str, str] = {
    "no_order": "不下单",
    "notrade": "不下单",
    "no_trade": "不下单",
    "hold": "不下单",
    "skip": "不下单",
    "none": "不下单",
    "wait": "不下单",
    "limit": "限价单",
    "limit_order": "限价单",
    "breakout": "突破单",
    "breakout_order": "突破单",
    "market": "市价单",
    "market_order": "市价单",
}
_NO_ORDER_PRICE_FIELDS = (
    "order_direction",
    "entry_price",
    "take_profit_price",
    "take_profit_price_2",
    "stop_loss_price",
    "entry_basis_bar",
    "entry_basis_extreme",
    "entry_rule",
)

_DECISION_SUBFIELD_KEYS: frozenset[str] = frozenset({
    "order_direction",
    "order_type",
    "entry_price",
    "entry_basis_bar",
    "entry_basis_extreme",
    "entry_rule",
    "take_profit_price",
    "take_profit_price_2",
    "stop_loss_price",
    "reasoning",
    "diagnosis_confidence",
    "diagnosis_confidence_reasoning",
    "trade_confidence",
    "trade_confidence_reasoning",
    "estimated_win_rate",
    "estimated_win_rate_reasoning",
    "key_factors",
    "watch_points",
    "risk_assessment",
    "invalidation_condition",
})

# Decision fields models sometimes nest under diagnosis_summary by mistake.
_DECISION_FIELDS_FROM_DIAG_SUMMARY: tuple[str, ...] = (
    "estimated_win_rate_reasoning",
    "estimated_win_rate",
    "trade_confidence_reasoning",
    "diagnosis_confidence_reasoning",
    "diagnosis_confidence",
    "trade_confidence",
)

# Valid enum values for features_used in next_bar_prediction / next_cycle_prediction.
# Must stay in sync with schemas.py _NEXT_BAR_PREDICTION / _NEXT_CYCLE_PREDICTION.
_VALID_FEATURES_USED = frozenset({
    "stage1_diagnosis",
    "kline_features",
    "analysis_history",
    "experience_library",
    "stage2_decision",
    "previous_prediction_summary",
})


def _strip_enum_suffix(raw: str) -> str:
    """Drop trailing annotations models append to closed enums (e.g. ``invalid（…）``)."""
    text = raw.strip()
    for sep in ("（", "(", "【", "[", "—", "–", " - ", "：", ":"):
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if head:
                return head
    return text


def _normalize_closed_enum(
    raw: object,
    allowed: frozenset[str],
    *,
    aliases: dict[str, str] | None = None,
) -> str | None:
    """Map messy model enum text to a schema token, or None if unrecognized."""
    if not isinstance(raw, str):
        return None
    text = _strip_enum_suffix(raw)
    key = text.strip().lower().replace(" ", "_")
    if aliases:
        key = aliases.get(key, key)
    if key in allowed:
        return key
    for token in sorted(allowed, key=len, reverse=True):
        if key.startswith(token):
            return token
    return None


def _stage1_bar_analysis_bar_type(stage1_json: dict[str, Any] | None) -> str | None:
    if not isinstance(stage1_json, dict):
        return None
    bar_analysis = stage1_json.get("bar_analysis")
    if not isinstance(bar_analysis, dict):
        return None
    return _normalize_closed_enum(bar_analysis.get("bar_type"), _BAR_TYPE_ENUM)


def _normalize_entry_bar_freshness(entry_bar: dict[str, Any]) -> bool:
    raw = entry_bar.get("freshness")
    mapped = _normalize_closed_enum(
        raw,
        _ENTRY_BAR_FRESHNESS_ENUM,
        aliases=_ENTRY_BAR_FRESHNESS_ALIASES,
    )
    if mapped and mapped != raw:
        entry_bar["freshness"] = mapped
        return True
    return False


def _normalize_order_type_aliases(decision: dict[str, Any]) -> bool:
    """Map English order_type slips (no_order, limit, …) to schema enums."""
    raw = str(decision.get("order_type", "") or "").strip()
    if not raw:
        return False
    key = raw.lower().replace(" ", "_").replace("-", "_")
    mapped = _ORDER_TYPE_ALIASES.get(key) or _ORDER_TYPE_ALIASES.get(raw.lower())
    if mapped and mapped != raw:
        decision["order_type"] = mapped
        logger.debug("order_type %r -> %r", raw, mapped)
        return True
    return False


def _normalize_stage2_bar_analysis_enums(
    out: dict[str, Any],
    *,
    stage1_json: dict[str, Any] | None = None,
) -> bool:
    """Strip enum annotations and sync bar_type from stage1 when available."""
    changed = False
    bar_analysis = out.get("bar_analysis")
    if not isinstance(bar_analysis, dict):
        return False

    stage1_bt = _stage1_bar_analysis_bar_type(stage1_json)
    raw_bt = bar_analysis.get("bar_type")
    norm_bt = stage1_bt or _normalize_closed_enum(raw_bt, _BAR_TYPE_ENUM)
    if norm_bt and norm_bt != raw_bt:
        bar_analysis["bar_type"] = norm_bt
        changed = True

    entry_bar = bar_analysis.get("entry_bar")
    if isinstance(entry_bar, dict):
        if _normalize_entry_bar_freshness(entry_bar):
            changed = True
        raw_strength = entry_bar.get("strength")
        norm_strength = _normalize_closed_enum(
            raw_strength,
            _ENTRY_BAR_STRENGTH_ENUM,
            aliases=_ENTRY_BAR_STRENGTH_ALIASES,
        )
        if norm_strength and norm_strength != raw_strength:
            entry_bar["strength"] = norm_strength
            changed = True

    signal_bar = bar_analysis.get("signal_bar")
    if isinstance(signal_bar, dict):
        raw_q = signal_bar.get("quality")
        norm_q = _normalize_closed_enum(
            raw_q,
            _SIGNAL_BAR_QUALITY_ENUM,
            aliases=_SIGNAL_BAR_QUALITY_ALIASES,
        )
        if norm_q and norm_q != raw_q:
            signal_bar["quality"] = norm_q
            changed = True
        raw_pat = str(signal_bar.get("pattern", "") or "").strip().lower()
        if raw_pat in ("no_signal", "no-signal", "nosignal", "not_triggered"):
            signal_bar["pattern"] = "none"
            changed = True
        if not str(signal_bar.get("reason") or "").strip():
            signal_bar["reason"] = "无独立信号棒（quality=invalid 或计划型观望）"
            changed = True

    second_entry = bar_analysis.get("second_entry")
    if isinstance(second_entry, dict) and _normalize_second_entry(second_entry):
        changed = True

    return changed


def _normalize_second_entry(second_entry: dict[str, Any]) -> bool:
    """``type`` must be a string; models often emit null when ``is_second_entry`` is false."""
    raw_type = second_entry.get("type")
    if raw_type is not None and not (
        isinstance(raw_type, str) and not str(raw_type).strip()
    ):
        return False
    second_entry["type"] = "none"
    return True


def _normalize_order_direction_value(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text in ("做多", "做空"):
        return text
    return _ORDER_DIRECTION_ALIASES.get(text.lower())


def _normalize_always_in_value(
    raw: object,
    *,
    diagnosis_direction: str | None = None,
) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    key = text.lower().replace(" ", "")
    if key in ("long", "short", "neutral"):
        return key
    if "失效" in text or "invalid" in key or key in ("none", "n/a", "na"):
        return "neutral"
    if "ais" in key or "空头" in text:
        return "short"
    if "ail" in key or "多头" in text:
        return "long"
    if "bear" in key:
        return "short"
    if "bull" in key:
        return "long"
    if "中性" in text or key == "neutral":
        return "neutral"
    if diagnosis_direction == "bearish":
        return "short"
    if diagnosis_direction == "bullish":
        return "long"
    return None


def _normalize_terminal_outcome_value(
    raw: object,
    *,
    order_type: str | None = None,
) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    key = text.lower().replace(" ", "_")
    mapped = _TERMINAL_OUTCOME_ALIASES.get(key)
    if mapped:
        if order_type == "不下单" and mapped == "trade":
            return "wait"
        return mapped
    if key in ("wait", "reject", "trade", "proceed"):
        return key
    return None


def _normalize_stage2_enum_aliases(out: dict[str, Any]) -> bool:
    """Map common OpenClaw/Agent enum slips before schema validation."""
    changed = False
    diag = out.get("diagnosis_summary")
    diag_direction = (
        str(diag.get("direction", "")).strip()
        if isinstance(diag, dict)
        else ""
    ) or None

    decision = out.get("decision")
    order_type = (
        str(decision.get("order_type", "")).strip()
        if isinstance(decision, dict)
        else None
    ) or None
    if isinstance(decision, dict):
        raw_dir = decision.get("order_direction")
        mapped_dir = _normalize_order_direction_value(raw_dir)
        if mapped_dir and mapped_dir != raw_dir:
            decision["order_direction"] = mapped_dir
            logger.debug("order_direction %r -> %r", raw_dir, mapped_dir)
            changed = True

    bar_analysis = out.get("bar_analysis")
    if isinstance(bar_analysis, dict):
        raw_ai = bar_analysis.get("always_in")
        mapped_ai = _normalize_always_in_value(
            raw_ai, diagnosis_direction=diag_direction
        )
        if mapped_ai and mapped_ai != raw_ai:
            bar_analysis["always_in"] = mapped_ai
            logger.debug("always_in %r -> %r", raw_ai, mapped_ai)
            changed = True

    terminal = out.get("terminal")
    if isinstance(terminal, dict):
        raw_outcome = terminal.get("outcome")
        mapped_outcome = _normalize_terminal_outcome_value(
            raw_outcome, order_type=order_type
        )
        if mapped_outcome and mapped_outcome != raw_outcome:
            terminal["outcome"] = mapped_outcome
            logger.debug("terminal.outcome %r -> %r", raw_outcome, mapped_outcome)
            changed = True

    return changed


def _order_type_from_decision_scalar(value: str) -> str | None:
    """Map a scalar decision token (wait/reject/limit/…) to order_type."""
    token = str(value or "").strip().lower()
    if not token:
        return None
    if token in _ORDER_TYPE_ALIASES:
        return _ORDER_TYPE_ALIASES[token]
    normalized = token.replace(" ", "_").replace("-", "_")
    if normalized in _ORDER_TYPE_ALIASES:
        return _ORDER_TYPE_ALIASES[normalized]
    outcome = _TERMINAL_OUTCOME_ALIASES.get(token) or _TERMINAL_OUTCOME_ALIASES.get(
        normalized
    )
    if outcome in ("wait", "reject"):
        return "不下单"
    return None


def _repair_diagnosis_summary_and_decision(
    out: dict[str, Any],
    *,
    stage1_json: dict[str, Any] | None = None,
) -> bool:
    """Hoist misplaced decision fields and ensure diagnosis_summary schema fields."""
    changed = False
    decision = out.get("decision")
    if not isinstance(decision, dict):
        return False
    s1 = stage1_json or {}
    dsum = out.get("diagnosis_summary")
    if isinstance(dsum, dict):
        for key in _DECISION_FIELDS_FROM_DIAG_SUMMARY:
            if key not in dsum:
                continue
            val = dsum.get(key)
            existing = decision.get(key)
            if existing not in (None, "", []) and key in decision:
                dsum.pop(key, None)
                changed = True
                continue
            if val in (None, "", []):
                dsum.pop(key, None)
                continue
            decision[key] = val
            dsum.pop(key, None)
            logger.debug("Hoisted diagnosis_summary.%s -> decision.%s", key, key)
            changed = True

        if not str(dsum.get("cycle_position") or "").strip():
            dsum["cycle_position"] = str(s1.get("cycle_position") or "unknown")
            changed = True
        if not str(dsum.get("direction") or "").strip():
            dsum["direction"] = str(s1.get("direction") or "neutral")
            changed = True
        if not isinstance(dsum.get("key_signals"), list):
            key_signals = dsum.get("key_signals")
            if not isinstance(key_signals, list):
                key_signals = list(s1.get("key_signals") or [])
            dsum["key_signals"] = key_signals
            changed = True
    return changed


def _unwrap_flat_stage2_decision(out: dict[str, Any]) -> bool:
    """Repair models that put decision fields at root or use decision=wait string."""
    changed = False
    hoisted: dict[str, Any] = {}
    for key in _DECISION_SUBFIELD_KEYS:
        if key in out:
            hoisted[key] = out.pop(key)
            changed = True

    raw = out.get("decision")
    if isinstance(raw, str):
        order_type = _order_type_from_decision_scalar(raw) or "不下单"
        decision: dict[str, Any] = {"order_type": order_type}
        decision.update(hoisted)
        out["decision"] = decision
        logger.debug(
            "Unwrapped scalar decision %r -> order_type=%s with %d hoisted fields",
            raw,
            order_type,
            len(hoisted),
        )
        return True

    if isinstance(raw, dict):
        for key, val in hoisted.items():
            existing = raw.get(key)
            if key not in raw or existing is None or existing == "" or existing == []:
                raw[key] = val
                changed = True
        return changed

    if hoisted:
        out["decision"] = hoisted
        logger.debug("Built decision object from %d hoisted root fields", len(hoisted))
        return True
    return changed


def _hoist_terminal_from_decision(out: dict[str, Any]) -> bool:
    """Move terminal nested under decision to the top level."""
    if isinstance(out.get("terminal"), dict):
        return False
    decision = out.get("decision")
    if not isinstance(decision, dict):
        return False
    nested = decision.pop("terminal", None)
    if not isinstance(nested, dict):
        return False
    out["terminal"] = nested
    logger.debug("Hoisted terminal from decision to top level")
    return True


def _ensure_decision_required_fields(
    out: dict[str, Any],
    *,
    stage1_json: dict[str, Any] | None = None,
) -> bool:
    """Fill missing decision sub-fields that commonly trigger schema retries."""
    decision = out.get("decision")
    if not isinstance(decision, dict):
        return False
    s1 = stage1_json or {}
    changed = _normalize_order_type_aliases(decision)
    if not isinstance(decision.get("key_factors"), list):
        decision["key_factors"] = []
        changed = True
    if not isinstance(decision.get("watch_points"), list):
        decision["watch_points"] = []
        changed = True
    text_defaults = {
        "reasoning": "基于阶段一诊断与当前K线结构的阶段二决策说明",
        "diagnosis_confidence_reasoning": (
            str(s1.get("htf_context") or "").strip()[:500]
            or "依据阶段一诊断与闸门结论"
        ),
        "risk_assessment": "见 watch_points 与 invalidation_condition",
    }
    for key, default in text_defaults.items():
        if not isinstance(decision.get(key), str) or not str(decision.get(key)).strip():
            decision[key] = default
            changed = True
    if decision.get("diagnosis_confidence") is None:
        try:
            decision["diagnosis_confidence"] = int(s1.get("diagnosis_confidence") or 50)
        except (TypeError, ValueError):
            decision["diagnosis_confidence"] = 50
        changed = True
    if decision.get("trade_confidence") is None:
        decision["trade_confidence"] = (
            0 if decision.get("order_type") == "不下单" else 50
        )
        changed = True
    if (
        not isinstance(decision.get("trade_confidence_reasoning"), str)
        or not decision["trade_confidence_reasoning"].strip()
    ):
        decision["trade_confidence_reasoning"] = (
            "无入场计划，不存在交易信心"
            if decision.get("order_type") == "不下单"
            else "基于结构与入场方案的综合评估"
        )
        changed = True
    if decision.get("order_type") == "不下单":
        if "estimated_win_rate" not in decision:
            decision["estimated_win_rate"] = None
            changed = True
    elif decision.get("estimated_win_rate") is None:
        decision["estimated_win_rate"] = 50
        changed = True
    if decision.get("estimated_win_rate_reasoning") is not None and not isinstance(
        decision.get("estimated_win_rate_reasoning"), str
    ):
        decision["estimated_win_rate_reasoning"] = None
        changed = True
    elif "estimated_win_rate_reasoning" not in decision:
        decision["estimated_win_rate_reasoning"] = (
            None
            if decision.get("order_type") == "不下单"
            else "基于入场/止损/目标三价与结构背景的胜率估算"
        )
        changed = True
    elif (
        decision.get("order_type") != "不下单"
        and isinstance(decision.get("estimated_win_rate_reasoning"), str)
        and not str(decision.get("estimated_win_rate_reasoning")).strip()
    ):
        decision["estimated_win_rate_reasoning"] = "基于入场/止损/目标三价与结构背景的胜率估算"
        changed = True
    terminal = out.get("terminal")
    if isinstance(terminal, dict) and not str(terminal.get("label") or "").strip():
        outcome = str(terminal.get("outcome") or "wait")
        terminal["label"] = {
            "trade": "执行下单方案",
            "reject": "交易者方程未通过",
            "wait": "等待更好 setup",
            "proceed": "继续评估",
        }.get(outcome, "阶段二终局")
        changed = True
    return changed


def _truncate_decision_reasoning(decision: dict[str, Any]) -> bool:
    """Cap decision.reasoning length to avoid verbose JSON and schema failures."""
    reasoning = decision.get("reasoning")
    if not isinstance(reasoning, str):
        return False
    text = reasoning.strip()
    if len(text) <= DECISION_REASONING_MAX_LEN:
        if text != reasoning:
            decision["reasoning"] = text
            return True
        return False
    decision["reasoning"] = text[: DECISION_REASONING_MAX_LEN - 1] + "…"
    return True


def _trace_node_answer(trace: Any, node_id: str) -> str | None:
    if not isinstance(trace, list):
        return None
    for item in trace:
        if not isinstance(item, dict):
            continue
        if str(item.get("node_id", "")).strip() == node_id:
            return str(item.get("answer", "") or "").strip()
    return None


def _section14_violated(trace: Any) -> bool:
    """Return True only when §14 answer is 是 AND the reason text confirms a violation.

    Background: §14 question is "是否触犯禁止行为清单？"
      answer=是  → violated (程序强制 order_type=不下单)
      answer=否  → not violated (can proceed)

    Some models incorrectly write answer=是 to mean "I completed the scan (no violations)".
    To guard against this common mistake we cross-check the reason text: if it contains
    explicit denial phrases (未触犯 / 未违反 / 无触犯 / 通过) we do NOT treat it as a
    violation.  This is a safety hatch — the prompt now clearly specifies answer=否 for
    the no-violation case, so future outputs should be correct.
    """
    _DENIAL_PHRASES = ("未触犯", "未违反", "无触犯", "无违规", "通过扫描", "扫描通过", "无禁止", "未触发")
    if not isinstance(trace, list):
        return False
    for item in trace:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("node_id", "")).strip()
        if not nid.startswith("14"):
            continue
        if str(item.get("answer", "")).strip() != "是":
            continue
        # answer=是: check reason for denial phrases before treating as violation
        reason = str(item.get("reason", "") or "")
        if any(phrase in reason for phrase in _DENIAL_PHRASES):
            # AI wrote answer=是 but reason says no violation — ignore (AI used wrong answer)
            logger.debug(
                "_section14_violated: node %s answer=是 but reason contains denial phrase; "
                "treating as NOT violated (AI should use answer=否 for no-violation)",
                nid,
            )
            continue
        return True
    return False


def _clear_decision_to_no_order(decision: dict[str, Any]) -> None:
    decision["order_type"] = "不下单"
    for field in _NO_ORDER_PRICE_FIELDS:
        decision[field] = None
    decision["estimated_win_rate"] = None
    decision["estimated_win_rate_reasoning"] = None
    # trade_confidence / trade_confidence_reasoning: schema requires non-null values.
    # When the breaker forces 不下单, provide valid defaults.
    if decision.get("trade_confidence") is None:
        decision["trade_confidence"] = 0
    if not isinstance(decision.get("trade_confidence_reasoning"), str) or not decision["trade_confidence_reasoning"]:
        decision["trade_confidence_reasoning"] = "无入场计划，不存在交易信心"


def _set_trace_node_answer(
    trace: Any,
    node_id: str,
    answer: str,
    *,
    reason_suffix: str = "",
) -> None:
    if not isinstance(trace, list):
        return
    for item in trace:
        if not isinstance(item, dict):
            continue
        if str(item.get("node_id", "")).strip() != node_id:
            continue
        item["answer"] = answer
        if reason_suffix:
            base = str(item.get("reason", "") or "").strip()
            item["reason"] = f"{base}{reason_suffix}".strip()
        return


def _coerce_decision_no_order(out: dict[str, Any]) -> bool:
    """When trace/terminal reject a trade, clear decision prices (common model slip)."""
    decision = out.get("decision")
    if not isinstance(decision, dict):
        return False
    _normalize_order_type_aliases(decision)

    terminal = out.get("terminal")
    outcome = (
        str(terminal.get("outcome", "")).strip()
        if isinstance(terminal, dict)
        else ""
    )
    order_type = decision.get("order_type")

    if order_type not in _TRADE_ORDER_TYPES:
        if outcome in ("wait", "reject") and order_type != "不下单":
            _clear_decision_to_no_order(decision)
            logger.debug("Coerced %r + terminal=%s to 不下单", order_type, outcome)
            return True
        return False

    trace = out.get("decision_trace")

    triggers: list[str] = []
    if _trace_node_answer(trace, "10.3") == "否":
        triggers.append("10.3=否")
    if outcome in ("wait", "reject"):
        triggers.append(f"terminal.outcome={outcome}")
    if _section14_violated(trace):
        triggers.append("§14触犯")

    if not triggers:
        return False

    _clear_decision_to_no_order(decision)
    logger.debug("Coerced decision to 不下单 (%s)", ", ".join(triggers))
    return True


def _coerce_breakout_without_basis(out: dict[str, Any]) -> bool:
    """Breakout orders require entry_basis_*; fall back to limit when missing."""
    decision = out.get("decision")
    if not isinstance(decision, dict):
        return False
    if decision.get("order_type") != "突破单":
        return False
    if decision.get("entry_basis_bar") and decision.get("entry_basis_extreme"):
        return False
    decision["order_type"] = "限价单"
    decision["entry_basis_bar"] = None
    decision["entry_basis_extreme"] = None
    decision["entry_rule"] = None
    logger.debug(
        "breakout order missing entry_basis_*; coerced to 限价单"
    )
    return True


def _repair_terminal_trade_node(out: dict[str, Any]) -> bool:
    """A successful trade should not terminate at §14 (prohibition scan)."""
    decision = out.get("decision")
    terminal = out.get("terminal")
    trace = out.get("decision_trace")
    if not isinstance(decision, dict) or not isinstance(terminal, dict):
        return False
    if decision.get("order_type") not in _TRADE_ORDER_TYPES:
        return False
    if terminal.get("outcome") != "trade":
        return False

    node_id = str(terminal.get("node_id", "") or "").strip()
    if not node_id.startswith("14"):
        return False

    replacement: str | None = None
    if isinstance(trace, list):
        for item in reversed(trace):
            if not isinstance(item, dict):
                continue
            nid = str(item.get("node_id", "") or "").strip()
            if nid.startswith("11."):
                replacement = nid
                break
        if replacement is None:
            for item in reversed(trace):
                if not isinstance(item, dict):
                    continue
                if str(item.get("node_id", "") or "").strip() == "10.3":
                    replacement = "10.3"
                    break

    if replacement is None:
        replacement = "10.3"
    terminal["node_id"] = replacement
    logger.debug("terminal.node_id %r -> %r (trade cannot terminate at §14)", node_id, replacement)
    return True


def _normalize_market_order_entry_bar(
    bar_analysis: dict[str, Any],
    decision: dict[str, Any],
) -> bool:
    """Market orders need a concrete entry_bar; borrow signal_bar when model left it pending."""
    if decision.get("order_type") != "市价单":
        return False
    entry_bar = bar_analysis.get("entry_bar")
    signal_bar = bar_analysis.get("signal_bar")
    if not isinstance(entry_bar, dict) or not isinstance(signal_bar, dict):
        return False
    if entry_bar.get("bar") is not None:
        return False
    sig_bar = signal_bar.get("bar")
    if not sig_bar:
        return False
    # Market order fills on the latest closed bar; signal_bar stays older (K2+).
    entry_bar["bar"] = str(bar_analysis.get("last_closed_bar") or "K1").strip() or "K1"
    raw_strength = str(entry_bar.get("strength") or signal_bar.get("quality") or "weak").strip().lower()
    strength_map = {"strong": "strong", "medium": "weak", "weak": "weak", "low": "weak", "high": "strong"}
    entry_bar["strength"] = strength_map.get(raw_strength, "weak")
    entry_bar["freshness"] = "fresh"
    entry_bar["follow_through"] = True
    entry_bar["still_valid"] = entry_bar.get("still_valid", True)
    logger.debug("market order: entry_bar.bar set from signal_bar %s", sig_bar)
    return True


def _normalize_signal_entry_bar_chain(bar_analysis: dict[str, Any], decision: dict[str, Any]) -> bool:
    """Signal K must be strictly older than entry K (larger seq); pending entry exempt."""
    if decision.get("order_type") not in _TRADE_ORDER_TYPES:
        return False
    signal_bar = bar_analysis.get("signal_bar")
    entry_bar = bar_analysis.get("entry_bar")
    if not isinstance(signal_bar, dict) or not isinstance(entry_bar, dict):
        return False

    strength = str(entry_bar.get("strength", "") or "").strip().lower()
    freshness = str(entry_bar.get("freshness", "") or "").strip().lower()
    pending = (
        strength == "not_triggered"
        or not entry_bar.get("bar")
        or freshness in ("pending", "stale", "invalid")
    )
    if pending:
        entry_bar["bar"] = None
        entry_bar["strength"] = "not_triggered"
        entry_bar.setdefault("freshness", "pending")
        if entry_bar.get("follow_through") in (None, "", False):
            entry_bar["follow_through"] = "pending"
        return False

    signal_seq = parse_k_seq(signal_bar.get("bar"))
    entry_seq = parse_k_seq(entry_bar.get("bar"))
    if signal_seq is None or entry_seq is None:
        return False
    if signal_seq > entry_seq:
        return False

    signal_bar["bar"] = f"K{entry_seq + 1}"
    logger.debug(
        "signal_bar K%s -> K%s (must be older than entry K%s)",
        signal_seq,
        entry_seq + 1,
        entry_seq,
    )
    return True


def _coerce_decision_when_trade_metrics_fail(
    out: dict[str, Any],
    *,
    decision_stance: str | None = None,
    kline_frame: Any = None,
) -> bool:
    """After breakout entry snap, reject orders that still fail RR / trader equation."""
    decision = out.get("decision")
    if not isinstance(decision, dict) or decision.get("order_type") not in _TRADE_ORDER_TYPES:
        return False
    if decision.get("entry_price") is None:
        return False

    from pa_agent.util.trade_metrics import validate_order_trade_metrics

    metric_errors = validate_order_trade_metrics(
        decision,
        decision_stance=decision_stance,
        kline_frame=kline_frame,
        bar_analysis=out.get("bar_analysis")
        if isinstance(out.get("bar_analysis"), dict)
        else None,
    )
    if not metric_errors:
        return False

    summary = metric_errors[0]
    _clear_decision_to_no_order(decision)
    _set_trace_node_answer(
        out.get("decision_trace"),
        "10.3",
        "否",
        reason_suffix=f"（程序按 decision 三价校验未通过：{summary}，已改为不下单。）",
    )
    terminal = out.get("terminal")
    if isinstance(terminal, dict):
        terminal["outcome"] = "reject"
        terminal["node_id"] = "10.3"
        terminal.setdefault(
            "label",
            "交易者方程/盈亏比未达标，不下单",
        )
    logger.debug("Coerced decision to 不下单 (trade metrics: %s)", summary)
    return True


def _normalize_next_cycle_prediction(
    prediction: dict[str, Any],
    *,
    stage1_json: dict[str, Any] | None = None,
) -> None:
    """In-place normalize next_cycle_prediction common model quirks. Idempotent."""
    from pa_agent.ai.cycle_enums import CYCLE_ORDER

    if not isinstance(prediction, dict):
        return

    for alt_key in ("predicted_next_cycle", "next_cycle"):
        alt_val = prediction.get(alt_key)
        if alt_val and not prediction.get("cycle"):
            prediction["cycle"] = str(alt_val).strip().lower()
    for stray in (
        "current_cycle",
        "predicted_next_cycle",
        "next_cycle",
        "confidence",
    ):
        prediction.pop(stray, None)

    # 0. Migrate primary/secondary shorthand → cycle + probabilities
    primary = prediction.pop("primary", None)
    prediction.pop("primary_probability", None)
    prediction.pop("secondary", None)
    prediction.pop("secondary_probability", None)
    if primary and not prediction.get("cycle"):
        prediction["cycle"] = str(primary).strip().lower()
    prediction.setdefault("unpredictable", False)

    # 1. unpredictable fallback
    unpredictable = bool(prediction.get("unpredictable", False))
    prediction["unpredictable"] = unpredictable

    # 2. features_used: ensure list, dedup, minimum set, filter invalid values
    feats = prediction.get("features_used")
    if not isinstance(feats, list):
        feats = []
    feats = [f for f in feats if isinstance(f, str)]
    # Filter out values not in the schema enum (e.g. "detected_patterns")
    invalid_feats = [f for f in feats if f not in _VALID_FEATURES_USED]
    if invalid_feats:
        logger.debug(
            "next_cycle_prediction.features_used dropped invalid values: %s",
            invalid_feats,
        )
    feats = [f for f in feats if f in _VALID_FEATURES_USED]
    if "stage1_diagnosis" not in feats:
        feats.insert(0, "stage1_diagnosis")
    seen: set[str] = set()
    deduped: list[str] = []
    for f in feats:
        if f not in seen:
            deduped.append(f)
            seen.add(f)
    prediction["features_used"] = deduped

    # 3. reasoning truncation
    reasoning = prediction.get("reasoning")
    if isinstance(reasoning, str) and len(reasoning) > 1500:
        prediction["reasoning"] = reasoning[:1499] + "…"
    elif not isinstance(reasoning, str):
        prediction["reasoning"] = ""

    if unpredictable:
        # unpredictable → force cycle / direction / probabilities = null
        prediction["cycle"] = None
        prediction["direction"] = None
        prediction["probabilities"] = None
        return

    # 4. probabilities integer rounding, clamping, and sum normalization
    probs = prediction.get("probabilities")
    if not unpredictable and not isinstance(probs, dict):
        cycle_guess = str(
            prediction.get("cycle")
            or (stage1_json or {}).get("cycle_position")
            or "trading_range"
        ).strip().lower()
        prediction["probabilities"] = _default_cycle_probs(cycle_guess)
        probs = prediction["probabilities"]
        logger.debug(
            "Synthesized next_cycle_prediction.probabilities from cycle=%r",
            cycle_guess,
        )
    if isinstance(probs, dict):
        normalized: dict[str, int] = {}
        for key in CYCLE_ORDER:
            raw = probs.get(key)
            try:
                value = int(round(float(raw))) if raw is not None else 0
            except (TypeError, ValueError):
                value = 0
            normalized[key] = max(0, min(100, value))

        # Auto-rescale if sum is outside [99, 101] (model arithmetic error)
        total = sum(normalized[k] for k in CYCLE_ORDER)
        if total > 0 and not (99 <= total <= 101):
            scale = 100.0 / total
            rescaled = {k: int(round(normalized[k] * scale)) for k in CYCLE_ORDER}
            # Fix rounding residual so sum == 100
            diff = 100 - sum(rescaled[k] for k in CYCLE_ORDER)
            if diff != 0:
                # Add/subtract from the largest bucket
                biggest = max(CYCLE_ORDER, key=lambda k: rescaled[k])
                rescaled[biggest] = max(0, rescaled[biggest] + diff)
            normalized = rescaled
            logger.debug(
                "next_cycle_prediction probabilities rescaled (sum was %d -> 100)", total
            )

        prediction["probabilities"] = normalized

        if not prediction.get("cycle"):
            max_value = max(normalized[k] for k in CYCLE_ORDER)
            prediction["cycle"] = next(k for k in CYCLE_ORDER if normalized[k] == max_value)

        # 5. cycle = argmax, tie-break by CYCLE_ORDER literal order
        max_value = max(normalized[k] for k in CYCLE_ORDER)
        # First winner in CYCLE_ORDER order
        argmax_cycle = next(k for k in CYCLE_ORDER if normalized[k] == max_value)

        model_cycle = str(prediction.get("cycle") or "").strip().lower()
        if model_cycle != argmax_cycle:
            logger.debug(
                "next_cycle_prediction cycle %r -> %r (argmax of %s)",
                model_cycle, argmax_cycle, normalized,
            )
            prediction["cycle"] = argmax_cycle

    # direction: keep model value; only type-coerce non-string to None
    direction = prediction.get("direction")
    if direction is not None and not isinstance(direction, str):
        prediction["direction"] = None


def _normalize_next_bar_prediction(prediction: dict[str, Any]) -> None:
    """In-place normalize next_bar_prediction common model quirks. Idempotent."""
    if not isinstance(prediction, dict):
        return

    # -1. Detect next_cycle_prediction content mistakenly placed here.
    #     If probabilities has cycle-position keys (spike/broad_channel/etc.) instead of
    #     direction keys (bullish/bearish/neutral), the model confused the two fields.
    #     Wipe probabilities so the synthesize-from-direction fallback kicks in.
    _CYCLE_KEYS = frozenset({
        "spike", "micro_channel", "tight_channel", "normal_channel",
        "broad_channel", "trending_tr", "trading_range", "extreme_tr",
    })
    _BAR_PROB_KEYS = frozenset({"bullish", "bearish", "neutral"})
    probs_raw = prediction.get("probabilities")
    if isinstance(probs_raw, dict):
        probs_keys = set(probs_raw.keys())
        if probs_keys & _CYCLE_KEYS and not (probs_keys & _BAR_PROB_KEYS):
            logger.debug(
                "next_bar_prediction.probabilities contains cycle keys (%s); "
                "replacing with direction-based default (model confused next_bar with next_cycle)",
                list(probs_keys & _CYCLE_KEYS)[:3],
            )
            # Replace with direction-based default right away
            raw_dir = str(prediction.get("direction") or "neutral").strip().lower()
            prediction["probabilities"] = _default_bar_probs(raw_dir)
            # Also remove cycle-specific fields that don't belong here
            prediction.pop("cycle", None)

    # 0. Extract probabilities from 'scenarios' dict if present and probabilities missing.
    #    Some models output: scenarios: {bullish: {probability: 30}, bearish: {probability: 35}, ...}
    if not isinstance(prediction.get("probabilities"), dict):
        scenarios = prediction.get("scenarios")
        if isinstance(scenarios, dict):
            extracted: dict[str, int] = {}
            for key in ("bullish", "bearish", "neutral"):
                s = scenarios.get(key)
                if isinstance(s, dict):
                    try:
                        extracted[key] = int(round(float(s.get("probability") or 0)))
                    except (TypeError, ValueError):
                        extracted[key] = 0
                else:
                    extracted[key] = 0
            if any(v > 0 for v in extracted.values()):
                prediction["probabilities"] = extracted
                logger.debug(
                    "next_bar_prediction: extracted probabilities from 'scenarios' dict"
                )

    # 1. unpredictable fallback
    unpredictable = bool(prediction.get("unpredictable", False))
    prediction["unpredictable"] = unpredictable

    # 2. features_used: ensure list, dedup, minimum set, filter invalid values
    feats = prediction.get("features_used")
    if not isinstance(feats, list):
        feats = []
    feats = [f for f in feats if isinstance(f, str)]
    # Filter out values not in the schema enum (e.g. "detected_patterns")
    invalid_feats = [f for f in feats if f not in _VALID_FEATURES_USED]
    if invalid_feats:
        logger.debug(
            "next_bar_prediction.features_used dropped invalid values: %s",
            invalid_feats,
        )
    feats = [f for f in feats if f in _VALID_FEATURES_USED]
    if "stage1_diagnosis" not in feats:
        feats.insert(0, "stage1_diagnosis")
    seen: set[str] = set()
    deduped: list[str] = []
    for f in feats:
        if f not in seen:
            deduped.append(f)
            seen.add(f)
    prediction["features_used"] = deduped

    # 3. reasoning truncation (R7.6)
    reasoning = prediction.get("reasoning")
    if isinstance(reasoning, str) and len(reasoning) > 1500:
        prediction["reasoning"] = reasoning[:1499] + "…"
    elif not isinstance(reasoning, str):
        prediction["reasoning"] = ""

    if unpredictable:
        # unpredictable → force direction / probabilities = null
        prediction["direction"] = None
        prediction["probabilities"] = None
        return

    # 3b. Legacy field migration: some models output separate probability keys
    #     instead of the required nested dict.
    #     e.g. bullish_probability/bearish_probability/neutral_probability + analysis
    if not isinstance(prediction.get("probabilities"), dict):
        bp = prediction.get("bullish_probability")
        berp = prediction.get("bearish_probability")
        np_ = prediction.get("neutral_probability")
        if any(v is not None for v in (bp, berp, np_)):
            try:
                prediction["probabilities"] = {
                    "bullish": int(round(float(bp or 0))),
                    "bearish": int(round(float(berp or 0))),
                    "neutral": int(round(float(np_ or 0))),
                }
                logger.debug(
                    "next_bar_prediction: migrated legacy flat probability fields -> probabilities dict"
                )
            except (TypeError, ValueError):
                pass  # leave for validator to catch

    # 3c. Legacy field migration: "analysis" -> "reasoning"
    if not isinstance(prediction.get("reasoning"), str) or not prediction["reasoning"]:
        analysis = prediction.get("analysis")
        if isinstance(analysis, str) and analysis:
            prediction["reasoning"] = analysis
            logger.debug(
                "next_bar_prediction: migrated 'analysis' field -> 'reasoning'"
            )

    # 4. probabilities integer rounding (R3.1)
    probs = prediction.get("probabilities")
    if isinstance(probs, dict):
        normalized: dict[str, int] = {}
        bar_order = ("bullish", "bearish", "neutral")
        for key in bar_order:
            raw = probs.get(key)
            try:
                value = int(round(float(raw))) if raw is not None else 0
            except (TypeError, ValueError):
                value = 0
            normalized[key] = max(0, min(100, value))

        # Auto-rescale if sum is outside [99, 101] (model arithmetic error)
        total = sum(normalized[k] for k in bar_order)
        if total > 0 and not (99 <= total <= 101):
            scale = 100.0 / total
            rescaled = {k: int(round(normalized[k] * scale)) for k in bar_order}
            diff = 100 - sum(rescaled[k] for k in bar_order)
            if diff != 0:
                biggest = max(bar_order, key=lambda k: rescaled[k])
                rescaled[biggest] = max(0, rescaled[biggest] + diff)
            normalized = rescaled
            logger.debug(
                "next_bar_prediction probabilities rescaled (sum was %d -> 100)", total
            )

        prediction["probabilities"] = normalized

        # 5. direction = argmax (R3.3) — respect model choice on ties
        order = ("bullish", "bearish", "neutral")
        max_value = max(normalized[k] for k in order)
        tied_winners = [k for k in order if normalized[k] == max_value]
        model_direction = str(prediction.get("direction") or "").strip().lower()

        if len(tied_winners) > 1:
            # Tie: preserve model's choice if it's one of the winners
            if model_direction in tied_winners:
                pass  # keep model's semantic choice
            else:
                # Model direction not in tied set — override with first winner
                logger.warning(
                    "next_bar_prediction direction=%r not in tied winners %s "
                    "(probs=%s); overriding to %r",
                    model_direction, tied_winners, normalized, tied_winners[0],
                )
                prediction["direction"] = tied_winners[0]
        else:
            # Clear winner
            expected = tied_winners[0]
            if model_direction != expected:
                logger.debug(
                    "next_bar_prediction direction %r -> %r (argmax of %s)",
                    model_direction, expected, normalized,
                )
                prediction["direction"] = expected
            # else: model direction matches argmax, no change needed
    # else: unparseable probabilities with unpredictable=False — leave for validator

    # 6. Strip extra keys not allowed by the schema (additionalProperties: false).
    #    This prevents schema validation failures caused by model adding creative fields
    #    like 'bar_type', 'key_levels', 'scenarios', 'confidence', 'analysis', etc.
    _ALLOWED_KEYS = frozenset({
        "direction", "probabilities", "reasoning", "unpredictable", "features_used",
    })
    extra_keys = [k for k in list(prediction.keys()) if k not in _ALLOWED_KEYS]
    if extra_keys:
        for k in extra_keys:
            del prediction[k]
        logger.debug(
            "next_bar_prediction: removed extra keys not allowed by schema: %s",
            extra_keys,
        )


_BAR_DIRECTION_ALIASES: dict[str, str] = {
    "up": "bullish",
    "long": "bullish",
    "bull": "bullish",
    "down": "bearish",
    "short": "bearish",
    "bear": "bearish",
    "sideways": "neutral",
    "flat": "neutral",
    "mixed": "neutral",
    "neutral_to_bullish": "bullish",
    "neutral_to_bearish": "bearish",
    "阴线": "bearish",
    "阳线": "bullish",
    "中性": "neutral",
    "阴": "bearish",
    "阳": "bullish",
    "看跌": "bearish",
    "看涨": "bullish",
}


def _alias_bar_direction(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for key in (text, text.lower()):
        if key in _BAR_DIRECTION_ALIASES:
            return _BAR_DIRECTION_ALIASES[key]
    return None


def _probabilities_from_singular(direction: str, value: Any) -> dict[str, int] | None:
    """Build a probabilities dict from model shorthand ``probability: 60``."""
    try:
        p = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    p = max(0, min(100, p))
    dom = _alias_bar_direction(direction) or str(direction or "").strip().lower()
    if dom not in ("bullish", "bearish", "neutral"):
        return None
    probs = {"bullish": 0, "bearish": 0, "neutral": 0}
    probs[dom] = p
    rest = 100 - p
    others = [k for k in ("bullish", "bearish", "neutral") if k != dom]
    probs[others[0]] = rest // 2
    probs[others[1]] = rest - rest // 2
    return probs


def _repair_next_bar_prediction_shape(prediction: dict[str, Any]) -> bool:
    """Migrate common shorthand (阴线/阳线, singular probability) before alien discard."""
    if not isinstance(prediction, dict):
        return False
    changed = False
    aliased = _alias_bar_direction(prediction.get("direction"))
    if aliased and prediction.get("direction") != aliased:
        prediction["direction"] = aliased
        changed = True
    if not isinstance(prediction.get("probabilities"), dict):
        probs = _probabilities_from_singular(
            str(prediction.get("direction") or ""),
            prediction.get("probability"),
        )
        if probs is not None:
            prediction["probabilities"] = probs
            prediction.pop("probability", None)
            changed = True
            logger.debug(
                "next_bar_prediction: migrated singular probability -> probabilities dict"
            )
    return changed


def _default_bar_probs(direction: str) -> dict[str, int]:
    d = (direction or "neutral").strip().lower()
    if d == "bullish":
        return {"bullish": 45, "bearish": 30, "neutral": 25}
    if d == "bearish":
        return {"bearish": 45, "bullish": 30, "neutral": 25}
    return {"neutral": 40, "bearish": 30, "bullish": 30}


def _default_cycle_probs(cycle: str) -> dict[str, int]:
    from pa_agent.ai.cycle_enums import CYCLE_ORDER

    c = (cycle or "unknown").strip().lower()
    base = {k: 0 for k in CYCLE_ORDER}
    if c in base:
        base[c] = 55
        rest = 45 // max(len(CYCLE_ORDER) - 1, 1)
        for k in CYCLE_ORDER:
            if k != c:
                base[k] = rest
        # fix sum
        diff = 100 - sum(base.values())
        base[c] = max(0, base[c] + diff)
    else:
        base["broad_channel"] = 30
        base["trading_range"] = 25
        base["normal_channel"] = 20
        base["trending_tr"] = 15
        base["spike"] = 10
    return base


def ensure_stage2_predictions(
    out: dict[str, Any],
    *,
    stage1_json: dict[str, Any] | None = None,
    skip_next_bar: bool = False,
) -> bool:
    """Inject next_bar/next_cycle prediction stubs when the model omitted them.

    Parameters
    ----------
    skip_next_bar:
        When True, skip injecting ``next_bar_prediction`` (UI replay path when
        the user disabled the feature).  Schema validation always injects via
        ``skip_next_bar=False``; orchestrator strips the field before save when
        disabled.  ``next_cycle_prediction`` is always injected when missing.
    """
    changed = False
    diag = out.get("diagnosis_summary") if isinstance(out.get("diagnosis_summary"), dict) else {}
    s1 = stage1_json or {}
    direction = str(diag.get("direction") or s1.get("direction") or "neutral")
    cycle = str(diag.get("cycle_position") or s1.get("cycle_position") or "unknown")

    decision = out.get("decision") if isinstance(out.get("decision"), dict) else {}
    reasoning = str(decision.get("reasoning") or "").strip()
    synth_note = "（程序根据阶段二诊断摘要补全，原模型未输出预测字段）"

    if not skip_next_bar and not isinstance(out.get("next_bar_prediction"), dict):
        probs = _default_bar_probs(direction)
        dom = max(probs, key=probs.get)  # type: ignore[arg-type]
        out["next_bar_prediction"] = {
            "direction": dom,
            "probabilities": probs,
            "unpredictable": False,
            "reasoning": (
                (reasoning[:400] + "…") if len(reasoning) > 400 else reasoning
            ) or f"基于当前方向 {direction} 的参考预测{synth_note}",
            "features_used": ["stage1_diagnosis", "stage2_decision"],
        }
        changed = True

    if not isinstance(out.get("next_cycle_prediction"), dict):
        c_probs = _default_cycle_probs(cycle)
        dom_c = max(c_probs, key=c_probs.get)  # type: ignore[arg-type]
        out["next_cycle_prediction"] = {
            "cycle": dom_c,
            "direction": direction if direction in ("bullish", "bearish", "neutral") else "neutral",
            "probabilities": c_probs,
            "unpredictable": False,
            "reasoning": (
                f"当前周期 {cycle}，方向 {direction}。"
                f"下一周期概率为程序参考分布{synth_note}"
            ),
            "features_used": ["stage1_diagnosis", "stage2_decision"],
        }
        changed = True

    return changed


def _max_bar_seq_from_frame(kline_frame: Any) -> int | None:
    bars = getattr(kline_frame, "bars", None) if kline_frame is not None else None
    if not bars:
        return None
    seqs = [int(getattr(b, "seq", 0)) for b in bars if getattr(b, "seq", None)]
    return max(seqs) if seqs else None


def _fix_background_limit_trace(out: dict[str, Any]) -> bool:
    """Ensure §9.0P=是 when a planned limit order follows §9.0=否."""
    try:
        from pa_agent.ai.decision_nodes import is_planned_limit_order
    except ImportError:
        return False
    if not is_planned_limit_order(out):
        return False
    trace = out.get("decision_trace")
    if not isinstance(trace, list):
        return False

    node_90: dict[str, Any] | None = None
    node_90p: dict[str, Any] | None = None
    for item in trace:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("node_id", "")).strip()
        if nid == "9.0":
            node_90 = item
        elif nid == "9.0P":
            node_90p = item

    changed = False
    if node_90 is not None:
        ans = str(node_90.get("answer", "") or "").strip()
        if ans in ("否", "等待"):
            if node_90p is None:
                trace.insert(
                    trace.index(node_90) + 1,
                    {
                        "node_id": "9.0P",
                        "section": "入场信号",
                        "question": "背景驱动限价单评估（§9.0=否 时必须评估）",
                        "answer": "是",
                        "reason": (
                            "程序校正：计划型限价单，周期/结构位支持挂限价，"
                            "继续 §10 定三价。"
                        ),
                        "skipped": False,
                        "bar_range": "K10-K1",
                    },
                )
                changed = True
            elif str(node_90p.get("answer", "") or "").strip() in ("否", "等待"):
                node_90p["answer"] = "是"
                base = str(node_90p.get("reason", "") or "").strip()
                suffix = "（程序校正：背景限价路径，非信号棒路径。）"
                node_90p["reason"] = f"{base}{suffix}".strip() if base else suffix.strip()
                changed = True
    return changed


def _fix_9_0_for_planned_limit(out: dict[str, Any]) -> bool:
    """When model outputs a valid planned limit but §9.0=否, upgrade to 是."""
    try:
        from pa_agent.ai.decision_nodes import is_planned_limit_order
    except ImportError:
        return False
    if not is_planned_limit_order(out):
        return False
    trace = out.get("decision_trace")
    if not isinstance(trace, list):
        return False
    changed = False
    for item in trace:
        if not isinstance(item, dict):
            continue
        if str(item.get("node_id", "")).strip() != "9.0":
            continue
        ans = str(item.get("answer", "") or "").strip()
        if ans not in ("否", "等待"):
            return False
        item["answer"] = "是"
        base = str(item.get("reason", "") or "").strip()
        suffix = (
            "（程序校正：计划型限价单，接受 weak/invalid 或无信号棒，"
            "等待回撤/反弹到位入场，非等下一根确认棒后放弃。）"
        )
        item["reason"] = f"{base}{suffix}".strip() if base else suffix.strip()
        changed = True
        break
    return changed


def normalize_stage2(
    obj: dict[str, Any],
    *,
    normalization_mode: str = "strict",
    kline_frame: Any = None,
    decision_stance: str | None = None,
    stage1_json: dict[str, Any] | None = None,
    skip_next_bar: bool = False,
    previous_record: Any | None = None,
    structure_flip_cooldown_bars: int = 3,
) -> dict[str, Any]:
    """Return a copy of *obj* with decision_trace quirks corrected."""
    out = copy.deepcopy(obj)
    frame_max = _max_bar_seq_from_frame(kline_frame)
    _unwrap_flat_stage2_decision(out)
    _hoist_terminal_from_decision(out)
    _repair_diagnosis_summary_and_decision(out, stage1_json=stage1_json)
    decision = out.get("decision")
    if isinstance(decision, dict):
        _normalize_order_type_aliases(decision)
    _ensure_decision_required_fields(out, stage1_json=stage1_json)
    decision = out.get("decision")
    if isinstance(decision, dict):
        _truncate_decision_reasoning(decision)
    _normalize_stage2_enum_aliases(out)
    _normalize_stage2_bar_analysis_enums(out, stage1_json=stage1_json)
    _coerce_decision_no_order(out)
    _repair_terminal_trade_node(out)
    decision = out.get("decision")
    if isinstance(decision, dict) and normalize_breakout_basis_extreme(decision):
        logger.debug(
            "breakout entry_basis_extreme aligned to %s for %s",
            decision.get("entry_basis_extreme"),
            decision.get("order_direction"),
        )
    if isinstance(decision, dict) and normalize_breakout_entry_price(
        decision, kline_frame=kline_frame
    ):
        logger.debug(
            "breakout entry_price adjusted to basis extreme ± 1 tick (basis=%s)",
            decision.get("entry_basis_bar"),
        )
    if isinstance(decision, dict):
        from pa_agent.util.trade_metrics import adjust_decision_stop_for_tp1_rr_cap

        if adjust_decision_stop_for_tp1_rr_cap(decision, kline_frame=kline_frame):
            logger.debug("stop_loss widened to bring TP1 RR within program cap")
    _coerce_decision_when_trade_metrics_fail(
        out,
        decision_stance=decision_stance,
        kline_frame=kline_frame,
    )
    if _fix_background_limit_trace(out):
        logger.debug("Ensured §9.0P for background planned limit order")
    if _fix_9_0_for_planned_limit(out):
        logger.debug("Upgraded §9.0 to 是 for planned limit order")

    # ── DecisionNodeEngine: fill §9.1/§9.2/§9.3/§9.5/§11 ─────────────────────
    if kline_frame is not None:
        try:
            from pa_agent.ai.decision_nodes import DecisionNodeEngine
            DecisionNodeEngine.apply_stage2(out, kline_frame, stage1_json)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DecisionNodeEngine.apply_stage2 failed: %s", exc)
    if _coerce_breakout_without_basis(out):
        logger.debug("Coerced breakout-without-basis to 限价单 after DecisionNodeEngine")

    normalize_stage2_traces(
        out,
        normalization_mode=normalization_mode,
        default_max_seq=frame_max,
    )
    decision = out.get("decision")
    if isinstance(decision, dict) and decision.get("order_type") == "不下单":
        # A no-order decision must satisfy the schema "then" branch:
        # all price fields + direction must be null.
        for field in _NO_ORDER_PRICE_FIELDS:
            decision[field] = None
        decision["estimated_win_rate"] = None
        # trade_confidence / trade_confidence_reasoning are required (non-nullable)
        # by schema; AI incorrectly sets them to null when order_type=不下单.
        # Patch to valid defaults.
        if decision.get("trade_confidence") is None:
            decision["trade_confidence"] = 0
        if not isinstance(decision.get("trade_confidence_reasoning"), str) or not decision["trade_confidence_reasoning"]:
            decision["trade_confidence_reasoning"] = "无入场计划，不存在交易信心"

    bar_analysis = out.get("bar_analysis")
    decision = out.get("decision")
    if isinstance(bar_analysis, dict) and isinstance(decision, dict):
        _normalize_market_order_entry_bar(bar_analysis, decision)
        if _normalize_signal_entry_bar_chain(bar_analysis, decision):
            pass
    if isinstance(bar_analysis, dict):
        signal_bar = bar_analysis.get("signal_bar")
        if isinstance(signal_bar, dict):
            if not signal_bar.get("bar"):
                signal_bar["bar"] = None
                signal_bar.setdefault("quality", "invalid")
                signal_bar.setdefault("pattern", "none")

        entry_bar = bar_analysis.get("entry_bar")
        if isinstance(entry_bar, dict):
            strength = str(entry_bar.get("strength", "") or "").strip().lower()
            has_bar = bool(entry_bar.get("bar"))
            if strength == "not_triggered" or not has_bar:
                # Pending limit/breakout orders do not have an actual entry bar
                # yet. Normalize common model variants before schema checks.
                entry_bar["strength"] = "not_triggered"
                entry_bar.setdefault("bar", None)
                fresh = str(entry_bar.get("freshness") or "").strip().lower()
                if fresh in ("stale", "invalid", "expired", ""):
                    entry_bar["freshness"] = "pending"
                else:
                    entry_bar.setdefault("freshness", "pending")
                if entry_bar.get("follow_through") in (None, "", "pending"):
                    entry_bar["follow_through"] = "pending"

    # ── diagnosis_summary ────────────────────────────────────────────────
    # Schema requires diagnosis_summary; inject minimal default if missing.
    if not isinstance(out.get("diagnosis_summary"), dict):
        s1 = stage1_json or {}
        out["diagnosis_summary"] = {
            "cycle_position": s1.get("cycle_position", "unknown"),
            "direction": s1.get("direction", "neutral"),
            "key_signals": [],
        }
        logger.debug(
            "Injected missing diagnosis_summary from stage1 (cycle=%s, dir=%s)",
            out["diagnosis_summary"]["cycle_position"],
            out["diagnosis_summary"]["direction"],
        )

    ensure_stage2_predictions(out, stage1_json=stage1_json, skip_next_bar=skip_next_bar)

    pred = out.get("next_bar_prediction")
    if isinstance(pred, dict):
        # ── Step 1: shorthand repair (阴线/阳线, singular probability) ───────
        _repair_next_bar_prediction_shape(pred)

        # ── Step 2: migrate legacy flat probability fields ───────────────────
        # e.g. bullish_probability/bearish_probability/neutral_probability
        if not isinstance(pred.get("probabilities"), dict):
            bp = pred.get("bullish_probability")
            berp = pred.get("bearish_probability")
            np_ = pred.get("neutral_probability")
            if any(v is not None for v in (bp, berp, np_)):
                try:
                    pred["probabilities"] = {
                        "bullish": int(round(float(bp or 0))),
                        "bearish": int(round(float(berp or 0))),
                        "neutral": int(round(float(np_ or 0))),
                    }
                    logger.debug(
                        "next_bar_prediction: migrated legacy flat probability fields -> probabilities dict"
                    )
                except (TypeError, ValueError):
                    pass

        # ── Step 3: detect completely alien formats and discard ──────────────
        # A valid (or migratable) prediction must have at least one structural
        # key: probabilities (the canonical form), unpredictable, OR a valid
        # direction enum value.  If direction exists but is still non-standard
        # after aliasing, and probabilities is absent, treat as alien.
        _valid_directions = frozenset({"bullish", "bearish", "neutral"})
        has_probs = isinstance(pred.get("probabilities"), dict)
        has_unpredictable = "unpredictable" in pred
        direction_after_alias = str(pred.get("direction") or "").strip().lower()
        has_valid_direction = direction_after_alias in _valid_directions
        is_alien = not has_probs and not has_unpredictable and not has_valid_direction
        if is_alien:
            logger.debug(
                "next_bar_prediction has unrecognised schema (keys=%s); discarding and re-injecting",
                list(pred.keys()),
            )
            del out["next_bar_prediction"]
            ensure_stage2_predictions(
                out,
                stage1_json=stage1_json,
                skip_next_bar=False,
            )
            pred = out.get("next_bar_prediction")

        # ── Step 4: if direction is valid but probabilities still missing,
        #    synthesize probabilities from the direction value ─────────────────
        elif has_valid_direction and not has_probs and not has_unpredictable:
            pred["probabilities"] = _default_bar_probs(direction_after_alias)
            pred.setdefault("unpredictable", False)
            logger.debug(
                "next_bar_prediction: synthesized probabilities from direction=%r",
                direction_after_alias,
            )

    if isinstance(pred, dict):
        _normalize_next_bar_prediction(pred)

    pred_c = out.get("next_cycle_prediction")
    if isinstance(pred_c, dict):
        _normalize_next_cycle_prediction(pred_c, stage1_json=stage1_json)

    # ── Hard guard: align decision direction with next cycle direction ──
    # User rule:
    # - "下一周期偏空就不要给出做多的决策"
    # - Symmetric: 下一周期偏多时也不要给出做空的决策
    try:
        pred_c = out.get("next_cycle_prediction")
        decision = out.get("decision")
        if isinstance(pred_c, dict) and isinstance(decision, dict):
            next_dir = str(pred_c.get("direction") or "").strip().lower()
            order_dir = str(decision.get("order_direction") or "").strip()
            order_type = str(decision.get("order_type") or "").strip()
            is_long = ("多" in order_dir) or (order_dir.lower() in ("long", "buy", "bullish"))
            is_short = ("空" in order_dir) or (order_dir.lower() in ("short", "sell", "bearish"))
            block_long = next_dir == "bearish" and is_long
            block_short = next_dir == "bullish" and is_short
            if (block_long or block_short) and order_type not in ("", "不下单"):
                decision = dict(decision)
                decision["order_type"] = "不下单"
                for key in (
                    "order_direction",
                    "entry_price",
                    "stop_loss_price",
                    "take_profit_price",
                    "take_profit_price_2",
                    "entry_rule",
                    "entry_basis_bar",
                    "entry_basis_extreme",
                    "estimated_win_rate",
                ):
                    decision[key] = None

                existing = str(decision.get("reasoning") or "")
                if block_long:
                    prefix = (
                        "【程序守卫】next_cycle_prediction.direction=bearish：禁止做多；改为不下单。 "
                    )
                else:
                    prefix = (
                        "【程序守卫】next_cycle_prediction.direction=bullish：禁止做空；改为不下单。 "
                    )
                decision["reasoning"] = (prefix + existing)[:DECISION_REASONING_MAX_LEN]

                terminal = dict(out.get("terminal") or {})
                terminal["outcome"] = "wait"
                terminal["node_id"] = "prediction_guard"
                terminal["label"] = (
                    "周期预测守卫：下一周期偏空，禁止做多"
                    if block_long
                    else "周期预测守卫：下一周期偏多，禁止做空"
                )

                out = dict(out)
                out["decision"] = decision
                out["terminal"] = terminal
    except Exception as exc:  # noqa: BLE001
        logger.warning("prediction direction guard failed: %s", exc)

    if kline_frame is not None and stage1_json:
        try:
            from pa_agent.ai.decision_continuity import (
                apply_continuity_guard,
                build_continuity_context,
            )

            ctx = build_continuity_context(
                frame=kline_frame,
                stage1_json=stage1_json,
                previous_record=previous_record,
                cooldown_bars=structure_flip_cooldown_bars,
            )
            out = apply_continuity_guard(out, ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("apply_continuity_guard failed: %s", exc)

    decision = out.get("decision")
    if isinstance(decision, dict):
        _truncate_decision_reasoning(decision)
        from pa_agent.util.trade_metrics import apply_decision_kelly_metrics

        if apply_decision_kelly_metrics(decision):
            logger.debug("Applied program-derived Kelly risk fraction")

    return out
