"""DecisionPanel — trading decision + market diagnosis summary."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from typing import Any

from pa_agent.util.trade_metrics import (
    compute_risk_reward,
    format_estimated_win_rate,
    min_risk_reward_ratio,
    passes_trader_equation,
)

from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

_NO_ORDER = "不下单"

# Reasoning text — larger than default mutedLabel (11px)
_REASON_FONT_CSS = "font-size: 14px; color: #c9d1d9; line-height: 1.45;"
_REASON_EDIT_CSS = (
    "font-size: 14px; color: #e6edf3; line-height: 1.45;"
    "font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;"
)

_PREDICTION_UNPREDICTABLE_COLOR = "#8b949e"
_PREDICTION_UNPREDICTABLE_LABEL = "不可预测"

# Brooks cycle_position → 中文（市场周期 / 频谱位置）
_CYCLE_POSITION_ZH: dict[str, str] = {
    "spike": "尖峰 (Spike)",
    "micro_channel": "微型通道",
    "tight_channel": "窄通道",
    "normal_channel": "正常通道",
    "broad_channel": "宽通道",
    "trending_tr": "趋势型交易区间",
    "trading_range": "交易区间",
    "extreme_tr": "极端交易区间",
    "unknown": "未知",
}

# 以震荡为主的周期类型
_RANGE_CYCLES = frozenset({"trading_range", "extreme_tr", "trending_tr"})

_MARKET_PHASE_ZH: dict[str, str] = {
    "stable": "稳定",
    "transitioning": "过渡",
}

_PREDICTION_DOMINANT_COLOR: dict[str, str] = {
    "bullish": "#3fb950",
    "bearish": "#f85149",
    "neutral": "#e6b800",
}


def _format_cycle_position(raw: str) -> str:
    key = (raw or "").strip().lower()
    return _CYCLE_POSITION_ZH.get(key, raw or "—")


def _format_market_phase(raw: str) -> str:
    key = (raw or "").strip().lower()
    return _MARKET_PHASE_ZH.get(key, raw or "—")


def _infer_trend_label(direction: str, cycle_position: str) -> str:
    """Map AI direction + cycle to 上涨 / 下跌 / 震荡."""
    cp = (cycle_position or "").strip().lower()
    d = (direction or "").strip().lower()

    if cp in _RANGE_CYCLES:
        return "震荡"

    if d == "bullish":
        return "上涨"
    if d == "bearish":
        return "下跌"
    if d == "neutral":
        return "震荡"

    if cp in ("spike", "micro_channel", "tight_channel"):
        return "趋势运行中"
    return "—"


def _trend_color(label: str) -> str:
    if label == "上涨":
        return "#3fb950"
    if label == "下跌":
        return "#f85149"
    if label in ("震荡", "趋势运行中"):
        return "#e6b800"
    return "#8b949e"


def _score_color(score: int) -> str:
    if score >= 70:
        return "#3fb950"
    if score >= 50:
        return "#e6b800"
    return "#f85149"


def _parse_score_100(value: object) -> int | None:
    """Parse 0–100 confidence score."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return max(0, min(100, int(value)))
    try:
        return max(0, min(100, int(float(str(value).strip()))))
    except (ValueError, TypeError):
        return None


def _format_prediction_probs_line(probs: dict) -> str:
    bull = probs.get("bullish", "?")
    bear = probs.get("bearish", "?")
    neut = probs.get("neutral", "?")
    return f"阳 {bull}%  ·  阴 {bear}%  ·  中 {neut}%"


def _dominant_prediction_direction(probs: dict) -> str | None:
    """Return bullish/bearish/neutral for styling by highest probability."""
    parsed: list[tuple[str, float]] = []
    for key in ("bullish", "bearish", "neutral"):
        raw = probs.get(key)
        if raw is None or raw == "":
            continue
        try:
            parsed.append((key, float(raw)))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return None
    return max(parsed, key=lambda item: item[1])[0]


class DecisionPanel(QWidget):
    """Renders market diagnosis + Stage-2 trading decision in compact card layout."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_ui()

    # ── UI builders ───────────────────────────────────────────────────────

    def _create_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            "QFrame {"
            "  background-color: #1c2128;"
            "  border: 1px solid #30363d;"
            "  border-radius: 8px;"
            "}"
        )
        return card

    def _create_section_title(self, text: str) -> tuple[QFrame, QLabel, QLabel]:
        frame = QFrame()
        frame.setFixedHeight(38)
        frame.setStyleSheet(
            "background-color: #21262d;"
            "border-bottom: 1px solid #30363d;"
            "border-top-left-radius: 8px;"
            "border-top-right-radius: 8px;"
        )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)

        lbl = QLabel(text)
        lbl.setStyleSheet("font-size: 13px; font-weight: bold; color: #e6edf3;")
        layout.addWidget(lbl)
        layout.addStretch(1)

        pill = QLabel("")
        pill.setStyleSheet(
            "font-size: 11px; font-weight: bold; padding: 2px 8px;"
            "border-radius: 999px; color: #e6edf3;"
            "border: 1px solid rgba(48,54,61,0.5);"
            "background-color: rgba(48,54,61,0.3);"
        )
        layout.addWidget(pill)

        return frame, lbl, pill

    def _create_decision_row(
        self, key: str, val: str = "—"
    ) -> tuple[QFrame, QLabel, QLabel]:
        row = QFrame()
        row.setFrameShape(QFrame.Shape.NoFrame)
        row.setStyleSheet("background: transparent; border: none;")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        key_lbl = QLabel(key)
        key_lbl.setFixedWidth(110)
        key_lbl.setStyleSheet("font-size: 12px; color: #8b949e;")

        val_lbl = QLabel(val)
        val_lbl.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #e6edf3;"
            "font-family: 'Microsoft YaHei UI', 'Segoe UI', monospace;"
        )
        val_lbl.setWordWrap(True)
        val_lbl.setMinimumHeight(0)
        val_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout.addWidget(key_lbl)
        layout.addWidget(val_lbl, stretch=1)

        return row, key_lbl, val_lbl

    def _create_price_highlight_card(
        self, title: str, accent: str
    ) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.NoFrame)
        card.setStyleSheet(
            "QFrame {"
            "  background-color: #161b22;"
            f"  border: 1px solid {accent};"
            "  border-radius: 8px;"
            "}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(2)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: bold; color: {accent};"
        )

        value_lbl = QLabel("—")
        value_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        value_lbl.setStyleSheet(
            "font-size: 22px; font-weight: 900; color: #e6edf3;"
            "font-family: 'Microsoft YaHei UI', 'Segoe UI', monospace;"
        )
        value_lbl.setMinimumHeight(28)
        value_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        layout.addWidget(title_lbl)
        layout.addWidget(value_lbl)
        return card, value_lbl

    def _add_decision_row(self, key: str, val: str = "—") -> QLabel:
        row, _, val_lbl = self._create_decision_row(key, val)
        self._decision_list.addWidget(row)
        return val_lbl

    def _create_path_item(self, index: int, text: str, status: str) -> QFrame:
        row = QFrame()
        row.setStyleSheet(
            "background-color: #161b22;"
            "border: 1px solid #30363d;"
            "border-radius: 6px;"
        )
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(8)

        idx_lbl = QLabel(str(index))
        idx_lbl.setFixedSize(22, 22)
        idx_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        idx_lbl.setStyleSheet(
            "font-size: 11px; font-weight: bold; color: #2dd4bf;"
            "background-color: rgba(45,212,191,0.12);"
            "border-radius: 11px;"
        )

        text_lbl = QLabel(text)
        text_lbl.setStyleSheet("font-size: 12px; color: #e6edf3;")
        text_lbl.setWordWrap(True)

        status_lower = status.lower()
        if status_lower == "pass":
            pill_style = (
                "color: #86efac; border: 1px solid rgba(34,197,94,0.35);"
                "background-color: rgba(34,197,94,0.10);"
            )
        elif status_lower == "wait":
            pill_style = (
                "color: #fbbf24; border: 1px solid rgba(245,158,11,0.35);"
                "background-color: rgba(245,158,11,0.10);"
            )
        elif status_lower == "hold":
            pill_style = (
                "color: #7dd3fc; border: 1px solid rgba(56,189,248,0.35);"
                "background-color: rgba(56,189,248,0.10);"
            )
        else:
            pill_style = (
                "color: #e6edf3; border: 1px solid rgba(48,54,61,0.5);"
                "background-color: rgba(48,54,61,0.3);"
            )

        pill = QLabel(status.upper())
        pill.setStyleSheet(
            f"font-size: 10px; font-weight: bold; padding: 1px 6px;"
            f"border-radius: 999px; {pill_style}"
        )

        layout.addWidget(idx_lbl)
        layout.addWidget(text_lbl, stretch=1)
        layout.addWidget(pill)

        return row

    def _set_pill_style(
        self, label: QLabel, status: str, text: str | None = None
    ) -> None:
        status_lower = status.lower()
        if status_lower in ("pass", "buy", "long", "做多"):
            style = (
                "color: #86efac; border: 1px solid rgba(34,197,94,0.35);"
                "background-color: rgba(34,197,94,0.10);"
            )
        elif status_lower in ("sell", "short", "做空"):
            style = (
                "color: #f85149; border: 1px solid rgba(248,81,73,0.35);"
                "background-color: rgba(248,81,73,0.10);"
            )
        elif status_lower == "wait":
            style = (
                "color: #fbbf24; border: 1px solid rgba(245,158,11,0.35);"
                "background-color: rgba(245,158,11,0.10);"
            )
        elif status_lower == "hold":
            style = (
                "color: #7dd3fc; border: 1px solid rgba(56,189,248,0.35);"
                "background-color: rgba(56,189,248,0.10);"
            )
        else:
            style = (
                "color: #e6edf3; border: 1px solid rgba(48,54,61,0.5);"
                "background-color: rgba(48,54,61,0.3);"
            )
        label.setStyleSheet(
            f"font-size: 11px; font-weight: bold; padding: 2px 8px;"
            f"border-radius: 999px; {style}"
        )
        if text is not None:
            label.setText(text)

    def _clear_path_items(self) -> None:
        for w in self._path_widgets:
            self._path_list.removeWidget(w)
            w.deleteLater()
        self._path_widgets.clear()

    def _add_path_item(self, index: int, text: str, status: str) -> None:
        item = self._create_path_item(index, text, status)
        self._path_list.addWidget(item)
        self._path_widgets.append(item)

    def _set_reasons(self, text: str) -> None:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            self._reasons_edit.clear()
            return
        html = (
            "<ol style='margin:0;padding-left:16px;"
            "color:#e6edf3;font-size:12px;line-height:1.7;'>"
        )
        for line in lines:
            clean = line
            if len(clean) > 2 and clean[0].isdigit() and clean[1] == ".":
                clean = clean[2:].strip()
            elif clean.startswith("- ") or clean.startswith("* "):
                clean = clean[2:].strip()
            html += f"<li style='margin-bottom:4px;'>{clean}</li>"
        html += "</ol>"
        self._reasons_edit.setHtml(html)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self._action_card = self._create_card()
        self._action_card.setObjectName("actionSummaryCard")
        action_layout = QGridLayout(self._action_card)
        action_layout.setContentsMargins(14, 12, 14, 12)
        action_layout.setHorizontalSpacing(12)
        action_layout.setVerticalSpacing(7)

        self._action_title = QLabel("等待分析")
        self._action_title.setStyleSheet(
            "font-size: 20px; font-weight: 800; color: #e6edf3;"
        )
        self._action_title.setWordWrap(True)
        action_layout.addWidget(self._action_title, 0, 0, 1, 4)

        (
            self._action_entry_card,
            self._action_entry_value,
        ) = self._create_price_highlight_card("入场价格", "#58a6ff")
        (
            self._action_take_profit_card,
            self._action_take_profit_value,
        ) = self._create_price_highlight_card("止盈目标", "#3fb950")
        action_layout.addWidget(self._action_entry_card, 1, 0, 1, 2)
        action_layout.addWidget(self._action_take_profit_card, 1, 2, 1, 2)

        self._action_reason = QLabel("提交分析后，这里会优先显示可执行结论。")
        self._action_reason.setStyleSheet("font-size: 13px; color: #c9d1d9;")
        self._action_reason.setWordWrap(True)
        action_layout.addWidget(self._action_reason, 2, 0, 1, 4)

        self._action_trigger = QLabel("等待条件：—")
        self._action_invalidation = QLabel("失效条件：—")
        self._action_next_bar = QLabel("下一根：—")
        for idx, lbl in enumerate(
            (self._action_trigger, self._action_invalidation, self._action_next_bar)
        ):
            lbl.setStyleSheet(
                "font-size: 12px; color: #8b949e; padding: 4px 8px;"
                "background-color: #161b22; border: 1px solid #30363d;"
                "border-radius: 6px;"
            )
            lbl.setWordWrap(True)
            action_layout.addWidget(lbl, 3, idx)
        action_layout.setColumnStretch(0, 1)
        action_layout.setColumnStretch(1, 1)
        action_layout.setColumnStretch(2, 1)
        action_layout.setColumnStretch(3, 0)
        layout.addWidget(self._action_card)

        # ── Two-column card layout ──────────────────────────────────────
        self._detail_scroll = QScrollArea()
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._detail_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        detail_content = QWidget()
        detail_content.setStyleSheet("background: transparent;")
        hbox = QHBoxLayout(detail_content)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.setSpacing(12)

        # Left card: 决策摘要
        self._left_card = self._create_card()
        left_layout = QVBoxLayout(self._left_card)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        (
            self._left_title_frame,
            self._left_title_lbl,
            self._left_pill,
        ) = self._create_section_title("决策摘要")
        left_layout.addWidget(self._left_title_frame)

        self._decision_list = QVBoxLayout()
        self._decision_list.setContentsMargins(8, 8, 8, 8)
        self._decision_list.setSpacing(4)

        self._row_order_type = self._add_decision_row("订单类型", "—")
        self._row_direction = self._add_decision_row("交易方向", "—")
        self._row_trigger = self._add_decision_row("触发条件", "—")
        self._row_invalidation = self._add_decision_row("失效条件", "—")
        self._row_diag = self._add_decision_row("趋势/周期/阶段", "—")
        self._row_diag_conf = self._add_decision_row("市场判断置信度", "—")
        self._row_trade_conf = self._add_decision_row("交易置信度", "—")
        self._row_prices = self._add_decision_row("入场/止盈/止损", "—")
        self._row_rr = self._add_decision_row("盈亏比/胜率", "—")
        self._row_next_bar = self._add_decision_row("下一根预测", "—")

        left_layout.addLayout(self._decision_list)
        left_layout.addStretch(1)
        hbox.addWidget(self._left_card, stretch=1)

        # Right card: 原因与路径
        self._right_card = self._create_card()
        right_layout = QVBoxLayout(self._right_card)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        (
            self._right_title_frame,
            self._right_title_lbl,
            self._right_pill,
        ) = self._create_section_title("原因与路径")
        right_layout.addWidget(self._right_title_frame)

        self._reasons_edit = QTextEdit()
        self._reasons_edit.setReadOnly(True)
        self._reasons_edit.setStyleSheet(
            "font-size: 12px; color: #e6edf3; line-height: 1.7;"
            "padding: 12px 16px 14px 28px;"
            "border: none; background: transparent;"
        )
        self._reasons_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self._reasons_edit.setMinimumHeight(60)
        self._reasons_edit.setMaximumHeight(160)
        right_layout.addWidget(self._reasons_edit)

        self._path_list = QVBoxLayout()
        self._path_list.setContentsMargins(8, 8, 8, 8)
        self._path_list.setSpacing(6)
        right_layout.addLayout(self._path_list)
        self._path_widgets: list[QWidget] = []

        hbox.addWidget(self._right_card, stretch=1)
        self._detail_scroll.setWidget(detail_content)
        layout.addWidget(self._detail_scroll, stretch=1)

        self.clear()

    # ── Data binding helpers (retained logic, adapted to new layout) ─────

    def _apply_market_diagnosis(
        self,
        diagnosis_summary: dict | None,
        stage1_diagnosis: dict | None = None,
    ) -> str:
        """Extract trend / cycle / phase and return a compact string."""
        src: dict = {}
        if diagnosis_summary:
            src.update(diagnosis_summary)
        if stage1_diagnosis:
            for k, v in stage1_diagnosis.items():
                src.setdefault(k, v)

        direction = str(src.get("direction", "") or "")
        cycle_position = str(src.get("cycle_position", "") or "")
        alt_cycle = src.get("alternative_cycle_position")
        market_phase = str(src.get("market_phase", "") or "")

        trend = _infer_trend_label(direction, cycle_position)
        cycle_zh = _format_cycle_position(cycle_position)
        if alt_cycle:
            cycle_zh += f"（备选 {_format_cycle_position(str(alt_cycle))}）"

        phase_zh = _format_market_phase(market_phase)
        if market_phase == "transitioning":
            risk = src.get("transition_risk")
            if risk:
                phase_zh += f" · 风险{risk}"

        return f"{trend} / {cycle_zh} / {phase_zh}"

    def _short_text(self, value: object, *, max_chars: int = 120) -> str:
        text = str(value or "").strip()
        if not text:
            return "—"
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"

    def _format_price_value(self, value: object) -> str:
        if value is None or value == "":
            return "—"
        try:
            return f"{float(value):.5g}"
        except (TypeError, ValueError):
            return str(value)

    def _set_action_price_highlights(
        self, entry: object, take_profit: object, *, visible: bool
    ) -> None:
        self._action_entry_value.setText(self._format_price_value(entry))
        self._action_take_profit_value.setText(self._format_price_value(take_profit))
        self._action_entry_card.setVisible(visible)
        self._action_take_profit_card.setVisible(visible)

    def _set_action_summary(
        self,
        *,
        order_type: object,
        direction: object,
        reasoning: object,
        trigger: object,
        invalidation: object,
        next_bar_text: str,
    ) -> None:
        order = str(order_type or _NO_ORDER)
        dir_text = str(direction or "")
        reason = self._short_text(reasoning, max_chars=150)
        trigger_text = self._short_text(trigger)
        invalidation_text = self._short_text(invalidation)
        next_text = self._short_text(next_bar_text, max_chars=90)

        if order == _NO_ORDER:
            self._action_title.setText("不下单 · 等待确认")
            self._action_title.setStyleSheet(
                "font-size: 20px; font-weight: 800; color: #86efac;"
            )
            self._action_reason.setText(f"原因：{reason}")
            self._action_trigger.setText(f"等待条件：{trigger_text}")
        else:
            action = dir_text or order
            self._action_title.setText(f"{action} · {order}")
            color = "#86efac"
            if "空" in action or "卖" in action or "short" in action.lower():
                color = "#f85149"
            self._action_title.setStyleSheet(
                f"font-size: 20px; font-weight: 800; color: {color};"
            )
            self._action_reason.setText(f"交易理由：{reason}")
            self._action_trigger.setText(f"触发条件：{trigger_text}")

        self._action_invalidation.setText(f"失效条件：{invalidation_text}")
        self._action_next_bar.setText(f"下一根：{next_text}")

    def _apply_diagnosis_confidence(
        self,
        diagnosis_confidence: object,
        diagnosis_confidence_reasoning: str | None,
    ) -> str | None:
        """Return formatted market-judgment confidence text."""
        score = _parse_score_100(diagnosis_confidence)
        if score is not None:
            return f"{score} / 100"
        return None

    def _apply_trade_confidence_inline(
        self,
        trade_confidence: object,
        trade_confidence_reasoning: str | None,
        *,
        no_order: bool = False,
    ) -> str | None:
        """Return formatted trade confidence text."""
        score = _parse_score_100(trade_confidence)
        if score is not None:
            hint = "观望" if no_order else "入场"
            return f"{score} / 100 · {hint}"
        return None

    def _format_next_bar_prediction(self, decision: dict) -> str:
        """Return formatted next-bar prediction string."""
        pred = decision.get("next_bar_prediction")
        if not isinstance(pred, dict):
            return "—"

        unpredictable = bool(pred.get("unpredictable", False))
        if unpredictable:
            return _PREDICTION_UNPREDICTABLE_LABEL

        probs = pred.get("probabilities")
        if isinstance(probs, dict):
            return _format_prediction_probs_line(probs)
        return "—"

    def _set_conclusion_bar_style(self) -> None:
        """No-op in new layout; kept for backward compatibility."""
        pass

    def _reset_conclusion_bar_side_labels(self) -> None:
        """No-op in new layout; kept for backward compatibility."""
        pass

    # ── Public API ────────────────────────────────────────────────────────

    def set_decision(
        self,
        decision: dict,
        *,
        diagnosis_summary: dict | None = None,
        stage1_diagnosis: dict | None = None,
        decision_stance: str | None = None,
    ) -> None:
        order_type = decision.get("order_type", _NO_ORDER)
        direction = decision.get("order_direction", "—")
        reasoning = decision.get("reasoning", decision.get("brief_reasoning", ""))
        trigger = decision.get("trigger_condition", "—")
        invalidation = decision.get("invalidation_condition", "—")
        diag_conf = decision.get("diagnosis_confidence", None)
        diag_conf_reasoning = decision.get("diagnosis_confidence_reasoning", None)
        trade_conf = decision.get("trade_confidence", None)
        trade_conf_reasoning = decision.get("trade_confidence_reasoning", None)

        # ── Left card: diagnosis & confidence ──
        diag_text = self._apply_market_diagnosis(diagnosis_summary, stage1_diagnosis)
        self._row_diag.setText(diag_text)

        diag_conf_text = self._apply_diagnosis_confidence(
            diag_conf, diag_conf_reasoning
        )
        if diag_conf_text:
            self._row_diag_conf.setText(diag_conf_text)
            self._row_diag_conf.parent().setVisible(True)
        else:
            self._row_diag_conf.parent().setVisible(False)

        trade_conf_text = self._apply_trade_confidence_inline(
            trade_conf, trade_conf_reasoning, no_order=(order_type == _NO_ORDER)
        )
        if trade_conf_text:
            self._row_trade_conf.setText(trade_conf_text)
            self._row_trade_conf.parent().setVisible(True)
        else:
            self._row_trade_conf.parent().setVisible(False)

        # ── Left card: order & direction ──
        self._row_order_type.setText(str(order_type))

        if order_type == _NO_ORDER:
            self._set_action_price_highlights(None, None, visible=False)
            self._row_direction.setText("等待")
            self._set_pill_style(self._left_pill, "wait", "WAIT")
            self._row_trigger.setText(str(trigger) if trigger != "—" else "无")
            self._row_invalidation.setText(
                str(invalidation) if invalidation != "—" else "无"
            )
            self._right_title_lbl.setText("为什么等待")
            self._row_prices.parent().setVisible(False)
            self._row_rr.parent().setVisible(False)
        else:
            dir_text = str(direction)
            self._row_direction.setText(dir_text)
            if "多" in dir_text or "买" in dir_text or "long" in dir_text.lower():
                self._set_pill_style(self._left_pill, "pass", "BUY")
                self._right_title_lbl.setText("为什么做多")
            elif "空" in dir_text or "卖" in dir_text or "short" in dir_text.lower():
                self._set_pill_style(self._left_pill, "sell", "SELL")
                self._right_title_lbl.setText("为什么做空")
            else:
                self._set_pill_style(self._left_pill, "hold", "ACTIVE")
                self._right_title_lbl.setText("交易分析")
            self._row_trigger.setText(
                str(trigger) if trigger != "—" else "即时"
            )
            self._row_invalidation.setText(
                str(invalidation) if invalidation != "—" else "无"
            )
            self._row_prices.parent().setVisible(True)
            self._row_rr.parent().setVisible(True)

            entry = decision.get("entry_price")
            tp = decision.get("take_profit_price")
            sl = decision.get("stop_loss_price")
            self._set_action_price_highlights(entry, tp, visible=True)

            price_text = (
                (f"入场 {self._format_price_value(entry)}" if entry is not None else "入场 —")
                + " / "
                + (f"止盈 {self._format_price_value(tp)}" if tp is not None else "止盈 —")
                + " / "
                + (f"止损 {self._format_price_value(sl)}" if sl is not None else "止损 —")
            )
            self._row_prices.setText(price_text)

            rr = compute_risk_reward(entry, tp, sl, direction)
            if rr is not None:
                ratio = float(rr["ratio"])
                risk = float(rr["risk"])
                reward = float(rr["reward"])
                win_pct = _parse_score_100(decision.get("estimated_win_rate"))
                eq_ok = (
                    win_pct is not None
                    and passes_trader_equation(win_pct, risk, reward)
                )
                min_rr = min_risk_reward_ratio(decision_stance)
                metrics_ok = ratio >= min_rr and (
                    eq_ok if win_pct is not None else True
                )
                eq_note = " · 方程通过" if eq_ok else " · 方程不通过"
                rr_text = (
                    f"{rr['ratio_text']}（风险 {risk:.4g} / 回报 {reward:.4g}）"
                    f"{eq_note}"
                )
                rr_color = "#3fb950" if metrics_ok else "#f85149"
            else:
                rr_text = "—（三价无效）"
                rr_color = "#f85149"

            win_rate = format_estimated_win_rate(decision)
            win_rate_text = f"预估胜率 {win_rate}" if win_rate else "预估胜率 —"

            self._row_rr.setText(f"{rr_text} / {win_rate_text}")
            self._row_rr.setStyleSheet(
                f"font-size: 12px; font-weight: bold; color: {rr_color};"
                f"font-family: 'Microsoft YaHei UI', 'Segoe UI', monospace;"
            )

        # ── Left card: next bar prediction ──
        next_bar_text = self._format_next_bar_prediction(decision)
        self._row_next_bar.setText(next_bar_text)
        self._set_action_summary(
            order_type=order_type,
            direction=direction,
            reasoning=reasoning,
            trigger=trigger,
            invalidation=invalidation,
            next_bar_text=next_bar_text,
        )

        # ── Right card: reasons ──
        self._set_reasons(str(reasoning) if reasoning else "")

        # ── Right card: path items ──
        self._clear_path_items()
        paths = (
            decision.get("execution_path")
            or decision.get("path_checks")
            or []
        )
        if paths:
            for i, p in enumerate(paths, 1):
                if isinstance(p, dict):
                    text = str(p.get("description", p.get("text", str(p))))
                    status = str(p.get("status", "hold"))
                else:
                    text = str(p)
                    status = "hold"
                self._add_path_item(i, text, status)
        else:
            if order_type == _NO_ORDER:
                self._add_path_item(1, "禁止行为检查：未触发禁止条款", "pass")
                self._add_path_item(2, "信号质量：无有效入场信号", "wait")
                reason_snippet = (
                    str(reasoning)[:30] if reasoning else "等待合适时机"
                )
                self._add_path_item(
                    3, f"执行结论：{reason_snippet}", "hold"
                )
            else:
                self._add_path_item(1, "禁止行为检查：未触发禁止条款", "pass")
                self._add_path_item(2, "信号质量：发现有效入场信号", "pass")
                self._add_path_item(
                    3, f"执行结论：{order_type} {direction}", "pass"
                )

        # ── Right card: pill — always show completion status in blue ──
        self._right_pill.setText("完成")
        self._set_pill_style(self._right_pill, "hold")

    def clear(self) -> None:
        self._action_title.setText("等待分析")
        self._action_title.setStyleSheet(
            "font-size: 20px; font-weight: 800; color: #e6edf3;"
        )
        self._action_reason.setText("提交分析后，这里会优先显示可执行结论。")
        self._action_trigger.setText("等待条件：—")
        self._action_invalidation.setText("失效条件：—")
        self._action_next_bar.setText("下一根：—")
        self._set_action_price_highlights(None, None, visible=False)

        self._row_order_type.setText("—")
        self._row_direction.setText("—")
        self._row_trigger.setText("—")
        self._row_invalidation.setText("—")
        self._row_diag.setText("—")

        self._row_diag_conf.setText("—")
        self._row_diag_conf.parent().setVisible(False)

        self._row_trade_conf.setText("—")
        self._row_trade_conf.parent().setVisible(False)

        self._row_prices.setText("—")
        self._row_prices.parent().setVisible(False)

        self._row_rr.setText("—")
        self._row_rr.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #e6edf3;"
            "font-family: 'Microsoft YaHei UI', 'Segoe UI', monospace;"
        )
        self._row_rr.parent().setVisible(False)

        self._row_next_bar.setText("—")

        self._set_pill_style(self._left_pill, "wait", "WAIT")
        self._left_title_lbl.setText("决策摘要")

        self._reasons_edit.clear()
        self._clear_path_items()

        self._right_title_lbl.setText("原因与路径")
        self._right_pill.setText("STAGE 2")
        self._set_pill_style(self._right_pill, "hold")
