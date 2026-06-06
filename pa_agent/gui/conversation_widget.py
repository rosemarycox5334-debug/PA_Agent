"""ConversationWidget — timeline summaries + lazy-loaded detail on select."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pa_agent.gui.widgets.ai_turn_card import AITurnCard, ChatBubble

if TYPE_CHECKING:
    from pa_agent.orchestrator.free_chat import FreeChatSession
    from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)

_YELLOW_PCT = 80.0
_RED_PCT = 95.0

_STYLE_NORMAL = ""
_STYLE_YELLOW = "QProgressBar#tokenProgress::chunk { background-color: #e6b800; }"
_STYLE_RED = "QProgressBar#tokenProgress::chunk { background-color: #cc0000; }"

_SUMMARY_MAX = 42


def _one_line_summary(text: str, max_len: int = _SUMMARY_MAX) -> str:
    line = text.strip().replace("\n", " ").replace("\r", "")
    line = re.sub(r"\s+", " ", line)
    if not line:
        return ""
    if len(line) > max_len:
        return line[: max_len - 1] + "…"
    return line


@dataclass
class _TurnRecord:
    """In-memory turn; detail widget created only when selected."""

    title: str
    kind: Literal["stage", "user", "chat"]
    status: Literal["streaming", "done"] = "streaming"
    system_prompt: str = ""
    user_prompt: str = ""
    show_prompt: bool = False
    reasoning: str = ""
    content: str = ""
    elapsed_s: float | None = None
    widget: AITurnCard | ChatBubble | None = field(default=None, repr=False)
    timeline_item: QListWidgetItem | None = field(default=None, repr=False)

    def timeline_summary(self) -> str:
        if self.status == "streaming":
            return f"{self.title}  ⟳"
        tail = ""
        if self.elapsed_s is not None:
            tail = f"  ·{self.elapsed_s:.0f}s"
        excerpt = _one_line_summary(self.content) or _one_line_summary(self.reasoning)
        if self.kind == "user":
            excerpt = _one_line_summary(self.content, 36)
            return f"用户: {excerpt}" if excerpt else "用户"
        if excerpt:
            return f"✓ {self.title}{tail} — {excerpt}"
        return f"✓ {self.title}{tail}"


class _ChatWorker(QThread):
    finished = pyqtSignal(str, str)
    error = pyqtSignal(str)
    reasoning_token = pyqtSignal(str)
    content_token = pyqtSignal(str)

    def __init__(
        self,
        session: "FreeChatSession",
        user_text: str,
        cancel_token: "CancelToken",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._user_text = user_text
        self._cancel_token = cancel_token

    def run(self) -> None:
        try:
            reply = self._session.send(
                self._user_text,
                self._cancel_token,
                on_reasoning_token=lambda chunk: self.reasoning_token.emit(chunk),
                on_content_token=lambda chunk: self.content_token.emit(chunk),
            )
            self.finished.emit(reply.content, reply.reasoning_content or "")
        except Exception as exc:  # noqa: BLE001
            logger.error("ChatWorker error: %s", exc, exc_info=True)
            self.error.emit(str(exc))


class ConversationWidget(QWidget):
    """Timeline summaries; only the active (streaming) turn is expanded by default."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session: Optional["FreeChatSession"] = None
        self._cancel_token: Optional["CancelToken"] = None
        self._worker: Optional[_ChatWorker] = None
        self._sending = False
        self._red_warned = False

        self._turns: list[_TurnRecord] = []
        self._selected_row: int = -1
        self._active_turn: Optional[_TurnRecord] = None
        self._stage1_turn: Optional[_TurnRecord] = None
        self._stage2_turn: Optional[_TurnRecord] = None
        self._chat_turn: Optional[_TurnRecord] = None
        self._stage1_t0: float = 0.0
        self._stage2_t0: float = 0.0

        self._setup_ui()

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        outer.addLayout(self._build_compact_token_bar())

        body = QSplitter(Qt.Orientation.Horizontal)

        self._timeline = QListWidget()
        self._timeline.setObjectName("timelineList")
        self._timeline.setMinimumWidth(148)
        self._timeline.setMaximumWidth(220)
        self._timeline.currentRowChanged.connect(self._on_timeline_selected)
        body.addWidget(self._timeline)

        self._detail_stack = QStackedWidget()
        self._detail_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._placeholder = QLabel(
            "提交分析后，仅当前进行中的阶段会在此展开；\n"
            "已完成条目在左侧显示摘要，点击可加载全文。"
        )
        self._placeholder.setObjectName("mutedLabel")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._detail_stack.addWidget(self._placeholder)
        body.addWidget(self._detail_stack)
        body.setStretchFactor(1, 1)

        outer.addWidget(body, stretch=1)
        outer.addWidget(self._build_input_area())

        self.set_input_enabled(False)

    def _build_compact_token_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("上下文")
        lbl.setObjectName("mutedLabel")
        row.addWidget(lbl)

        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("tokenProgress")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("0%")
        self._progress_bar.setFixedHeight(14)
        self._progress_bar.setMaximumWidth(280)
        row.addWidget(self._progress_bar)

        self._token_label = QLabel("—")
        self._token_label.setObjectName("mutedLabel")
        row.addWidget(self._token_label, stretch=1)
        return row

    def _build_input_area(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._input_edit = QPlainTextEdit()
        self._input_edit.setObjectName("chatInput")
        self._input_edit.setPlaceholderText("分析完成后可继续追问…")
        self._input_edit.setMaximumHeight(72)
        layout.addWidget(self._input_edit, stretch=1)

        self._send_btn = QPushButton("发送")
        self._send_btn.setObjectName("primaryButton")
        self._send_btn.setMinimumWidth(72)
        self._send_btn.clicked.connect(self._on_send_or_stop)
        layout.addWidget(self._send_btn)
        return container

    def _turn_index(self, record: _TurnRecord) -> int:
        return self._turns.index(record)

    def _refresh_timeline_text(self, record: _TurnRecord) -> None:
        if record.timeline_item is not None:
            summary = record.timeline_summary()
            record.timeline_item.setText(summary)
            record.timeline_item.setToolTip(
                f"{record.title}\n{summary}\n（点击加载全文）"
            )

    def _register_turn(self, record: _TurnRecord) -> int:
        """Append to timeline only (collapsed until activated or clicked)."""
        self._turns.append(record)
        item = QListWidgetItem(record.timeline_summary())
        item.setToolTip(f"{record.title}\n（点击加载全文）")
        record.timeline_item = item
        self._timeline.addItem(item)
        return len(self._turns) - 1

    def _collapse_all_turns(self) -> None:
        for turn in self._turns:
            self._unmount_turn(turn)

    def _show_collapsed_placeholder(self, record: _TurnRecord | None = None) -> None:
        self._clear_detail_slot()
        if record is not None:
            self._placeholder.setText(
                f"「{record.title}」已完成，内容已折叠。\n\n"
                f"左侧摘要：{record.timeline_summary()}\n\n"
                "点击左侧该条目可重新加载全文。"
            )
        else:
            self._placeholder.setText(
                "仅当前进行中的阶段会在此自动展开；\n"
                "历史条目请从左侧点击加载。"
            )
        self._detail_stack.setCurrentIndex(0)

    def _activate_turn(self, record: _TurnRecord) -> None:
        """Collapse all history; expand only this turn (current stage / chat)."""
        self._collapse_all_turns()
        self._active_turn = record
        idx = self._turn_index(record)
        self._timeline.blockSignals(True)
        self._timeline.setCurrentRow(idx)
        self._timeline.blockSignals(False)
        self._selected_row = idx
        self._mount_turn(record)

    def _clear_detail_slot(self) -> None:
        while self._detail_stack.count() > 1:
            w = self._detail_stack.widget(1)
            self._detail_stack.removeWidget(w)
            if w is not None:
                w.deleteLater()

    def _unmount_turn(self, record: _TurnRecord) -> None:
        if record.widget is None:
            return
        if self._detail_stack.indexOf(record.widget) >= 0:
            self._detail_stack.removeWidget(record.widget)
        record.widget.deleteLater()
        record.widget = None
        if self._detail_stack.count() == 1:
            self._detail_stack.setCurrentIndex(0)

    def _build_widget_for_turn(self, record: _TurnRecord) -> AITurnCard | ChatBubble:
        if record.kind == "user":
            return ChatBubble("user", record.content)

        card = AITurnCard(
            record.title,
            system_prompt=record.system_prompt,
            user_prompt=record.user_prompt,
            show_prompt=record.show_prompt,
        )
        if record.reasoning:
            card.set_reasoning(record.reasoning)
        if record.content:
            card.set_content(record.content)
        if record.status == "streaming":
            card.set_streaming(True)
        else:
            card.mark_done(record.elapsed_s)
        return card

    def _mount_turn(self, record: _TurnRecord) -> None:
        if record.widget is None:
            record.widget = self._build_widget_for_turn(record)
            self._clear_detail_slot()
            self._detail_stack.addWidget(record.widget)
        self._detail_stack.setCurrentWidget(record.widget)

    def _open_turn(self, row: int, *, user_initiated: bool = False) -> None:
        if row < 0 or row >= len(self._turns):
            self._selected_row = -1
            self._show_collapsed_placeholder()
            return

        if self._selected_row >= 0 and self._selected_row < len(self._turns):
            self._unmount_turn(self._turns[self._selected_row])

        self._selected_row = row
        record = self._turns[row]

        if record is self._active_turn:
            self._mount_turn(record)
        elif user_initiated:
            self._mount_turn(record)
        else:
            self._show_collapsed_placeholder(record)

    def _on_timeline_selected(self, row: int) -> None:
        if row == self._selected_row:
            return
        self._open_turn(row, user_initiated=True)

    def _complete_turn(self, record: _TurnRecord, elapsed_s: float | None = None) -> None:
        record.status = "done"
        record.elapsed_s = elapsed_s
        self._refresh_timeline_text(record)
        if record.widget is not None and isinstance(record.widget, AITurnCard):
            record.widget.mark_done(elapsed_s)
        self._unmount_turn(record)
        if record is self._active_turn:
            self._active_turn = None
        if self._selected_row == self._turn_index(record):
            self._show_collapsed_placeholder(record)

    def on_stage_prompt_ready(self, stage: str, system: str, user: str) -> None:
        title = "阶段一 · 诊断" if stage == "stage1" else "阶段二 · 决策"
        record = _TurnRecord(
            title=title,
            kind="stage",
            status="streaming",
            system_prompt=system,
            user_prompt=user,
            show_prompt=True,
        )
        self._register_turn(record)
        self._activate_turn(record)

        if stage == "stage1":
            self._stage1_turn = record
            self._stage1_t0 = time.monotonic()
        else:
            self._stage2_turn = record
            self._stage2_t0 = time.monotonic()

    def _append_to_turn(
        self,
        record: _TurnRecord | None,
        *,
        reasoning: str = "",
        content: str = "",
    ) -> None:
        if record is None:
            return
        if reasoning:
            record.reasoning += reasoning
        if content:
            record.content += content
        if record.status == "streaming":
            self._refresh_timeline_text(record)
        if record is not self._active_turn:
            return
        idx = self._turn_index(record)
        if self._selected_row != idx:
            return
        if record.widget is None:
            self._mount_turn(record)
        elif isinstance(record.widget, AITurnCard):
            if reasoning:
                record.widget.append_reasoning(reasoning)
            if content:
                record.widget.append_content(content)

    def on_reasoning_token(self, stage: str, chunk: str) -> None:
        record = self._stage1_turn if stage == "stage1" else self._stage2_turn
        self._append_to_turn(record, reasoning=chunk)

    def on_content_token(self, stage: str, chunk: str) -> None:
        record = self._stage1_turn if stage == "stage1" else self._stage2_turn
        self._append_to_turn(record, content=chunk)

    def finalize_stage(self, stage: str) -> None:
        record = self._stage1_turn if stage == "stage1" else self._stage2_turn
        if record is None:
            return
        t0 = self._stage1_t0 if stage == "stage1" else self._stage2_t0
        self._complete_turn(record, time.monotonic() - t0)

    def show_stage_result(self, stage: str, content: str, reasoning: str) -> None:
        record = self._stage1_turn if "一" in stage or "1" in stage else self._stage2_turn
        if record is None:
            record = _TurnRecord(
                title=stage[:12],
                kind="stage",
                status="done",
                reasoning=reasoning,
                content=content,
            )
            self._register_turn(record)
            self._refresh_timeline_text(record)
            return
        if reasoning and not record.reasoning:
            record.reasoning = reasoning
        if content and not record.content:
            record.content = content
        if record.status != "done":
            t0 = self._stage1_t0 if record is self._stage1_turn else self._stage2_t0
            self._complete_turn(record, time.monotonic() - t0)
        else:
            self._refresh_timeline_text(record)

    def append_message(self, role: str, content: str, reasoning: str = "") -> None:
        if role == "user":
            record = _TurnRecord(
                title="用户",
                kind="user",
                status="done",
                content=content,
            )
            self._register_turn(record)
            self._refresh_timeline_text(record)
            return

        record = _TurnRecord(
            title="AI 回复",
            kind="chat",
            status="done",
            reasoning=reasoning,
            content=content,
        )
        self._register_turn(record)
        self._complete_turn(record)

    def set_input_enabled(self, enabled: bool) -> None:
        self._input_edit.setEnabled(enabled)
        self._send_btn.setEnabled(enabled)

    def update_token_display(self, data: dict) -> None:
        context_used = data.get("context_used", 0)
        context_window = data.get("context_window", 1_000_000)
        total_input = data.get("total_input", 0)
        total_output = data.get("total_output", 0)

        pct = (context_used / context_window * 100.0) if context_window > 0 else 0.0
        pct_int = min(100, int(pct))

        self._progress_bar.setValue(pct_int)
        self._progress_bar.setFormat(f"{pct:.1f}%")

        if pct >= _RED_PCT:
            self._progress_bar.setStyleSheet(_STYLE_RED)
            if not self._red_warned:
                self._red_warned = True
                QMessageBox.warning(
                    self,
                    "上下文用量警告",
                    f"上下文用量已达 {pct:.1f}%，接近上限，建议开启新会话。",
                )
        elif pct >= _YELLOW_PCT:
            self._progress_bar.setStyleSheet(_STYLE_YELLOW)
        else:
            self._progress_bar.setStyleSheet(_STYLE_NORMAL)

        total_tokens = total_input + total_output
        self._token_label.setText(
            f"{total_tokens:,} tokens · in {total_input:,} / out {total_output:,}"
        )

    def clear(self) -> None:
        for record in self._turns:
            if record.widget is not None:
                record.widget.deleteLater()
                record.widget = None

        self._turns.clear()
        self._timeline.clear()
        self._selected_row = -1
        self._active_turn = None
        self._stage1_turn = None
        self._stage2_turn = None
        self._chat_turn = None
        self._red_warned = False

        self._show_collapsed_placeholder()

        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("0%")
        self._progress_bar.setStyleSheet(_STYLE_NORMAL)
        self._token_label.setText("—")

    def on_record_saved(self) -> None:
        self.set_input_enabled(True)

    def on_analysis_started(self) -> None:
        self.set_input_enabled(False)
        self._session = None
        self._cancel_token = None

    def set_session(
        self,
        session: "FreeChatSession",
        cancel_token: "CancelToken",
    ) -> None:
        self._session = session
        self._cancel_token = cancel_token

    def _on_send_or_stop(self) -> None:
        if self._sending:
            self._on_stop()
        else:
            self._on_send()

    def _on_send(self) -> None:
        if self._session is None:
            return
        text = self._input_edit.toPlainText().strip()
        if not text:
            return

        from pa_agent.util.threading import CancelToken

        self._cancel_token = CancelToken()

        user_rec = _TurnRecord(title="用户", kind="user", status="done", content=text)
        self._register_turn(user_rec)
        self._refresh_timeline_text(user_rec)
        self._input_edit.clear()

        chat_rec = _TurnRecord(title="追问", kind="chat", status="streaming")
        self._chat_turn = chat_rec
        self._register_turn(chat_rec)
        self._activate_turn(chat_rec)

        self._sending = True
        self._send_btn.setText("停止")
        self._send_btn.setObjectName("dangerButton")
        self._send_btn.style().unpolish(self._send_btn)
        self._send_btn.style().polish(self._send_btn)
        self._input_edit.setEnabled(False)

        self._worker = _ChatWorker(self._session, text, self._cancel_token, parent=self)
        self._worker.reasoning_token.connect(
            lambda c: self._append_to_turn(self._chat_turn, reasoning=c)
        )
        self._worker.content_token.connect(
            lambda c: self._append_to_turn(self._chat_turn, content=c)
        )
        self._worker.finished.connect(self._on_reply_received)
        self._worker.error.connect(self._on_reply_error)
        self._worker.finished.connect(lambda *_: self._on_worker_done())
        self._worker.error.connect(lambda *_: self._on_worker_done())
        self._worker.start()

    def _on_stop(self) -> None:
        if self._cancel_token is not None:
            self._cancel_token.set()

    def _on_reply_received(self, content: str, reasoning: str) -> None:
        rec = self._chat_turn
        if rec is not None:
            if reasoning and not rec.reasoning:
                rec.reasoning = reasoning
            if content and not rec.content:
                rec.content = content
            self._complete_turn(rec)
        if self._session is not None:
            ledger = getattr(self._session, "_ledger", None)
            if ledger is not None and hasattr(ledger, "breakdown"):
                breakdown = ledger.breakdown()
                if breakdown:
                    self.update_token_display(breakdown)

    def _on_reply_error(self, error_msg: str) -> None:
        rec = self._chat_turn
        if rec is not None:
            rec.content += f"\n[错误] {error_msg}"
            self._refresh_timeline_text(rec)
            self._complete_turn(rec)
        else:
            self.append_message("assistant", f"[错误] {error_msg}")

    def _on_worker_done(self) -> None:
        self._sending = False
        self._send_btn.setText("发送")
        self._send_btn.setObjectName("primaryButton")
        self._send_btn.style().unpolish(self._send_btn)
        self._send_btn.style().polish(self._send_btn)
        self._input_edit.setEnabled(True)
        self._worker = None
        self._chat_turn = None
