"""FutureTrendPanel — 未来走势预期页.

Hosts two prediction modules:
  1. 下一根K线预期 (migrated from DecisionPanel)
  2. 下一个市场周期预期 (new, AI-generated next_cycle_prediction)
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pa_agent.ai.cycle_enums import (
    CYCLE_ORDER,
    CYCLE_POSITION_ZH,
    format_cycle_with_direction,
)
from pa_agent.gui.prediction_format import (
    _PREDICTION_DOMINANT_COLOR,
    _PREDICTION_UNPREDICTABLE_COLOR,
    _PREDICTION_UNPREDICTABLE_LABEL,
    _dominant_prediction_direction,
    _format_prediction_probs_line,
)

_REASON_EDIT_CSS = (
    "font-size: 16px; color: #e6edf3; line-height: 1.45;"
    "font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;"
)

_DIRECTION_ZH: dict[str, str] = {
    "bullish": "看涨",
    "bearish": "看跌",
    "neutral": "中性",
}

_CHIP_BASE_CSS = (
    "font-size: 14px; font-weight: bold; padding: 8px 10px;"
    "background-color: #21262d; border-radius: 8px;"
)


def _chip_style(color: str) -> str:
    return f"{_CHIP_BASE_CSS} color: {color};"


class FutureTrendPanel(QWidget):
    """Renders next-bar and next-cycle prediction modules."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel("未来走势预期")
        title.setObjectName("toolbarTitle")
        layout.addWidget(title)

        disclaimer = QLabel("预测仅供参考，不构成投资建议")
        disclaimer.setObjectName("mutedLabel")
        disclaimer.setWordWrap(True)
        layout.addWidget(disclaimer)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

        # ── Module 1: 下一根K线预期 ───────────────────────────────────────────
        self._bar_group = QFrame()
        self._bar_group.setObjectName("predictionGroup")
        bar_layout = QVBoxLayout(self._bar_group)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(6)

        self._bar_title = QLabel("下一根K线预期")
        self._bar_title.setStyleSheet("font-weight: bold; color: #79c0ff;")
        bar_layout.addWidget(self._bar_title)

        self._bar_direction_label = QLabel("—")
        self._bar_direction_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bar_direction_label.setWordWrap(True)
        self._bar_direction_label.setStyleSheet(
            "font-size: 16px; font-weight: bold; padding: 8px;"
            "background-color: #21262d; border-radius: 6px; color: #8b949e;"
        )
        bar_layout.addWidget(self._bar_direction_label)

        self._bar_reasoning_edit = QTextEdit()
        self._bar_reasoning_edit.setReadOnly(True)
        self._bar_reasoning_edit.setObjectName("answerPane")
        self._bar_reasoning_edit.setStyleSheet(_REASON_EDIT_CSS)
        self._bar_reasoning_edit.setMinimumHeight(80)
        self._bar_reasoning_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        bar_layout.addWidget(self._bar_reasoning_edit, stretch=1)

        self._bar_group.setVisible(False)
        layout.addWidget(self._bar_group, stretch=1)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep2)

        # ── Module 2: 下一个市场周期预期 ─────────────────────────────────────
        self._cycle_group = QFrame()
        self._cycle_group.setObjectName("cyclePredictionGroup")
        cycle_layout = QVBoxLayout(self._cycle_group)
        cycle_layout.setContentsMargins(0, 0, 0, 0)
        cycle_layout.setSpacing(6)

        self._cycle_title = QLabel("下一个市场周期预期")
        self._cycle_title.setStyleSheet("font-weight: bold; color: #79c0ff;")
        cycle_layout.addWidget(self._cycle_title)

        # 3 chips side by side (top-3 cycles by probability)
        self._top3_row = QWidget()
        top3_layout = QHBoxLayout(self._top3_row)
        top3_layout.setContentsMargins(0, 0, 0, 0)
        top3_layout.setSpacing(8)

        self._chip_labels: list[QLabel] = []
        for _ in range(3):
            lbl = QLabel("—")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setWordWrap(True)
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            lbl.setStyleSheet(_chip_style("#8b949e"))
            top3_layout.addWidget(lbl, stretch=1)
            self._chip_labels.append(lbl)

        cycle_layout.addWidget(self._top3_row)

        self._cycle_direction_label = QLabel("—")
        self._cycle_direction_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cycle_direction_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8b949e;"
        )
        cycle_layout.addWidget(self._cycle_direction_label)

        # Remaining 5 cycles label
        self._cycle_probs_label = QLabel("—")
        self._cycle_probs_label.setWordWrap(True)
        self._cycle_probs_label.setStyleSheet(
            "font-size: 13px; color: #c9d1d9; padding: 6px;"
            "background-color: #161b22; border-radius: 6px;"
        )
        cycle_layout.addWidget(self._cycle_probs_label)

        from pa_agent.gui.analysis_details import AnalysisDetails
        self._cycle_details = AnalysisDetails()
        self._cycle_probability_notice = self._cycle_details.body
        cycle_layout.addWidget(self._cycle_details)

        self._cycle_reasoning_edit = QTextEdit()
        self._cycle_reasoning_edit.setReadOnly(True)
        self._cycle_reasoning_edit.setObjectName("answerPane")
        self._cycle_reasoning_edit.setStyleSheet(_REASON_EDIT_CSS)
        self._cycle_reasoning_edit.setMinimumHeight(100)
        self._cycle_reasoning_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        cycle_layout.addWidget(self._cycle_reasoning_edit, stretch=1)

        self._cycle_group.setVisible(False)
        layout.addWidget(self._cycle_group, stretch=2)

    # ── Module 1: next_bar_prediction ────────────────────────────────────────

    def _apply_next_bar_prediction(self, decision: dict) -> None:
        """Render 下一根K线预期 module. Hides on missing/invalid data."""
        pred = decision.get("next_bar_prediction")
        if not isinstance(pred, dict):
            self._bar_group.setVisible(False)
            self._bar_direction_label.setText("—")
            self._bar_reasoning_edit.clear()
            return

        unpredictable = bool(pred.get("unpredictable", False))
        if unpredictable:
            line = _PREDICTION_UNPREDICTABLE_LABEL
            color = _PREDICTION_UNPREDICTABLE_COLOR
        else:
            probs = pred.get("probabilities")
            if isinstance(probs, dict):
                line = _format_prediction_probs_line(probs)
                dominant = _dominant_prediction_direction(probs)
                color = _PREDICTION_DOMINANT_COLOR.get(dominant, _PREDICTION_UNPREDICTABLE_COLOR)
            elif decision.get("trace_source") == "local_rules":
                direction = str(pred.get("direction") or "")
                line = f"方向：{_DIRECTION_ZH.get(direction, '—')}"
                color = _PREDICTION_DOMINANT_COLOR.get(direction, _PREDICTION_UNPREDICTABLE_COLOR)
            else:
                line = "—"
                color = _PREDICTION_UNPREDICTABLE_COLOR

        self._bar_direction_label.setText(line)
        self._bar_direction_label.setStyleSheet(
            f"font-size: 16px; font-weight: bold; padding: 8px;"
            f"background-color: #21262d; border-radius: 6px; color: {color};"
        )

        reasoning = str(pred.get("reasoning", "")).strip()
        if "程序根据阶段二诊断摘要补全" in reasoning or "程序参考分布" in reasoning:
            prefix = "【程序补全】模型未输出 next_bar_prediction，以下为参考预测。\n\n"
            if not reasoning.startswith("【程序补全】"):
                reasoning = prefix + reasoning
        self._bar_reasoning_edit.setPlainText(reasoning)
        self._bar_group.setVisible(True)

    # ── Module 2: next_cycle_prediction ──────────────────────────────────────

    def _reset_chips(self) -> None:
        for lbl in self._chip_labels:
            lbl.setText("—")
            lbl.setStyleSheet(_chip_style("#8b949e"))

    def _apply_next_cycle_prediction(self, decision: dict) -> None:
        """Render 下一个市场周期预期 module. Hides on missing/invalid data."""
        pred = decision.get("next_cycle_prediction")
        if (isinstance(pred, dict) and decision.get("trace_source") == "local_rules"
                and not isinstance(pred.get("probabilities"), dict)):
            # Old saved lite records have no probability block. Display a
            # clearly marked prior without mutating their persisted contents.
            from pa_agent.ai.local_cycle_forecast import fill_cycle_probabilities
            pred = fill_cycle_probabilities(pred, stage1_json=decision.get("diagnosis_summary"))
        program_probs = isinstance(pred, dict) and pred.get("probability_source") == "program_rules"
        self._cycle_details.set_details(
            ("概率来源：程序规则估算（未经统计校准，非交易胜率）。" + str(pred.get("probability_basis", "")))
            if program_probs else ""
        )
        if not isinstance(pred, dict):
            self._cycle_group.setVisible(False)
            self._reset_chips()
            self._cycle_direction_label.setText("—")
            self._cycle_probs_label.setText("—")
            self._cycle_reasoning_edit.clear()
            return

        unpredictable = bool(pred.get("unpredictable", False))
        if unpredictable and not program_probs:
            self._reset_chips()
            self._chip_labels[0].setText(_PREDICTION_UNPREDICTABLE_LABEL)
            self._chip_labels[0].setStyleSheet(_chip_style(_PREDICTION_UNPREDICTABLE_COLOR))
            self._chip_labels[1].setVisible(False)
            self._chip_labels[2].setVisible(False)
            self._cycle_direction_label.setVisible(False)
            self._cycle_probs_label.setVisible(False)
            reasoning = str(pred.get("reasoning", "")).strip()
            self._cycle_reasoning_edit.setPlainText(reasoning)
            self._cycle_group.setVisible(True)
            return

        # Restore chip visibility
        for lbl in self._chip_labels:
            lbl.setVisible(True)

        direction = pred.get("direction")
        dir_key = str(direction or "").strip().lower()
        if dir_key == "bullish":
            cycle_color = "#3fb950"
        elif dir_key == "bearish":
            cycle_color = "#f85149"
        else:
            cycle_color = "#e6b800"

        direction_zh = _DIRECTION_ZH.get(dir_key, str(direction or "—"))

        # Sort all 8 cycles by probability descending
        probs = pred.get("probabilities")
        sorted_probs: list[tuple[str, int]] = []
        if isinstance(probs, dict):
            for key in CYCLE_ORDER:
                try:
                    pct = int(probs.get(key, 0) or 0)
                except (TypeError, ValueError):
                    pct = 0
                sorted_probs.append((key, pct))
            sorted_probs.sort(key=lambda x: x[1], reverse=True)

        # ── Top-3 chips, side by side ──
        top3 = sorted_probs[:3] if sorted_probs else []
        for i, lbl in enumerate(self._chip_labels):
            if i < len(top3):
                key, pct = top3[i]
                zh = format_cycle_with_direction(key, direction)
                lbl.setText(f"{zh}的概率为{pct}%")
                lbl.setStyleSheet(_chip_style(cycle_color))
            else:
                lbl.setText("—")
                lbl.setStyleSheet(_chip_style("#8b949e"))

        if decision.get("trace_source") == "local_rules" and not sorted_probs:
            self._chip_labels[0].setText("简化走势预期（未提供周期概率）")
            self._chip_labels[1].setVisible(False)
            self._chip_labels[2].setVisible(False)

        self._cycle_direction_label.setText(f"方向：{direction_zh}")
        self._cycle_direction_label.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {cycle_color};"
        )
        self._cycle_direction_label.setVisible(True)

        # ── Remaining 5, sorted by probability ──
        rest = sorted_probs[3:] if len(sorted_probs) > 3 else []
        if rest:
            rest_parts = [f"{CYCLE_POSITION_ZH.get(k, k)} {p}%" for k, p in rest]
            self._cycle_probs_label.setText("  |  ".join(rest_parts))
            self._cycle_probs_label.setVisible(True)
        else:
            self._cycle_probs_label.setVisible(False)

        reasoning = str(pred.get("reasoning", "")).strip()
        if "程序根据阶段二诊断摘要补全" in reasoning or "程序参考分布" in reasoning:
            prefix = "【程序补全】模型未输出 next_cycle_prediction，以下为参考预测。\n\n"
            if not reasoning.startswith("【程序补全】"):
                reasoning = prefix + reasoning
        self._cycle_reasoning_edit.setPlainText(reasoning)
        self._cycle_group.setVisible(True)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_prediction(self, decision: dict) -> None:
        """Render both prediction modules from the decision dict."""
        self._apply_next_bar_prediction(decision)
        self._apply_next_cycle_prediction(decision)

    def clear(self) -> None:
        """Reset both modules to initial empty state and hide them."""
        self._bar_group.setVisible(False)
        self._bar_direction_label.setText("—")
        self._bar_direction_label.setStyleSheet(
            "font-size: 16px; font-weight: bold; padding: 8px;"
            "background-color: #21262d; border-radius: 6px; color: #8b949e;"
        )
        self._bar_reasoning_edit.clear()

        self._cycle_group.setVisible(False)
        self._cycle_details.set_details("")
        self._reset_chips()
        for lbl in self._chip_labels:
            lbl.setVisible(True)
        self._cycle_direction_label.setText("—")
        self._cycle_direction_label.setVisible(True)
        self._cycle_probs_label.setText("—")
        self._cycle_probs_label.setVisible(True)
        self._cycle_reasoning_edit.clear()
