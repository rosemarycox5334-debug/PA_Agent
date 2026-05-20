"""Main application window for PA Agent."""
from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtGui import QAction
from PyQt6.QtCore import Qt

from pa_agent.app_context import AppContext

logger = logging.getLogger(__name__)

# Zombie timeout in milliseconds (5 seconds)
_WORKER_JOIN_TIMEOUT_MS = 5000


# ── AI Worker ─────────────────────────────────────────────────────────────────

class _AnalysisWorker(QThread):
    """Runs TwoStageOrchestrator.submit() on a background thread.

    Signals
    -------
    finished(dict):
        Emitted with the stage2_decision dict on success (or empty dict on
        failure / cancellation).
    status_update(str):
        Emitted with human-readable progress text.
    reasoning_token(str, str):
        Emitted with (stage, token_chunk) for each reasoning token streamed.
        stage is "stage1" or "stage2".
    content_token(str, str):
        Emitted with (stage, token_chunk) for each content token streamed.
        stage is "stage1" or "stage2".
    stage_prompt_ready(str, str, str):
        Emitted with (stage, system_prompt, user_prompt) just before each
        API call, so the conversation tab can show what was sent.
    """

    finished = pyqtSignal(dict)
    record_ready = pyqtSignal(object)   # emits the full AnalysisRecord
    status_update = pyqtSignal(str)
    reasoning_token = pyqtSignal(str, str)   # (stage, chunk)
    content_token = pyqtSignal(str, str)     # (stage, chunk)
    stage_prompt_ready = pyqtSignal(str, str, str)  # (stage, system, user)
    stage2_files_ready = pyqtSignal(list)  # strategy .txt filenames for stage 2

    def __init__(
        self,
        orchestrator: Any,
        frame: Any,
        cancel_token: Any,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._orchestrator = orchestrator
        self._frame = frame
        self._cancel_token = cancel_token

    def run(self) -> None:
        from pa_agent.util.threading import OrchestratorEvent

        _EVENT_LABELS = {
            OrchestratorEvent.Stage1Started: "阶段一分析中…",
            OrchestratorEvent.Stage1Done: "阶段一完成",
            OrchestratorEvent.Stage2Started: "阶段二分析中…",
            OrchestratorEvent.Stage2Done: "阶段二完成",
            OrchestratorEvent.RecordSaved: "记录已保存",
            OrchestratorEvent.Cancelled: "已取消",
            OrchestratorEvent.Stage1Failed: "阶段一失败",
            OrchestratorEvent.Stage2Failed: "阶段二失败",
        }

        def on_event(event: OrchestratorEvent) -> None:
            label = _EVENT_LABELS.get(event, str(event))
            self.status_update.emit(label)

        def on_stage1_reasoning(chunk: str) -> None:
            self.reasoning_token.emit("stage1", chunk)

        def on_stage1_content(chunk: str) -> None:
            self.content_token.emit("stage1", chunk)

        def on_stage2_reasoning(chunk: str) -> None:
            self.reasoning_token.emit("stage2", chunk)

        def on_stage2_content(chunk: str) -> None:
            self.content_token.emit("stage2", chunk)

        def on_stage_prompt(stage: str, system: str, user: str) -> None:
            self.stage_prompt_ready.emit(stage, system, user)

        def on_stage2_files(files: list[str]) -> None:
            self.stage2_files_ready.emit(files)

        try:
            record = self._orchestrator.submit(
                self._frame,
                self._cancel_token,
                on_event,
                on_stage1_reasoning=on_stage1_reasoning,
                on_stage1_content=on_stage1_content,
                on_stage2_reasoning=on_stage2_reasoning,
                on_stage2_content=on_stage2_content,
                on_stage_prompt=on_stage_prompt,
                on_stage2_files=on_stage2_files,
            )
            decision = record.stage2_decision or {}
        except Exception as exc:  # noqa: BLE001
            logger.error("Analysis worker error: %s", exc, exc_info=True)
            decision = {}
            record = None  # type: ignore[assignment]

        if record is not None:
            self.record_ready.emit(record)
        self.finished.emit(decision)


# ── MainWindow ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    """Top-level workbench: chart + AI sidebar (analysis / raw / decision)."""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PA Agent — Trading Terminal")
        self.resize(1440, 900)
        self._ctx = ctx
        self._worker: _AnalysisWorker | None = None
        self._cancel_token: Any = None
        self._analysis_in_progress = False
        self._switching = False
        self._chart_refresh_paused = False
        self._pending_submit_after_close = False
        self._wait_forming_ts: int | None = None
        self._pending_submit_symbol = ""
        self._pending_submit_timeframe = ""
        self._pending_submit_bar_count = 0
        self._last_forming_ts_open: int | None = None
        self._free_chat_session: Any = None
        self._last_stage1_diagnosis: dict | None = None
        self._demo_mode = False
        self._demo_mode_kind: str | None = None  # manual | auto
        self._demo_record_path: str | None = None
        self._demo_replayer: Any = None
        self._demo_auto_next_armed = False
        self._demo_waiting_flow_playback = False
        # RefreshLoop runs in its own QThread
        self._refresh_loop: Any = None
        self._refresh_thread: QThread | None = None
        self._setup_ui()
        self._connect_event_bus()
        self._start_refresh_loop()
        self._update_ai_mode_label()
        self._debug_widget.streak_reset.connect(self._on_exception_streak_reset)
        self._sync_submit_button_state()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        from pa_agent.gui.ai_sidebar import AISidebar

        _api_key = ""
        _exc_counter = getattr(self._ctx, "exc_counter", None)
        _settings = getattr(self._ctx, "settings", None)
        if _settings is not None:
            _api_key = getattr(_settings.provider, "api_key", "") or ""

        self._ai_sidebar = AISidebar(
            api_key=_api_key,
            exc_counter=_exc_counter,
            settings=_settings,
        )
        self._stream_panel = self._ai_sidebar.stream
        self._debug_widget = self._ai_sidebar.debug
        self._prompt_files_panel = self._ai_sidebar.prompt_files
        self._decision_panel = self._ai_sidebar.decision
        self._decision_tree_panel = self._ai_sidebar.decision_tree
        self._decision_flow_viz_panel = self._ai_sidebar.decision_flow_viz

        # Auto demo: when flow playback ends, return to stream tab.
        try:
            self._decision_flow_viz_panel.playback_finished.connect(
                self._on_demo_flow_playback_finished,
                Qt.ConnectionType.UniqueConnection,
            )
        except Exception:  # noqa: BLE001
            pass

        self._central = self._build_workbench()
        self.setCentralWidget(self._central)

        # ── Status bar ────────────────────────────────────────────────────────
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._demo_mode_label = QLabel("")
        self._demo_mode_label.setStyleSheet(
            "color: #e6b800; font-weight: 600; padding-left: 4px;"
        )
        self._demo_mode_label.hide()
        self._status_bar.addWidget(self._demo_mode_label, 1)
        self._status_bar.showMessage("就绪")
        self._sync_submit_button_state()

        # ── Menu bar ──────────────────────────────────────────────────────────
        menu_bar: QMenuBar = self.menuBar()  # type: ignore[assignment]
        settings_menu = menu_bar.addMenu("设置")

        open_settings_action = QAction("打开设置…", self)
        open_settings_action.triggered.connect(self._open_settings_dialog)
        settings_menu.addAction(open_settings_action)

    def _build_workbench(self) -> QWidget:
        """Build chart + AI sidebar workbench."""
        from pa_agent.gui.chart_widget import ChartWidget

        tab = QWidget()
        outer_layout = QVBoxLayout(tab)
        outer_layout.setContentsMargins(8, 8, 8, 8)
        outer_layout.setSpacing(6)

        # ── Control bar ───────────────────────────────────────────────────────
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setSpacing(8)

        # Symbol — editable combo (user can type any MT5 symbol)
        ctrl_layout.addWidget(QLabel("品种:"))
        self._symbol_combo = QComboBox()
        self._symbol_combo.setEditable(True)
        self._symbol_combo.addItems(["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "XAGUSD"])
        # Restore last-used symbol from settings
        _last_symbol = "XAUUSD"
        _last_tf = "1h"
        _settings = getattr(self._ctx, "settings", None)
        if _settings is not None:
            _last_symbol = getattr(_settings.general, "last_symbol", "XAUUSD") or "XAUUSD"
            _last_tf = getattr(_settings.general, "last_timeframe", "1h") or "1h"
        self._symbol_combo.setCurrentText(_last_symbol)
        self._symbol_combo.setMinimumWidth(110)
        self._symbol_combo.lineEdit().setPlaceholderText("输入品种名…")
        ctrl_layout.addWidget(self._symbol_combo)

        # Timeframe
        ctrl_layout.addWidget(QLabel("周期:"))
        self._tf_combo = QComboBox()
        self._tf_combo.addItems(["1m", "5m", "15m", "1h", "4h", "1d"])
        self._tf_combo.setCurrentText(_last_tf)
        self._tf_combo.setMinimumWidth(60)
        ctrl_layout.addWidget(self._tf_combo)
        # Bar count
        ctrl_layout.addWidget(QLabel("K线数:"))
        self._bar_count_spin = QSpinBox()
        self._bar_count_spin.setRange(2, 5000)
        self._bar_count_spin.setValue(100)
        self._bar_count_spin.setMinimumWidth(70)
        ctrl_layout.addWidget(self._bar_count_spin)

        ctrl_layout.addStretch()

        self._wait_close_checkbox = QCheckBox("等待最新K线收盘后再提交分析")
        self._wait_close_checkbox.setObjectName("waitCloseCheckbox")
        self._wait_close_checkbox.setChecked(False)
        self._wait_close_checkbox.setToolTip(
            "勾选后，点击提交分析将先等待当前未收盘K线走完，再抓取数据并开始分析"
        )
        self._wait_close_checkbox.stateChanged.connect(self._on_wait_close_checkbox_changed)
        ctrl_layout.addWidget(self._wait_close_checkbox)

        self._wait_close_countdown_label = QLabel("")
        self._wait_close_countdown_label.setObjectName("mutedLabel")
        self._wait_close_countdown_label.setMinimumWidth(100)
        ctrl_layout.addWidget(self._wait_close_countdown_label)

        self._submit_btn = QPushButton("提交分析")
        self._submit_btn.setObjectName("primaryButton")
        self._submit_btn.setMinimumWidth(100)
        self._submit_btn.clicked.connect(self._on_submit_analysis)
        ctrl_layout.addWidget(self._submit_btn)

        self._demo_btn = QPushButton("演示模式")
        self._demo_btn.setToolTip("用 records/pending 中已保存的分析记录回放界面")
        self._demo_btn.clicked.connect(self._on_demo_mode_button)
        ctrl_layout.addWidget(self._demo_btn)

        self._resume_chart_btn = QPushButton("图表实时更新")
        self._resume_chart_btn.setEnabled(False)
        self._resume_chart_btn.setToolTip("分析进行中会暂停图表刷新；分析开始后点此恢复 K 线实时更新")
        self._resume_chart_btn.clicked.connect(self._on_resume_chart_refresh)
        ctrl_layout.addWidget(self._resume_chart_btn)

        self._decision_badge = QLabel("")
        self._decision_badge.setObjectName("mutedLabel")
        ctrl_layout.addWidget(self._decision_badge)

        self._ai_mode_label = QLabel("")
        self._ai_mode_label.setObjectName("mutedLabel")
        ctrl_layout.addWidget(self._ai_mode_label)

        outer_layout.addLayout(ctrl_layout)

        status_row = QHBoxLayout()
        status_row.addStretch()
        self._last_refresh_ts: float = 0.0
        self._refresh_elapsed_label = QLabel("距上次刷新: —")
        self._refresh_elapsed_label.setObjectName("mutedLabel")
        status_row.addWidget(self._refresh_elapsed_label)

        from PyQt6.QtCore import QTimer as _QTimer
        self._elapsed_ticker = _QTimer(tab)
        self._elapsed_ticker.setInterval(1000)
        self._elapsed_ticker.timeout.connect(self._update_refresh_elapsed)
        self._elapsed_ticker.start()

        outer_layout.addLayout(status_row)

        workbench = QSplitter(Qt.Orientation.Horizontal)

        self._chart_widget = ChartWidget()
        self._chart_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        workbench.addWidget(self._chart_widget)

        self._ai_sidebar.setMinimumWidth(400)
        workbench.addWidget(self._ai_sidebar)

        workbench.setStretchFactor(0, 3)
        workbench.setStretchFactor(1, 2)

        outer_layout.addWidget(workbench, stretch=1)

        # Connect symbol/timeframe combo boxes to the switch handler
        self._symbol_combo.currentTextChanged.connect(
            lambda _: self._on_symbol_or_tf_changed(
                self._symbol_combo.currentText(), self._tf_combo.currentText()
            )
        )
        self._tf_combo.currentTextChanged.connect(
            lambda _: self._on_symbol_or_tf_changed(
                self._symbol_combo.currentText(), self._tf_combo.currentText()
            )
        )

        return tab

    def _connect_event_bus(self) -> None:
        """Wire EventBus signals to status bar and tab slots (if bus is ready)."""
        bus = self._ctx.event_bus
        if bus is None:
            return
        bus.status.connect(self._on_status_update)

    def _start_refresh_loop(self) -> None:
        """Start the RefreshLoop only when the data source is connected."""
        data_source = getattr(self._ctx, "data_source", None)
        buffer = getattr(self._ctx, "buffer", None)
        if data_source is None or buffer is None:
            logger.debug("RefreshLoop not started: data_source or buffer not available")
            return

        # Don't start if the data source hasn't connected yet
        if not getattr(data_source, "_connected", False):
            logger.info("Data source not connected — RefreshLoop deferred.")
            self._status_bar.showMessage("数据源未连接，请检查网络后重启程序")
            return

        from pa_agent.data.refresh_loop import RefreshLoop
        from pa_agent.util.threading import CancelToken

        settings = getattr(self._ctx, "settings", None)
        interval_ms = 1000
        n_bars = 200
        if settings is not None:
            interval_ms = getattr(settings.general, "refresh_interval_ms", 1000)
            n_bars = getattr(settings.general, "default_bar_count", 200)

        self._refresh_cancel_token = CancelToken()
        self._refresh_loop = RefreshLoop(
            data_source=data_source,
            buffer=buffer,
            n_bars=n_bars,
            interval_ms=interval_ms,
            cancel_token=self._refresh_cancel_token,
        )

        # Wire RefreshLoop signals
        self._refresh_loop.frame_ready.connect(self._on_refresh_frame_ready)
        self._refresh_loop.status_changed.connect(self._on_status_update)

        self._refresh_loop.start()
        logger.info("RefreshLoop started for %s %s",
                    getattr(data_source, "_symbol", "?"),
                    getattr(data_source, "_timeframe", "?"))

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_status_update(self, text: str) -> None:
        """Update the status bar with subscription / analysis / data-delay text."""
        self._status_bar.showMessage(text)
        if self._analysis_in_progress:
            panel = getattr(self, "_stream_panel", None)
            if panel is not None:
                panel.on_analysis_progress(text)

    def _set_chart_refresh_paused(self, paused: bool) -> None:
        """Pause or resume live chart updates from RefreshLoop."""
        self._chart_refresh_paused = paused
        btn = getattr(self, "_resume_chart_btn", None)
        if btn is not None:
            btn.setEnabled(paused)

    def _on_resume_chart_refresh(self) -> None:
        """User requested live chart updates again."""
        if not self._chart_refresh_paused:
            return
        self._set_chart_refresh_paused(False)
        self._status_bar.showMessage("图表已恢复实时更新")
        self._refresh_chart_once()

    def _refresh_chart_once(self) -> None:
        """Apply one immediate chart refresh (e.g. after resuming)."""
        data_source = getattr(self._ctx, "data_source", None)
        if data_source is None or not getattr(data_source, "_connected", False):
            return
        try:
            n_bars = self._bar_count_spin.value() + 5
            bars = data_source.latest_snapshot(n_bars)
            if bars:
                self._on_refresh_frame_ready(bars)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Immediate chart refresh failed: %s", exc)

    def _update_refresh_elapsed(self) -> None:
        """Update the 'distance from last refresh' label every second."""
        import time as _time

        self._update_wait_close_countdown_display()

        label = getattr(self, "_refresh_elapsed_label", None)
        if label is None:
            return
        if self._pending_submit_after_close:
            secs = self._forming_bar_seconds_remaining()
            if secs is not None:
                label.setText(f"等待K线收盘，还剩 {secs}s")
            else:
                label.setText("等待最新K线收盘…")
            label.setStyleSheet("color: #58a6ff; font-size: 11px;")
            return
        if self._wait_close_checkbox.isChecked():
            secs = self._forming_bar_seconds_remaining()
            if secs is not None:
                label.setText(f"距最新K线收盘还剩 {secs}s")
            else:
                label.setText("距最新K线收盘: —")
            label.setStyleSheet("color: #58a6ff; font-size: 11px;")
            return
        if self._chart_refresh_paused:
            label.setText("图表刷新已暂停（分析中）")
            label.setStyleSheet("color: #e6b800; font-size: 11px;")
            return
        if self._last_refresh_ts == 0.0:
            label.setText("距上次刷新: —")
            return
        elapsed = int(_time.monotonic() - self._last_refresh_ts)
        if elapsed < 60:
            label.setText(f"距上次刷新: {elapsed}s")
        else:
            m, s = divmod(elapsed, 60)
            label.setText(f"距上次刷新: {m}m{s:02d}s")
        # Turn red if stale (> 10 seconds without update)
        if elapsed > 10:
            label.setStyleSheet("color: #f85149; font-size: 11px;")
        else:
            label.setObjectName("mutedLabel")
            label.setStyleSheet("")

    def _on_data_frame(self, frame: Any) -> None:
        """Forward a new KlineFrame to the chart widget (throttled by 30 Hz timer)."""
        self._chart_widget.set_frame(frame)

    def _on_refresh_frame_ready(self, bars: Any) -> None:
        """Handle frame_ready signal from RefreshLoop.

        Builds a KlineFrame directly from the bars returned by latest_snapshot()
        rather than reading back from the buffer, which avoids ordering issues
        caused by repeated appendleft() calls corrupting the buffer's deque.
        """
        if bars:
            from pa_agent.data.bar_close_wait import current_forming_ts

            ts = current_forming_ts(bars)
            if ts is not None:
                self._last_forming_ts_open = ts

        if self._pending_submit_after_close and bars:
            self._check_pending_bar_close(bars)

        if self._chart_refresh_paused:
            return

        if not bars:
            return

        try:
            import time as _time

            frame = self._build_chart_frame_from_bars(bars, include_forming=True)
            if frame is None:
                return

            self._chart_widget.set_frame(frame)

            # Record the time of this successful chart update
            self._last_refresh_ts = _time.monotonic()
            self._update_refresh_elapsed()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Frame build skipped: %s", exc)

    def _on_symbol_or_tf_changed(self, new_symbol: str, new_tf: str) -> None:
        """Handle symbol or timeframe combo box change.

        Steps (design §B.10, R3.1–R3.5):
        1. Cancel current AI worker and wait up to 5 s (zombie if timeout).
        2. Save partial record if analysis was in progress.
        3. Unsubscribe data source, clear buffer, re-subscribe.
        4. Reset ChartWidget.
        5. Destroy FreeChatSession, disable Tab2 input.
        6. Reset or preserve ledger based on settings.
        """
        if self._switching:
            return  # Prevent re-entrant calls

        self._clear_pending_bar_close_wait()

        self._switching = True
        try:
            # ── Step 1: Cancel current AI worker ─────────────────────────────
            if self._worker is not None and self._worker.isRunning():
                if self._cancel_token is not None:
                    self._cancel_token.set()
                finished = self._worker.wait(_WORKER_JOIN_TIMEOUT_MS)
                if not finished:
                    logger.warning(
                        "AI worker did not finish within %d ms after symbol/tf switch; "
                        "marking as zombie",
                        _WORKER_JOIN_TIMEOUT_MS,
                    )
                    # Mark as zombie — do not force-kill
                self._worker = None

            # ── Step 2: Save partial record if analysis was in progress ───────
            if self._analysis_in_progress:
                pending_writer = getattr(self._ctx, "pending_writer", None)
                if pending_writer is not None:
                    # We don't have the active record here; the orchestrator
                    # handles save_partial via the cancel token path.
                    # This is a belt-and-suspenders call for any record that
                    # may have been built but not yet saved.
                    try:
                        pending_writer.save_partial(None, reason="user_switched")
                    except Exception:  # noqa: BLE001
                        pass
                self._analysis_in_progress = False
                self._update_submit_button_state()

            # ── Step 3: Unsubscribe, clear buffer, re-subscribe ───────────────
            data_source = getattr(self._ctx, "data_source", None)
            buffer = getattr(self._ctx, "buffer", None)
            if data_source is not None:
                try:
                    data_source.unsubscribe()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("unsubscribe failed: %s", exc)
            if buffer is not None:
                buffer.clear()
            if data_source is not None:
                try:
                    data_source.subscribe(new_symbol, new_tf)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("subscribe(%s, %s) failed: %s", new_symbol, new_tf, exc)

            # ── Step 4: Reset ChartWidget ─────────────────────────────────────
            if hasattr(self, "_chart_widget"):
                self._chart_widget.reset()

            # ── Step 5: Destroy FreeChatSession, disable Tab2 input ───────────
            self._free_chat_session = None
            self._disable_chat_input()

            # ── Step 6: Reset ledger (always reset on symbol/tf switch) ───────
            ledger = getattr(self._ctx, "ledger", None)
            if ledger is not None:
                try:
                    ledger.reset()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("ledger.reset() failed: %s", exc)

            self._set_chart_refresh_paused(False)

            self._status_bar.showMessage(f"已切换至 {new_symbol} {new_tf}")
            logger.info("Symbol/TF switched to %s %s", new_symbol, new_tf)

            # Persist last-used symbol/timeframe to settings
            settings = getattr(self._ctx, "settings", None)
            if settings is not None:
                settings.general.last_symbol = new_symbol
                settings.general.last_timeframe = new_tf
                try:
                    from pa_agent.config.settings import save_settings
                    save_settings(settings)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Failed to persist symbol/tf to settings: %s", exc)

        finally:
            self._switching = False
            if self._wait_close_checkbox.isChecked():
                self._refresh_last_forming_ts()
                self._update_wait_close_countdown_display()

    def _disable_chat_input(self) -> None:
        """Disable free-chat input in the AI stream window."""
        panel = getattr(self, "_stream_panel", None)
        if panel is not None:
            panel.set_input_enabled(False)

    def _on_wait_close_checkbox_changed(self, _state: int) -> None:
        """Cancel pending wait if user unchecks the option."""
        if self._wait_close_checkbox.isChecked():
            self._refresh_last_forming_ts()
        else:
            if self._pending_submit_after_close:
                self._clear_pending_bar_close_wait()
            self._status_bar.showMessage("已取消等待K线收盘")
        self._update_wait_close_countdown_display()

    def _refresh_last_forming_ts(self) -> None:
        """Snapshot newest forming bar ts_open for countdown display."""
        from pa_agent.data.bar_close_wait import current_forming_ts

        data_source = getattr(self._ctx, "data_source", None)
        if data_source is None or not getattr(data_source, "_connected", False):
            return
        try:
            bars = data_source.latest_snapshot(10)
            ts = current_forming_ts(bars)
            if ts is not None:
                self._last_forming_ts_open = ts
        except Exception as exc:  # noqa: BLE001
            logger.debug("refresh_last_forming_ts failed: %s", exc)

    def _forming_bar_seconds_remaining(self) -> int | None:
        """Seconds until the relevant forming bar closes."""
        from pa_agent.data.bar_close_wait import seconds_until_bar_closes
        from pa_agent.util.timefmt import now_local_ms

        if self._pending_submit_after_close:
            ts = self._wait_forming_ts
            tf = self._pending_submit_timeframe
        elif self._wait_close_checkbox.isChecked():
            ts = self._last_forming_ts_open
            tf = self._tf_combo.currentText()
        else:
            return None
        if ts is None or not tf:
            return None
        return seconds_until_bar_closes(int(ts), tf, now_ms=now_local_ms())

    def _update_wait_close_countdown_display(self) -> None:
        """Update checkbox-adjacent countdown and status bar while waiting."""
        lbl = getattr(self, "_wait_close_countdown_label", None)
        show = self._wait_close_checkbox.isChecked() or self._pending_submit_after_close
        if lbl is not None:
            if not show:
                lbl.setText("")
            else:
                secs = self._forming_bar_seconds_remaining()
                if secs is None:
                    lbl.setText("")
                else:
                    lbl.setText(f"还剩 {secs} 秒")
                    lbl.setStyleSheet("color: #58a6ff; font-size: 11px;")
        if self._pending_submit_after_close:
            secs = self._forming_bar_seconds_remaining()
            if secs is not None:
                self._status_bar.showMessage(
                    f"等待当前K线收盘…还剩 {secs} 秒（收盘后将自动提交分析）"
                )

    def _clear_pending_bar_close_wait(self) -> None:
        """Cancel wait-for-bar-close armed by the checkbox."""
        self._pending_submit_after_close = False
        self._wait_forming_ts = None
        self._pending_submit_symbol = ""
        self._pending_submit_timeframe = ""
        self._pending_submit_bar_count = 0
        self._update_submit_button_state()
        self._update_wait_close_countdown_display()

    def _check_pending_bar_close(self, bars: Any) -> None:
        """If the forming bar rolled over, start the deferred analysis."""
        from pa_agent.data.bar_close_wait import forming_bar_has_closed

        if not self._pending_submit_after_close or self._wait_forming_ts is None:
            return
        if not forming_bar_has_closed(self._wait_forming_ts, bars):
            return

        symbol = self._pending_submit_symbol
        timeframe = self._pending_submit_timeframe
        bar_count = self._pending_submit_bar_count
        self._clear_pending_bar_close_wait()
        self._status_bar.showMessage("最新K线已收盘，正在提交分析…")
        self._start_analysis(symbol, timeframe, bar_count)

    def _arm_wait_for_bar_close(self, symbol: str, timeframe: str, bar_count: int) -> bool:
        """Wait until bars[0] ts_open changes, then call _start_analysis."""
        from datetime import datetime

        from pa_agent.data.bar_close_wait import current_forming_ts

        data_source = getattr(self._ctx, "data_source", None)
        if data_source is None or not getattr(data_source, "_connected", False):
            self._status_bar.showMessage("数据源未连接")
            return False

        try:
            bars_raw = data_source.latest_snapshot(bar_count + 5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Wait-for-close snapshot failed: %s", exc)
            self._status_bar.showMessage("获取K线失败，请稍后重试")
            return False

        if not bars_raw:
            self._status_bar.showMessage("数据不足，请等待缓冲区填满后再提交")
            return False

        forming_ts = current_forming_ts(bars_raw)
        if forming_ts is None:
            self._status_bar.showMessage("无法识别当前K线")
            return False

        self._pending_submit_after_close = True
        self._wait_forming_ts = forming_ts
        self._last_forming_ts_open = forming_ts
        self._pending_submit_symbol = symbol.strip()
        self._pending_submit_timeframe = timeframe
        self._pending_submit_bar_count = bar_count
        self._update_submit_button_state()
        self._update_wait_close_countdown_display()

        secs = self._forming_bar_seconds_remaining()
        try:
            dt = datetime.fromtimestamp(forming_ts / 1000).strftime("%H:%M:%S")
            ts_hint = f"开盘 {dt}"
        except (OSError, OverflowError, ValueError):
            ts_hint = f"ts={forming_ts}"

        if secs is not None:
            self._status_bar.showMessage(
                f"等待当前K线收盘…还剩 {secs} 秒（{ts_hint}，收盘后将自动提交）"
            )
        else:
            self._status_bar.showMessage(
                f"等待当前K线收盘…（{ts_hint}，收盘后将自动提交分析）"
            )
        return True

    def _on_demo_mode_button(self) -> None:
        """Enter demo mode (manual/auto) or exit if already active."""
        if self._demo_mode:
            self._exit_demo_mode()
            return
        menu = QMenu(self)
        menu.addAction("手动选择记录…", lambda: self._start_demo_mode("manual"))
        menu.addAction("自动随机记录", lambda: self._start_demo_mode("auto"))
        menu.exec(self._demo_btn.mapToGlobal(self._demo_btn.rect().bottomLeft()))

    def _start_demo_mode(self, mode: str) -> None:
        """Load a pending JSON record and replay it through the UI."""
        from pa_agent.config.paths import RECORDS_PENDING_DIR
        from pa_agent.demo.record_loader import load_analysis_record, pick_random_record_path

        self._demo_mode_kind = str(mode)
        self._demo_auto_next_armed = False

        if mode == "manual":
            start_dir = str(RECORDS_PENDING_DIR)
            path_str, _ = QFileDialog.getOpenFileName(
                self,
                "选择演示记录",
                start_dir,
                "分析记录 (*.json);;所有文件 (*.*)",
            )
            if not path_str:
                return
            from pathlib import Path

            path = Path(path_str)
        else:
            path = pick_random_record_path()
            if path is None:
                QMessageBox.warning(
                    self,
                    "演示模式",
                    f"未找到可用记录：\n{RECORDS_PENDING_DIR}",
                )
                return

        try:
            record = load_analysis_record(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "演示模式", f"无法读取记录：\n{exc}")
            return

        if not record.stage2_decision and not record.stage1_diagnosis:
            QMessageBox.warning(
                self,
                "演示模式",
                "该记录缺少阶段一/阶段二结果，无法完整演示。",
            )
            return

        self._enter_demo_mode(path, record)

    def _try_load_random_demo_record(self) -> tuple[Any, Any] | tuple[None, None]:
        """Return (path, record) for a random pending record, or (None, None)."""
        from pa_agent.demo.record_loader import load_analysis_record, list_pending_record_paths

        paths = list_pending_record_paths()
        if not paths:
            return None, None

        def _has_flow_viz_payload(rec: Any) -> bool:
            s2 = getattr(rec, "stage2_decision", None)
            if not isinstance(s2, dict):
                return False
            return bool(s2.get("decision_trace") or s2.get("terminal"))

        # Try a few times to avoid repeating the same record when possible.
        last = self._demo_record_path or ""
        # Pass 1: prefer records that can render decision-flow visualization
        for _ in range(10):
            import random

            path = random.choice(paths)
            if len(paths) > 1 and str(path) == last:
                continue
            try:
                record = load_analysis_record(path)
            except Exception:  # noqa: BLE001
                continue
            if _has_flow_viz_payload(record):
                return path, record
        # Pass 2 fallback: any record with stage1/stage2 payload (may not have flow viz)
        for _ in range(10):
            import random

            path = random.choice(paths)
            if len(paths) > 1 and str(path) == last:
                continue
            try:
                record = load_analysis_record(path)
            except Exception:  # noqa: BLE001
                continue
            if getattr(record, "stage2_decision", None) or getattr(record, "stage1_diagnosis", None):
                return path, record
        # Fallback: just take the newest valid one
        for path in paths:
            try:
                record = load_analysis_record(path)
            except Exception:  # noqa: BLE001
                continue
            if _has_flow_viz_payload(record) or getattr(record, "stage2_decision", None) or getattr(record, "stage1_diagnosis", None):
                return path, record
        return None, None

    def _schedule_next_auto_demo(self, *, delay_ms: int = 650) -> None:
        """In auto demo mode, schedule the next random record replay."""
        from PyQt6.QtCore import QTimer

        if not self._demo_mode or self._demo_mode_kind != "auto":
            return
        if self._demo_auto_next_armed:
            return
        self._demo_auto_next_armed = True

        def _go() -> None:
            self._demo_auto_next_armed = False
            if not self._demo_mode or self._demo_mode_kind != "auto":
                return
            path, record = self._try_load_random_demo_record()
            if path is None or record is None:
                self._status_bar.showMessage("自动演示：未找到可用记录，已停止")
                self._exit_demo_mode()
                return
            self._enter_demo_mode(path, record)

        QTimer.singleShot(max(60, int(delay_ms)), _go)

    def _enter_demo_mode(self, path: Any, record: Any) -> None:
        """Switch UI into demo state and start timed replay."""
        from pathlib import Path

        from pa_agent.demo.record_loader import frame_from_record_klines
        from pa_agent.demo.replayer import DemoReplayer

        if self._worker is not None and self._worker.isRunning():
            if self._cancel_token is not None:
                self._cancel_token.set()
            self._worker.wait(_WORKER_JOIN_TIMEOUT_MS)
            self._worker = None

        # When auto-chaining records, we reuse the same demo "kind".
        prev_kind = self._demo_mode_kind
        self._exit_demo_mode(silent=True)
        self._demo_mode_kind = prev_kind

        self._demo_mode = True
        self._demo_record_path = str(Path(path))
        self._demo_btn.setText("退出演示模式")

        meta = record.meta
        self._symbol_combo.setCurrentText(meta.symbol)
        self._tf_combo.setCurrentText(meta.timeframe)
        self._bar_count_spin.setValue(int(meta.bar_count))

        try:
            frame = frame_from_record_klines(
                record.kline_data,
                symbol=meta.symbol,
                timeframe=meta.timeframe,
                snapshot_ts_local_ms=meta.timestamp_local_ms,
            )
        except Exception as exc:  # noqa: BLE001
            self._exit_demo_mode()
            QMessageBox.critical(self, "演示模式", f"无法构建 K 线快照：\n{exc}")
            return

        self._chart_widget.set_frame(frame)
        self._set_chart_refresh_paused(True)
        self._analysis_in_progress = True
        self._update_submit_button_state()

        name = Path(path).name
        self._demo_mode_label.setText(f"当前为演示模式 · {name}")
        self._demo_mode_label.show()
        self._status_bar.showMessage(f"演示回放中… ({name})")
        self._decision_badge.setText("演示中…")

        self._ai_sidebar.focus_stream()
        panel = self._stream_panel
        panel.clear()
        panel.on_analysis_started()
        panel.set_input_enabled(False)
        self._debug_widget.clear()
        self._decision_tree_panel.clear()
        self._decision_flow_viz_panel.clear()
        self._decision_panel.clear()

        from pa_agent.ai.prompt_assembler import stage1_prompt_txt_files

        self._prompt_files_panel.clear()
        self._prompt_files_panel.set_stage1_files(stage1_prompt_txt_files())
        self._prompt_files_panel.set_extras(stage1_builtin=True)

        self._demo_replayer = DemoReplayer(record, parent=self)
        self._demo_replayer.status_update.connect(self._on_status_update)
        self._demo_replayer.finished.connect(self._on_analysis_finished)
        self._demo_replayer.record_ready.connect(self._on_record_ready)
        self._demo_replayer.stage_prompt_ready.connect(panel.on_stage_prompt_ready)
        self._demo_replayer.reasoning_token.connect(panel.on_reasoning_token)
        self._demo_replayer.stage2_files_ready.connect(self._on_stage2_files_ready)
        self._demo_replayer.replay_finished.connect(self._on_demo_replay_done)
        self._demo_replayer.start()

    def _on_demo_replay_done(self) -> None:
        """End demo analysis-in-progress state after replay completes."""
        from pathlib import Path
        from PyQt6.QtCore import QTimer

        self._analysis_in_progress = False
        self._update_submit_button_state()
        if self._demo_mode:
            name = Path(self._demo_record_path).name if self._demo_record_path else ""
            self._status_bar.showMessage(f"演示回放完成 · {name}")
        panel = getattr(self, "_stream_panel", None)
        if panel is not None:
            panel.set_input_enabled(False)
        if self._demo_mode and self._demo_mode_kind == "auto":
            # Wait for decision-flow playback to complete before switching records.
            self._demo_waiting_flow_playback = True

            def _fallback_if_no_flow_started() -> None:
                if not self._demo_mode or self._demo_mode_kind != "auto":
                    return
                if not self._demo_waiting_flow_playback:
                    return
                flow = getattr(self, "_decision_flow_viz_panel", None)
                if flow is not None and getattr(flow, "is_playing", None) and flow.is_playing():
                    return
                # No playback started (no path), proceed to next record.
                self._demo_waiting_flow_playback = False
                self._status_bar.showMessage("自动演示：准备下一条…")
                self._schedule_next_auto_demo()

            # Give _present_decision_flow_playback() a moment to start play_path().
            QTimer.singleShot(450, _fallback_if_no_flow_started)

    def _on_demo_flow_playback_finished(self) -> None:
        """After flow-viz playback completes, return to stream in auto demo mode."""
        if not getattr(self, "_demo_mode", False):
            return
        if getattr(self, "_demo_mode_kind", None) != "auto":
            return
        sidebar = getattr(self, "_ai_sidebar", None)
        if sidebar is not None:
            sidebar.focus_stream()
        if getattr(self, "_demo_waiting_flow_playback", False):
            self._demo_waiting_flow_playback = False
            self._status_bar.showMessage("自动演示：准备下一条…")
            self._schedule_next_auto_demo()

    def _exit_demo_mode(self, *, silent: bool = False) -> None:
        """Leave demo mode and restore live controls."""
        from pathlib import Path

        self._demo_auto_next_armed = False
        self._demo_waiting_flow_playback = False
        if self._demo_replayer is not None:
            self._demo_replayer.stop()
            self._demo_replayer.deleteLater()
            self._demo_replayer = None

        was_demo = self._demo_mode
        self._demo_mode = False
        self._demo_mode_kind = None
        self._demo_record_path = None
        self._demo_btn.setText("演示模式")
        self._demo_mode_label.hide()
        self._analysis_in_progress = False
        self._set_chart_refresh_paused(False)
        self._update_submit_button_state()
        self._decision_badge.setText("")

        if was_demo and not silent:
            self._status_bar.showMessage("已退出演示模式")
            self._refresh_chart_once()

    def _on_submit_analysis(self) -> None:
        """Handle the '提交分析' button click."""
        if not self._can_submit():
            return

        # Cancel any existing worker before starting a new one
        if self._worker is not None and self._worker.isRunning():
            if self._cancel_token is not None:
                self._cancel_token.set()
            self._worker.wait(_WORKER_JOIN_TIMEOUT_MS)
            self._worker = None

        symbol = self._symbol_combo.currentText().strip()
        timeframe = self._tf_combo.currentText()
        bar_count = self._bar_count_spin.value()

        if self._wait_close_checkbox.isChecked():
            if not self._arm_wait_for_bar_close(symbol, timeframe, bar_count):
                return
            return

        self._start_analysis(symbol, timeframe, bar_count)

    def _start_analysis(self, symbol: str, timeframe: str, bar_count: int) -> None:
        """Build snapshot and run two-stage analysis (after optional bar-close wait)."""
        frame = self._take_snapshot(symbol, timeframe, bar_count)
        if frame is None:
            self._status_bar.showMessage("数据不足，请等待缓冲区填满后再提交")
            return

        orchestrator = self._build_orchestrator()
        if orchestrator is None:
            self._status_bar.showMessage("编排器未就绪，请检查设置")
            return

        # Create cancel token
        from pa_agent.util.threading import CancelToken

        self._cancel_token = CancelToken()

        # Start worker in its own QThread (worker IS a QThread subclass)
        self._worker = _AnalysisWorker(
            orchestrator=orchestrator,
            frame=frame,
            cancel_token=self._cancel_token,
            parent=None,
        )
        self._worker.finished.connect(self._on_analysis_finished)
        self._worker.record_ready.connect(self._on_record_ready)
        self._worker.status_update.connect(self._on_status_update)
        self._worker.finished.connect(lambda _: self._on_worker_done())

        panel = getattr(self, "_stream_panel", None)
        if panel is not None:
            self._worker.stage_prompt_ready.connect(panel.on_stage_prompt_ready)
            self._worker.reasoning_token.connect(panel.on_reasoning_token)
            self._worker.content_token.connect(panel.on_content_token)

        # Freeze chart on the exact frame sent to the AI (avoids K1 = forming vs K1 = closed mismatch).
        self._chart_widget.set_frame(frame)

        self._set_chart_refresh_paused(True)

        self._analysis_in_progress = True
        self._update_submit_button_state()
        self._status_bar.showMessage(
            "分析中…（图表已冻结，K1=最新已收盘K线，与提交给 AI 的数据一致）"
        )
        self._decision_badge.setText("分析中…")
        self._ai_sidebar.focus_stream()

        panel = getattr(self, "_stream_panel", None)
        if panel is not None:
            panel.clear()
            panel.on_analysis_started()
        debug = getattr(self, "_debug_widget", None)
        if debug is not None:
            debug.clear()

        tree_panel = getattr(self, "_decision_tree_panel", None)
        if tree_panel is not None:
            tree_panel.clear()
            flow_viz = getattr(self, "_decision_flow_viz_panel", None)
            if flow_viz is not None:
                flow_viz.clear()

        pf = getattr(self, "_prompt_files_panel", None)
        if pf is not None:
            from pa_agent.ai.prompt_assembler import stage1_prompt_txt_files

            pf.clear()
            pf.set_stage1_files(stage1_prompt_txt_files())
            pf.set_extras(stage1_builtin=True)

        self._worker.stage2_files_ready.connect(
            self._on_stage2_files_ready,
            Qt.ConnectionType.UniqueConnection,
        )
        self._worker.start()

    def _on_stage2_files_ready(self, strategy_files: list) -> None:
        """Update 调试 tab when Stage 2 strategy .txt list is known."""
        pf = getattr(self, "_prompt_files_panel", None)
        if pf is None:
            return
        from pa_agent.ai.prompt_assembler import stage2_prompt_txt_files

        pf.set_stage2_files(stage2_prompt_txt_files(strategy_files))
        pf.set_extras(stage1_builtin=True, stage2_builtin=True)

    def _on_analysis_finished(self, decision: dict) -> None:
        """Called on the main thread when the AI worker completes.

        *decision* is the full stage2 JSON dict (``{"decision": {...},
        "diagnosis_summary": {...}}``).  The chart and panel widgets expect
        the inner ``decision`` sub-dict, so we extract it here.
        """
        if decision:
            inner = decision.get("decision", decision)
            self._chart_widget.set_decision(inner)
            self._decision_panel.set_decision(
                inner,
                diagnosis_summary=decision.get("diagnosis_summary"),
                stage1_diagnosis=self._last_stage1_diagnosis,
            )
            self._bind_decision_tree(decision, self._last_stage1_diagnosis)
            order = inner.get("order_type", "—")
            self._decision_badge.setText(f"决策: {order}")
            if getattr(self, "_demo_mode", False):
                self._present_decision_flow_playback(force_play=True)
        else:
            self._decision_panel.clear()
            self._decision_tree_panel.clear()
            if getattr(self, "_decision_flow_viz_panel", None) is not None:
                self._decision_flow_viz_panel.clear()
            self._decision_badge.setText("")

    def _on_record_ready(self, record: Any) -> None:
        """Push the full AnalysisRecord to the conversation and debug tabs."""
        import json as _json

        # ── Debug tab: add Stage1 and Stage2 turns ────────────────────────────
        debug = getattr(self, "_debug_widget", None)
        if debug is not None:
            # Stage 1 turn
            s1_msgs = getattr(record, "stage1_messages", []) or []
            s1_system = next((m.get("content", "") for m in s1_msgs if m.get("role") == "system"), "")
            s1_user = next((m.get("content", "") for m in s1_msgs if m.get("role") == "user"), "")
            s1_raw = getattr(record, "stage1_response", {}) or {}
            s1_diag = getattr(record, "stage1_diagnosis", None)
            s1_validation = _json.dumps(s1_diag, ensure_ascii=False, indent=2) if s1_diag else "（验证失败或无数据）"
            debug.add_turn({
                "label": "Stage1 诊断",
                "system_prompt": s1_system,
                "user_prompt": s1_user,
                "raw_response": s1_raw,
                "validation_info": s1_validation,
            })

            # Stage 2 turn
            s2_msgs = getattr(record, "stage2_messages", []) or []
            s2_system = next((m.get("content", "") for m in s2_msgs if m.get("role") == "system"), "")
            s2_user = next((m.get("content", "") for m in s2_msgs if m.get("role") == "user"), "")
            s2_raw = getattr(record, "stage2_response", {}) or {}
            s2_decision = getattr(record, "stage2_decision", None)
            s2_validation = _json.dumps(s2_decision, ensure_ascii=False, indent=2) if s2_decision else "（验证失败或无数据）"
            debug.add_turn({
                "label": "Stage2 决策",
                "system_prompt": s2_system,
                "user_prompt": s2_user,
                "raw_response": s2_raw,
                "validation_info": s2_validation,
            })

            # Exception info if any
            exc_info = getattr(record, "exception", None)
            if exc_info:
                debug.add_turn({
                    "label": "⚠ 异常",
                    "system_prompt": "",
                    "user_prompt": "",
                    "raw_response": {},
                    "validation_info": _json.dumps(exc_info, ensure_ascii=False, indent=2),
                })

        pf = getattr(self, "_prompt_files_panel", None)
        if pf is not None:
            from pa_agent.ai.prompt_assembler import (
                stage1_prompt_txt_files,
                stage2_prompt_txt_files,
            )

            strategy = getattr(record, "strategy_files_used", None) or []
            experience = getattr(record, "experience_loaded", None) or []
            pf.set_latest_run(
                stage1_prompt_txt_files(),
                stage2_prompt_txt_files(strategy),
                experience_count=len(experience),
            )

        s1_diag = getattr(record, "stage1_diagnosis", None) or {}
        # Cache for _on_analysis_finished (which fires after this)
        self._last_stage1_diagnosis = s1_diag if isinstance(s1_diag, dict) else None
        s2_full = getattr(record, "stage2_decision", None)
        if s2_full:
            inner = s2_full.get("decision", s2_full)
            self._decision_panel.set_decision(
                inner,
                diagnosis_summary=s2_full.get("diagnosis_summary"),
                stage1_diagnosis=s1_diag if isinstance(s1_diag, dict) else None,
            )
            self._bind_decision_tree(
                s2_full,
                s1_diag if isinstance(s1_diag, dict) else None,
            )

        panel = getattr(self, "_stream_panel", None)
        if panel is not None:
            s1_diag = getattr(record, "stage1_diagnosis", None)
            if s1_diag:
                s1_content = _json.dumps(s1_diag, ensure_ascii=False, indent=2)
                s1_raw = getattr(record, "stage1_response", {}) or {}
                s1_reasoning = ""
                if isinstance(s1_raw, dict):
                    choices = s1_raw.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        s1_reasoning = msg.get("reasoning_content", "") or ""
                panel.show_stage_result("阶段一：市场诊断", s1_content, s1_reasoning)

            s2_decision = getattr(record, "stage2_decision", None)
            if s2_decision:
                s2_content = _json.dumps(s2_decision, ensure_ascii=False, indent=2)
                s2_raw = getattr(record, "stage2_response", {}) or {}
                s2_reasoning = ""
                if isinstance(s2_raw, dict):
                    choices = s2_raw.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        s2_reasoning = msg.get("reasoning_content", "") or ""
                panel.show_stage_result("阶段二：交易决策", s2_content, s2_reasoning)

            if getattr(self, "_demo_mode", False):
                panel.on_record_saved()
                panel.set_input_enabled(False)
                usage_total = getattr(record, "usage_total", {}) or {}
                if usage_total:
                    settings = getattr(self._ctx, "settings", None)
                    context_window = 1_000_000
                    if settings is not None:
                        context_window = (
                            getattr(settings.provider, "context_window", 1_000_000)
                            or 1_000_000
                        )
                    prompt_tokens = usage_total.get("prompt_tokens", 0)
                    cached_tokens = usage_total.get("cached_prompt_tokens", 0)
                    completion_tokens = usage_total.get("completion_tokens", 0)
                    total_tokens = usage_total.get("total_tokens", 0) or (
                        prompt_tokens + completion_tokens
                    )
                    panel.update_token_display(
                        {
                            "context_used": total_tokens,
                            "context_window": context_window,
                            "total_input": prompt_tokens,
                            "total_cached_input": cached_tokens,
                            "total_output": completion_tokens,
                        }
                    )
                return

            # ── Create FreeChatSession and wire to stream panel ───────────────
            try:
                from pa_agent.orchestrator.free_chat import FreeChatSession
                from pa_agent.util.threading import CancelToken as _CancelToken

                client = getattr(self._ctx, "client", None)
                assembler = getattr(self._ctx, "assembler", None)
                pending_writer = getattr(self._ctx, "pending_writer", None)
                ledger = getattr(self._ctx, "ledger", None)
                settings = getattr(self._ctx, "settings", None)

                if all(x is not None for x in [client, assembler, pending_writer, ledger]):
                    # Build a snapshot function that returns the latest closed K-line data
                    kline_snapshot_fn = self._make_kline_snapshot_fn()

                    session = FreeChatSession(
                        base_record=record,
                        client=client,
                        assembler=assembler,
                        pending_writer=pending_writer,
                        ledger=ledger,
                        settings=settings,
                        kline_snapshot_fn=kline_snapshot_fn,
                    )
                    chat_cancel_token = _CancelToken()
                    panel.set_session(session, chat_cancel_token)
                    logger.info("FreeChatSession created for record %s", getattr(record.meta, "timestamp_local_iso", "?"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to create FreeChatSession: %s", exc)

            panel.on_record_saved()

            usage_total = getattr(record, "usage_total", {}) or {}
            if usage_total:
                settings = getattr(self._ctx, "settings", None)
                context_window = 1_000_000
                if settings is not None:
                    context_window = getattr(settings.provider, "context_window", 1_000_000) or 1_000_000

                prompt_tokens = usage_total.get("prompt_tokens", 0)
                cached_tokens = usage_total.get("cached_prompt_tokens", 0)
                completion_tokens = usage_total.get("completion_tokens", 0)
                total_tokens = usage_total.get("total_tokens", 0) or (prompt_tokens + completion_tokens)

                panel.update_token_display({
                    "context_used": total_tokens,
                    "context_window": context_window,
                    "total_input": prompt_tokens,
                    "total_cached_input": cached_tokens,
                    "total_output": completion_tokens,
                })

    def _bind_decision_tree(
        self,
        stage2_full: dict,
        stage1_diagnosis: dict | None,
    ) -> None:
        """Push gate + decision traces to the decision tree tab."""
        panel = getattr(self, "_decision_tree_panel", None)
        if panel is None:
            return
        s1 = stage1_diagnosis or {}
        trace_kw = dict(
            gate_trace=s1.get("gate_trace"),
            decision_trace=stage2_full.get("decision_trace"),
            terminal=stage2_full.get("terminal"),
            gate_result=s1.get("gate_result"),
            gate_shortcircuited=bool(stage2_full.get("gate_shortcircuited")),
        )
        panel.set_trace(**trace_kw)
        flow_viz = getattr(self, "_decision_flow_viz_panel", None)
        has_path = False
        if flow_viz is not None:
            has_path = bool(flow_viz.set_trace(**trace_kw))
        if has_path and flow_viz is not None:
            # 演示模式：等 finished 回调后再切「决策树可视化」，与真实流式结束顺序一致
            if getattr(self, "_demo_mode", False):
                pass
            elif flow_viz.should_auto_play_after_load():
                self._present_decision_flow_playback(force_play=False)

    def _trigger_decision_flow_playback(self) -> None:
        """Switch to flow viz tab and play path (settings button or auto)."""
        self._present_decision_flow_playback(force_play=True)

    def _present_decision_flow_playback(self, *, force_play: bool = False) -> None:
        """Show decision-flow tab, then start path animation."""
        from PyQt6.QtCore import QTimer

        flow_viz = getattr(self, "_decision_flow_viz_panel", None)
        sidebar = getattr(self, "_ai_sidebar", None)
        if flow_viz is None or sidebar is None:
            return
        if not force_play and not flow_viz.should_auto_play_after_load():
            return
        sidebar.focus_decision_flow_viz()
        QTimer.singleShot(120, flow_viz.play_path)

    def _on_worker_done(self) -> None:
        """Reset in-progress flag and re-enable the submit button."""
        self._analysis_in_progress = False
        self._worker = None
        self._update_submit_button_state()
        self._status_bar.showMessage("分析完成")

    def _open_settings_dialog(self) -> None:
        """Open the SettingsDialog; import lazily to avoid circular imports."""
        from pa_agent.gui.settings_dialog import SettingsDialog
        from pa_agent.config.settings import Settings

        settings: Settings = self._ctx.settings  # type: ignore[assignment]
        if settings is None:
            settings = Settings()

        dlg = SettingsDialog(settings, parent=self)
        dlg.set_decision_flow_play_handler(self._trigger_decision_flow_playback)
        if dlg.exec():
            self._ctx.settings = settings
            client = getattr(self._ctx, "client", None)
            if client is not None:
                try:
                    client._settings = settings.provider  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            if settings is not None:
                key = getattr(settings.provider, "api_key", "") or ""
                self._debug_widget._api_key = key
                self._ai_sidebar.bind_settings(settings)
            self._update_ai_mode_label()

    def _update_ai_mode_label(self) -> None:
        """Show current thinking / reasoning_effort / model in the toolbar."""
        settings = getattr(self._ctx, "settings", None)
        if settings is None:
            self._ai_mode_label.setText("")
            return
        p = settings.provider
        thinking = "开" if p.thinking else "关"
        self._ai_mode_label.setText(
            f"思考: {thinking} · effort={p.reasoning_effort} · {p.model}"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _can_submit(self) -> bool:
        """Return True if the submit button should be enabled."""
        return self._submit_block_reason() is None

    def _on_exception_streak_reset(self) -> None:
        """Re-enable submit after user clears validation error streak (原始 tab)."""
        self._sync_submit_button_state()
        if getattr(self, "_status_bar", None) is not None:
            self._status_bar.showMessage("连续异常计数已清除，可以重新提交分析")

    def _submit_block_reason(self) -> str | None:
        """Human-readable reason when submit is disabled, or None if allowed."""
        if self._demo_mode:
            return "演示模式中，请退出演示后再提交真实分析"
        if self._analysis_in_progress:
            return "分析进行中"
        if self._pending_submit_after_close:
            return "等待最新K线收盘"
        if self._switching:
            return "正在切换品种/周期"
        exc_count = self._get_consecutive_count()
        if exc_count >= 2:
            return (
                f"连续 JSON 校验失败 {exc_count} 次（已达上限 2）。"
                "请到「原始」页点击「清除连续异常计数」，或删除 config/exception_state.json 后重启"
            )
        return None

    def _sync_submit_button_state(self) -> None:
        """Enable submit button and surface why it may be locked."""
        if not hasattr(self, "_submit_btn"):
            return
        reason = self._submit_block_reason()
        can = reason is None
        self._submit_btn.setEnabled(can)
        if can:
            self._submit_btn.setToolTip("")
        else:
            self._submit_btn.setToolTip(reason or "")
            status_bar = getattr(self, "_status_bar", None)
            if status_bar is not None and reason and "连续 JSON" in reason:
                cur = status_bar.currentMessage() or ""
                if cur in ("就绪", "") or "连续" in cur or "提交分析已锁定" in cur:
                    status_bar.showMessage(f"提交分析已锁定：{reason}")

    def _update_submit_button_state(self) -> None:
        """Enable or disable the submit button based on current state."""
        self._sync_submit_button_state()

    def _get_consecutive_count(self) -> int:
        """Return the current consecutive exception count (0 if unavailable)."""
        try:
            exc_counter = getattr(self._ctx, "exc_counter", None)
            if exc_counter is not None:
                return exc_counter.consecutive_count
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _build_chart_frame_from_bars(
        self,
        bars_raw: Any,
        *,
        bar_count: int | None = None,
        include_forming: bool = True,
    ) -> Any:
        """Build chart KlineFrame.

        - include_forming=True: show forming bar + N closed bars (live UI)
        - include_forming=False: closed-only (matches AI snapshot semantics)
        """
        from pa_agent.data.snapshot import build_display_frame, build_live_frame

        n = bar_count if bar_count is not None else self._bar_count_spin.value()
        symbol = self._symbol_combo.currentText().strip()
        timeframe = self._tf_combo.currentText()
        if not bars_raw:
            return None
        if include_forming:
            return build_live_frame(bars_raw, n, symbol, timeframe)
        return build_display_frame(bars_raw, n, symbol, timeframe)

    def _take_snapshot(self, symbol: str, timeframe: str, bar_count: int) -> Any:
        """Snapshot for analysis: *bar_count* closed bars (newest forming bar excluded)."""
        try:
            data_source = getattr(self._ctx, "data_source", None)
            if data_source is None or not getattr(data_source, "_connected", False):
                return None

            bars_raw = data_source.latest_snapshot(bar_count + 5)
            if not bars_raw:
                return None

            return self._build_chart_frame_from_bars(
                bars_raw,
                bar_count=bar_count,
                include_forming=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Snapshot failed: %s", exc)
            return None

    def _make_kline_snapshot_fn(self) -> Any:
        """Return a callable that captures the latest closed K-line data as a text table.

        The returned function reads from the live data source at call time,
        so FreeChatSession always gets the most recent market data when the
        user sends a follow-up message.
        """
        from pa_agent.ai.prompt_assembler import PromptAssembler

        symbol = self._symbol_combo.currentText()
        timeframe = self._tf_combo.currentText()
        bar_count = self._bar_count_spin.value()

        def _snapshot() -> str:
            frame = self._take_snapshot(symbol, timeframe, bar_count)
            if frame is None:
                return ""
            return PromptAssembler._render_kline_table(frame)

        return _snapshot

    def _build_orchestrator(self) -> Any:
        """Build a TwoStageOrchestrator from ctx components, or return None."""
        try:
            from pa_agent.orchestrator.two_stage import TwoStageOrchestrator

            client = getattr(self._ctx, "client", None)
            assembler = getattr(self._ctx, "assembler", None)
            router = getattr(self._ctx, "router", None)
            validator = getattr(self._ctx, "validator", None)
            exc_counter = getattr(self._ctx, "exc_counter", None)
            pending_writer = getattr(self._ctx, "pending_writer", None)
            exp_reader = getattr(self._ctx, "exp_reader", None)
            settings = getattr(self._ctx, "settings", None)

            if any(
                x is None
                for x in [client, assembler, router, validator, exc_counter,
                           pending_writer, exp_reader]
            ):
                return None

            return TwoStageOrchestrator(
                client=client,
                assembler=assembler,
                router=router,
                validator=validator,
                exc_counter=exc_counter,
                pending_writer=pending_writer,
                exp_reader=exp_reader,
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not build orchestrator: %s", exc)
            return None
