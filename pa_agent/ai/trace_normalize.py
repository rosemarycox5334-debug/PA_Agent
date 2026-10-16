"""Normalize gate_trace / decision_trace items before JSON schema validation."""
from __future__ import annotations

import copy
import logging
import re
from typing import Any

from pa_agent.ai.coherence_checks import _CYCLE_BRANCH_ALIASES, _normalize_direction_branch
from pa_agent.ai.decision_tree import load_decision_tree

_PROCEED_FINAL_TOKENS = (
    "进入阶段二",
    "可进入阶段二",
    "闸门通过",
    "继续阶段二",
    "进入策略",
    "可继续分析",
)

logger = logging.getLogger(__name__)

NormalizationMode = str  # "strict" | "lenient"

# branch values that mean yes/no but not a cycle id (node 1.2 must use cycle_position).
_GATE_12_GENERIC_BRANCHES = frozenset(
    {"yes", "no", "y", "n", "是", "否", "true", "false", ""}
)

_BAR_RANGE_RE = re.compile(r"^K(\d+)-K(\d+)$", re.IGNORECASE)
_SINGLE_BAR_RE = re.compile(r"^K(\d+)$", re.IGNORECASE)
_PROG_REF_BLOCK_RE = re.compile(r"【程序参考数据（[^】]*）：.*?】", re.DOTALL)

# Model-invented summary nodes (not in decision tree); gate_result belongs in gate_result field.
_GATE_END_NODE_IDS = frozenset({"gate_end", "gate_summary", "summary"})
_GATE_RESULT_ANSWER_ALIASES: dict[str, str] = {
    "proceed": "是",
    "wait": "等待",
    "unknown": "中性",
}

# node_id -> {raw answer -> (canonical answer, branch)}
_NODE_ANSWER_BY_ID: dict[str, dict[str, tuple[str, str]]] = {
    "2.3": {
        "多头": ("是", "bullish"),
        "空头": ("是", "bearish"),
        "做多": ("是", "bullish"),
        "做空": ("是", "bearish"),
        "bullish": ("是", "bullish"),
        "bearish": ("是", "bearish"),
        "bull": ("是", "bullish"),
        "bear": ("是", "bearish"),
        "中性": ("中性", "neutral"),
        "neutral": ("中性", "neutral"),
    },
    "4.2": {
        "上涨": ("是", "bullish"),
        "下跌": ("是", "bearish"),
        "上涨通道": ("是", "bullish"),
        "下跌通道": ("是", "bearish"),
        "多头": ("是", "bullish"),
        "空头": ("是", "bearish"),
        "bullish": ("是", "bullish"),
        "bearish": ("是", "bearish"),
    },
    "6.2": {
        "普通交易区间": ("是", "trading_range"),
        "普通区间": ("是", "trading_range"),
        "普通": ("是", "trading_range"),
        "趋势型交易区间": ("是", "trending_tr"),
        "趋势型区间": ("是", "trending_tr"),
        "趋势型": ("是", "trending_tr"),
        "trading_range": ("是", "trading_range"),
        "trending_tr": ("是", "trending_tr"),
    },
    "6.3": {
        "下边界": ("是", "lower"),
        "上边界": ("是", "upper"),
        "在下边界": ("是", "lower"),
        "在上边界": ("是", "upper"),
        "区间下边界": ("是", "lower"),
        "区间上边界": ("是", "upper"),
        "下边界附近": ("是", "lower"),
        "上边界附近": ("是", "upper"),
        "中间": ("否", "middle"),
        "中间1/3": ("否", "middle"),
        "在中间": ("否", "middle"),
        "中间区域": ("否", "middle"),
        "不在边界": ("否", "middle"),
        "lower": ("是", "lower"),
        "upper": ("是", "upper"),
        "middle": ("否", "middle"),
    },
    "8.2": {
        "楔形回撤": ("是", "pullback"),
        "楔形反转": ("是", "reversal"),
        "回撤": ("是", "pullback"),
        "反转": ("是", "reversal"),
        "pullback": ("是", "pullback"),
        "reversal": ("是", "reversal"),
    },
    "3.5": {
        "路径A": ("是", "path_a"),
        "路径B": ("是", "path_b"),
        "路径C": ("是", "path_c"),
        "A": ("是", "path_a"),
        "B": ("是", "path_b"),
        "C": ("是", "path_c"),
    },
    "2.2": {
        "冲突": ("是", "conflict"),
        "背景冲突": ("是", "conflict"),
        "方向冲突": ("是", "conflict"),
        "新旧冲突": ("是", "conflict"),
        "conflict": ("是", "conflict"),
        "同向": ("是", "aligned"),
        "共振": ("是", "aligned"),
        "方向一致": ("是", "aligned"),
        "aligned": ("是", "aligned"),
        "中性背景": ("中性", "neutral_background"),
        "背景中性": ("中性", "neutral_background"),
        "neutral_background": ("中性", "neutral_background"),
        "mixed": ("中性", "mixed"),
    },
}

_GENERIC_ANSWER: dict[str, str] = {
    "通过": "是",
    "未通过": "否",
    "不通过": "否",
    "违反": "否",
    "触犯": "否",
    "无交易计划，不存在触犯": "否",
    "未触犯": "否",
    "不存在触犯": "否",
    "pass": "是",
    "fail": "否",
    "yes": "是",
    "no": "否",
    "not_applicable": "不适用",
    "n/a": "不适用",
    "na": "不适用",
    # Common AI synonyms outside the strict enum (map before schema validation).
    "部分": "中性",
    "部分一致": "中性",
    "部分通过": "中性",
    "部分符合": "中性",
    "部分是": "中性",
    "部分否": "否",
    "是部分": "中性",
    "待确认": "等待",
    "待定": "等待",
    "需确认": "等待",
    "尚未确认": "等待",
    "未确认": "等待",
    "不确定": "中性",
    "中立": "中性",
}

_BAR_RANGE_ALIASES = frozenset({"全局", "全图", "整体", "全部", "all"})
_PENDING_BAR_RANGE_VALUES = frozenset(
    {
        "pending",
        "tbd",
        "n/a",
        "na",
        "等待触发",
        "待触发",
        "未触发",
        "尚无",
        "等待",
    }
)
_NULLISH_STRINGS = frozenset({"", "null", "none", "nil", "n/a"})


def _is_nullish(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in _NULLISH_STRINGS


def _ensure_trace_string_fields(item: dict[str, Any]) -> None:
    """Coerce missing / null JSON values to strings before schema validation."""
    for key in ("node_id", "question", "answer", "reason"):
        if key not in item or _is_nullish(item.get(key)):
            if key == "answer" and item.get("skipped"):
                item[key] = "不适用"
            elif key in ("question", "reason"):
                item[key] = "—"
            elif key == "node_id":
                item[key] = ""
            elif key == "answer":
                item[key] = "不适用" if item.get("skipped") else "否"
            else:
                item[key] = ""

_COMPOSITE_ANSWER_RE = re.compile(
    r"^(是|否|中性|等待|不适用)\s*[,，:：\-—]\s*(.+)$"
)
_COMPOSITE_ANSWER_PAREN_RE = re.compile(
    r"^(是|否|中性|等待|不适用)\s*[（(](.+?)[）)]\s*$"
)


def infer_max_bar_seq_from_trace(trace: list[Any]) -> int | None:
    """Infer largest K index mentioned in trace bar_range or reason text."""
    max_seq = 0
    for item in trace:
        if not isinstance(item, dict):
            continue
        for field in ("bar_range", "reason"):
            raw = str(item.get(field, "") or "")
            for m in re.finditer(r"K(\d+)", raw, re.IGNORECASE):
                max_seq = max(max_seq, int(m.group(1)))
    return max_seq or None


def _comma_separated_bar_range(compact: str) -> str | None:
    """Turn K7,K1 or K1、K7 into K7-K1 when two or more K refs are comma/顿号-separated."""
    if "," not in compact and "、" not in compact:
        return None
    parts = re.split(r"[,，、]", compact)
    seqs: list[int] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _SINGLE_BAR_RE.match(part)
        if m:
            seqs.append(int(m.group(1)))
    if len(seqs) < 2:
        return None
    older, newer = max(seqs), min(seqs)
    return f"K{older}" if older == newer else f"K{older}-K{newer}"


def _bar_range_is_canonical(text: str) -> bool:
    compact = str(text or "").strip().upper().replace(" ", "")
    if not compact or compact in ("不适用", "—", "-"):
        return True
    return bool(_BAR_RANGE_RE.match(compact) or _SINGLE_BAR_RE.match(compact))


def _bar_range_from_reason(
    item: dict[str, Any],
    *,
    default_max_seq: int | None = None,
) -> str | None:
    cited = _bar_seqs_from_reason_text(str(item.get("reason", "") or ""))
    if not cited:
        return None
    older, newer = max(cited), min(cited)
    if default_max_seq and default_max_seq >= 1:
        cited = {s for s in cited if 1 <= s <= default_max_seq}
        if not cited:
            return None
        older, newer = max(cited), min(cited)
    return f"K{older}" if older == newer else f"K{older}-K{newer}"


def fix_bar_range_string(text: str, *, default_max_seq: int | None = None) -> str:
    """Canonicalize bar_range: order, aliases, spacing."""
    raw = str(text).strip()
    if not raw:
        return ""

    if raw.lower() in _PENDING_BAR_RANGE_VALUES:
        return ""

    if raw in _BAR_RANGE_ALIASES or raw.lower() in {"global", "all"}:
        if default_max_seq and default_max_seq > 1:
            return f"K{default_max_seq}-K1"
        return "不适用"

    compact = raw.upper().replace(" ", "")
    comma_range = _comma_separated_bar_range(compact)
    if comma_range:
        compact = comma_range.replace(" ", "")
    m = _BAR_RANGE_RE.match(compact)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a == b:
            capped = _cap_bar_seq(a, default_max_seq)
            return f"K{capped}"
        if a < b:
            logger.debug(
                "bar_range=%r has reversed order (K%d before K%d); "
                "K1=newest, K{N}=older. Auto-fixing to K%d-K%d.",
                text, a, b, b, a,
            )
            a, b = b, a
        a = _cap_bar_seq(a, default_max_seq)
        b = _cap_bar_seq(b, default_max_seq)
        if a == b:
            return f"K{a}"
        return f"K{a}-K{b}"

    single = _SINGLE_BAR_RE.match(compact)
    if single:
        return f"K{_cap_bar_seq(int(single.group(1)), default_max_seq)}"

    return raw


def _cap_bar_seq(seq: int, max_seq: int | None) -> int:
    if max_seq is not None and max_seq >= 1:
        return max(1, min(seq, max_seq))
    return seq


def _branch_from_tail(per_node: dict[str, tuple[str, str]], tail: str) -> str | None:
    """Match classification tail text to a branch id."""
    tail = tail.strip()
    if not tail or not per_node:
        return None
    if tail in per_node:
        return per_node[tail][1]
    tail_l = tail.lower()
    if tail_l in per_node:
        return per_node[tail_l][1]
    for key in sorted(per_node.keys(), key=len, reverse=True):
        if key in tail or key.lower() in tail_l:
            return per_node[key][1]
    return None


def _resolve_trace_answer(
    node_id: str,
    answer: str,
) -> tuple[str, str | None] | None:
    """Map raw AI answer to (canonical answer, optional branch). None = unchanged."""
    ans = answer.strip()
    if not ans:
        return None

    per_node = _NODE_ANSWER_BY_ID.get(node_id, {})

    mapped = per_node.get(ans) or per_node.get(ans.lower())
    if mapped:
        return mapped

    for pat in (_COMPOSITE_ANSWER_RE, _COMPOSITE_ANSWER_PAREN_RE):
        m = pat.match(ans)
        if m:
            base, tail = m.group(1), m.group(2).strip()
            branch = _branch_from_tail(per_node, tail)
            if branch:
                return base, branch
            if per_node:
                return base, None
            return base, None

    for key in sorted(per_node.keys(), key=len, reverse=True):
        if key in ans or key.lower() in ans.lower():
            return per_node[key]

    if ans in _GENERIC_ANSWER:
        return _GENERIC_ANSWER[ans], None
    if ans.lower() in _GENERIC_ANSWER:
        return _GENERIC_ANSWER[ans.lower()], None

    # Qualified partial answers (e.g. 部分符合 / 部分是)
    if ans.startswith("部分"):
        return "中性", None

    return None


def _coerce_bar_range(
    item: dict[str, Any],
    *,
    default_max_seq: int | None = None,
    prior_bar_range: str | None = None,
) -> None:
    """Ensure bar_range is a non-null string (schema + validator)."""
    nid = str(item.get("node_id", ""))
    skipped = bool(item.get("skipped"))
    ans = str(item.get("answer", "")).strip()

    if skipped and not ans:
        item["answer"] = "不适用"
        ans = "不适用"

    br = item.get("bar_range")
    br_text = str(br or "").strip()
    if br_text.lower() in _PENDING_BAR_RANGE_VALUES:
        inferred = _bar_range_from_reason(item, default_max_seq=default_max_seq)
        if inferred:
            item["bar_range"] = inferred
            logger.debug("bar_range %r -> %s (node %s, from reason)", br, inferred, nid)
            _expand_bar_range_for_reason_citations(item, default_max_seq=default_max_seq)
            return
        if skipped or ans == "不适用":
            item["bar_range"] = "不适用"
            return
        if prior_bar_range and prior_bar_range not in ("不适用", "—"):
            item["bar_range"] = prior_bar_range
            logger.debug(
                "bar_range %r -> copied %s (node %s)",
                br,
                prior_bar_range,
                nid,
            )
            return

    if _is_nullish(br):
        if skipped or ans == "不适用":
            item["bar_range"] = "不适用"
            return
        inferred = _bar_range_from_reason(item, default_max_seq=default_max_seq)
        if inferred:
            item["bar_range"] = inferred
            logger.debug("bar_range null -> %s (node %s, from reason)", inferred, nid)
            _expand_bar_range_for_reason_citations(item, default_max_seq=default_max_seq)
            return
        if prior_bar_range and prior_bar_range not in ("不适用", "—"):
            item["bar_range"] = prior_bar_range
            logger.debug(
                "bar_range null -> copied %s (node %s)",
                prior_bar_range,
                nid,
            )
            return
        if default_max_seq and default_max_seq > 1:
            item["bar_range"] = f"K{default_max_seq}-K1"
            logger.debug(
                "bar_range null -> K%s-K1 (node %s)",
                default_max_seq,
                nid,
            )
            return
        item["bar_range"] = "不适用"
        return

    fixed = fix_bar_range_string(str(br), default_max_seq=default_max_seq)
    if fixed != br_text:
        logger.debug("bar_range %s -> %s (node %s)", br, fixed, nid)
    item["bar_range"] = fixed
    if not _bar_range_is_canonical(item["bar_range"]):
        inferred = _bar_range_from_reason(item, default_max_seq=default_max_seq)
        if inferred:
            item["bar_range"] = inferred
            logger.debug(
                "bar_range %r -> %s (node %s, repaired non-canonical)",
                br,
                inferred,
                nid,
            )
        elif skipped or ans == "不适用":
            item["bar_range"] = "不适用"
    _expand_bar_range_for_reason_citations(item, default_max_seq=default_max_seq)


def _bar_seqs_from_range_text(bar_range: str) -> set[int]:
    text = (bar_range or "").strip().upper().replace(" ", "")
    if not text or text in ("不适用", "—", "全局", "GLOBAL"):
        return set()
    m = _BAR_RANGE_RE.match(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        lo, hi = min(a, b), max(a, b)
        return set(range(lo, hi + 1))
    single = _SINGLE_BAR_RE.match(text)
    if single:
        return {int(single.group(1))}
    return set()


def _bar_seqs_from_reason_text(reason: str) -> set[int]:
    return {int(m.group(1)) for m in re.finditer(r"K\s*(\d+)", reason or "", re.IGNORECASE)}


def _expand_bar_range_for_reason_citations(
    item: dict[str, Any],
    *,
    default_max_seq: int | None = None,
) -> None:
    """Widen bar_range when reason cites K-lines outside the declared window."""
    reason = str(item.get("reason", "") or "")
    br = str(item.get("bar_range", "") or "").strip()
    cited = _bar_seqs_from_reason_text(reason)
    if not cited or br in ("不适用", "—", "全局", "GLOBAL", ""):
        return

    allowed = _bar_seqs_from_range_text(br)
    if not allowed:
        return
    if cited.issubset(allowed):
        return

    merged = allowed | cited
    # Clip only when frame cap is known; always keep cited seqs from reason.
    if default_max_seq and default_max_seq >= 1:
        merged = {s for s in merged if 1 <= s <= default_max_seq} | cited
        if not merged:
            return

    older, newer = max(merged), min(merged)
    expanded = f"K{older}" if older == newer else f"K{older}-K{newer}"
    if expanded != br:
        logger.debug(
            "bar_range expanded %s -> %s (node %s, cited=%s)",
            br,
            expanded,
            item.get("node_id"),
            sorted(cited),
        )
        item["bar_range"] = expanded


def normalize_trace_item(
    item: dict[str, Any],
    *,
    default_max_seq: int | None = None,
    prior_bar_range: str | None = None,
    normalization_mode: NormalizationMode = "strict",
) -> None:
    """Mutate one trace item: answer + bar_range."""
    if _is_nullish(item.get("node_id")) and not _is_nullish(item.get("node")):
        item["node_id"] = str(item.get("node", "")).strip()
    _ensure_trace_string_fields(item)
    lenient = normalization_mode == "lenient"
    nid = str(item.get("node_id", "")).strip()
    # Strip decorative prefixes like "§" that the model sometimes adds
    nid = nid.lstrip("§")
    if nid != str(item.get("node_id", "")).strip():
        item["node_id"] = nid

    if nid == "14" or (nid.startswith("14") and not nid.startswith("14.")):
        item["node_id"] = "14.1"
        nid = "14.1"

    nid = str(item.get("node_id", ""))

    ans = str(item.get("answer", "")).strip()
    if ans:
        resolved = _resolve_trace_answer(nid, ans)
        if resolved is not None:
            new_ans, branch = resolved
            # Safe enum synonyms (待定→等待、通过→是) and node-specific maps always apply.
            # Fuzzy partial answers (部分→中性) only in lenient mode.
            node_specific = bool(_NODE_ANSWER_BY_ID.get(nid))
            generic_hit = ans in _GENERIC_ANSWER or ans.lower() in _GENERIC_ANSWER
            if generic_hit or node_specific or lenient:
                if new_ans != ans:
                    logger.debug(
                        "trace answer %r -> %r (node %s branch=%s)",
                        ans,
                        new_ans,
                        nid,
                        branch,
                    )
                item["answer"] = new_ans
                if branch:
                    item.setdefault("branch", branch)

    bar_from = item.get("bar_from")
    bar_to = item.get("bar_to")
    if bar_from is not None and bar_to is not None and _is_nullish(item.get("bar_range")):
        bf, bt = int(bar_from), int(bar_to)
        item["bar_range"] = f"K{max(bf, bt)}-K{min(bf, bt)}" if bf != bt else f"K{bf}"

    _coerce_bar_range(
        item,
        default_max_seq=default_max_seq,
        prior_bar_range=prior_bar_range if lenient else None,
    )


def _strip_ai_gate_14(gate_trace: list[Any]) -> None:
    """Remove AI-written 14.1 nodes from gate_trace (program injects its own at front).

    The model sometimes outputs node 14.1 (禁止行为扫描) in gate_trace.
    After program injects its authoritative 14.1 at the front, duplicates
    cause node ordering errors. We keep the first 14.1 and drop the rest.
    """
    if not isinstance(gate_trace, list) or not gate_trace:
        return
    kept: list[Any] = []
    seen_14 = False
    removed = 0
    for item in gate_trace:
        if isinstance(item, dict) and str(item.get("node_id", "")) == "14.1":
            if not seen_14:
                seen_14 = True
                kept.append(item)
            else:
                removed += 1
        else:
            kept.append(item)
    if removed:
        gate_trace[:] = kept
        logger.debug("Stripped %s duplicate AI-written 14.1 from gate_trace", removed)


def normalize_trace_list(
    trace: list[Any] | None,
    *,
    default_max_seq: int | None = None,
    normalization_mode: NormalizationMode = "strict",
) -> list[Any] | None:
    if not isinstance(trace, list):
        return trace

    # Reorder by chapter: AI may output nodes in any order; the correct
    # canonical order is by node_id prefix (3.x → 4.x → ... → 14).
    _CHAPTER_ORDER: dict[str, int] = {
        "3.": 30, "4.": 40, "5.": 50, "6.": 60,
        "7.": 70, "8.": 80, "9.": 90, "10.": 100,
        "11.": 110, "12.": 120, "13.": 130, "14": 140,
    }

    def _chapter_rank(item: Any) -> int:
        if not isinstance(item, dict):
            return 999
        nid = str(item.get("node_id", ""))
        for prefix, rank in _CHAPTER_ORDER.items():
            if nid.startswith(prefix) or nid == prefix.rstrip("."):
                return rank
        return 500  # unrecognised nodes go in the middle

    trace.sort(key=_chapter_rank)

    max_seq = default_max_seq or infer_max_bar_seq_from_trace(trace)
    last_br: str | None = None
    lenient = normalization_mode == "lenient"
    for item in trace:
        if isinstance(item, dict):
            normalize_trace_item(
                item,
                default_max_seq=max_seq,
                prior_bar_range=last_br if lenient else None,
                normalization_mode=normalization_mode,
            )
            br = str(item.get("bar_range", "") or "")
            if br and br not in ("不适用", "—", "-"):
                last_br = br
    return trace


def _normalize_cycle_branch_value(raw: object) -> str | None:
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key:
        return None
    return _CYCLE_BRANCH_ALIASES.get(key, key.replace(" ", "_"))


def _sync_gate_12_branch_with_cycle(obj: dict[str, Any]) -> None:
    """Node 1.2 branch must be the identified cycle, not yes/no."""
    gate = obj.get("gate_trace")
    cycle = _normalize_cycle_branch_value(obj.get("cycle_position"))
    if not cycle or not isinstance(gate, list):
        return
    alt = _normalize_cycle_branch_value(obj.get("alternative_cycle_position"))

    for item in gate:
        if not isinstance(item, dict) or str(item.get("node_id", "")).strip() != "1.2":
            continue
        ans = str(item.get("answer", "")).strip()
        br_raw = item.get("branch")
        br = _normalize_cycle_branch_value(br_raw)
        if br_raw is not None and br and br not in _GATE_12_GENERIC_BRANCHES:
            if br != str(br_raw).strip().lower().replace(" ", "_"):
                item["branch"] = br
            return

        if ans == "是" and (br is None or br in _GATE_12_GENERIC_BRANCHES):
            item["branch"] = cycle
            logger.debug("gate_trace 1.2 branch -> cycle_position %s", cycle)
            return
        if ans == "否" and (br is None or br in _GATE_12_GENERIC_BRANCHES):
            item["branch"] = "unknown"
            logger.debug("gate_trace 1.2 branch -> unknown (answer=否)")
            return
        if br and cycle and br not in (cycle, alt or ""):
            if br in _GATE_12_GENERIC_BRANCHES:
                item["branch"] = cycle if ans == "是" else "unknown"
                logger.debug("gate_trace 1.2 generic branch %r -> %s", br_raw, item["branch"])
        return


def _canonical_tree_questions() -> dict[str, str]:
    tree = load_decision_tree()
    index = tree.get("node_index", {})
    return {
        str(nid): str(node.get("question", "") or "").strip()
        for nid, node in index.items()
        if str(node.get("question", "") or "").strip()
    }


def _canonical_gate_questions() -> dict[str, str]:
    return _canonical_tree_questions()


def _repair_stage2_decision_trace_questions(trace: list[Any]) -> None:
    """Align decision_trace question text with the decision tree (format-only)."""
    canonical_q = _canonical_tree_questions()
    for item in trace:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("node_id", "") or "").strip()
        if nid in canonical_q:
            item["question"] = canonical_q[nid]


def _repair_stage2_terminal(obj: dict[str, Any]) -> None:
    """When 10.3 is 否 on a no-order path, terminal must cite node 10.3."""
    trace = obj.get("decision_trace")
    terminal = obj.get("terminal")
    decision = obj.get("decision")
    if not isinstance(trace, list) or not isinstance(terminal, dict):
        return
    if not isinstance(decision, dict) or decision.get("order_type") != "不下单":
        return
    if terminal.get("outcome") not in ("wait", "reject"):
        return

    for item in trace:
        if not isinstance(item, dict) or str(item.get("node_id")) != "10.3":
            continue
        if item.get("answer") != "否":
            return
        old_nid = str(terminal.get("node_id", "") or "")
        if old_nid != "10.3":
            terminal["node_id"] = "10.3"
            logger.debug(
                "stage2 terminal.node_id %r -> 10.3 (10.3 answer=否, order_type=不下单)",
                old_nid,
            )
        return


def _infer_direction_from_reason(reason: str) -> str | None:
    blob = (reason or "").lower()
    if any(tok in blob for tok in ("空头", "bearish", "做空", "偏空")):
        return "bearish"
    if any(tok in blob for tok in ("多头", "bullish", "做多", "偏多")):
        return "bullish"
    if any(tok in blob for tok in ("中性", "震荡", "neutral", "横盘")):
        return "neutral"
    return None


def _sync_gate_23_answer_with_direction(obj: dict[str, Any]) -> None:
    """Node 2.3: answer 是/否/中性 encodes certainty; direction lives in branch."""
    gate = obj.get("gate_trace")
    if not isinstance(gate, list):
        return
    top_dir = _normalize_direction_branch(obj.get("direction"))
    for item in gate:
        if not isinstance(item, dict) or str(item.get("node_id", "")).strip() != "2.3":
            continue
        if item.get("skipped"):
            return
        ans = str(item.get("answer", "") or "").strip()
        branch_dir = _normalize_direction_branch(item.get("branch"))
        if not branch_dir:
            branch_dir = _infer_direction_from_reason(str(item.get("reason", "") or ""))
        if not branch_dir and top_dir:
            branch_dir = top_dir
        if branch_dir and not item.get("branch"):
            item["branch"] = branch_dir

        if branch_dir in ("bullish", "bearish"):
            if ans == "中性":
                item["answer"] = "是"
                logger.debug(
                    "gate_trace 2.3 answer 中性 -> 是 (branch=%s)", branch_dir
                )
        elif branch_dir == "neutral" and ans in ("是", "否"):
            item["answer"] = "中性"
            logger.debug("gate_trace 2.3 answer %s -> 中性 (branch=neutral)", ans)
        return


def _repair_gate_result(obj: dict[str, Any]) -> None:
    """Fix gate_result when AI incorrectly sets wait/unknown despite passing gates.

    Per prompt rules, gate_result=wait/unknown is only valid for:
    - §1.2 answer≠是 (cannot identify cycle)
    - §1.3 answer=是 (market is extremely chaotic)

    If neither condition holds but gate_result is wait/unknown, force to proceed.

    Positive evidence is required: the §1.2/§1.3 gate nodes must actually be
    present in ``gate_trace``. A truncated stage-1 response gets an ``AUTO`` stub
    trace injected by ``json_validator._inject_stage1_missing_tail`` (plus
    ``gate_result="unknown"``); inferring "gates passed" from *missing* nodes
    would promote an un-diagnosed chart straight to stage-2 order evaluation, so
    in that case the status is left as-is and the orchestrator short-circuits.
    """
    gate_result = str(obj.get("gate_result", "")).strip().lower()
    if gate_result not in ("wait", "unknown"):
        return
    gate = obj.get("gate_trace")
    if not isinstance(gate, list) or not gate:
        return

    def _answers(node_id: str) -> list[str]:
        return [
            str(item.get("answer", "")).strip()
            for item in gate
            if isinstance(item, dict) and str(item.get("node_id", "")) == node_id
        ]

    answers_12 = _answers("1.2")
    answers_13 = _answers("1.3")
    if not answers_12 and not answers_13:
        logger.warning(
            "gate_result %r kept: gate_trace has %d entries but no §1.2/§1.3 node "
            "— refusing to infer 'proceed' from an incomplete trace",
            gate_result,
            len(gate),
        )
        return

    # Check for valid blocking conditions
    node_12_block = any(answer != "是" for answer in answers_12)
    node_13_block = any(answer == "是" for answer in answers_13)
    if not node_12_block and not node_13_block:
        obj["gate_result"] = "proceed"
        logger.debug(
            "gate_result %r -> proceed (no valid blocking condition found)",
            gate_result,
        )


def _strip_program_reference_blocks(text: str) -> str:
    """Remove merged program-metric blocks from trace reason (§2.5 cleanup)."""
    cleaned = _PROG_REF_BLOCK_RE.sub("", text or "")
    return " ".join(cleaned.split()).strip()


def _repair_gate_trace_answer_aliases(gate: list[Any]) -> None:
    """Map gate_result tokens mistakenly written as trace answer (e.g. proceed → 是)."""
    for item in gate:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("answer", "") or "").strip()
        mapped = _GATE_RESULT_ANSWER_ALIASES.get(raw.lower())
        if mapped:
            item["answer"] = mapped
            branch = str(item.get("branch", "") or "").strip()
            if raw.lower() == "proceed" and not branch:
                item["branch"] = "proceed"


def _strip_gate_end_nodes(gate: list[Any]) -> None:
    """Drop model-invented gate_end / summary nodes from gate_trace."""
    gate[:] = [
        item
        for item in gate
        if not (
            isinstance(item, dict)
            and str(item.get("node_id", "") or "").strip().lower() in _GATE_END_NODE_IDS
        )
    ]


def _repair_stage1_gate_trace(obj: dict[str, Any]) -> None:
    """Format-only repairs so strict trace semantics pass on good-faith AI output."""
    gate = obj.get("gate_trace")
    if not isinstance(gate, list) or not gate:
        return

    _repair_gate_trace_answer_aliases(gate)
    _strip_gate_end_nodes(gate)

    canonical_q = _canonical_gate_questions()
    for item in gate:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("node_id", "") or "").strip()
        if nid in canonical_q:
            item["question"] = canonical_q[nid]
        if nid in ("1.3", "2.5"):
            reason = str(item.get("reason", "") or "")
            stripped = _strip_program_reference_blocks(reason)
            if stripped != reason.strip():
                item["reason"] = stripped

    _sync_gate_23_answer_with_direction(obj)
    _repair_gate_result(obj)

    if str(obj.get("gate_result", "")).lower() == "proceed":
        last = gate[-1]
        if isinstance(last, dict):
            blob = str(last.get("reason", "") or "")
            if not any(tok in blob for tok in _PROCEED_FINAL_TOKENS):
                last["reason"] = (blob.rstrip("。") + "，闸门通过，可进入阶段二。").strip()


def normalize_stage1_traces(
    obj: dict[str, Any],
    *,
    normalization_mode: NormalizationMode = "strict",
) -> None:
    gate = obj.get("gate_trace")
    if isinstance(gate, list):
        _strip_ai_gate_14(gate)
    normalize_trace_list(
        gate,
        normalization_mode=normalization_mode,
    )
    _repair_stage1_gate_trace(obj)
    if normalization_mode == "lenient":
        _sync_gate_12_branch_with_cycle(obj)


def normalize_stage2_traces(
    obj: dict[str, Any],
    *,
    normalization_mode: NormalizationMode = "strict",
    default_max_seq: int | None = None,
) -> None:
    trace = obj.get("decision_trace")
    if isinstance(trace, list):
        normalize_trace_list(
            trace,
            default_max_seq=default_max_seq,
            normalization_mode=normalization_mode,
        )
        _repair_stage2_decision_trace_questions(trace)
    _repair_stage2_terminal(obj)
