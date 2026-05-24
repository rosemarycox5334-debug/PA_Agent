"""Replay control panel widget for bar-by-bar historical data replay."""
from __future__ import annotations

from datetime import datetime, timedelta

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QDateTimeEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ReplayPanel(QWidget):
    """Control panel for bar-by-bar replay mode.

    Signals
    -------
    load_history_requested(start_dt, end_dt):
        Emitted when user clicks "加载历史数据".
    next_bar_requested():
        Emitted when user clicks "下一步".
    exit_replay_requested():
        Emitted when user clicks "退出回放".
    submit_analysis_requested():
        Emitted when user clicks "提交分析" during replay.
    incremental_analysis_requested():
        Emitted when user clicks "增量分析" during replay.
    """

    load_history_requested = pyqtSignal(object, object)  # (datetime, datetime)
    next_bar_requested = pyqtSignal()
    exit_replay_requested = pyqtSignal()
    submit_analysis_requested = pyqtSignal()
    incremental_analysis_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        self.set_replay_active(False)

    def _build_ui(self) -> None:
        # Main layout with a styled frame border
        self.setStyleSheet(
            "ReplayPanel {"
            "  background-color: #161b22;"
            "  border: 1px solid #30363d;"
            "  border-radius: 6px;"
            "  padding: 8px;"
            "}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        # ── Title ─────────────────────────────────────────────────────────────
        title = QLabel("📽 逐K回放模式")
        title.setStyleSheet("font-weight: 600; font-size: 13px; color: #e6edf3;")
        layout.addWidget(title)

        # ── Date/time row ────────────────────────────────────────────────────
        dt_layout = QHBoxLayout()
        dt_layout.setSpacing(8)

        dt_layout.addWidget(QLabel("起始时间:"))
        self._start_dt = QDateTimeEdit()
        self._start_dt.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._start_dt.setCalendarPopup(True)
        # Default: 7 days ago
        self._start_dt.setDateTime(
            datetime.now() - timedelta(days=7)
        )
        self._start_dt.setStyleSheet("min-width: 160px;")
        dt_layout.addWidget(self._start_dt)

        dt_layout.addWidget(QLabel("结束时间:"))
        self._end_dt = QDateTimeEdit()
        self._end_dt.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._end_dt.setCalendarPopup(True)
        self._end_dt.setDateTime(datetime.now())
        self._end_dt.setStyleSheet("min-width: 160px;")
        dt_layout.addWidget(self._end_dt)

        self._load_btn = QPushButton("加载历史数据")
        self._load_btn.setObjectName("primaryButton")
        self._load_btn.setMinimumWidth(120)
        self._load_btn.clicked.connect(self._on_load)
        dt_layout.addWidget(self._load_btn)

        layout.addLayout(dt_layout)

        # ── Replay control row ───────────────────────────────────────────────
        replay_layout = QHBoxLayout()
        replay_layout.setSpacing(8)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet(
            "font-weight: 600; font-size: 12px; color: #58a6ff; min-width: 100px;"
        )
        replay_layout.addWidget(self._progress_label)

        self._next_btn = QPushButton("下一步 →")
        self._next_btn.setMinimumWidth(100)
        self._next_btn.clicked.connect(self.next_bar_requested.emit)
        replay_layout.addWidget(self._next_btn)

        self._submit_btn = QPushButton("提交分析")
        self._submit_btn.setObjectName("primaryButton")
        self._submit_btn.setMinimumWidth(100)
        self._submit_btn.clicked.connect(self.submit_analysis_requested.emit)
        replay_layout.addWidget(self._submit_btn)

        self._incremental_btn = QPushButton("增量分析")
        self._incremental_btn.setMinimumWidth(80)
        self._incremental_btn.clicked.connect(self.incremental_analysis_requested.emit)
        replay_layout.addWidget(self._incremental_btn)

        replay_layout.addStretch()

        self._exit_btn = QPushButton("退出回放")
        self._exit_btn.setMinimumWidth(100)
        self._exit_btn.setStyleSheet(
            "QPushButton { color: #f85149; border-color: #f85149; }"
            "QPushButton:hover { background-color: #3d1a1a; }"
        )
        self._exit_btn.clicked.connect(self.exit_replay_requested.emit)
        replay_layout.addWidget(self._exit_btn)

        layout.addLayout(replay_layout)

        # ── Status label ─────────────────────────────────────────────────────
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

    # ── Public API ───────────────────────────────────────────────────────────

    def set_replay_active(self, active: bool) -> None:
        """Enable/disable replay controls based on state."""
        self._next_btn.setEnabled(active)
        self._submit_btn.setEnabled(active)
        self._incremental_btn.setEnabled(active)
        self._exit_btn.setEnabled(active)
        self._load_btn.setEnabled(not active)
        self._start_dt.setEnabled(not active)
        self._end_dt.setEnabled(not active)
        if not active:
            self._progress_label.setText("")
            self._status_label.setText("")

    def set_loading(self, loading: bool) -> None:
        """Show loading state."""
        self._load_btn.setEnabled(not loading)
        self._load_btn.setText("加载中…" if loading else "加载历史数据")

    def update_progress(self, current: int, total: int) -> None:
        """Update the progress display."""
        self._progress_label.setText(f"K线 {current}/{total}")

    def set_status(self, text: str) -> None:
        """Update status text."""
        self._status_label.setText(text)

    def get_datetime_range(self) -> tuple[datetime, datetime]:
        """Return (start_dt, end_dt) from the pickers."""
        return (
            self._start_dt.dateTime().toPyDateTime(),
            self._end_dt.dateTime().toPyDateTime(),
        )

    def set_next_enabled(self, enabled: bool) -> None:
        """Enable or disable the '下一步' button."""
        self._next_btn.setEnabled(enabled)

    def set_submit_enabled(self, enabled: bool) -> None:
        """Enable or disable the '提交分析' button."""
        self._submit_btn.setEnabled(enabled)

    def set_incremental_enabled(self, enabled: bool) -> None:
        """Enable or disable the '增量分析' button."""
        self._incremental_btn.setEnabled(enabled)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _on_load(self) -> None:
        """Validate and emit load request."""
        start, end = self.get_datetime_range()
        if start >= end:
            self.set_status("错误：起始时间必须早于结束时间")
            return
        self.load_history_requested.emit(start, end)