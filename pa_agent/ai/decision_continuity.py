"""Previous-decision continuity: invalidation checks, flip cooldown, Stage-2 prompt block."""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pa_agent.util.price_tick import infer_price_tick_from_frame

_TRADE_RECORDS_DIR = Path("trade_records")

# Default: no opposite-direction plan at the same structure within N closed bars.
DEFAULT_STRUCTURE_FLIP_COOLDOWN_BARS = 3
# Resting limit plans auto-expire after this many closed bars without a fill.
DEFAULT_MAX_PENDING_LIMIT_BARS = 3
_STRUCTURE_TOLERANCE_TICKS = 3

_REL_SAME = "same_direction"
_REL_FLIP = "flip"
_REL_INVALIDATED = "invalidated"
_REL_FIRST = "first"
_REL_NO_ORDER_PREV = "no_order_prev"
_REL_WAIT = "wait_continued"

_REL_LABELS_ZH = {
    _REL_SAME: "同向",
    _REL_FLIP: "反手",
    _REL_INVALIDATED: "已失效",
    _REL_FIRST: "首单",
    _REL_NO_ORDER_PREV: "上轮无单",
    _REL_WAIT: "延续等待",
}


def _parse_price(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
        return v if v == v else None  # NaN guard
    except (TypeError, ValueError):
        return None


def order_direction_sign(direction: str | None) -> int:
    if not direction:
        return 0
    d = str(direction).strip().lower()
    if "多" in d or d in ("long", "buy", "bullish"):
        return 1
    if "空" in d or d in ("short", "sell", "bearish"):
        return -1
    return 0


def order_direction_label(sign: int) -> str:
    if sign > 0:
        return "做多"
    if sign < 0:
        return "做空"
    return "—"


def extract_always_in_branch(stage1_json: dict | None) -> str | None:
    """Return AIL / AIS from gate_trace §2.4, or None if not Always In."""
    if not stage1_json:
        return None
    for node in stage1_json.get("gate_trace") or []:
        if not isinstance(node, dict):
            continue
        nid = str(node.get("node_id", "")).replace("§", "")
        if nid not in ("2.4", "2.4.0"):
            continue
        if str(node.get("answer", "")).strip() not in ("是", "yes", "true"):
            continue
        branch = str(node.get("branch") or "").strip().upper()
        if branch in ("AIL", "AIS"):
            return branch
    return None


def _timeframe_minutes(timeframe: str) -> int:
    tf = (timeframe or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*m", tf)
    if m:
        return max(1, int(m.group(1)))
    h = re.fullmatch(r"(\d+)\s*h", tf)
    if h:
        return max(1, int(h.group(1))) * 60
    d = re.fullmatch(r"(\d+)\s*d", tf)
    if d:
        return max(1, int(d.group(1))) * 1440
    return 5


def _parse_local_iso_ms(iso: str | None) -> int | None:
    """Parse record timestamps (space or ``T`` separator, optional fractional seconds)."""
    if not iso:
        return None
    raw = str(iso).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(raw).timestamp() * 1000)
    except (TypeError, ValueError, OSError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(raw[:19], fmt).timestamp() * 1000)
        except (TypeError, ValueError, OSError):
            continue
    return None


def bars_elapsed_between(
    prev_time_iso: str | None,
    current_ms: int | None,
    timeframe: str,
    *,
    fallback: int = 1,
) -> int:
    if not prev_time_iso or not current_ms:
        return fallback
    prev_ms = _parse_local_iso_ms(prev_time_iso)
    if prev_ms is None:
        return fallback
    bar_ms = _timeframe_minutes(timeframe) * 60 * 1000
    if bar_ms <= 0:
        return fallback
    return max(1, round((int(current_ms) - prev_ms) / bar_ms))


def entries_same_structure(
    entry_a: float | None,
    entry_b: float | None,
    *,
    tick: float,
    tolerance_ticks: int = _STRUCTURE_TOLERANCE_TICKS,
) -> bool:
    if entry_a is None or entry_b is None:
        return False
    tol = max(float(tick) * tolerance_ticks, float(tick))
    return abs(entry_a - entry_b) <= tol


def is_order_plan(decision: dict | None) -> bool:
    if not decision:
        return False
    ot = str(decision.get("order_type") or "").strip()
    return ot not in ("", "不下单", "none", "null")


def assess_plan_invalidation(
    decision: dict | None,
    frame: Any,
) -> tuple[bool, str]:
    """True if latest closed bar (K1) invalidated the previous plan (stop touched)."""
    if not is_order_plan(decision):
        return False, ""
    sign = order_direction_sign(str(decision.get("order_direction") or ""))
    stop = _parse_price(decision.get("stop_loss_price"))
    bars = list(getattr(frame, "bars", ()) or ())
    if not bars or stop is None or sign == 0:
        return False, ""

    k1 = bars[0]
    close = float(getattr(k1, "close", 0))
    high = float(getattr(k1, "high", close))
    low = float(getattr(k1, "low", close))

    if sign < 0:
        if close >= stop or high >= stop:
            return True, f"K1 触及/突破止损 {stop}（做空方案失效）"
    elif sign > 0:
        if close <= stop or low <= stop:
            return True, f"K1 触及/跌破止损 {stop}（做多方案失效）"
    return False, ""


def assess_limit_order_triggered(
    decision: dict | None,
    frame: Any,
    *,
    max_bars: int | None = None,
) -> tuple[bool, str, int | None]:
    """True when a resting limit entry was touched on a recent closed bar."""
    if not is_order_plan(decision):
        return False, "", None
    order_type = str(decision.get("order_type") or "").strip()
    if order_type not in ("限价单", "突破单"):
        return False, "", None

    sign = order_direction_sign(str(decision.get("order_direction") or ""))
    entry = _parse_price(decision.get("entry_price"))
    if entry is None or sign == 0:
        return False, "", None

    bars = list(getattr(frame, "bars", ()) or [])
    if not bars:
        return False, "", None

    scan = bars if max_bars is None else bars[: max(1, int(max_bars))]
    tick = float(infer_price_tick_from_frame(frame) or 0.01)
    tol = max(tick * 0.5, 1e-9)

    for bar in scan:
        seq = int(getattr(bar, "seq", 0) or 0)
        low = float(getattr(bar, "low", 0))
        high = float(getattr(bar, "high", low))
        if sign > 0 and low <= entry + tol:
            return (
                True,
                f"K{seq} low={low} 已触及/跌破限价入场 {entry}（做多限价视为已触发）",
                seq,
            )
        if sign < 0 and high >= entry - tol:
            return (
                True,
                f"K{seq} high={high} 已触及/突破限价入场 {entry}（做空限价视为已触发）",
                seq,
            )
    return False, "", None


def load_last_trade_csv_row(symbol: str, timeframe: str) -> dict[str, str] | None:
    safe_symbol = symbol.replace("/", "-").replace("\\", "-")
    safe_tf = timeframe.replace("/", "-")
    csv_path = _TRADE_RECORDS_DIR / f"{safe_symbol}_{safe_tf}.csv"
    if not csv_path.is_file():
        return None
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        return rows[-1] if rows else None
    except OSError:
        return None


def decision_from_previous_record(previous_record: Any) -> dict[str, Any] | None:
    if previous_record is None:
        return None
    s2 = getattr(previous_record, "stage2_decision", None)
    if s2 is None and isinstance(previous_record, dict):
        s2 = previous_record.get("stage2_decision")
    if not isinstance(s2, dict):
        return None
    dec = s2.get("decision")
    return dec if isinstance(dec, dict) else None


def previous_record_time_iso(previous_record: Any) -> str | None:
    if previous_record is None:
        return None
    meta = getattr(previous_record, "meta", None)
    if meta is None and isinstance(previous_record, dict):
        meta = previous_record.get("meta")
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta.get("timestamp_local_iso")
    return getattr(meta, "timestamp_local_iso", None)


def stage1_from_previous_record(previous_record: Any) -> dict[str, Any] | None:
    """Return stage1_diagnosis dict from a previous analysis record (if present)."""
    if previous_record is None:
        return None
    s1 = getattr(previous_record, "stage1_diagnosis", None)
    if s1 is None and isinstance(previous_record, dict):
        s1 = previous_record.get("stage1_diagnosis")
    return s1 if isinstance(s1, dict) else None


def assess_unfilled_limit_auto_cancel(
    *,
    prev_decision: dict | None,
    prev_stage1: dict | None,
    curr_stage1: dict | None,
    bars_since: int,
    invalidated: bool,
    limit_triggered: bool,
    max_pending_bars: int = DEFAULT_MAX_PENDING_LIMIT_BARS,
) -> tuple[bool, str]:
    """Auto-cancel a resting limit plan when regime changed or it aged out.

    Rule (user): if previous limit plan is still unfilled (not triggered) and either
    - pending for > max_pending_bars closed bars, OR
    - trend direction changed, OR
    - cycle_position changed,
    then mark it invalidated so the next turn can propose a new plan.
    """
    if invalidated or limit_triggered:
        return invalidated, ""
    if not is_order_plan(prev_decision):
        return invalidated, ""
    if str((prev_decision or {}).get("order_type") or "").strip() != "限价单":
        return invalidated, ""

    prev_cycle = str((prev_stage1 or {}).get("cycle_position") or "").strip()
    curr_cycle = str((curr_stage1 or {}).get("cycle_position") or "").strip()
    if prev_cycle and curr_cycle and prev_cycle != curr_cycle:
        return True, f"周期从 {prev_cycle} 变为 {curr_cycle}，未成交限价单自动取消"

    prev_dir = str((prev_stage1 or {}).get("direction") or "").strip()
    curr_dir = str((curr_stage1 or {}).get("direction") or "").strip()
    if prev_dir and curr_dir and prev_dir != curr_dir:
        return True, f"趋势方向从 {prev_dir} 变为 {curr_dir}，未成交限价单自动取消"

    if int(bars_since or 0) > int(max_pending_bars):
        return True, f"未成交限价单已等待 {bars_since} 根 K 线（>{max_pending_bars}）自动取消"

    return invalidated, ""


def assess_combined_plan_invalidation(
    prev_decision: dict | None,
    frame: Any,
    *,
    prev_stage1: dict | None = None,
    curr_stage1: dict | None = None,
    bars_since: int = 1,
    max_pending_bars: int = DEFAULT_MAX_PENDING_LIMIT_BARS,
) -> tuple[bool, str, bool, str, int | None]:
    """Stop-loss, auto-expiry, and regime-change invalidation for a previous plan."""
    invalidated, invalidation_reason = assess_plan_invalidation(prev_decision, frame)
    limit_triggered, trigger_reason, trigger_bar = assess_limit_order_triggered(
        prev_decision,
        frame,
        max_bars=bars_since,
    )
    invalidated, auto_reason = assess_unfilled_limit_auto_cancel(
        prev_decision=prev_decision,
        prev_stage1=prev_stage1,
        curr_stage1=curr_stage1,
        bars_since=bars_since,
        invalidated=invalidated,
        limit_triggered=limit_triggered,
        max_pending_bars=max_pending_bars,
    )
    if auto_reason:
        invalidation_reason = (
            auto_reason
            if not invalidation_reason
            else f"{invalidation_reason}；{auto_reason}"
        )
    return invalidated, invalidation_reason, limit_triggered, trigger_reason, trigger_bar


def classify_vs_previous(
    prev_decision: dict | None,
    curr_decision: dict | None,
    *,
    frame: Any,
    prev_invalidated: bool,
    same_structure: bool,
) -> str:
    if prev_decision is None or not is_order_plan(prev_decision):
        return _REL_FIRST if is_order_plan(curr_decision) else _REL_NO_ORDER_PREV
    if not is_order_plan(curr_decision):
        return _REL_WAIT if not prev_invalidated else _REL_INVALIDATED

    prev_sign = order_direction_sign(str(prev_decision.get("order_direction") or ""))
    curr_sign = order_direction_sign(str(curr_decision.get("order_direction") or ""))
    if prev_invalidated:
        return _REL_INVALIDATED
    if prev_sign == curr_sign:
        return _REL_SAME
    if prev_sign != 0 and curr_sign != 0 and prev_sign != curr_sign:
        return _REL_FLIP if same_structure else _REL_FLIP
    return _REL_SAME


def build_continuity_context(
    *,
    frame: Any,
    stage1_json: dict,
    previous_record: Any | None = None,
    cooldown_bars: int = DEFAULT_STRUCTURE_FLIP_COOLDOWN_BARS,
) -> dict[str, Any]:
    """Assemble continuity facts for prompt injection and CSV audit."""
    symbol = getattr(frame, "symbol", "") or ""
    timeframe = getattr(frame, "timeframe", "") or ""
    tick = infer_price_tick_from_frame(frame)

    prev_decision = decision_from_previous_record(previous_record)
    prev_time = previous_record_time_iso(previous_record)
    prev_source = "analysis_record"
    prev_stage1 = stage1_from_previous_record(previous_record)

    if prev_decision is None:
        csv_row = load_last_trade_csv_row(symbol, timeframe)
        if csv_row:
            prev_source = "trade_csv"
            prev_time = csv_row.get("record_time") or prev_time
            prev_decision = {
                "order_direction": csv_row.get("order_direction"),
                "order_type": csv_row.get("order_type"),
                "entry_price": csv_row.get("entry_price"),
                "stop_loss_price": csv_row.get("stop_loss_price"),
                "take_profit_price": csv_row.get("take_profit_price"),
                "invalidation_condition": csv_row.get("invalidation_condition"),
            }

    current_ms = getattr(frame, "snapshot_ts_local_ms", None)
    bars_since = bars_elapsed_between(prev_time, current_ms, timeframe)

    (
        invalidated,
        invalidation_reason,
        limit_triggered,
        trigger_reason,
        trigger_bar,
    ) = assess_combined_plan_invalidation(
        prev_decision,
        frame,
        prev_stage1=prev_stage1,
        curr_stage1=stage1_json,
        bars_since=bars_since,
    )
    prev_entry = _parse_price((prev_decision or {}).get("entry_price"))
    direction = str(stage1_json.get("direction") or "neutral")
    always_in = extract_always_in_branch(stage1_json)

    return {
        "has_previous_plan": is_order_plan(prev_decision),
        "previous_decision": prev_decision or {},
        "previous_time": prev_time,
        "previous_source": prev_source,
        "bars_since": bars_since,
        "cooldown_bars": max(1, int(cooldown_bars)),
        "invalidated": invalidated,
        "invalidation_reason": invalidation_reason,
        "limit_triggered": limit_triggered,
        "limit_trigger_reason": trigger_reason,
        "limit_trigger_bar": trigger_bar,
        "previous_entry": prev_entry,
        "tick": tick,
        "direction": direction,
        "always_in_branch": always_in,
        "previous_cycle_position": str((prev_stage1 or {}).get("cycle_position") or ""),
        "previous_direction": str((prev_stage1 or {}).get("direction") or ""),
        "timeframe": timeframe,
    }


def render_continuity_prompt_block(ctx: dict[str, Any]) -> str:
    if not ctx.get("has_previous_plan"):
        direction = ctx.get("direction", "neutral")
        always_in = ctx.get("always_in_branch")
        neutral_lines = [
            "## 方案连续性规则（程序强制，阶段二必须遵守）",
            "",
            "### A. direction=neutral 时的方向约束",
            "- 阶段一 `direction=neutral` 时，**禁止**在上下边界双向同时给刮头皮方案。",
        ]
        if always_in == "AIL":
            neutral_lines.append(
                "- 当前 §2.4=**AIL** → **仅允许做多侧** setup（回踩支撑/下边界/顺势回撤做多）。"
                "禁止 §9.0P 阻力做空。"
            )
        elif always_in == "AIS":
            neutral_lines.append(
                "- 当前 §2.4=**AIS** → **仅允许做空侧** setup（反弹阻力/上边界/顺势回撤做空）。"
                "禁止 §9.0P 支撑做多。"
            )
        else:
            neutral_lines.append(
                "- 当前 §2.4 **非** Always In → §9.0P 计划型限价默认 **wait**；"
                "仅当出现与 §2.4 方向一致的强信号棒（§9.0=是）才可下单。"
            )
        neutral_lines.extend([
            "",
            "### B. 同结构位反手冷却",
            f"- 若上一轮有可执行方案且**未失效**，{ctx.get('cooldown_bars', 3)} 根已收盘 K 线内，"
            "禁止在**同一结构位**（entry 相差≤3跳）提出**反向**新方案；"
            "除非 K1 **收盘**突破上一轮 `invalidation_condition` / 止损结构位。",
            "",
            "（本轮无上一轮下单方案记录，仅适用 A/B 通用规则。）",
        ])
        return "\n".join(neutral_lines)

    prev = ctx.get("previous_decision") or {}
    prev_dir = str(prev.get("order_direction") or "—")
    prev_type = str(prev.get("order_type") or "—")
    prev_entry = prev.get("entry_price", "—")
    prev_stop = prev.get("stop_loss_price", "—")
    prev_time = ctx.get("previous_time") or "—"
    bars_since = ctx.get("bars_since", 1)
    cooldown = ctx.get("cooldown_bars", 3)
    inv = ctx.get("invalidated", False)
    inv_reason = ctx.get("invalidation_reason") or ""
    limit_triggered = bool(ctx.get("limit_triggered"))
    trigger_reason = str(ctx.get("limit_trigger_reason") or "")

    status = "**已失效**" if inv else "**未失效（仍有效）**"
    if inv and inv_reason:
        status += f"：{inv_reason}"
    if limit_triggered:
        trigger_status = "**限价已触发**"
        if trigger_reason:
            trigger_status += f"：{trigger_reason}"
    else:
        trigger_status = "**限价未触发**"

    direction = ctx.get("direction", "neutral")
    always_in = ctx.get("always_in_branch")

    lines = [
        "## 上一轮交易方案连续性（程序评估，阶段二必须遵守）",
        "",
        f"上一轮（{prev_time}，约 {bars_since} 根 {ctx.get('timeframe', '')} K 线前）方案：",
        f"- **{prev_dir}** {prev_type} @ {prev_entry}，止损 {prev_stop}",
        f"- 程序判定：{status}；{trigger_status}",
        "",
        "### 裁定（按优先级）",
        "0. **已失效**（止损触及，或程序自动取消：未成交限价单等待"
        f">{DEFAULT_MAX_PENDING_LIMIT_BARS} 根 K 线 / 阶段一 `cycle_position` 变化 / "
        "`direction` 变化）→ **不得**写「仍等待上一轮限价/setup」；须按本轮结构重新评估，"
        "可给新方案或明确观望。",
        f"1. **未失效 + 限价未触发** → 默认 `order_type=不下单`、`terminal.outcome=wait`，"
        "在 watch_points 说明仍等待上一轮限价/setup 触价；"
        "**禁止**立即在相近结构位反手，除非 K1 收盘已触发失效。",
        "2. **未失效 + 限价已触发** → **禁止**写「仍等待限价触发/尚未触价」；"
        "须承认限价已在程序标注的 K 线触价，改为评估触发后跟随（follow_through）、"
        "止损是否仍有效、是否需观望平仓条件；可 `order_type=不下单`，"
        "但 reasoning/watch_points 必须写「限价已于 Kx 触发」及后续条件，"
        "不得把已触价当成未成交挂单。",
        f"3. **同结构位反手冷却**：{cooldown} 根 K 线内，"
        "若新 entry 与上一轮 entry 相差≤3跳，**禁止反向**新单（失效后除外）。",
        "4. **direction=neutral** 时仅顺 §2.4：",
    ]
    if always_in == "AIL":
        lines.append("   - 当前 **AIL** → 只允许做多侧；禁止做空限价/突破。")
    elif always_in == "AIS":
        lines.append("   - 当前 **AIS** → 只允许做空侧；禁止做多限价/突破。")
    else:
        lines.append("   - §2.4 非 Always In → 无强信号则 wait，禁止边界双向刮头皮。")

    if direction != "neutral":
        lines.append(f"   - （本轮 direction={direction}，neutral 约束不适用。）")

    lines.extend([
        "",
        "若确需覆盖上述连续性规则，须在 `decision.reasoning` **首句**写明「连续性覆盖」及 K 线收盘证据。",
    ])
    return "\n".join(lines)


def continuity_violation_reason(
    ctx: dict[str, Any],
    decision: dict,
) -> str | None:
    """Return a reason string if decision violates continuity rules (for normalizer guard)."""
    if not isinstance(decision, dict):
        return None
    if not is_order_plan(decision):
        return None

    direction = str(ctx.get("direction") or "neutral")
    always_in = ctx.get("always_in_branch")
    curr_sign = order_direction_sign(str(decision.get("order_direction") or ""))

    if direction == "neutral":
        if always_in == "AIL" and curr_sign < 0:
            return "direction=neutral 且 §2.4=AIL：禁止做空方案"
        if always_in == "AIS" and curr_sign > 0:
            return "direction=neutral 且 §2.4=AIS：禁止做多方案"
        if always_in is None and curr_sign != 0:
            return "direction=neutral 且 §2.4 非 Always In：禁止无强信号的方向性方案"

    if not ctx.get("has_previous_plan"):
        return None

    prev = ctx.get("previous_decision") or {}
    prev_sign = order_direction_sign(str(prev.get("order_direction") or ""))
    prev_entry = ctx.get("previous_entry")
    curr_entry = _parse_price(decision.get("entry_price"))
    tick = float(ctx.get("tick") or 0.01)
    same_struct = entries_same_structure(prev_entry, curr_entry, tick=tick)
    bars_since = int(ctx.get("bars_since") or 1)
    cooldown = int(ctx.get("cooldown_bars") or DEFAULT_STRUCTURE_FLIP_COOLDOWN_BARS)
    invalidated = bool(ctx.get("invalidated"))

    if not invalidated and prev_sign != 0 and curr_sign != 0 and prev_sign != curr_sign:
        if same_struct and bars_since <= cooldown:
            return (
                f"上一轮方案未失效，{bars_since} 根 K 线内同结构位反手"
                f"（entry {prev_entry} → {curr_entry}）"
            )
    return None


def apply_continuity_guard(
    stage2: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """Force wait/no-order when continuity rules are clearly violated."""
    if not ctx or not isinstance(stage2, dict):
        return stage2

    decision = stage2.get("decision")
    if not isinstance(decision, dict):
        return stage2

    reason = continuity_violation_reason(ctx, decision)
    if not reason:
        return stage2

    # 审计用：先把模型给的原始方案留下来。下面会清空价格字段——若不保留，
    # “模型本来想做什么”就永久丢失，台账/复盘都无法评估这次改写是否值得。
    original_plan = {
        k: decision.get(k)
        for k in (
            "order_direction",
            "order_type",
            "entry_price",
            "stop_loss_price",
            "take_profit_price",
            "take_profit_price_2",
            "estimated_win_rate",
        )
        if decision.get(k) is not None
    }

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
    prefix = f"【程序连续性守卫】{reason}；改为不下单。 "
    # Keep in sync with stage2_normalizer.DECISION_REASONING_MAX_LEN (schema maxLength).
    max_len = 280
    budget = max_len - len(prefix)
    if budget < 1:
        decision["reasoning"] = prefix[:max_len]
    else:
        body = existing
        if len(body) > budget:
            body = body[: budget - 1] + "…"
        decision["reasoning"] = prefix + body

    terminal = dict(stage2.get("terminal") or {})
    terminal["outcome"] = "wait"
    terminal["node_id"] = "continuity"
    terminal["label"] = "方案连续性守卫"

    stage2 = dict(stage2)
    stage2["decision"] = decision
    stage2["terminal"] = terminal
    # 审计用：把被改写的原始方案挂回 stage2 根节点（归一化在 schema 校验之后执行，
    # 不会触发校验失败）。台账/报告据此评估“守卫这次拦得对不对”。
    stage2["continuity_guard"] = {
        "applied": True,
        "reason": reason,
        "original_plan": original_plan,
    }
    return stage2


def audit_relation_fields(
    prev_row: dict[str, str] | None,
    curr_decision: dict | None,
    *,
    frame: Any,
    cooldown_bars: int = DEFAULT_STRUCTURE_FLIP_COOLDOWN_BARS,
) -> dict[str, str]:
    """Fields for trade_records CSV audit columns."""
    if not prev_row:
        rel = _REL_FIRST if is_order_plan(curr_decision) else _REL_NO_ORDER_PREV
        return {
            "prev_plan_relation": _REL_LABELS_ZH.get(rel, rel),
            "prev_plan_invalidated": "",
            "prev_plan_entry": "",
            "bars_since_prev_plan": "",
        }

    prev_decision = {
        "order_direction": prev_row.get("order_direction"),
        "order_type": prev_row.get("order_type"),
        "entry_price": prev_row.get("entry_price"),
        "stop_loss_price": prev_row.get("stop_loss_price"),
    }
    tick = infer_price_tick_from_frame(frame) if frame is not None else 0.01
    prev_entry = _parse_price(prev_decision.get("entry_price"))
    curr_entry = _parse_price((curr_decision or {}).get("entry_price"))
    same_struct = entries_same_structure(prev_entry, curr_entry, tick=tick)

    timeframe = getattr(frame, "timeframe", "") if frame is not None else ""
    current_ms = getattr(frame, "snapshot_ts_local_ms", None) if frame is not None else None
    bars_since = bars_elapsed_between(
        prev_row.get("record_time"),
        current_ms,
        timeframe,
    )
    invalidated, _, _, _, _ = assess_combined_plan_invalidation(
        prev_decision,
        frame,
        bars_since=bars_since,
    )

    rel_key = classify_vs_previous(
        prev_decision,
        curr_decision,
        frame=frame,
        prev_invalidated=invalidated,
        same_structure=same_struct,
    )
    if (
        rel_key == _REL_FLIP
        and bars_since > cooldown_bars
        and not same_struct
    ):
        rel_key = _REL_SAME  # flip at different structure — label as new setup

    return {
        "prev_plan_relation": _REL_LABELS_ZH.get(rel_key, rel_key),
        "prev_plan_invalidated": "true" if invalidated else "false",
        "prev_plan_entry": str(prev_entry) if prev_entry is not None else "",
        "bars_since_prev_plan": str(bars_since),
    }
