"""Trade record logger — saves order opportunities to CSV and K-line chart images.

When stage-2 produces an order (限价单 / 突破单 / 市价单), this module:
  1. Appends a rich row to  trade_records/<symbol>_<timeframe>.csv
  2. Renders a K-line + EMA20 chart for the last ≤50 bars and saves it as a
     PNG next to the CSV.

File naming convention
----------------------
CSV   : trade_records/<symbol>_<timeframe>.csv
Image : trade_records/<symbol>_<timeframe>_<timestamp>.png

The image filename uses the same timestamp as the ``record_time`` field so
entries are easy to correlate.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TRADE_RECORDS_DIR = Path("trade_records")

# Maximum bars to show in the chart image
_CHART_MAX_BARS = 50

# ── CSV column definitions ─────────────────────────────────────────────────────

_CSV_FIELDNAMES = [
    # ── Meta ──────────────────────────────────────────────────────────────────
    "record_time",
    "symbol",
    "timeframe",
    "decision_stance",
    "model",
    # ── Decision core ─────────────────────────────────────────────────────────
    "order_direction",
    "order_type",
    "entry_price",
    "stop_loss_price",
    "take_profit_price",
    "take_profit_price_2",
    "entry_rule",
    "entry_basis_bar",
    "entry_basis_extreme",
    # ── Confidence & win-rate ──────────────────────────────────────────────────
    "diagnosis_confidence",
    "diagnosis_confidence_reasoning",
    "trade_confidence",
    "trade_confidence_reasoning",
    "estimated_win_rate",
    "estimated_win_rate_reasoning",
    # ── Reasoning & factors ───────────────────────────────────────────────────
    "reasoning",
    "key_factors",
    "watch_points",
    "risk_assessment",
    "invalidation_condition",
    # ── Diagnosis summary ─────────────────────────────────────────────────────
    "diag_cycle_position",
    "diag_direction",
    "diag_key_signals",
    # ── Bar analysis (stage-2) ────────────────────────────────────────────────
    "s2_always_in",
    "s2_bar_type",
    "s2_signal_bar_bar",
    "s2_signal_bar_quality",
    "s2_signal_bar_pattern",
    "s2_signal_bar_reason",
    "s2_entry_bar_strength",
    "s2_entry_bar_freshness",
    "s2_entry_bar_follow_through",
    "s2_is_second_entry",
    "s2_second_entry_type",
    # ── Next cycle prediction ─────────────────────────────────────────────────
    "next_cycle",
    "next_cycle_direction",
    "next_cycle_probabilities",
    "next_cycle_reasoning",
    # ── Terminal ──────────────────────────────────────────────────────────────
    "terminal_node_id",
    "terminal_outcome",
    "terminal_label",
    # ── Decision trace summary ────────────────────────────────────────────────
    "decision_trace_summary",
    # ── Continuity audit (vs previous CSV row) ────────────────────────────────
    "prev_plan_relation",
    "prev_plan_invalidated",
    "prev_plan_entry",
    "bars_since_prev_plan",
    # ── Image path ────────────────────────────────────────────────────────────
    "chart_image",
]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_sr_price(raw: object) -> float | None:
    """Parse a price value that may be a number, string, or range (e.g. '5380-5400').

    Returns the midpoint for ranges, the numeric value for singles, or None.
    """
    import re as _re
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if v > 0 else None
    text = str(raw).strip()
    # Range: e.g. "5380-5400" or "5380~5400"
    m = _re.search(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)", text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo + hi) / 2.0
    # Single number
    m2 = _re.search(r"\d+(?:\.\d+)?", text)
    if m2:
        return float(m2.group(0))
    return None


def _j(value: Any) -> str:
    """Serialize a value to a compact JSON string for CSV storage."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _get(d: dict | None, *keys: str, default: Any = "") -> Any:
    """Safe nested dict get."""
    if not isinstance(d, dict):
        return default
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur if cur is not None else default


# ── Chart rendering ───────────────────────────────────────────────────────────

def _render_chart(bars_newest_first: list[Any], ema20_newest_first: list[float],
                  symbol: str, timeframe: str, image_path: Path,
                  entry_price: float | None = None,
                  stop_loss_price: float | None = None,
                  take_profit_price: float | None = None,
                  take_profit_price_2: float | None = None,
                  order_direction: str = "",
                  order_type: str = "",
                  diagnosis_confidence: str = "",
                  trade_confidence: str = "",
                  estimated_win_rate: str = "") -> bool:
    """Draw a candlestick + EMA20 chart and save to *image_path*.

    Returns True on success, False if matplotlib is unavailable.
    bars_newest_first: list of KlineBar (or dict with open/high/low/close/ts_open/seq).
    ema20_newest_first: aligned EMA20 values (NaN for warm-up bars).
    entry_price / stop_loss_price / take_profit_price / take_profit_price_2: optional price levels drawn
    as horizontal dashed lines extending into the right-side margin.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.lines import Line2D
    except ImportError:
        logger.warning("matplotlib not installed; skipping chart generation")
        return False

    # Try to use a CJK-capable font so Chinese labels render correctly
    import matplotlib.font_manager as _fm
    _cjk_candidates = [
        "Microsoft YaHei", "SimHei", "WenQuanYi Micro Hei",
        "Noto Sans CJK SC", "Source Han Sans CN",
    ]
    _available = {f.name for f in _fm.fontManager.ttflist}
    for _fc in _cjk_candidates:
        if _fc in _available:
            matplotlib.rcParams["font.family"] = _fc
            break

    # Limit to _CHART_MAX_BARS
    bars = list(reversed(bars_newest_first[:_CHART_MAX_BARS]))  # oldest → newest
    emas = list(reversed(ema20_newest_first[:_CHART_MAX_BARS]))

    n = len(bars)
    if n == 0:
        return False

    fig, ax = plt.subplots(figsize=(16, 7), facecolor="#0d1117")
    ax.set_facecolor("#0d1117")

    # ── Candles ───────────────────────────────────────────────────────────────
    bar_width = 0.6
    for i, bar in enumerate(bars):
        # Support both KlineBar dataclass and plain dict
        if hasattr(bar, "open"):
            o, h, l, c = bar.open, bar.high, bar.low, bar.close
            seq = getattr(bar, "seq", None)
        else:
            o = float(bar.get("open", 0))
            h = float(bar.get("high", 0))
            l = float(bar.get("low", 0))
            c = float(bar.get("close", 0))
            seq = bar.get("seq")

        is_bull = c >= o
        color = "#26a641" if is_bull else "#f85149"

        # Wick
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=2)
        # Body
        body_low = min(o, c)
        body_height = max(abs(c - o), (h - l) * 0.005)
        rect = mpatches.FancyBboxPatch(
            (i - bar_width / 2, body_low),
            bar_width,
            body_height,
            boxstyle="square,pad=0",
            facecolor=color,
            edgecolor=color,
            linewidth=0,
            zorder=3,
        )
        ax.add_patch(rect)

        # Sequence label on every 10th bar (newest = seq 1 at right)
        if seq is not None and seq % 10 == 0:
            ax.text(
                i, h * 1.0003, f"K{seq}",
                color="#8b949e", fontsize=6.5, ha="center", va="bottom", zorder=4,
            )

    # ── EMA20 line ────────────────────────────────────────────────────────────
    ema_x, ema_y = [], []
    for i, v in enumerate(emas):
        if not math.isnan(float(v)):
            ema_x.append(i)
            ema_y.append(v)
    if ema_x:
        ax.plot(ema_x, ema_y, color="#fbbf24", linewidth=1.2, zorder=5, label="EMA20")

    # ── Styling ───────────────────────────────────────────────────────────────
    # Reserve ~8 bar-widths on the right for price labels
    _RIGHT_MARGIN = 8
    ax.set_xlim(-1, n - 1 + _RIGHT_MARGIN)
    ax.tick_params(colors="#8b949e", labelsize=7)
    for spine in ax.spines.values():
        spine.set_edgecolor("#30363d")
    ax.set_title(
        f"{symbol} {timeframe}  —  最近 {n} 根K线（K1=最新收盘）",
        color="#e6edf3", fontsize=10, pad=8,
    )

    # ── Order type badge (top-left corner) ────────────────────────────────────
    if order_type:
        _ot = str(order_type).strip()
        _ot_colors = {
            "限价单": "#fbbf24",   # amber
            "突破单": "#a78bfa",   # purple
            "市价单": "#34d399",   # teal
        }
        _ot_color = _ot_colors.get(_ot, "#8b949e")
        ax.text(
            0.01, 0.97, _ot,
            transform=ax.transAxes,
            color=_ot_color, fontsize=9, fontweight="bold",
            va="top", ha="left",
            bbox=dict(facecolor="#161b22", edgecolor=_ot_color,
                      linewidth=1.2, alpha=0.9, pad=3, boxstyle="round,pad=0.3"),
            zorder=10,
        )

    # ── Confidence badges (top-left, right of order type) ─────────────────────
    # Show diagnosis_confidence / trade_confidence / estimated_win_rate as a
    # compact info row just below the order-type badge.
    _conf_parts = []
    if diagnosis_confidence:
        _conf_parts.append(f"诊断置信 {diagnosis_confidence}")
    if trade_confidence:
        _conf_parts.append(f"交易置信 {trade_confidence}")
    if estimated_win_rate:
        _conf_parts.append(f"胜率 {estimated_win_rate}")
    if _conf_parts:
        _conf_text = "   ".join(_conf_parts)
        ax.text(
            0.01, 0.905, _conf_text,
            transform=ax.transAxes,
            color="#cbd5e1", fontsize=8,
            va="top", ha="left",
            bbox=dict(facecolor="#161b22", edgecolor="#30363d",
                      linewidth=0.8, alpha=0.85, pad=3, boxstyle="round,pad=0.3"),
            zorder=10,
        )

    # ── Entry / SL / TP horizontal lines ──────────────────────────────────────
    # Determine bull/bear from order_direction for colour defaults
    _is_long = "short" not in order_direction.lower() and "做空" not in order_direction
    _ENTRY_COLOR = "#60a5fa"   # blue
    _TP_COLOR    = "#4ade80"   # green
    _TP2_COLOR   = "#86efac"   # lighter green
    _SL_COLOR    = "#f87171"   # red

    _price_lines: list[tuple[float, str, str]] = []  # (price, color, label)
    if entry_price is not None:
        _price_lines.append((entry_price, _ENTRY_COLOR, f"入场  {entry_price}"))
    if take_profit_price is not None:
        _price_lines.append((take_profit_price, _TP_COLOR, f"TP1  {take_profit_price}"))
    if take_profit_price_2 is not None:
        _price_lines.append((take_profit_price_2, _TP2_COLOR, f"TP2  {take_profit_price_2}"))
    if stop_loss_price is not None:
        _price_lines.append((stop_loss_price, _SL_COLOR, f"止损  {stop_loss_price}"))

    _label_x = n - 1 + _RIGHT_MARGIN - 0.3  # anchor for right-side text
    for _price, _color, _label in _price_lines:
        # Dashed line from bar 0 to right margin
        ax.axhline(_price, color=_color, linewidth=1.0, linestyle="--",
                   alpha=0.85, zorder=6)
        # Price label at right margin
        ax.text(
            _label_x, _price, _label,
            color=_color, fontsize=7.5, ha="right", va="center",
            bbox=dict(facecolor="#0d1117", edgecolor="none", alpha=0.7, pad=1.5),
            zorder=7,
        )

    # ── Direction arrow ───────────────────────────────────────────────────────
    # Draw a prominent up/down arrow at the right edge of the last bar to show
    # trade direction.  The arrow is anchored at entry_price when available,
    # otherwise at the last bar's close.
    if entry_price is not None or n > 0:
        # Arrow anchor Y
        _last_bar = bars[-1] if bars else None
        if _last_bar is not None:
            _last_close = (_last_bar.close if hasattr(_last_bar, "close")
                           else float(_last_bar.get("close", 0)))
            _last_high  = (_last_bar.high if hasattr(_last_bar, "high")
                           else float(_last_bar.get("high", 0)))
            _last_low   = (_last_bar.low if hasattr(_last_bar, "low")
                           else float(_last_bar.get("low", 0)))
        else:
            _last_close = _last_high = _last_low = entry_price or 0

        # Estimate price range for sizing the arrow
        all_prices = []
        for _b in bars:
            if hasattr(_b, "high"):
                all_prices += [_b.high, _b.low]
            else:
                all_prices += [float(_b.get("high", 0)), float(_b.get("low", 0))]
        _price_range = max(all_prices) - min(all_prices) if all_prices else 1.0
        _arrow_len = _price_range * 0.06   # 6% of visible range
        _arrow_x   = n - 1                 # x = last bar index

        if _is_long:
            # Up arrow: tail at low, head above
            _tail_y = (_last_low - _price_range * 0.01)
            _head_y = _tail_y + _arrow_len
            ax.annotate(
                "", xy=(_arrow_x, _head_y), xytext=(_arrow_x, _tail_y),
                arrowprops=dict(arrowstyle="-|>", color="#4ade80",
                                lw=2.5, mutation_scale=18),
                zorder=8,
            )
            ax.text(
                _arrow_x, _tail_y - _price_range * 0.005, "做多",
                color="#4ade80", fontsize=8, ha="center", va="top",
                fontweight="bold", zorder=9,
            )
        else:
            # Down arrow: tail at high, head below
            _tail_y = (_last_high + _price_range * 0.01)
            _head_y = _tail_y - _arrow_len
            ax.annotate(
                "", xy=(_arrow_x, _head_y), xytext=(_arrow_x, _tail_y),
                arrowprops=dict(arrowstyle="-|>", color="#f87171",
                                lw=2.5, mutation_scale=18),
                zorder=8,
            )
            ax.text(
                _arrow_x, _tail_y + _price_range * 0.005, "做空",
                color="#f87171", fontsize=8, ha="center", va="bottom",
                fontweight="bold", zorder=9,
            )

    legend_handles = [Line2D([0], [0], color="#fbbf24", linewidth=1.5, label="EMA20")]
    if entry_price is not None:
        legend_handles.append(Line2D([0], [0], color=_ENTRY_COLOR, linewidth=1.0,
                                     linestyle="--", label="入场"))
    if take_profit_price is not None:
        legend_handles.append(Line2D([0], [0], color=_TP_COLOR, linewidth=1.0,
                                     linestyle="--", label="TP1"))
    if take_profit_price_2 is not None:
        legend_handles.append(Line2D([0], [0], color=_TP2_COLOR, linewidth=1.0,
                                     linestyle="--", label="TP2"))
    if stop_loss_price is not None:
        legend_handles.append(Line2D([0], [0], color=_SL_COLOR, linewidth=1.0,
                                     linestyle="--", label="止损"))

    ax.legend(handles=legend_handles,
              facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3", fontsize=8)
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.grid(axis="y", color="#21262d", linewidth=0.5, zorder=1)
    ax.set_xticks([])

    plt.tight_layout()
    image_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(str(image_path), dpi=120, bbox_inches="tight",
                facecolor="#0d1117")
    plt.close(fig)
    logger.info("Trade chart saved: %s", image_path)
    return True


# ── Public API ────────────────────────────────────────────────────────────────

def save_trade_record(
    *,
    decision_inner: dict,
    stage2_full: dict,
    stage1_diagnosis: dict | None,
    frame: Any,            # KlineFrame or None
    meta_symbol: str,
    meta_timeframe: str,
    decision_stance: str,
    model_name: str,
    structure_flip_cooldown_bars: int = 3,
) -> None:
    """Append one row to the trade CSV and generate the chart image.

    All arguments are best-effort; missing data is recorded as empty string.
    """
    try:
        _save_trade_record_impl(
            decision_inner=decision_inner,
            stage2_full=stage2_full,
            stage1_diagnosis=stage1_diagnosis,
            frame=frame,
            meta_symbol=meta_symbol,
            meta_timeframe=meta_timeframe,
            decision_stance=decision_stance,
            model_name=model_name,
            structure_flip_cooldown_bars=structure_flip_cooldown_bars,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("save_trade_record failed: %s", exc, exc_info=True)


def _save_trade_record_impl(
    *,
    decision_inner: dict,
    stage2_full: dict,
    stage1_diagnosis: dict | None,
    frame: Any,
    meta_symbol: str,
    meta_timeframe: str,
    decision_stance: str,
    model_name: str,
    structure_flip_cooldown_bars: int = 3,
) -> None:
    s1 = stage1_diagnosis or {}
    dec = decision_inner or {}
    diag = stage2_full.get("diagnosis_summary") or {}
    bar_analysis = stage2_full.get("bar_analysis") or {}
    terminal = stage2_full.get("terminal") or {}
    next_cycle = stage2_full.get("next_cycle_prediction") or {}
    signal_bar = bar_analysis.get("signal_bar") or {}
    entry_bar = bar_analysis.get("entry_bar") or {}
    second_entry = bar_analysis.get("second_entry") or {}
    now = datetime.now()
    ts_str = now.strftime("%Y%m%d_%H%M%S")
    record_time = now.strftime("%Y-%m-%d %H:%M:%S")

    # ── File paths ────────────────────────────────────────────────────────────
    safe_symbol = meta_symbol.replace("/", "-").replace("\\", "-")
    safe_tf = meta_timeframe.replace("/", "-")
    _TRADE_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = _TRADE_RECORDS_DIR / f"{safe_symbol}_{safe_tf}.csv"
    image_filename = f"{safe_symbol}_{safe_tf}_{ts_str}.png"
    image_path = _TRADE_RECORDS_DIR / image_filename

    # ── Render chart (before CSV so we can record image path) ─────────────────
    chart_written = False
    if frame is not None:
        try:
            bars = list(getattr(frame, "bars", []))
            indicators = getattr(frame, "indicators", None)
            ema20_vals = list(getattr(indicators, "ema20", []) or [])
            chart_written = _render_chart(
                bars_newest_first=bars,
                ema20_newest_first=ema20_vals,
                symbol=meta_symbol,
                timeframe=meta_timeframe,
                image_path=image_path,
                entry_price=_parse_sr_price(dec.get("entry_price")),
                stop_loss_price=_parse_sr_price(dec.get("stop_loss_price")),
                take_profit_price=_parse_sr_price(dec.get("take_profit_price")),
                take_profit_price_2=_parse_sr_price(dec.get("take_profit_price_2")),
                order_direction=str(dec.get("order_direction") or ""),
                order_type=str(dec.get("order_type") or ""),
                diagnosis_confidence=str(dec.get("diagnosis_confidence") or ""),
                trade_confidence=str(dec.get("trade_confidence") or ""),
                estimated_win_rate=str(dec.get("estimated_win_rate") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("chart render failed: %s", exc)

    # ── Decision trace summary (node_id + answer, compact) ───────────────────
    trace = stage2_full.get("decision_trace") or []
    trace_summary = " | ".join(
        f"{t.get('node_id','')}:{t.get('answer','')}" for t in trace if isinstance(t, dict)
    )

    from pa_agent.ai.decision_continuity import (
        audit_relation_fields,
        load_last_trade_csv_row,
    )

    prev_csv_row = load_last_trade_csv_row(meta_symbol, meta_timeframe)
    audit = audit_relation_fields(
        prev_csv_row,
        dec,
        frame=frame,
        cooldown_bars=structure_flip_cooldown_bars,
    )

    # ── Build CSV row ─────────────────────────────────────────────────────────
    row = {
        "record_time": record_time,
        "symbol": meta_symbol,
        "timeframe": meta_timeframe,
        "decision_stance": decision_stance,
        "model": model_name,

        "order_direction": _get(dec, "order_direction"),
        "order_type": _get(dec, "order_type"),
        "entry_price": _get(dec, "entry_price"),
        "stop_loss_price": _get(dec, "stop_loss_price"),
        "take_profit_price": _get(dec, "take_profit_price"),
        "take_profit_price_2": _get(dec, "take_profit_price_2"),
        "entry_rule": _get(dec, "entry_rule"),
        "entry_basis_bar": _get(dec, "entry_basis_bar"),
        "entry_basis_extreme": _get(dec, "entry_basis_extreme"),

        "diagnosis_confidence": _get(dec, "diagnosis_confidence"),
        "diagnosis_confidence_reasoning": _get(dec, "diagnosis_confidence_reasoning"),
        "trade_confidence": _get(dec, "trade_confidence"),
        "trade_confidence_reasoning": _get(dec, "trade_confidence_reasoning"),
        "estimated_win_rate": _get(dec, "estimated_win_rate"),
        "estimated_win_rate_reasoning": _get(dec, "estimated_win_rate_reasoning"),

        "reasoning": _get(dec, "reasoning"),
        "key_factors": _j(_get(dec, "key_factors")),
        "watch_points": _j(_get(dec, "watch_points")),
        "risk_assessment": _get(dec, "risk_assessment"),
        "invalidation_condition": _get(dec, "invalidation_condition"),

        "diag_cycle_position": _get(diag, "cycle_position"),
        "diag_direction": _get(diag, "direction"),
        "diag_key_signals": _j(_get(diag, "key_signals")),

        "s2_always_in": _get(bar_analysis, "always_in"),
        "s2_bar_type": _get(bar_analysis, "bar_type"),
        "s2_signal_bar_bar": _get(signal_bar, "bar"),
        "s2_signal_bar_quality": _get(signal_bar, "quality"),
        "s2_signal_bar_pattern": _get(signal_bar, "pattern"),
        "s2_signal_bar_reason": _get(signal_bar, "reason"),
        "s2_entry_bar_strength": _get(entry_bar, "strength"),
        "s2_entry_bar_freshness": _get(entry_bar, "freshness"),
        "s2_entry_bar_follow_through": _get(entry_bar, "follow_through"),
        "s2_is_second_entry": _get(second_entry, "is_second_entry"),
        "s2_second_entry_type": _get(second_entry, "type"),

        "next_cycle": _get(next_cycle, "cycle"),
        "next_cycle_direction": _get(next_cycle, "direction"),
        "next_cycle_probabilities": _j(_get(next_cycle, "probabilities")),
        "next_cycle_reasoning": _get(next_cycle, "reasoning"),

        "terminal_node_id": _get(terminal, "node_id"),
        "terminal_outcome": _get(terminal, "outcome"),
        "terminal_label": _get(terminal, "label"),

        "decision_trace_summary": trace_summary,

        "prev_plan_relation": audit.get("prev_plan_relation", ""),
        "prev_plan_invalidated": audit.get("prev_plan_invalidated", ""),
        "prev_plan_entry": audit.get("prev_plan_entry", ""),
        "bars_since_prev_plan": audit.get("bars_since_prev_plan", ""),

        "chart_image": image_filename if chart_written else "",
    }

    # ── Write CSV (rewrite with unified header for schema migrations) ─────────
    existing_rows: list[dict[str, str]] = []
    if csv_path.exists():
        try:
            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                existing_rows = list(csv.DictReader(f))
        except OSError:
            existing_rows = []
    merged_row = {k: str(row.get(k, "")) for k in _CSV_FIELDNAMES}
    for k, v in row.items():
        if k in _CSV_FIELDNAMES:
            merged_row[k] = "" if v is None else str(v)
    existing_rows.append(merged_row)
    # Atomic replace: the previous in-place ``open(..., "w")`` destroyed the
    # whole trade history if the process died mid-write.
    import io

    from pa_agent.util.atomic_write import atomic_write_text

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    for r in existing_rows:
        writer.writerow({k: r.get(k, "") for k in _CSV_FIELDNAMES})
    atomic_write_text(csv_path, buffer.getvalue(), encoding="utf-8-sig", newline="")

    logger.info("Trade record appended: %s", csv_path)
