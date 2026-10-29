"""Decision tree panel — binary decision path replay + full tree view."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pa_agent.ai.decision_tree import (
    format_bar_basis_suffix,
    format_trace_answer,
    load_decision_tree,
    merge_traces,
    normalize_bar_range,
    plain_trace_question,
    strip_question_bar_basis_suffix,
)
from pa_agent.gui.theme import tokens as T

_OUTCOME_ZH = {
    "wait": "等待",
    "reject": "放弃",
    "trade": "交易",
    "proceed": "继续评估",
}

_ANSWER_COLOR = {
    "是": T.ACCENT_SUCCESS,
    "否": T.ACCENT_DANGER,
    "中性": T.ACCENT_WARNING,
    "等待": T.ACCENT_WARNING,
    "不适用": T.TEXT_MUTED,
}

_PHASE_ZH = {"gate": "闸门", "decision": "策略"}


def _answer_color(answer: str) -> str:
    return _ANSWER_COLOR.get(answer, T.TEXT_SECONDARY)


def _apply_dark_data_palette(widget: QTableWidget | QTreeWidget) -> None:
    """Force dark backgrounds — avoids white alternate/selection rows on Windows."""
    pal = widget.palette()
    pal.setColor(QPalette.ColorRole.Base, QColor(T.BG_BASE))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(T.BG_PANEL))
    pal.setColor(QPalette.ColorRole.Text, QColor(T.TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.Window, QColor(T.BG_BASE))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#264f78"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(T.TEXT_PRIMARY))
    widget.setPalette(pal)


class DecisionTreePanel(QWidget):
    """Shows AI walk through the binary decision tree (方案 A)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._visited_ids: set[str] = set()
        self._node_basis: dict[str, str] = {}
        self._node_answers: dict[str, str] = {}
        self._setup_ui()
        self.clear()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("二元决策树")
        title.setObjectName("toolbarTitle")
        layout.addWidget(title)
        from pa_agent.gui.analysis_details import AnalysisDetails
        self._analysis_details = AnalysisDetails()
        layout.addWidget(self._analysis_details)

        self._terminal_banner = QLabel("等待分析…")
        self._terminal_banner.setWordWrap(True)
        self._terminal_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._terminal_banner.setStyleSheet(
            f"font-size: 14px; font-weight: bold; padding: 10px;"
            f"color: {T.TEXT_SECONDARY}; background-color: {T.BG_ELEVATED};"
            f"border-radius: {T.RADIUS}px;"
        )
        layout.addWidget(self._terminal_banner)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        splitter = QSplitter(Qt.Orientation.Vertical)

        path_wrap = QWidget()
        path_layout = QVBoxLayout(path_wrap)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_title = QLabel("路径回放")
        path_title.setStyleSheet(f"font-weight: bold; color: {T.ACCENT_PRIMARY};")
        path_layout.addWidget(path_title)
        path_sub = QLabel("阶段一闸门 → 阶段二策略（悬停节点可查看完整问题）")
        path_sub.setObjectName("mutedLabel")
        path_layout.addWidget(path_sub)

        self._path_table = QTableWidget(0, 6)
        self._path_table.setObjectName("pathReplayTable")
        _apply_dark_data_palette(self._path_table)
        self._path_table.setHorizontalHeaderLabels(
            ["步", "阶段", "节点", "回答", "K线依据", "理由"]
        )
        self._path_table.verticalHeader().setVisible(False)
        self._path_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._path_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._path_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._path_table.setAlternatingRowColors(True)
        self._path_table.setWordWrap(True)
        self._path_table.setShowGrid(False)
        hdr = self._path_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._path_table.setColumnWidth(0, 28)
        self._path_table.setColumnWidth(1, 40)
        self._path_table.setColumnWidth(2, 44)
        self._path_table.setColumnWidth(3, 52)
        self._path_table.setColumnWidth(4, 72)
        path_layout.addWidget(self._path_table)
        splitter.addWidget(path_wrap)

        tree_wrap = QWidget()
        tree_layout = QVBoxLayout(tree_wrap)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_title = QLabel("完整决策树（已走过 = 高亮）")
        tree_title.setStyleSheet(f"font-weight: bold; color: {T.TEXT_SECONDARY};")
        tree_layout.addWidget(tree_title)
        self._tree = QTreeWidget()
        self._tree.setObjectName("decisionTreeView")
        _apply_dark_data_palette(self._tree)
        self._tree.setHeaderLabels(["节点", "问题", "回答", "K线依据"])
        self._tree.setColumnWidth(0, 72)
        self._tree.setColumnWidth(2, 72)
        self._tree.setColumnWidth(3, 88)
        tree_layout.addWidget(self._tree)
        splitter.addWidget(tree_wrap)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, stretch=1)

        self._gate_hint = QLabel("")
        self._gate_hint.setObjectName("mutedLabel")
        self._gate_hint.setWordWrap(True)
        layout.addWidget(self._gate_hint)

        self._path_table.itemSelectionChanged.connect(self._on_path_row_selected)

    def _on_path_row_selected(self) -> None:
        """Highlight the matching node in the full tree when a path row is selected."""
        row = self._path_table.currentRow()
        if row < 0:
            return
        node_item = self._path_table.item(row, 2)
        if node_item is None:
            return
        nid = node_item.data(Qt.ItemDataRole.UserRole)
        if not nid:
            return
        self._scroll_tree_to_node(str(nid))

    def _scroll_tree_to_node(self, node_id: str) -> None:
        """Expand tree and select ``node_id``."""
        for i in range(self._tree.topLevelItemCount()):
            sec = self._tree.topLevelItem(i)
            if sec is None:
                continue
            for j in range(sec.childCount()):
                child = sec.child(j)
                if child is None:
                    continue
                if child.data(0, Qt.ItemDataRole.UserRole) == node_id:
                    sec.setExpanded(True)
                    self._tree.setCurrentItem(child)
                    self._tree.scrollToItem(child)
                    return

    def _build_static_tree(self) -> None:
        self._tree.clear()
        data = load_decision_tree()
        for sec in data.get("sections", []):
            sec_item = QTreeWidgetItem(
                [f"§{sec['id']}", str(sec.get("title", "")), "", ""]
            )
            sec_item.setData(0, Qt.ItemDataRole.UserRole, f"sec:{sec['id']}")
            sec_font = sec_item.font(0)
            sec_font.setBold(True)
            sec_item.setFont(0, sec_font)
            sec_item.setFont(1, sec_font)

            for node in sec.get("nodes", []):
                nid = str(node.get("id", ""))
                q_display = strip_question_bar_basis_suffix(str(node.get("question", "")))
                visited = nid in self._visited_ids
                basis_col = self._node_basis.get(nid, "") if visited else ""
                answer_col = self._node_answers.get(nid, "") if visited else ""
                node_item = QTreeWidgetItem([nid, q_display, answer_col, basis_col])
                node_item.setData(0, Qt.ItemDataRole.UserRole, nid)
                if visited:
                    for col in range(4):
                        node_item.setForeground(col, QColor(T.ACCENT_PRIMARY))
                        f = node_item.font(col)
                        f.setBold(True)
                        node_item.setFont(col, f)
                    if answer_col:
                        node_item.setForeground(
                            2, QColor(_answer_color(answer_col.split("（")[0]))
                        )
                else:
                    for col in range(4):
                        node_item.setForeground(col, QColor(T.TEXT_MUTED))
                sec_item.addChild(node_item)

            self._tree.addTopLevelItem(sec_item)
            if sec.get("nodes"):
                sec_item.setExpanded(int(sec["id"]) <= 2)

    def _cell(
        self,
        text: str,
        *,
        color: str | None = None,
        tip: str = "",
        alt_row: bool = False,
    ) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        bg = QColor(T.BG_PANEL if alt_row else T.BG_BASE)
        it.setBackground(bg)
        if color:
            it.setForeground(QColor(color))
        else:
            it.setForeground(QColor(T.TEXT_PRIMARY))
        if tip:
            it.setToolTip(tip)
        return it

    def _fill_path_table(self, merged: list[dict[str, Any]]) -> None:
        self._path_table.setRowCount(len(merged))
        for row, item in enumerate(merged):
            phase = str(item.get("phase", ""))
            phase_zh = _PHASE_ZH.get(phase, phase)
            nid = str(item.get("node_id", "?"))
            question = plain_trace_question(item)
            basis = normalize_bar_range(item)
            answer = format_trace_answer(item) or str(item.get("answer", "—"))
            reason = str(item.get("reason", "") or "").strip()
            skipped = item.get("skipped")

            if skipped:
                answer_display = f"{answer}（跳过）"
            else:
                answer_display = answer
            if not format_bar_basis_suffix(item) and not skipped:
                reason_suffix = " [K线依据未标注]"
            else:
                reason_suffix = ""
            reason_display = (reason + reason_suffix) if reason or reason_suffix else "—"

            tip_lines = [question]
            if basis:
                tip_lines.append(f"K线依据：{basis}")
            if reason:
                tip_lines.append(f"理由：{reason}")
            tooltip = "\n".join(tip_lines)

            base_ans = str(answer).split("（", 1)[0]
            ans_color = _answer_color(base_ans)

            alt = row % 2 == 1
            self._path_table.setItem(row, 0, self._cell(str(row + 1), alt_row=alt))
            self._path_table.setItem(row, 1, self._cell(phase_zh, tip=tooltip, alt_row=alt))
            node_cell = self._cell(nid, tip=tooltip, alt_row=alt)
            node_cell.setData(Qt.ItemDataRole.UserRole, nid)
            self._path_table.setItem(row, 2, node_cell)
            self._path_table.setItem(
                row,
                3,
                self._cell(answer_display, color=ans_color, tip=tooltip, alt_row=alt),
            )
            self._path_table.setItem(
                row, 4, self._cell(basis or "—", tip=tooltip, alt_row=alt)
            )
            self._path_table.setItem(
                row, 5, self._cell(reason_display, tip=tooltip, alt_row=alt)
            )

        self._path_table.resizeRowsToContents()

    def set_trace(
        self,
        *,
        gate_trace: list[dict[str, Any]] | None = None,
        decision_trace: list[dict[str, Any]] | None = None,
        terminal: dict[str, Any] | None = None,
        gate_result: str | None = None,
        gate_shortcircuited: bool = False,
    ) -> None:
        """Bind Stage1 gate_trace + Stage2 decision_trace to the panel."""
        from pa_agent.ai.decision_display import SOURCE_DETAILS, compact_trace, compact_terminal
        local = any(isinstance(x, dict) and x.get("source") == "local_rules" for x in (decision_trace or []))
        self._analysis_details.set_details(SOURCE_DETAILS if local else "")
        if local:
            decision_trace = compact_trace(decision_trace)
            terminal = compact_terminal(terminal)
        merged = merge_traces(gate_trace, decision_trace)
        self._visited_ids = set()
        self._node_basis = {}
        self._node_answers = {}
        for x in merged:
            if not isinstance(x, dict):
                continue
            nid = x.get("node_id")
            if not nid:
                continue
            sid = str(nid)
            self._visited_ids.add(sid)
            br = normalize_bar_range(x)
            if br:
                self._node_basis[sid] = br
            ans = format_trace_answer(x)
            if ans:
                self._node_answers[sid] = ans
        if terminal and terminal.get("node_id"):
            self._visited_ids.add(str(terminal["node_id"]))

        self._fill_path_table(merged)

        if terminal:
            outcome = str(terminal.get("outcome", ""))
            outcome_zh = _OUTCOME_ZH.get(outcome, outcome)
            label = terminal.get("label", "")
            node_id = terminal.get("node_id", "")
            self._terminal_banner.setText(
                f"终点 · §{node_id} · {outcome_zh}\n{label}"
            )
            oc = T.ACCENT_SUCCESS if outcome == "trade" else T.ACCENT_WARNING
            if outcome in ("reject",):
                oc = T.ACCENT_DANGER
            self._terminal_banner.setStyleSheet(
                f"font-size: 14px; font-weight: bold; padding: 10px;"
                f"color: {oc}; background-color: {T.BG_ELEVATED};"
                f"border-radius: {T.RADIUS}px;"
            )
        elif gate_result in ("wait", "unknown"):
            gr_zh = "等待" if gate_result == "wait" else "未知"
            self._terminal_banner.setText(
                f"阶段一闸门：{gr_zh}（{gate_result}）\n"
                "未调用阶段二模型；下方为闸门路径。交易决策区为程序生成的「不下单」占位。"
            )
        else:
            self._terminal_banner.setText("无终点信息")

        if any(x.get("source") == "local_rules" for x in merged):
            self._gate_hint.setText("")
        elif gate_shortcircuited:
            self._gate_hint.setText(
                "阶段一闸门未通过（wait/unknown）：已跳过阶段二 API 调用；"
                "决策页「不下单」与「不可预测」为程序根据闸门结论自动生成，非模型策略评估。"
            )
        elif gate_result:
            self._gate_hint.setText(f"阶段一 gate_result：{gate_result}")
        else:
            self._gate_hint.setText("")

        self._build_static_tree()

    def clear(self) -> None:
        self._analysis_details.set_details("")
        self._visited_ids = set()
        self._node_basis = {}
        self._node_answers = {}
        self._path_table.setRowCount(0)
        self._tree.clear()
        self._terminal_banner.setText("等待分析…")
        self._terminal_banner.setStyleSheet(
            f"font-size: 14px; font-weight: bold; padding: 10px;"
            f"color: {T.TEXT_MUTED}; background-color: {T.BG_ELEVATED};"
            f"border-radius: {T.RADIUS}px;"
        )
        self._gate_hint.setText("")
        self._build_static_tree()
