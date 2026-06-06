"""Live AI stream panel: reasoning stream, or content stream when API has no reasoning."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import QThread, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from pa_agent.config.settings import Settings
    from pa_agent.orchestrator.free_chat import FreeChatSession
    from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)

_YELLOW_PCT = 80.0
_RED_PCT = 95.0
_STYLE_NORMAL = ""
_STYLE_YELLOW = "QProgressBar#tokenProgress::chunk { background-color: #e6b800; }"
_STYLE_RED = "QProgressBar#tokenProgress::chunk { background-color: #cc0000; }"


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
                on_reasoning_token=lambda c: self.reasoning_token.emit(c),
                on_content_token=lambda c: self.content_token.emit(c),
            )
            self.finished.emit(reply.content, reply.reasoning_content or "")
        except Exception as exc:  # noqa: BLE001
            logger.error("ChatWorker error: %s", exc, exc_info=True)
            self.error.emit(str(exc))


class AIStreamPanel(QWidget):
    """Live stream viewer: reasoning when present, else streamed answer text."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session: Optional["FreeChatSession"] = None
        self._cancel_token: Optional["CancelToken"] = None
        self._worker: Optional[_ChatWorker] = None
        self._sending = False
        self._red_warned = False
        self._settings: Optional["Settings"] = None

        self._stage: str = ""
        self._reasoning_chars = 0
        self._content_chars = 0
        self._stage_t0 = 0.0
        self._finalized_stages: set[str] = set()
        # Per-stage streamed char counts; text in the pane is never cleared between stages.
        self._stage_chars: dict[str, dict[str, int]] = {}
        self._stage_headers_written: set[str] = set()
        self._content_headers_written: set[str] = set()

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self._phase_label = QLabel("等待分析…")
        self._phase_label.setObjectName("stageHeader")
        layout.addWidget(self._phase_label)

        self._mode_label = QLabel("")
        self._mode_label.setObjectName("mutedLabel")
        layout.addWidget(self._mode_label)

        rl = QLabel("🧠 思考过程 / 撰写回答")
        rl.setStyleSheet("color: #a371f7; font-weight: bold;")
        layout.addWidget(rl)

        self._reasoning_edit = QPlainTextEdit()
        self._reasoning_edit.setObjectName("reasoningPane")
        self._reasoning_edit.setReadOnly(True)
        self._reasoning_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self._reasoning_edit, stretch=1)

        self._stats_label = QLabel("思考 0 字")
        self._stats_label.setObjectName("mutedLabel")
        layout.addWidget(self._stats_label)

        self._disclaimer_label = QLabel("分析仅供参考，不构成投资建议")
        self._disclaimer_label.setObjectName("mutedLabel")
        self._disclaimer_label.setWordWrap(True)
        layout.addWidget(self._disclaimer_label)

        layout.addLayout(self._build_token_bar())
        layout.addWidget(self._build_input_area())

        self._apply_stream_font()
        self.set_input_enabled(False)

    @staticmethod
    def _mono_font(point_size: int) -> QFont:
        font = QFont("Cascadia Mono", point_size)
        if not font.exactMatch():
            font = QFont("Consolas", point_size)
        return font

    def _apply_stream_font(self) -> None:
        point_size = 11
        if self._settings is not None:
            point_size = int(getattr(self._settings.general, "stream_pane_font_pt", 11) or 11)
        font = self._mono_font(point_size)
        self._reasoning_edit.setFont(font)
        self._input_edit.setFont(font)

    def _build_token_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("上下文"))
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("tokenProgress")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setFixedHeight(14)
        self._progress_bar.setMaximumWidth(320)
        row.addWidget(self._progress_bar)
        self._token_label = QLabel("—")
        self._token_label.setObjectName("mutedLabel")
        row.addWidget(self._token_label, stretch=1)
        return row

    def _build_input_area(self) -> QWidget:
        box = QWidget()
        box.setStyleSheet("""
            QWidget {
                background: #161b22;
                border-top: 1px solid #30363d;
            }
        """)
        row = QHBoxLayout(box)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(8)

        self._input_edit = QLineEdit()
        self._input_edit.setObjectName("chatInput")
        self._input_edit.setPlaceholderText("分析完成后可继续追问…")
        self._input_edit.setFixedHeight(44)
        self._input_edit.setStyleSheet("""
            QLineEdit {
                border: 1px solid #30363d;
                border-radius: 6px;
                background: #0a0e14;
                color: #e6edf3;
                padding: 0 14px;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #38bdf8;
            }
            QLineEdit::placeholder {
                color: #6e7681;
            }
        """)
        self._input_edit.returnPressed.connect(self._on_send_or_stop)
        row.addWidget(self._input_edit, stretch=1)

        button_col = QVBoxLayout()
        button_col.setSpacing(6)
        self._send_btn = QPushButton("发送")
        self._send_btn.setStyleSheet("""
            QPushButton {
                background: #15803d;
                color: #fff;
                border: 1px solid #16a34a;
                min-width: 80px;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QPushButton:disabled {
                background: #1f2937;
                color: #6b7280;
                border-color: #374151;
            }
        """)
        self._send_btn.clicked.connect(self._on_send_or_stop)
        button_col.addWidget(self._send_btn)

        self._clear_output_btn = QPushButton("清空")
        self._clear_output_btn.setMinimumWidth(72)
        self._clear_output_btn.setToolTip("清空上方“思考过程”窗口内容，不影响当前会话")
        self._clear_output_btn.clicked.connect(self.clear_stream_output)
        button_col.addWidget(self._clear_output_btn)
        button_col.addStretch()
        row.addLayout(button_col)
        return box

    def bind_settings(self, settings: Optional["Settings"]) -> None:
        self._settings = settings
        self._apply_stream_font()
        self._refresh_mode_label()

    def _refresh_mode_label(self) -> None:
        if self._settings is None:
            self._mode_label.setText("")
            return
        p = self._settings.provider
        base = (p.base_url or "").lower()
        if "deepseek.com" in base:
            thinking = "enabled" if p.thinking else "disabled"
            self._mode_label.setText(
                f"API: thinking={thinking} · reasoning_effort={p.reasoning_effort} · {p.model}"
            )
        elif "kkone.vip" in base:
            thinking = "开" if p.thinking else "关"
            self._mode_label.setText(
                f"KKAI: 思考={thinking} · budget≈"
                f"{p.reasoning_effort if p.thinking else '—'} · {p.model} "
                f"(部分线路不回传 reasoning_content)"
            )
        elif "yunwu.ai" in base:
            thinking = "开" if p.thinking else "关"
            self._mode_label.setText(
                f"云雾: 思考={thinking} · effort="
                f"{p.reasoning_effort if p.thinking else '—'} · {p.model}"
            )
        elif "packyapi.com" in base:
            thinking = "开" if p.thinking else "关"
            self._mode_label.setText(
                f"PackyAPI: 思考={thinking} · effort="
                f"{p.reasoning_effort if p.thinking else '—'} · {p.model}"
            )
        else:
            self._mode_label.setText(
                f"API: {p.model} · 思考={('开' if p.thinking else '关')}"
            )

    def _update_stats(self) -> None:
        labels = {"stage1": "阶段一", "stage2": "阶段二", "chat": "追问"}
        parts: list[str] = []
        for sid, label in labels.items():
            counts = self._stage_chars.get(sid)
            if not counts:
                continue
            reasoning_n = counts.get("reasoning", 0)
            content_n = counts.get("content", 0)
            if reasoning_n and content_n:
                parts.append(f"{label} 思考{reasoning_n:,}+回答{content_n:,}字")
            elif reasoning_n:
                parts.append(f"{label} 思考{reasoning_n:,}字")
            elif content_n:
                parts.append(f"{label} 回答{content_n:,}字")
        if parts:
            self._stats_label.setText(" · ".join(parts))
        else:
            self._stats_label.setText("思考 0 字")

    def _stream_phase_suffix(self) -> str:
        counts = self._stage_chars.get(self._stage, {})
        reasoning_n = counts.get("reasoning", 0)
        content_n = counts.get("content", 0)
        if reasoning_n > 0 and content_n > 0:
            return "思考中 · 撰写回答中…"
        if reasoning_n > 0:
            return "思考中…"
        if content_n > 0:
            return "撰写回答中…"
        return "等待响应…"

    def _ensure_stage_header(self, stage: str) -> None:
        if stage in self._stage_headers_written:
            return
        self._stage_headers_written.add(stage)
        if stage in ("stage1", "stage2"):
            title = self._stage_title(stage)
        else:
            title = "追问"
        prefix = ""
        if self._reasoning_edit.toPlainText():
            prefix = "\n" + "─" * 48 + "\n"
        self._reasoning_edit.appendPlainText(f"{prefix}【{title}】\n")

    def _ensure_content_header(self, stage: str) -> None:
        if stage in self._content_headers_written:
            return
        self._content_headers_written.add(stage)
        prefix = "\n" + "─" * 48 + "\n" if self._reasoning_edit.toPlainText() else ""
        label = "撰写回答" if stage in ("stage1", "stage2") else "回答"
        self._reasoning_edit.appendPlainText(f"{prefix}【{label}】\n")

    def _append_stream_text_for_stage(self, stage: str, chunk: str, *, kind: str) -> None:
        if not chunk:
            return
        self._ensure_stage_header(stage)
        if kind == "content":
            self._ensure_content_header(stage)
        counts = self._stage_chars.setdefault(stage, {"reasoning": 0, "content": 0})
        key = "reasoning" if kind == "reasoning" else "content"
        counts[key] += len(chunk)
        if stage == self._stage:
            if kind == "reasoning":
                self._reasoning_chars += len(chunk)
            else:
                self._content_chars += len(chunk)
        self._reasoning_edit.moveCursor(QTextCursor.MoveOperation.End)
        self._reasoning_edit.insertPlainText(chunk)
        self._reasoning_edit.moveCursor(QTextCursor.MoveOperation.End)
        self._update_stats()
        if stage == self._stage:
            title = self._stage_title(self._stage) if self._stage in ("stage1", "stage2") else "追问"
            self._phase_label.setText(f"▶ {title} — {self._stream_phase_suffix()}")

    def _append_reasoning(self, chunk: str) -> None:
        stage = self._stage or "chat"
        self._append_stream_text_for_stage(stage, chunk, kind="reasoning")

    def _append_user_message(self, text: str) -> None:
        """Append follow-up user text in red in the reasoning pane."""
        from pa_agent.gui.theme.tokens import ACCENT_DANGER

        cursor = self._reasoning_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        normal = QTextCharFormat()
        user_fmt = QTextCharFormat()
        user_fmt.setForeground(QColor(ACCENT_DANGER))

        cursor.insertText("\n【用户】\n", normal)
        cursor.insertText(text, user_fmt)
        cursor.insertText("\n", normal)
        self._reasoning_edit.setTextCursor(cursor)
        self._reasoning_edit.ensureCursorVisible()

    @staticmethod
    def _stage_title(stage: str) -> str:
        return "阶段一 · 市场诊断" if stage == "stage1" else "阶段二 · 交易决策"

    def _begin_stage(self, stage: str, title: str) -> None:
        if self._stage and self._stage != stage and self._stage not in self._finalized_stages:
            self.finalize_stage(self._stage)
        self._stage = stage
        self._stage_t0 = time.monotonic()
        self._reasoning_chars = 0
        self._content_chars = 0
        self._ensure_stage_header(stage)
        self._phase_label.setText(f"▶ {title} — {self._stream_phase_suffix()}")
        self._update_stats()

    def _end_stage(self, title: str, *, stage: str | None = None) -> None:
        elapsed = time.monotonic() - self._stage_t0
        stage_key = stage or self._stage
        counts = self._stage_chars.get(stage_key, {})
        reasoning_n = counts.get("reasoning", 0)
        content_n = counts.get("content", 0)
        if reasoning_n > 0 and content_n > 0:
            detail = f"思考 {reasoning_n:,} 字 · 回答 {content_n:,} 字"
        elif reasoning_n > 0:
            detail = f"思考 {reasoning_n:,} 字"
        elif content_n > 0:
            detail = f"回答 {content_n:,} 字"
        else:
            detail = "无流式文本"
        self._phase_label.setText(f"✓ {title} — 完成 ({elapsed:.1f}s) · {detail}")

    def clear(self) -> None:
        self._reasoning_edit.clear()
        self._reasoning_chars = 0
        self._content_chars = 0
        self._stage = ""
        self._finalized_stages.clear()
        self._stage_chars.clear()
        self._stage_headers_written.clear()
        self._content_headers_written.clear()
        self._phase_label.setText("等待分析…")
        self._update_stats()
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("0%")
        self._progress_bar.setStyleSheet(_STYLE_NORMAL)
        self._token_label.setText("—")
        self._red_warned = False

    def clear_stream_output(self) -> None:
        """Clear only the visible live output pane and local text counters."""
        self._reasoning_edit.clear()
        self._reasoning_chars = 0
        self._content_chars = 0
        self._stage_chars.clear()
        self._stage_headers_written.clear()
        self._content_headers_written.clear()
        self._finalized_stages.clear()
        if self._stage:
            title = self._stage_title(self._stage) if self._stage in ("stage1", "stage2") else "追问"
            self._phase_label.setText(f"▶ {title} — {self._stream_phase_suffix()}")
        else:
            self._phase_label.setText("等待分析…")
        self._update_stats()

    def on_analysis_started(self) -> None:
        self.set_input_enabled(False)
        self._session = None
        self._cancel_token = None
        self.clear()

    def on_record_saved(self) -> None:
        self.set_input_enabled(True)

    def set_input_enabled(self, enabled: bool) -> None:
        self._input_edit.setEnabled(enabled)
        self._send_btn.setEnabled(enabled)

    def set_session(
        self,
        session: "FreeChatSession",
        cancel_token: "CancelToken",
    ) -> None:
        self._session = session
        self._cancel_token = cancel_token
        self.set_input_enabled(True)

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
                    f"上下文用量已达 {pct:.1f}%，接近上限。",
                )
        elif pct >= _YELLOW_PCT:
            self._progress_bar.setStyleSheet(_STYLE_YELLOW)
        else:
            self._progress_bar.setStyleSheet(_STYLE_NORMAL)
        self._token_label.setText(
            f"{context_used:,} / {context_window:,} · "
            f"in {total_input:,} / out {total_output:,}"
        )

    def on_stage_prompt_ready(self, stage: str, system: str, user: str) -> None:
        del system, user
        self._begin_stage(stage, self._stage_title(stage))

    def on_analysis_progress(self, text: str) -> None:
        """Sync phase header with orchestrator progress events."""
        if text == "阶段二分析中…":
            self._begin_stage("stage2", self._stage_title("stage2"))
        elif text in ("阶段一完成", "阶段一失败"):
            self.finalize_stage("stage1")
        elif text in ("阶段二完成", "阶段二失败"):
            self.finalize_stage("stage2")
        elif text == "已取消" and self._stage:
            self.finalize_stage(self._stage)

    def on_reasoning_token(self, stage: str, chunk: str) -> None:
        self._append_stream_text_for_stage(stage, chunk, kind="reasoning")

    def on_content_token(self, stage: str, chunk: str) -> None:
        """Stream assistant content (JSON / 撰写回答) into the same pane as reasoning."""
        if not chunk:
            return
        self._append_stream_text_for_stage(stage, chunk, kind="content")

    def finalize_stage(self, stage: str) -> None:
        if stage in self._finalized_stages:
            return
        self._finalized_stages.add(stage)
        self._end_stage(self._stage_title(stage), stage=stage)

    def show_stage_result(self, stage: str, content: str, reasoning: str) -> None:
        stage_id = "stage1" if "一" in stage else "stage2"
        counts = self._stage_chars.get(stage_id, {})
        if reasoning and counts.get("reasoning", 0) == 0:
            self._append_stream_text_for_stage(stage_id, reasoning, kind="reasoning")
        if content and counts.get("content", 0) == 0:
            self._append_stream_text_for_stage(stage_id, content, kind="content")
        if stage_id not in self._finalized_stages:
            self.finalize_stage(stage_id)

    def _on_send_or_stop(self) -> None:
        if self._sending:
            if self._cancel_token is not None:
                self._cancel_token.set()
        else:
            self._on_send()

    def _on_send(self) -> None:
        if self._session is None:
            return
        text = self._input_edit.text().strip()
        if not text:
            return
        from pa_agent.util.threading import CancelToken

        self._cancel_token = CancelToken()
        self._input_edit.clear()

        self._begin_stage("chat", "追问")
        self._append_user_message(text)
        self._phase_label.setText("▶ 追问 — 生成中…")

        self._sending = True
        self._send_btn.setText("停止")
        self._send_btn.setObjectName("dangerButton")
        self._send_btn.style().unpolish(self._send_btn)
        self._send_btn.style().polish(self._send_btn)
        self._input_edit.setEnabled(False)

        self._worker = _ChatWorker(self._session, text, self._cancel_token, parent=self)
        self._worker.reasoning_token.connect(self._append_reasoning)
        self._worker.content_token.connect(
            lambda chunk: self._append_stream_text_for_stage("chat", chunk, kind="content")
        )
        self._worker.finished.connect(self._on_reply_done)
        self._worker.error.connect(self._on_reply_error)
        self._worker.finished.connect(lambda *_: self._on_worker_done())
        self._worker.error.connect(lambda *_: self._on_worker_done())
        self._worker.start()

    def _on_reply_done(self, content: str, reasoning: str) -> None:
        if reasoning and self._reasoning_chars == 0:
            self._append_reasoning(reasoning)
        chat_counts = self._stage_chars.get("chat", {})
        if content and chat_counts.get("content", 0) == 0:
            self._append_stream_text_for_stage("chat", content, kind="content")
        self._end_stage("追问", stage="chat")
        if self._session is not None:
            ledger = getattr(self._session, "_ledger", None)
            if ledger is not None and hasattr(ledger, "breakdown"):
                bd = ledger.breakdown()
                if bd:
                    self.update_token_display(bd)

    def _on_reply_error(self, msg: str) -> None:
        self._append_reasoning(f"\n[错误] {msg}\n")
        self._end_stage("追问（失败）")

    def _on_worker_done(self) -> None:
        self._sending = False
        self._send_btn.setText("发送")
        self._send_btn.setObjectName("primaryButton")
        self._send_btn.style().unpolish(self._send_btn)
        self._send_btn.style().polish(self._send_btn)
        self._input_edit.setEnabled(True)
        self._worker = None
