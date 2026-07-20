"""Main application window for PA Agent."""
from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import QThread, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QCloseEvent, QShowEvent
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
from PyQt6.QtCore import Qt

from pa_agent.ai.response_extract import reasoning_from_response
from pa_agent.app_context import AppContext
from pa_agent.gui.validation_debug_dialog import show_validation_debug_dialog

logger = logging.getLogger(__name__)

# Zombie timeout in milliseconds (5 seconds)
_WORKER_JOIN_TIMEOUT_MS = 5000


def _qobject_alive(obj: QObject | None) -> bool:
    """Return False when the underlying Qt C++ object has been destroyed."""
    if obj is None:
        return False
    try:
        from PyQt6 import sip

        return not sip.isdeleted(obj)
    except (ImportError, RuntimeError, TypeError):
        return True


def _parse_sr_price(raw: object) -> float | None:
    """Parse a support/resistance price string from the AI output.

    Accepts single values (``"5402"``, ``5402``) and range strings
    (``"5380-5400"``).  Returns the midpoint for ranges, or the value
    itself for single prices.  Returns None on parse failure.
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


def _best_probability_key(probs: dict) -> str | None:
    """Return the key with highest numeric probability; tolerate bad values."""
    best_key: str | None = None
    best_val = -1
    for key, raw in probs.items():
        try:
            val = int(raw)
        except (TypeError, ValueError):
            continue
        if val > best_val:
            best_val = val
            best_key = str(key)
    return best_key


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
    error_occurred = pyqtSignal(str)    # unhandled worker/orchestrator failure
    status_update = pyqtSignal(str)
    reasoning_token = pyqtSignal(str, str)   # (stage, chunk)
    content_token = pyqtSignal(str, str)     # (stage, chunk)
    stage_prompt_ready = pyqtSignal(str, str, str)  # (stage, system, user)
    stage2_files_ready = pyqtSignal(list)  # strategy .txt filenames for stage 2
    retry_occurred = pyqtSignal(str)  # stage ("stage1" or "stage2")

    def __init__(
        self,
        orchestrator: Any,
        frame: Any,
        cancel_token: Any,
        previous_record: Any = None,
        incremental_new_bar_count: int | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._orchestrator = orchestrator
        self._frame = frame
        self._cancel_token = cancel_token
        self._previous_record = previous_record
        self._incremental_new_bar_count = incremental_new_bar_count

    def _persist_program_error_record(self, exc: Exception) -> Any:
        """Write a minimal failed record to pending when submit() raises unexpectedly."""
        try:
            from pa_agent.orchestrator.two_stage import _build_empty_record

            settings = getattr(self._orchestrator, "_settings", None)
            pending_writer = getattr(self._orchestrator, "_pending_writer", None)
            if pending_writer is None:
                return None
            record = _build_empty_record(self._frame, settings)
            record = record.model_copy(
                update={
                    "exception": {
                        "type": "program_error",
                        "stage": "unknown",
                        "message": str(exc),
                    }
                }
            )
            pending_writer.save_partial(record, "program_error")
            return record
        except Exception as save_exc:  # noqa: BLE001
            logger.warning("Failed to persist program_error record: %s", save_exc)
            return None

    def run(self) -> None:
        from pa_agent.util.threading import OrchestratorEvent

        _EVENT_LABELS = {
            OrchestratorEvent.Stage1Started: "阶段一分析中…",
            OrchestratorEvent.Stage1Retry: "阶段一重试",
            OrchestratorEvent.Stage1Done: "阶段一完成",
            OrchestratorEvent.Stage2Started: "阶段二分析中…",
            OrchestratorEvent.Stage2Retry: "阶段二重试",
            OrchestratorEvent.Stage2Done: "阶段二完成",
            OrchestratorEvent.RecordSaved: "记录已保存",
            OrchestratorEvent.Cancelled: "已取消",
            OrchestratorEvent.Stage1Failed: "阶段一失败",
            OrchestratorEvent.Stage2Failed: "阶段二失败",
        }

        def on_event(event: OrchestratorEvent) -> None:
            label = _EVENT_LABELS.get(event, str(event))
            self.status_update.emit(label)
            if event == OrchestratorEvent.Stage1Retry:
                self.retry_occurred.emit("stage1")
            elif event == OrchestratorEvent.Stage2Retry:
                self.retry_occurred.emit("stage2")

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
                previous_record=self._previous_record,
                incremental_new_bar_count=self._incremental_new_bar_count,
            )
            decision = record.stage2_decision or {}
        except Exception as exc:  # noqa: BLE001
            from pa_agent.ai.deepseek_client import CancelledError as _CancelledError
            if isinstance(exc, _CancelledError):
                logger.info("Analysis worker cancelled: %s", exc)
                decision = {}
                record = None  # type: ignore[assignment]
            else:
                logger.error("Analysis worker error: %s", exc, exc_info=True)
                decision = {}
                record = self._persist_program_error_record(exc)
            self.error_occurred.emit(str(exc))

        if record is not None:
            self.record_ready.emit(record)
        self.finished.emit(decision)


# ── MainWindow ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    """Top-level workbench: chart + AI sidebar (analysis / raw / decision)."""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            "PA Agent — Trading Terminal（分析仅供参考，不构成投资建议）"
        )
        self.resize(1440, 900)
        self._ctx = ctx
        self._worker: _AnalysisWorker | None = None
        self._analysis_worker_id: object | None = None
        self._prep_worker: Any = None
        self._prep_worker_id: object | None = None
        self._cancel_token: Any = None
        self._window_closing = False
        self._analysis_in_progress = False
        self._last_analysis_had_error = False
        self._switching = False
        self._chart_refresh_paused = False
        self._pending_submit_after_close = False
        self._pending_force_incremental = False
        self._wait_forming_ts: int | None = None
        self._pending_submit_symbol = ""
        self._pending_submit_timeframe = ""
        self._pending_submit_bar_count = 0
        self._last_forming_ts_open: int | None = None
        self._last_frame_ready_bars: list[Any] | None = None
        self._auto_incremental_pending: bool = False
        self._incremental_available: bool = False  # drives submit button label
        self._keep_analysis_last_closed_ts: int | None = None  # tracks last closed bar for keep-analysis
        self._keep_analysis_submit_closed_ts: int | None = None  # closed bar ts at analysis submit time
        self._free_chat_session: Any = None
        self._last_stage1_diagnosis: dict | None = None
        self._last_analysis_record: Any = None
        self._last_analysis_frame: Any = None  # KlineFrame of the most recent analysis
        self._analysis_previous_record: Any = None  # incremental base record for chart continuity
        self._demo_mode = False
        self._demo_mode_kind: str | None = None  # manual | auto
        self._demo_record_path: str | None = None
        self._demo_replayer: Any = None
        self._demo_auto_next_armed = False
        self._demo_waiting_flow_playback = False
        self._startup_api_key_check_done = False
        self._startup_tv_connectivity_check_done = False
        self._symbol_switch_timer: QTimer | None = None
        self._pending_symbol_switch: tuple[str, str] | None = None
        # RefreshLoop runs in its own QThread
        self._refresh_loop: Any = None
        self._refresh_thread: QThread | None = None
        # Pre-create status bar so any early callbacks don't hit AttributeError.
        # _setup_ui() will replace this with the real QStatusBar instance.
        self._status_bar: QStatusBar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._setup_ui()
        self._connect_event_bus()
        self._update_ai_mode_label()
        self._sync_submit_button_state()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        from pa_agent.gui.ai_sidebar import AISidebar

        _api_key = ""
        _settings = getattr(self._ctx, "settings", None)
        if _settings is not None:
            _api_key = getattr(_settings.provider, "api_key", "") or ""

        self._ai_sidebar = AISidebar(
            api_key=_api_key,
            settings=_settings,
        )
        self._stream_panel = self._ai_sidebar.stream
        self._debug_widget = self._ai_sidebar.debug
        self._prompt_files_panel = self._ai_sidebar.prompt_files
        self._decision_panel = self._ai_sidebar.decision
        self._future_trend_panel = self._ai_sidebar.future_trend
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
        self._refresh_api_key_ui_state()

        # ── Menu bar ─── 顶层直接触发按钮 + 演示模式下拉 ────────────────────
        menu_bar: QMenuBar = self.menuBar()  # type: ignore[assignment]

        # 1. AI 模型设置 — 点击直接弹对话框（无下拉）
        _ai_model_action = QAction("AI 模型设置", self)
        _ai_model_action.triggered.connect(self._open_ai_model_settings_dialog)
        menu_bar.addAction(_ai_model_action)

        # 2. 飞书发送通知设置 — 点击直接弹对话框（无下拉）
        _feishu_action = QAction("飞书发送通知设置", self)
        _feishu_action.triggered.connect(self._open_feishu_settings_dialog)
        menu_bar.addAction(_feishu_action)

        # 3. 其他通用设置 — 点击直接弹对话框（无下拉）
        _general_action = QAction("其他通用设置", self)
        _general_action.triggered.connect(self._open_general_settings_dialog)
        menu_bar.addAction(_general_action)

        # 4. 演示模式 — 保留下拉菜单
        demo_menu = menu_bar.addMenu("演示模式")
        self._demo_manual_action = QAction("手动选择记录…", self)
        self._demo_manual_action.triggered.connect(lambda: self._on_demo_menu_action("manual"))
        demo_menu.addAction(self._demo_manual_action)
        self._demo_auto_action = QAction("自动随机记录", self)
        self._demo_auto_action.triggered.connect(lambda: self._on_demo_menu_action("auto"))
        demo_menu.addAction(self._demo_auto_action)
        demo_menu.addSeparator()
        self._demo_exit_action = QAction("退出演示模式", self)
        self._demo_exit_action.triggered.connect(self._exit_demo_mode)
        self._demo_exit_action.setEnabled(False)
        demo_menu.addAction(self._demo_exit_action)

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

        _settings = getattr(self._ctx, "settings", None)
        _last_symbol = "XAUUSDm"
        _last_tf = "15m"
        if _settings is not None:
            _last_symbol = getattr(_settings.general, "last_symbol", "XAUUSDm") or "XAUUSDm"
            _last_tf = getattr(_settings.general, "last_timeframe", "15m") or "15m"

        # Data source
        from pa_agent.data.factory import DATA_SOURCE_CHOICES, normalize_data_source_kind

        _last_ds = "mt5"
        if _settings is not None:
            _last_ds = normalize_data_source_kind(
                getattr(_settings.general, "last_data_source", "mt5")
            )
        # 如果上次保存的数据源不在 UI 可选列表中, 强制回退 MT5
        _ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
        if _last_ds not in _ui_kinds:
            _last_ds = "mt5"
        self._active_data_source_kind = _last_ds

        ctrl_layout.addWidget(QLabel("数据来源:"))
        self._data_source_combo = QComboBox()
        for kind, label in DATA_SOURCE_CHOICES:
            self._data_source_combo.addItem(label, kind)
        ds_index = self._data_source_combo.findData(_last_ds)
        if ds_index >= 0:
            self._data_source_combo.setCurrentIndex(ds_index)
        self._data_source_combo.setMinimumWidth(108)
        self._data_source_combo.setToolTip(
            "K 线数据来源：MT5（需终端登录）、TradingView（tvDatafeed）、"
            "本地仅支持 MT5 与 TradingView"
        )
        self._data_source_combo.currentIndexChanged.connect(
            self._on_data_source_combo_changed
        )
        ctrl_layout.addWidget(self._data_source_combo)

        # TradingView exchange is forced to «auto» whenever the data source is TV.
        # We still keep the field visible for clarity, but it is not user-editable.

        self._tv_exchange_label = QLabel("交易所:")
        self._tv_exchange_combo = QComboBox()
        self._tv_exchange_combo.setEditable(False)
        self._tv_exchange_combo.setMinimumWidth(96)
        self._tv_exchange_combo.setToolTip(
            "现货黄金（已实测可用）：\n"
            "· OANDA / PEPPERSTONE / FOREXCOM + XAUUSD\n"
            "· TVC / CAPITALCOM + GOLD（勿用 TVC:XAUUSD，无效）\n"
            "A 股 / 港股 / 名称（AkShare 不可用时）：\n"
            "· 「（自动）」：黄金/外汇依次试 OANDA、PEPPERSTONE、FOREXCOM、FX、TVC、CAPITALCOM；"
            "A 股试 SSE/SZSE，港股试 HKEX\n"
            "· 港股代码勿加前导零（1810 非 01810）；可输入名称如 小米集团\n"
            "· 自定义别名：config/tv_symbol_aliases.json"
        )
        from pa_agent.data.tradingview import TV_EXCHANGE_PRESETS

        # Display labels with category hints (crypto exchanges get no suffix)
        _EXCHANGE_LABELS: dict[str, str] = {
            "SSE":       "SSE（A股）",
            "SZSE":      "SZSE（A股）",
            "HKEX":      "HKEX（港股）",
            "NYSE":      "NYSE（美股）",
            "NASDAQ":    "NASDAQ（美股）",
            "SP":        "SP（美股指数）",
            "OANDA":     "OANDA（外汇）",
            "PEPPERSTONE": "PEPPERSTONE（外汇）",
            "FOREXCOM":  "FOREXCOM（外汇）",
            "FX":        "FX（外汇）",
            "TVC":       "TVC（商品/指数）",
            "CAPITALCOM": "CAPITALCOM（商品/外汇）",
            "CBOT":      "CBOT（期货）",
            "CME_MINI":  "CME_MINI（期货）",
            "":          "（自动）",
        }

        for ex in TV_EXCHANGE_PRESETS:
            label = _EXCHANGE_LABELS.get(ex, ex)
            self._tv_exchange_combo.addItem(label, ex)
        # Restore saved exchange from settings, default to auto.
        saved_ex = ""
        try:
            from pa_agent.config.settings import load_settings
            from pa_agent.config.paths import SETTINGS_JSON_PATH
            _s = load_settings(SETTINGS_JSON_PATH)
            saved_ex = getattr(_s.general, 'last_tradingview_exchange', '') or ''
        except Exception:
            pass
        idx_ex = self._tv_exchange_combo.findData(saved_ex)
        if idx_ex < 0:
            idx_ex = self._tv_exchange_combo.findData("")
        if idx_ex >= 0:
            self._tv_exchange_combo.setCurrentIndex(idx_ex)
        self._tv_exchange_combo.currentIndexChanged.connect(
            self._on_tv_exchange_changed
        )
        ctrl_layout.addWidget(self._tv_exchange_label)
        ctrl_layout.addWidget(self._tv_exchange_combo)

        # 品种选择 (仅东方财富期货数据源显示, 两级选择: 品种→合约)
        self._variety_label = QLabel("品种:")
        self._variety_combo = QComboBox()
        self._variety_combo.setMinimumWidth(120)
        self._variety_combo.setToolTip("选择品种大类, 再在右侧选择具体合约")
        self._variety_combo.currentIndexChanged.connect(
            self._on_variety_combo_changed
        )
        ctrl_layout.addWidget(self._variety_label)
        ctrl_layout.addWidget(self._variety_combo)

        # 合约/品种 — editable combo (user can type any symbol)
        self._symbol_label = QLabel("合约:")
        ctrl_layout.addWidget(self._symbol_label)
        self._symbol_combo = QComboBox()
        self._symbol_combo.setEditable(True)
        self._symbol_combo.setCurrentText(_last_symbol)
        self._symbol_combo.setMinimumWidth(110)
        self._apply_data_source_symbol_placeholder()
        ctrl_layout.addWidget(self._symbol_combo)
        self._populate_symbol_combo_for_source()

        self._symbol_alert_label = QLabel("")
        self._symbol_alert_label.setStyleSheet("color: #f85149; font-size: 11px;")
        self._symbol_alert_label.setWordWrap(True)
        self._symbol_alert_label.hide()
        ctrl_layout.addWidget(self._symbol_alert_label)

        # Timeframe
        ctrl_layout.addWidget(QLabel("周期:"))
        self._tf_combo = QComboBox()
        self._tf_combo.addItems(["1m", "5m", "15m", "1h", "4h", "1d"])
        self._tf_combo.setCurrentText(_last_tf)
        self._tf_combo.setMinimumWidth(60)
        ctrl_layout.addWidget(self._tf_combo)
        self._populate_timeframe_combo_for_source()
        self._sync_tv_exchange_visibility()

        ctrl_layout.addStretch()

        self._fetch_data_btn = QPushButton("获取数据")
        self._fetch_data_btn.setObjectName("primaryButton")
        self._fetch_data_btn.setMinimumWidth(90)
        self._fetch_data_btn.setToolTip("开始从当前数据源持续拉取 K 线数据并实时更新图表")
        self._fetch_data_btn.clicked.connect(self._on_fetch_data_clicked)
        ctrl_layout.addWidget(self._fetch_data_btn)

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

        # Incremental button is kept for programmatic use but hidden from the
        # toolbar — the submit button's label changes to "增量分析" automatically
        # when an eligible prior record is detected.
        self._incremental_submit_btn = QPushButton("增量分析")
        self._incremental_submit_btn.setMinimumWidth(100)
        self._incremental_submit_btn.setToolTip(
            "强制基于同品种/周期最近一条成功记录做增量分析，"
            "不受「增量分析最大新增K线」阈值限制；"
            "若无可用上一轮记录或 K 线无法对齐，将提示失败。"
        )
        self._incremental_submit_btn.clicked.connect(self._on_submit_incremental_analysis)
        self._incremental_submit_btn.hide()

        # 演示模式按钮已移至左上角「设置」菜单，此处保留引用供内部逻辑使用（不加入 ctrl_layout）
        self._demo_btn = QPushButton("演示模式")
        self._demo_btn.setToolTip("用 records/pending 中已保存的分析记录回放界面")
        self._demo_btn.clicked.connect(self._on_demo_mode_button)

        # 持续跟踪分析勾选框：勾选后有新K线收盘时自动开始新一轮分析
        # 每次启动强制为未勾选，避免程序启动时立即自动拉取数据
        self._keep_analysis_checkbox = QCheckBox("持续跟踪分析")
        self._keep_analysis_checkbox.setChecked(False)
        self._keep_analysis_checkbox.setToolTip(
            "勾选后，每当有新的K线收盘时自动开始新一轮分析"
        )
        self._keep_analysis_checkbox.stateChanged.connect(self._on_keep_analysis_checkbox_changed)
        ctrl_layout.addWidget(self._keep_analysis_checkbox)

        # Reset persisted keep_analysis flag so future restarts also start unchecked
        if _settings is not None:
            try:
                _settings.general.keep_analysis = False
            except Exception:  # noqa: BLE001
                pass

        self._resume_chart_btn = QPushButton("图表实时更新")
        self._resume_chart_btn.setEnabled(False)
        self._resume_chart_btn.setToolTip(
            "恢复 K 线实时刷新；最右侧未收盘 K 线为浅色空心 K 线，不参与 AI 分析"
        )
        self._resume_chart_btn.clicked.connect(self._on_resume_chart_refresh)
        ctrl_layout.addWidget(self._resume_chart_btn)

        self._fit_chart_btn = QPushButton("恢复图表")
        self._fit_chart_btn.setToolTip(
            "自动调整图表缩放，将 K 线和价格线适配到可视区域"
        )
        self._fit_chart_btn.clicked.connect(self._on_fit_chart)
        ctrl_layout.addWidget(self._fit_chart_btn)

        self._decision_badge = QLabel("")
        self._decision_badge.setObjectName("mutedLabel")
        ctrl_layout.addWidget(self._decision_badge)

        self._ai_mode_label = QLabel("")
        self._ai_mode_label.setObjectName("mutedLabel")
        ctrl_layout.addWidget(self._ai_mode_label)

        outer_layout.addLayout(ctrl_layout)

        self._api_key_alert_label = QLabel(
            "未配置 API Key：请点击左上角「AI 模型」按钮，在设置中填写 API Key 后才能进行 AI 分析。"
        )
        self._api_key_alert_label.setWordWrap(True)
        self._api_key_alert_label.setStyleSheet(
            "background-color: #3d2a00; color: #ffb86c; padding: 8px 10px; "
            "border: 1px solid #8a6d2f; border-radius: 4px; font-weight: 600;"
        )
        self._api_key_alert_label.hide()
        outer_layout.addWidget(self._api_key_alert_label)

        # Risk disclaimer (UI-only; never included in AI prompts)
        self._disclaimer_label = QLabel("分析仅供参考，不构成投资建议")
        self._disclaimer_label.setObjectName("mutedLabel")
        self._disclaimer_label.setWordWrap(True)
        self._disclaimer_label.setStyleSheet(
            "color: #8b949e; font-size: 11px; padding: 2px 0;"
        )
        outer_layout.addWidget(self._disclaimer_label)

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

        # ── FlowBar: 5-step analysis progress indicator ───────────────────────
        from pa_agent.gui.widgets.flow_bar import FlowBar
        self._flow_bar = FlowBar()
        self._flow_bar.setFixedHeight(52)
        self._flow_bar.setStyleSheet(
            "background-color: #161b22; border-bottom: 1px solid #30363d;"
        )
        outer_layout.addWidget(self._flow_bar)

        workbench = QSplitter(Qt.Orientation.Horizontal)

        self._chart_widget = ChartWidget()
        self._chart_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._apply_chart_display_settings()
        workbench.addWidget(self._chart_widget)

        self._ai_sidebar.setMinimumWidth(400)
        workbench.addWidget(self._ai_sidebar)

        workbench.setStretchFactor(0, 3)
        workbench.setStretchFactor(1, 2)

        # ── SummaryStrip: 5-metric card strip above workbench ─────────────────
        from pa_agent.gui.widgets.summary_strip import SummaryStrip
        self._summary_strip = SummaryStrip()
        outer_layout.addWidget(self._summary_strip)

        outer_layout.addWidget(workbench, stretch=1)

        # Connect symbol/timeframe combo boxes to the switch handler
        self._symbol_switch_timer = QTimer(self)
        self._symbol_switch_timer.setSingleShot(True)
        self._symbol_switch_timer.setInterval(500)
        # Timer no longer auto-triggers symbol switch on text change.
        # It is kept for _update_symbol_data_alert debouncing only.
        self._symbol_switch_timer.timeout.connect(self._update_symbol_data_alert)
        self._symbol_combo.currentTextChanged.connect(self._on_symbol_combo_text_changed)
        sym_line = self._symbol_combo.lineEdit()
        if sym_line is not None:
            # editingFinished only updates the alert label; no longer triggers a
            # data subscription change.  The user must click 「获取数据」 or
            # 「提交分析」 to apply the new symbol/timeframe.
            sym_line.editingFinished.connect(self._update_symbol_data_alert)
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
        # Reap any zombie workers / loops before starting a fresh one
        self._reap_zombie_workers()
        self._reap_zombie_loops()

        data_source = getattr(self._ctx, "data_source", None)
        if data_source is None:
            logger.debug("RefreshLoop not started: data_source not available")
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
            n_bars = self._analysis_bar_count()
        if self._current_data_source_kind() in ("akshare", "eastmoney", "tushare") and interval_ms < 2500:
            interval_ms = 2500

        self._refresh_cancel_token = CancelToken()
        self._refresh_loop = RefreshLoop(
            data_source=data_source,
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
        self._update_symbol_data_alert()

    def _stop_refresh_loop(self) -> None:
        """Stop the background RefreshLoop thread if running.

        Disconnects signals before waiting so that a zombie loop's callbacks
        cannot fire after the owning MainWindow has moved on (e.g. symbol/tf
        switch, new worker started).

        If the loop does not finish within ``_WORKER_JOIN_TIMEOUT_MS`` it is
        tracked as a zombie.  Zombie loops are reaped later in
        ``_reap_zombie_loops()`` so their QThread resources are eventually
        freed.
        """
        loop = getattr(self, "_refresh_loop", None)
        token = getattr(self, "_refresh_cancel_token", None)
        if loop is None:
            return
        # Disconnect signals first to prevent zombie callbacks
        try:
            loop.frame_ready.disconnect(self._on_refresh_frame_ready)
        except (TypeError, RuntimeError):
            pass
        try:
            loop.status_changed.disconnect(self._on_status_update)
        except (TypeError, RuntimeError):
            pass
        if token is not None:
            token.set()
        # Actively close the live WebSocket so any blocked get_hist() recv()
        # exits immediately instead of waiting out the full timeout.  This is
        # the only reliable way to unblock the RefreshLoop thread before the
        # join below.
        data_source = getattr(self._ctx, "data_source", None)
        if data_source is not None:
            close_ws = getattr(data_source, "_close_tv_socket", None)
            if callable(close_ws):
                try:
                    close_ws()
                except Exception:  # noqa: BLE001
                    pass
        if loop.isRunning():
            loop.wait(_WORKER_JOIN_TIMEOUT_MS)
            if loop.isRunning():
                # RefreshLoop is stuck in a blocking WebSocket call — it will
                # eventually time out and check the cancel token, but until
                # then we track it as a zombie so it can be reaped later.
                logger.warning(
                    "RefreshLoop did not finish within %d ms; tracking as zombie",
                    _WORKER_JOIN_TIMEOUT_MS,
                )
                zombies = getattr(self, "_zombie_loops", None)
                if zombies is None:
                    zombies = []
                    self._zombie_loops = zombies
                zombies.append(loop)
            else:
                loop.deleteLater()
        else:
            loop.deleteLater()
        self._refresh_loop = None
        self._refresh_cancel_token = None

    def _ui_is_alive(self) -> bool:
        """True while MainWindow (and its widgets) can still be touched from slots."""
        if getattr(self, "_window_closing", False):
            return False
        return _qobject_alive(self)

    def _disconnect_analysis_worker(self, worker: _AnalysisWorker) -> None:
        """Detach all MainWindow / panel slots from a worker thread."""
        for signal_name in (
            "finished",
            "record_ready",
            "error_occurred",
            "status_update",
            "reasoning_token",
            "content_token",
            "stage_prompt_ready",
            "stage2_files_ready",
            "retry_occurred",
        ):
            signal = getattr(worker, signal_name, None)
            if signal is None:
                continue
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass

    def _reap_zombie_workers(self) -> None:
        """Join zombie analysis workers that finished since last check."""
        zombies = getattr(self, "_zombie_workers", None)
        if not zombies:
            return
        still_alive: list[_AnalysisWorker] = []
        for worker in zombies:
            if worker.isRunning():
                still_alive.append(worker)
            else:
                worker.deleteLater()
        if still_alive:
            self._zombie_workers = still_alive
        else:
            self._zombie_workers = []

    def _cancel_analysis_prep_worker(self) -> None:
        """Drop any in-flight analysis prep worker (does not block the UI thread)."""
        self._prep_worker_id = None
        worker = getattr(self, "_prep_worker", None)
        self._prep_worker = None
        if worker is None:
            return
        try:
            worker.ready.disconnect()
            worker.failed.disconnect()
        except (TypeError, RuntimeError):
            pass
        if worker.isRunning():
            worker.wait(0)
        worker.deleteLater()

    def _cancel_analysis_worker(self, *, join_ms: int = 0) -> None:
        """Cancel the AI worker and invalidate any pending finished callbacks."""
        self._cancel_analysis_prep_worker()
        self._analysis_worker_id = None
        if self._cancel_token is not None:
            self._cancel_token.set()
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        # Disconnect before join so stale slots cannot touch the UI mid-switch.
        self._disconnect_analysis_worker(worker)
        if worker.isRunning():
            worker.wait(join_ms)
        if worker.isRunning():
            logger.info(
                "Analysis worker still running after %d ms cancel wait; "
                "tracking as zombie",
                _WORKER_JOIN_TIMEOUT_MS,
            )
            zombies = getattr(self, "_zombie_workers", None)
            if zombies is None:
                zombies = []
                self._zombie_workers = zombies
            zombies.append(worker)
        else:
            worker.deleteLater()

    def _cancel_snapshot_fetch_worker(self) -> None:
        """Cancel any running SnapshotFetchWorker and nullify its reference.

        Uses a generation-based invalidation: the callback closures check
        ``_snapshot_fetch_id`` before acting, so stale workers that finish
        after cancellation are silently ignored.
        """
        sfw = getattr(self, "_snapshot_fetch_worker", None)
        if sfw is not None:
            # Invalidate the fetch generation so stale callbacks are no-ops
            self._snapshot_fetch_id = None
            self._snapshot_fetch_worker = None
            if sfw.isRunning():
                sfw.wait(_WORKER_JOIN_TIMEOUT_MS)
                if sfw.isRunning():
                    logger.warning(
                        "SnapshotFetchWorker did not finish within %d ms; "
                        "it will eventually finish but results will be ignored",
                        _WORKER_JOIN_TIMEOUT_MS,
                    )

    def _reap_zombie_loops(self) -> None:
        """Join any zombie RefreshLoops that have finished since last check.

        Called periodically (e.g. from ``_on_worker_done``) to free QThread
        resources that were stranded when ``_stop_refresh_loop`` timed out.
        """
        zombies = getattr(self, "_zombie_loops", None)
        if not zombies:
            return
        still_alive: list = []
        for loop in zombies:
            if loop.isRunning():
                still_alive.append(loop)
            else:
                loop.deleteLater()
        if still_alive:
            self._zombie_loops = still_alive
        else:
            self._zombie_loops = []

    def _disconnect_data_source(self, data_source: Any) -> None:
        if data_source is None:
            return
        try:
            data_source.unsubscribe()
        except Exception as exc:  # noqa: BLE001
            logger.debug("unsubscribe failed: %s", exc)
        try:
            data_source.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.debug("disconnect failed: %s", exc)

    def _current_data_source_kind(self) -> str:
        return getattr(self, "_active_data_source_kind", "mt5")

    def _tv_exchange_text(self) -> str:
        combo = getattr(self, "_tv_exchange_combo", None)
        if combo is None:
            return ""
        data = combo.currentData()
        if data is not None and str(data).strip():
            return str(data).strip().upper()
        text = combo.currentText().strip()
        if text in ("（自动）", "(auto)", ""):
            return ""
        return text.upper()

    def _sync_tv_exchange_visibility(self) -> None:
        """Show exchange field only for TradingView, allow manual selection."""
        visible = (
            self._current_data_source_kind() == "tradingview"
            and not getattr(self, "_demo_mode", False)
        )
        for w in (
            getattr(self, "_tv_exchange_label", None),
            getattr(self, "_tv_exchange_combo", None),
        ):
            if w is not None:
                w.setVisible(visible)
                w.setEnabled(visible)

    def _force_tv_exchange_auto(self) -> None:
        """Force TradingView exchange UI to «auto» (empty string)."""
        combo = getattr(self, "_tv_exchange_combo", None)
        if combo is None:
            return
        idx = combo.findData("")
        if idx < 0:
            return
        combo.blockSignals(True)
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _apply_gold_defaults_for_data_source(self, kind: str) -> None:
        """Reset symbol/exchange to defaults when switching data source."""
        from pa_agent.data.market_defaults import (
            A_SHARE_DEFAULT_TIMEFRAME,
            normalize_gold_symbol_for_kind,
        )

        sym = normalize_gold_symbol_for_kind(
            kind, self._symbol_combo.currentText().strip()
        )
        self._symbol_combo.blockSignals(True)
        self._symbol_combo.setCurrentText(sym)
        self._symbol_combo.blockSignals(False)
        if kind == "akshare":
            if self._tf_combo.currentText() not in ("1h", "4h", "1d"):
                self._tf_combo.setCurrentText(A_SHARE_DEFAULT_TIMEFRAME)

    def _apply_tv_exchange_to_source(self, data_source: Any) -> None:
        from pa_agent.data.tradingview import TradingViewSource

        if isinstance(data_source, TradingViewSource):
            data_source.set_exchange(self._tv_exchange_text())

    def _on_tv_probe_status(self, symbol: str, exchange: str, label: str) -> None:
        """Callback from TradingViewSource auto-probe: show current exchange being tried.
        
        Called from worker thread; use invokeMethod to update GUI on main thread.
        """
        from PyQt6.QtCore import Qt, QMetaObject, Q_ARG
        timeframe = self._tf_combo.currentText() if hasattr(self, "_tf_combo") else ""
        msg = f"TV 自动探测 {label} {timeframe}…"
        # Update status bar on main thread to avoid race with other updates
        QMetaObject.invokeMethod(
            self._status_bar,
            "showMessage",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, msg)
        )

    def _persist_tradingview_exchange(self) -> None:
        settings = getattr(self._ctx, "settings", None)
        if settings is None:
            return
        settings.general.last_tradingview_exchange = self._tv_exchange_text()
        try:
            from pa_agent.config.settings import save_settings

            save_settings(settings)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to persist TV exchange: %s", exc)

    def _on_tv_exchange_changed(self, _index: int = 0) -> None:
        if getattr(self, "_switching", False):
            return
        if getattr(self, "_demo_mode", False):
            return
        if self._current_data_source_kind() != "tradingview":
            return
        from pa_agent.data.market_defaults import is_partial_tv_symbol_input

        sym_raw = self._symbol_combo.currentText().strip()
        if is_partial_tv_symbol_input(sym_raw):
            return
        ex_val = self._tv_exchange_text()
        logger.info("TV exchange changed → %r (raw combo data=%r)",
                     ex_val, self._tv_exchange_combo.currentData())
        self._persist_tradingview_exchange()
        data_source = getattr(self._ctx, "data_source", None)
        self._apply_tv_exchange_to_source(data_source)
        # Stop any running refresh and immediately restart so the new exchange
        # takes effect without requiring the user to click "获取数据" again.
        self._stop_refresh_loop()
        timeframe = self._tf_combo.currentText()
        ex_show = ex_val or "自动"
        if data_source is not None and getattr(data_source, "_connected", False):
            try:
                data_source.unsubscribe()
                data_source.subscribe(sym_raw, timeframe)
                self._status_bar.showMessage(
                    f"TradingView 正在拉取 {ex_show}:{sym_raw} {timeframe}…"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("TV resubscribe after exchange change: %s", exc)
                self._status_bar.showMessage(f"订阅失败：{exc}")
            else:
                self._start_refresh_loop()

    def _apply_data_source_symbol_placeholder(self) -> None:
        line = self._symbol_combo.lineEdit()
        if line is None:
            return
        kind = self._current_data_source_kind()
        # 东方财富期货: 两级选择 (品种→合约), 显示品种下拉框, 标签改"合约"
        is_futures = kind == "eastmoney_futures"
        self._variety_label.setVisible(is_futures)
        self._variety_combo.setVisible(is_futures)
        self._symbol_label.setText("合约:" if is_futures else "品种:")
        if kind == "tradingview":
            line.setPlaceholderText(
                "A股 6 位 / 港股 1810 / 名称 小米集团；交易所可自动；或 XAUUSD+OANDA"
            )
        elif kind == "eastmoney_futures":
            line.setPlaceholderText("选择左侧品种后在此选合约, 或直接输入如 AO2509")
        elif kind in ("akshare", "eastmoney", "tushare"):
            line.setPlaceholderText("A股 6 位代码，如 600519；指数 000300 或 sh000300")
        else:
            line.setPlaceholderText("输入 MT5 品种名，如 XAUUSDm…")

    def _populate_symbol_combo_for_source(self) -> None:
        """Refresh symbol suggestions for the active data source."""
        from pa_agent.data.factory import default_symbol_for_kind

        data_source = getattr(self._ctx, "data_source", None)
        current = self._symbol_combo.currentText().strip()
        kind = self._current_data_source_kind()

        # 东方财富期货: 两级选择 (品种 → 合约)
        if kind == "eastmoney_futures" and data_source is not None:
            self._populate_futures_variety_and_contracts(data_source, current)
            self._apply_data_source_symbol_placeholder()
            return

        symbols: list[str] = []
        if data_source is not None and getattr(data_source, "_connected", False):
            try:
                symbols = list(data_source.list_symbols())
            except Exception as exc:  # noqa: BLE001
                logger.debug("list_symbols failed: %s", exc)

        self._symbol_combo.blockSignals(True)
        self._symbol_combo.clear()
        if symbols:
            cap = 80 if kind == "mt5" else len(symbols)
            self._symbol_combo.addItems(symbols[:cap])
        if current:
            if self._symbol_combo.findText(current) < 0:
                self._symbol_combo.addItem(current)
            self._symbol_combo.setCurrentText(current)
        else:
            default = default_symbol_for_kind(kind)
            if self._symbol_combo.findText(default) < 0:
                self._symbol_combo.addItem(default)
            self._symbol_combo.setCurrentText(default)
        self._symbol_combo.blockSignals(False)
        self._apply_data_source_symbol_placeholder()

    def _populate_futures_variety_and_contracts(
        self, data_source: object, current: str
    ) -> None:
        """填充期货品种下拉框 + 合约下拉框 (两级选择)."""
        varieties: list[str] = []
        try:
            varieties = list(data_source.list_symbols())  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.debug("list_symbols failed: %s", exc)

        # 1. 品种下拉框
        self._variety_combo.blockSignals(True)
        self._variety_combo.clear()
        self._variety_combo.addItems(varieties)
        variety_idx = 0
        if current:
            cur_code = current.split()[0].upper() if current.split() else ""
            for i, v in enumerate(varieties):
                v_code = v.split()[0].upper() if v.split() else ""
                if v_code and (v_code == cur_code or cur_code.startswith(v_code)):
                    variety_idx = i
                    break
        if variety_idx < self._variety_combo.count():
            self._variety_combo.setCurrentIndex(variety_idx)
        self._variety_combo.blockSignals(False)

        # 2. 合约下拉框 (基于选中品种生成)
        selected_variety = self._variety_combo.currentText()
        contracts: list[str] = []
        try:
            contracts = list(data_source.generate_contracts(selected_variety))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.debug("generate_contracts failed: %s", exc)
        self._symbol_combo.blockSignals(True)
        self._symbol_combo.clear()
        if contracts:
            self._symbol_combo.addItems(contracts)
        if current:
            idx = self._symbol_combo.findText(current)
            if idx >= 0:
                self._symbol_combo.setCurrentIndex(idx)
            elif contracts:
                self._symbol_combo.setCurrentIndex(0)
        elif contracts:
            self._symbol_combo.setCurrentIndex(0)
        self._symbol_combo.blockSignals(False)

    def _on_variety_combo_changed(self, index: int) -> None:
        """品种下拉框切换 → 重新生成合约列表."""
        if getattr(self, "_switching", False):
            return
        kind = self._current_data_source_kind()
        if kind != "eastmoney_futures":
            return
        data_source = getattr(self._ctx, "data_source", None)
        if data_source is None:
            return
        variety_text = self._variety_combo.currentText()
        if not variety_text:
            return
        try:
            contracts = list(data_source.generate_contracts(variety_text))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.debug("generate_contracts failed: %s", exc)
            return
        self._symbol_combo.blockSignals(True)
        self._symbol_combo.clear()
        if contracts:
            self._symbol_combo.addItems(contracts)
            self._symbol_combo.setCurrentIndex(0)  # 默认主力
        self._symbol_combo.blockSignals(False)

    def _populate_timeframe_combo_for_source(self) -> None:
        data_source = getattr(self._ctx, "data_source", None)
        preferred = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
        supported: list[str] = []
        if data_source is not None:
            try:
                supported = list(data_source.supported_timeframes())
            except Exception as exc:  # noqa: BLE001
                logger.debug("supported_timeframes failed: %s", exc)
        items = [tf for tf in preferred if tf in supported]
        if not items and supported:
            items = supported[:12]

        current = self._tf_combo.currentText()
        self._tf_combo.blockSignals(True)
        self._tf_combo.clear()
        if items:
            self._tf_combo.addItems(items)
            if current in items:
                self._tf_combo.setCurrentText(current)
            else:
                self._tf_combo.setCurrentText(items[0])
        self._tf_combo.blockSignals(False)

    def _ensure_tradingview_reachable(self) -> bool:
        """Always allow switching to TV; connectivity is checked on-demand when user clicks '获取数据'."""
        return True

    def _select_data_source_kind(self, kind: str, *, switch: bool) -> None:
        """Set data-source combo to *kind*; optionally run full switch."""
        idx = self._data_source_combo.findData(kind)
        if idx < 0:
            return
        self._data_source_combo.blockSignals(True)
        self._data_source_combo.setCurrentIndex(idx)
        self._data_source_combo.blockSignals(False)
        if switch and kind != self._current_data_source_kind():
            self._switch_data_source(kind)

    def _on_data_source_combo_changed(self, index: int) -> None:
        """Switch K-line data source (MT5 / TradingView)."""
        if getattr(self, "_switching", False):
            return
        if getattr(self, "_demo_mode", False):
            return
        kind = self._data_source_combo.itemData(index)
        if kind is None:
            return
        kind = str(kind)
        if kind == self._current_data_source_kind():
            return
        prev_index = self._data_source_combo.findData(self._current_data_source_kind())
        if kind == "tradingview" and not self._ensure_tradingview_reachable():
            return
        try:
            self._switch_data_source(kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Data source switch failed: %s", exc)
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.warning(
                self,
                "切换数据来源失败",
                f"无法切换到 {self._data_source_combo.currentText()}：\n{exc}",
            )
            if prev_index >= 0:
                self._data_source_combo.blockSignals(True)
                self._data_source_combo.setCurrentIndex(prev_index)
                self._data_source_combo.blockSignals(False)

    def _switch_data_source(self, kind: str) -> None:
        """Replace ctx.data_source, reconnect, and restart RefreshLoop."""
        from pa_agent.config.settings import save_settings
        from pa_agent.data.factory import create_data_source, data_source_label

        if self._switching:
            return
        self._switching = True
        try:
            self._cancel_analysis_worker()
            self._analysis_in_progress = False
            if self._ui_is_alive():
                self._update_submit_button_state()

            self._stop_refresh_loop()
            self._disconnect_data_source(getattr(self._ctx, "data_source", None))

            self._last_frame_ready_bars = None

            self._active_data_source_kind = kind
            self._sync_tv_exchange_visibility()
            self._apply_gold_defaults_for_data_source(kind)

            # Restore saved TV exchange before applying to data source
            if kind == "tradingview":
                settings = getattr(self._ctx, "settings", None)
                saved_ex = ""
                if settings is not None:
                    saved_ex = getattr(settings.general, 'last_tradingview_exchange', '') or ''
                idx = self._tv_exchange_combo.findData(saved_ex)
                if idx < 0:
                    idx = self._tv_exchange_combo.findData("")
                if idx >= 0:
                    self._tv_exchange_combo.blockSignals(True)
                    self._tv_exchange_combo.setCurrentIndex(idx)
                    self._tv_exchange_combo.blockSignals(False)

            symbol = self._symbol_combo.currentText().strip()
            timeframe = self._tf_combo.currentText()

            new_source = create_data_source(kind)
            # Wire auto-probe status callback for TV
            from pa_agent.data.tradingview import TradingViewSource
            if isinstance(new_source, TradingViewSource):
                new_source.on_probe_status = self._on_tv_probe_status
            new_source.connect()
            self._apply_tv_exchange_to_source(new_source)
            new_source.subscribe(symbol, timeframe)

            self._ctx.data_source = new_source

            self._populate_symbol_combo_for_source()
            self._populate_timeframe_combo_for_source()
            if kind == "tradingview":
                self._persist_tradingview_exchange()

            if hasattr(self, "_chart_widget"):
                self._chart_widget.reset()
                self._chart_widget.request_fit_on_next_render()

            self._set_chart_refresh_paused(False)
            self._free_chat_session = None
            self._disable_chat_input()

            settings = getattr(self._ctx, "settings", None)
            if settings is not None:
                settings.general.last_data_source = kind  # type: ignore[assignment]
                settings.general.last_symbol = self._symbol_combo.currentText().strip()
                settings.general.last_timeframe = self._tf_combo.currentText()
                try:
                    save_settings(settings)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Failed to persist data source: %s", exc)

            label = data_source_label(kind)
            if kind == "tradingview":
                ex_display = self._tv_exchange_text() or "自动"
                self._status_bar.showMessage(
                    f"已切换至 {label} {ex_display} · "
                    f"{self._symbol_combo.currentText()} {self._tf_combo.currentText()}"
                )
            else:
                self._status_bar.showMessage(
                    f"已切换数据来源至 {label} · {self._symbol_combo.currentText()} "
                    f"{self._tf_combo.currentText()}"
                )
            logger.info(
                "Data source switched to %s (%s %s)",
                kind,
                self._symbol_combo.currentText(),
                self._tf_combo.currentText(),
            )
            self._update_symbol_data_alert()
            self._refresh_chart_once()
        finally:
            self._switching = False
            self._update_submit_button_state()

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_symbol_combo_text_changed(self, _text: str = "") -> None:
        """Update the alert label while the user is typing; no longer triggers subscription."""
        self._update_symbol_data_alert()
        # Debounce the alert refresh for slow typists
        if self._symbol_switch_timer is not None:
            self._symbol_switch_timer.start()

    def _on_symbol_combo_editing_finished(self) -> None:
        """Update the alert label when the user leaves the symbol field.
        No longer triggers a subscription change — use 「获取数据」 or 「提交分析」."""
        if self._symbol_switch_timer is not None:
            self._symbol_switch_timer.stop()
        self._update_symbol_data_alert()

    def _flush_deferred_symbol_switch(self) -> None:
        pending = self._pending_symbol_switch
        if pending is None:
            return
        self._pending_symbol_switch = None
        self._on_symbol_or_tf_changed(pending[0], pending[1])

    def _status_message_after_symbol_switch(self, symbol: str, timeframe: str) -> str:
        """Status bar text after symbol/tf change (TV shows resolved feed, not raw typing)."""
        if self._current_data_source_kind() == "tradingview":
            ex_show = self._tv_exchange_text() or "自动"
            return f"TradingView 正在拉取 {ex_show}:{symbol.strip()} {timeframe}…"
        return f"已切换至 {symbol} {timeframe}"

    def _update_symbol_data_alert(self) -> None:
        """Show hints when the symbol is unavailable (MT5) or source disconnected."""
        label = getattr(self, "_symbol_alert_label", None)
        if label is None:
            return
        symbol = self._symbol_combo.currentText().strip()
        if not symbol:
            label.hide()
            return
        data_source = getattr(self._ctx, "data_source", None)
        if not getattr(data_source, "_connected", False):
            label.hide()
            return
        kind = self._current_data_source_kind()
        if kind == "tradingview":
            if symbol.lower().endswith("m") and len(symbol) > 2:
                label.setText(
                    "TradingView 提示：品种名勿用 MT5 的 m 后缀；"
                    "请用交易所 OANDA + 品种 XAUUSD"
                )
                label.setStyleSheet("color: #e6b800; font-size: 11px;")
                label.show()
                return
            label.hide()
            return
        if kind != "mt5":
            label.hide()
            return
        checker = getattr(data_source, "is_symbol_available", None)
        if not callable(checker):
            label.hide()
            return
        if checker(symbol):
            label.hide()
            return
        label.setText(
            "未在 MT5 获取到该品种，请检查当前输入是否与 MT5「市场报价」中的名称完全一致"
            "（含后缀，如 XAUUSDm）。"
        )
        label.show()

    def _analysis_bar_count(self) -> int:
        """Closed-bar count for AI analysis and chart fetch (from settings)."""
        settings = getattr(self._ctx, "settings", None)
        if settings is None:
            return 100
        return int(getattr(settings.general, "analysis_bar_count", 100))

    def _on_status_update(self, text: str) -> None:
        """Update the status bar with subscription / analysis / data-delay text."""
        if not self._ui_is_alive():
            return
        if not text:
            # Empty string means data fetch recovered — clear any previous error.
            if not self._analysis_in_progress:
                self._status_bar.clearMessage()
            return
        self._status_bar.showMessage(text)
        if text == "数据延迟":
            self._update_symbol_data_alert()
        if self._analysis_in_progress:
            panel = getattr(self, "_stream_panel", None)
            if panel is not None:
                if text in ("阶段一重试",):
                    panel.mark_retry("stage1")
                elif text in ("阶段二重试",):
                    panel.mark_retry("stage2")
                else:
                    panel.on_analysis_progress(text)
        # ── Drive FlowBar step indicators ────────────────────────────────────
        flow = getattr(self, "_flow_bar", None)
        if flow is not None:
            # Steps: 0=数据 1=快照 2=诊断(Stage1) 3=决策(Stage2) 4=追问
            if text == "阶段一分析中…":
                flow.set_step_status(0, "done")
                flow.set_step_caption(0, "已就绪")
                flow.set_step_status(1, "done")
                flow.set_step_caption(1, "已获取")
                flow.set_step_status(2, "active")
                flow.set_step_caption(2, "分析中…")
            elif text in ("阶段一完成",):
                flow.set_step_status(2, "done")
                flow.set_step_caption(2, "已完成")
            elif text in ("阶段一失败",):
                flow.set_step_status(2, "error")
                flow.set_step_caption(2, "失败")
            elif text == "阶段二分析中…":
                flow.set_step_status(3, "active")
                flow.set_step_caption(3, "决策中…")
            elif text in ("阶段二完成",):
                flow.set_step_status(3, "done")
                flow.set_step_caption(3, "已完成")
            elif text in ("阶段二失败",):
                flow.set_step_status(3, "error")
                flow.set_step_caption(3, "失败")
            elif text in ("已取消",):
                for idx in range(5):
                    flow.set_step_status(idx, "idle")

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
        # 恢复图表前先刷新哨兵，防止立即触发持续跟踪分析
        self._refresh_keep_analysis_sentinel()
        self._set_chart_refresh_paused(False)
        self._status_bar.showMessage("图表已恢复实时更新")
        self._refresh_chart_once()

    def _on_fetch_data_clicked(self) -> None:
        """Start (or restart) continuous data refresh for the current symbol/timeframe."""
        data_source = getattr(self._ctx, "data_source", None)
        if data_source is None or not getattr(data_source, "_connected", False):
            self._status_bar.showMessage("数据源未连接，请先切换数据来源")
            return

        # Apply any pending symbol/timeframe change before fetching.
        # If the user typed a new symbol/tf and clicked 「获取数据」, honour it.
        new_symbol = self._symbol_combo.currentText().strip()
        new_tf = self._tf_combo.currentText().strip()
        cur_symbol = str(getattr(data_source, "_symbol", "") or "").strip()
        cur_tf = str(getattr(data_source, "_timeframe", "") or "").strip()
        if new_symbol and (new_symbol != cur_symbol or new_tf != cur_tf):
            # _on_symbol_or_tf_changed handles everything including starting the
            # refresh loop at the end, so we can return right away.
            self._on_symbol_or_tf_changed(new_symbol, new_tf)
            return

        # For TradingView, probe connectivity on-demand (not at startup)
        if self._current_data_source_kind() == "tradingview":
            from pa_agent.data.tradingview_connectivity import check_tradingview_connectivity
            ok, detail = check_tradingview_connectivity()
            if not ok:
                if detail:
                    logger.info("TradingView unreachable: %s", detail)
                from pa_agent.gui.tv_connectivity_dialog import show_tv_connectivity_blocked_dialog
                choice = show_tv_connectivity_blocked_dialog(self)
                if choice == "mt5":
                    self._select_data_source_kind("mt5", switch=True)
                return
            # Brief pause to let the probe's WebSocket fully disconnect before
            # the refresh loop opens its own connection (avoids TV rate-limiting)
            import time as _time
            _time.sleep(1.5)
        # Stop any existing loop first so we can start fresh.
        # Reset the keep-analysis sentinel so a stale closed-bar ts from a
        # previous session / interrupted fetch does not immediately fire a
        # spurious analysis round on the very first frame after reconnect.
        self._stop_refresh_loop()
        self._keep_analysis_last_closed_ts = None
        self._set_chart_refresh_paused(False)
        self._start_refresh_loop()

    def _ensure_refresh_loop_running(self) -> None:
        """Start data fetch automatically if RefreshLoop is not already running.

        Called when the user enables 「持续跟踪分析」 or 「等待最新K线收盘后再提交分析」
        so they don't need to click 「获取数据」 first.
        Does nothing if the data source is not connected or the loop is already active.
        """
        loop = getattr(self, "_refresh_loop", None)
        if loop is not None and loop.isRunning():
            logger.debug("_ensure_refresh_loop_running：RefreshLoop 已在运行，无需重启")
            return  # already running — nothing to do
        data_source = getattr(self._ctx, "data_source", None)
        if data_source is None or not getattr(data_source, "_connected", False):
            logger.warning(
                "_ensure_refresh_loop_running：数据源未连接（data_source=%s connected=%s），"
                "无法自动启动 RefreshLoop",
                data_source,
                getattr(data_source, "_connected", None) if data_source else "N/A",
            )
            return  # no data source — can't start
        logger.info("Auto-starting RefreshLoop triggered by checkbox enable")
        self._stop_refresh_loop()
        # Reset sentinel so the fresh loop's first frame initialises it correctly
        # rather than triggering a spurious analysis by comparing against a stale ts.
        self._keep_analysis_last_closed_ts = None
        self._set_chart_refresh_paused(False)
        self._start_refresh_loop()

    def _on_fit_chart(self) -> None:
        """Auto-fit chart view to show recent bars with proper price range."""
        chart = getattr(self, "_chart_widget", None)
        if chart is not None:
            chart.fit_view()
            self._status_bar.showMessage("图表已恢复默认缩放")

    def _auto_resume_chart_after_analysis_enabled(self) -> bool:
        settings = getattr(self._ctx, "settings", None)
        if settings is None:
            return True
        return bool(getattr(settings.general, "auto_resume_chart_after_analysis", False))

    def _maybe_auto_resume_chart_after_analysis(self) -> bool:
        """Resume live chart refresh after analysis if settings allow."""
        if getattr(self, "_demo_mode", False):
            return False
        if not self._auto_resume_chart_after_analysis_enabled():
            return False
        if not self._chart_refresh_paused:
            return False
        self._set_chart_refresh_paused(False)
        self._refresh_chart_once()
        return True

    def _refresh_chart_once(self) -> None:
        """Apply one immediate chart refresh (e.g. after resuming)."""
        frame = self._pull_chart_frame_from_source()
        chart = getattr(self, "_chart_widget", None)
        if frame is None or chart is None:
            return
        # User-triggered refresh/resume should always re-fit to the latest frame,
        # otherwise the chart can remain panned/zoomed away from the newest bars.
        chart.set_frame_now(frame, fit_view=True)

    def _chart_wants_forming_bar(self) -> bool:
        """Show semi-virtual forming bar on chart when live refresh is active."""
        return not self._chart_refresh_paused

    def _reference_now_ms(self) -> int:
        """Broker/server time when available (MT5), else local — for forming-bar semantics."""
        from pa_agent.data.bar_close_wait import reference_now_ms

        return reference_now_ms(data_source=getattr(self._ctx, "data_source", None))

    def _bars_sufficient_for_analysis(self, bars: list[Any], bar_count: int) -> bool:
        """True when *bars* can build an analysis frame of *bar_count* closed bars."""
        from pa_agent.data.bar_close_wait import has_forming_bar_at_head

        if not bars or len(bars) < bar_count:
            return False
        timeframe = self._tf_combo.currentText()
        symbol = self._symbol_combo.currentText().strip()
        if has_forming_bar_at_head(
            bars,
            timeframe,
            symbol=symbol,
            now_ms=self._reference_now_ms(),
        ):
            return len(bars) >= bar_count + 1
        return True

    def _sync_buffer_from_snapshot_bars(self, bars: Any) -> None:
        """Align cached newest-first bars with the snapshot used for analysis (no KlineBuffer)."""
        if bars:
            self._last_frame_ready_bars = list(bars)

    def _bars_for_analysis_submit(self, bar_count: int) -> list[Any] | None:
        """Newest-first bars from the latest RefreshLoop tick (same source as the chart)."""
        fresh = self._last_frame_ready_bars
        if not fresh or not self._bars_sufficient_for_analysis(fresh, bar_count):
            return None
        from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS

        need = bar_count + INDICATOR_WARMUP_BARS + 5
        return list(fresh[:need]) if len(fresh) >= need else list(fresh)

    def _pull_chart_frame_from_source(
        self,
        *,
        include_forming: bool | None = None,
    ) -> Any:
        """Build chart frame from the latest RefreshLoop snapshot (no separate buffer)."""
        if not getattr(self._ctx, "data_source", None) or not getattr(
            self._ctx.data_source, "_connected", False
        ):
            return None
        try:
            bars = self._bars_for_analysis_submit(self._analysis_bar_count())
            if not bars:
                return None
            if include_forming is None:
                include_forming = self._chart_wants_forming_bar()
            return self._build_chart_frame_from_bars(bars, include_forming=include_forming)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Chart frame pull failed: %s", exc)
            return None

    def snapshot_klines_for_followup(self) -> str:
        """Refresh chart once, freeze updates, return K-line table matching the chart."""
        import time as _time

        from pa_agent.ai.prompt_assembler import PromptAssembler

        data_source = getattr(self._ctx, "data_source", None)
        chart = getattr(self, "_chart_widget", None)
        display_frame = None
        export_frame = None
        if getattr(self._ctx, "data_source", None) and getattr(
            self._ctx.data_source, "_connected", False
        ):
            try:
                bars = self._bars_for_analysis_submit(self._analysis_bar_count())
                if bars:
                    display_frame = self._build_chart_frame_from_bars(
                        bars, include_forming=True
                    )
                    export_frame = self._build_chart_frame_from_bars(
                        bars, include_forming=False
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Followup chart pull failed: %s", exc)

        if display_frame is not None and chart is not None:
            from pa_agent.data.snapshot import frame_is_pure_closed, frames_equal_for_chart

            current = chart.displayed_frame()
            if not (
                export_frame is not None
                and current is not None
                and frame_is_pure_closed(current)
                and frames_equal_for_chart(current, export_frame)
            ):
                chart.set_frame_now(export_frame or display_frame)
            self._last_refresh_ts = _time.monotonic()
        elif chart is not None:
            display_frame = chart.displayed_frame()
            export_frame = display_frame

        self._set_chart_refresh_paused(True)
        self._update_refresh_elapsed()
        if getattr(self, "_status_bar", None) is not None:
            self._status_bar.showMessage("追问：已刷新并冻结图表，K线与屏幕一致")

        if export_frame is None:
            export_frame = display_frame
        if export_frame is None:
            return ""
        return PromptAssembler._render_kline_table(export_frame)

    def _update_refresh_elapsed(self) -> None:
        """Update the 'distance from last refresh' label every second."""
        import time as _time

        self._update_wait_close_countdown_display()

        label = getattr(self, "_refresh_elapsed_label", None)
        if label is None:
            return
        if self._chart_refresh_paused:
            label.setText("图表刷新已暂停")
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

        Builds a KlineFrame from bars delivered by RefreshLoop (background fetch).
        Chart updates on the UI thread only render; network I/O stays on RefreshLoop.
        """
        if bars:
            self._last_frame_ready_bars = list(bars)
            from pa_agent.data.bar_close_wait import current_forming_ts

            ts = current_forming_ts(
                bars,
                self._tf_combo.currentText(),
                symbol=self._symbol_combo.currentText().strip(),
                now_ms=self._reference_now_ms(),
            )
            if ts is not None:
                self._last_forming_ts_open = ts

            # FlowBar step 0: data connected
            flow = getattr(self, "_flow_bar", None)
            if flow is not None and not self._analysis_in_progress:
                flow.set_step_status(0, "done")
                flow.set_step_caption(0, "已就绪")

        if self._pending_submit_after_close:
            # bars 可能为空 (数据拉取失败), forming_bar_has_closed 会用时间判断是否收盘
            self._check_pending_bar_close(bars)

        if self._chart_refresh_paused:
            # Even when chart is frozen, keep-analysis must still detect new bar closes
            # so it can resume the chart and trigger the next round.
            if bars:
                self._check_keep_analysis(bars)
            # In keep-analysis mode while waiting for the next bar close,
            # continue rendering the live chart (including the forming bar)
            # so the user sees real-time price action during the wait.
            keep_analysis_on = (
                getattr(self, "_keep_analysis_checkbox", None) is not None
                and self._keep_analysis_checkbox.isChecked()
            )
            if not (keep_analysis_on and self._pending_submit_after_close):
                return

        # Auto-incremental: if a switch set the pending flag, trigger now
        if self._auto_incremental_pending and bars:
            self._auto_incremental_pending = False
            symbol = self._symbol_combo.currentText().strip()
            tf = self._tf_combo.currentText()
            bar_count = self._analysis_bar_count()
            if self._bars_sufficient_for_analysis(bars, bar_count):
                self._start_analysis_with_bars(
                    symbol, tf, bar_count, bars, force_incremental=False
                )
                return
            # Not enough bars yet — keep the flag and try again next time
            self._auto_incremental_pending = True

        if not bars:
            self._update_symbol_data_alert()
            return

        alert = getattr(self, "_symbol_alert_label", None)
        if alert is not None:
            alert.hide()

        try:
            import time as _time

            frame = self._build_chart_frame_from_bars(
                bars, include_forming=self._chart_wants_forming_bar()
            )
            if frame is None:
                return

            self._chart_widget.set_frame(frame)

            # Record the time of this successful chart update
            self._last_refresh_ts = _time.monotonic()
            self._update_refresh_elapsed()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Frame build skipped: %s", exc)

        # Update submit button label after each successful data tick (throttled).
        import time as _time

        now = _time.monotonic()
        last = getattr(self, "_incremental_label_refresh_ts", 0.0)
        if now - last >= 2.0:
            self._incremental_label_refresh_ts = now
            self._refresh_incremental_label()

        # 保持分析：检测是否有新K线收盘，若有则自动触发分析
        self._check_keep_analysis(bars)

    def _on_symbol_or_tf_changed(self, new_symbol: str, new_tf: str) -> None:
        """Handle symbol or timeframe combo box change.

        Steps (design §B.10, R3.1–R3.5):
        1. Cancel current AI worker and wait up to 5 s (zombie if timeout).
        2. Save partial record if analysis was in progress.
        3. Unsubscribe data source, clear cached bars, re-subscribe.
        4. Reset ChartWidget.
        5. Destroy FreeChatSession, disable Tab2 input.
        6. Reset or preserve ledger based on settings.
        """
        if self._switching:
            return  # Prevent re-entrant calls
        if getattr(self, "_demo_mode", False):
            return

        self._clear_pending_bar_close_wait()

        # Cancel any running SnapshotFetchWorker so its stale callbacks don't
        # fire after we've already changed symbol/tf (would corrupt state).
        self._cancel_snapshot_fetch_worker()

        # Stop any running refresh — user must click "获取数据" to re-fetch
        self._stop_refresh_loop()

        from pa_agent.data.market_defaults import is_partial_tv_symbol_input

        if (
            self._current_data_source_kind() == "tradingview"
            and is_partial_tv_symbol_input(new_symbol.strip())
        ):
            from pa_agent.data.tv_symbol_lookup import is_tv_name_input

            hint = (
                "请输入至少 2 个字的股票名称"
                if is_tv_name_input(new_symbol)
                else "请输入完整代码（A 股 6 位如 600519，港股如 1810）"
            )
            self._status_bar.showMessage(f"{hint} — 当前：{new_symbol.strip()}")
            self._update_symbol_data_alert()
            return

        self._switching = True
        # Reset the auto-incremental flag immediately — a manual symbol/tf
        # switch means the user wants to control when analysis starts.
        # The flag will be re-evaluated at the end of the switch by
        # _check_auto_incremental if a prior record is found.
        self._auto_incremental_pending = False
        # Reset keep-analysis sentinel so a stale closed-bar ts doesn't
        # immediately fire after the switch.
        self._keep_analysis_last_closed_ts = None
        try:
            # ── Step 1: Cancel current AI worker ─────────────────────────────
            had_analysis = self._analysis_in_progress
            self._cancel_analysis_worker(join_ms=_WORKER_JOIN_TIMEOUT_MS)

            # ── Step 2: Save partial record if analysis was in progress ───────
            if had_analysis:
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

            # ── Step 3: Unsubscribe, clear cached snapshot, re-subscribe ───────
            data_source = getattr(self._ctx, "data_source", None)
            if data_source is not None:
                try:
                    data_source.unsubscribe()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("unsubscribe failed: %s", exc)
            self._last_frame_ready_bars = None
            self._incremental_available = False
            if hasattr(self, "_submit_btn"):
                self._submit_btn.setText("提交分析")
            if data_source is not None:
                self._apply_tv_exchange_to_source(data_source)
                try:
                    data_source.subscribe(new_symbol, new_tf)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "subscribe(%s, %s) failed: %s", new_symbol, new_tf, exc
                    )
                    self._status_bar.showMessage(f"订阅失败：{exc}")

            # ── Step 4: Reset ChartWidget ─────────────────────────────────────
            if hasattr(self, "_chart_widget"):
                self._chart_widget.reset()
                self._chart_widget.request_fit_on_next_render()

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

            switch_msg = self._status_message_after_symbol_switch(new_symbol, new_tf)
            if had_analysis:
                switch_msg = f"已切换至 {new_symbol} {new_tf}，进行中的分析已取消 — {switch_msg}"
            self._status_bar.showMessage(switch_msg)
            logger.info("Symbol/TF switched to %s %s", new_symbol, new_tf)
            self._update_symbol_data_alert()

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
            self._refresh_chart_once()

            # Auto-restart the refresh loop so data arrives immediately after a
            # symbol/timeframe switch without requiring the user to click
            # "获取数据" again.
            data_source = getattr(self._ctx, "data_source", None)
            if data_source is not None and getattr(data_source, "_connected", False):
                self._start_refresh_loop()

            # Check for prior analysis record — if found, show a hint in the
            # status bar but do NOT auto-trigger analysis; the user decides
            # when to submit.
            self._check_auto_incremental(new_symbol, new_tf)
            # Ensure the flag is always cleared after the check so the next
            # frame_ready signal does not unexpectedly fire analysis.
            self._auto_incremental_pending = False
            self._update_submit_button_state()

    def _check_auto_incremental(self, symbol: str, timeframe: str) -> None:
        """After a symbol/tf switch, look for a prior record and set the
        auto-incremental flag so analysis triggers once bars are available."""
        self._auto_incremental_pending = False

        settings = getattr(self._ctx, "settings", None)
        threshold = int(
            getattr(getattr(settings, "general", None), "incremental_max_new_bars", 10)
        )
        if threshold <= 0:
            return

        try:
            from pa_agent.records.analysis_history import find_latest_successful_record

            previous = find_latest_successful_record(
                symbol=symbol, timeframe=timeframe
            )
            if previous is None:
                return

            self._auto_incremental_pending = True
            self._status_bar.showMessage(
                f"找到历史记录，下次分析将自动增量（{symbol} {timeframe}）"
            )
            logger.info(
                "Auto-incremental: found prior record for %s %s, flag set",
                symbol,
                timeframe,
            )

            # If bars are already cached, trigger immediately
            bar_count = self._analysis_bar_count()
            bars = self._bars_for_analysis_submit(bar_count)
            if bars and self._bars_sufficient_for_analysis(bars, bar_count):
                self._auto_incremental_pending = False
                self._start_analysis_with_bars(
                    symbol, timeframe, bar_count, bars, force_incremental=False
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Auto-incremental check failed: %s", exc)

    def _disable_chat_input(self) -> None:
        """Disable free-chat input in the AI stream window."""
        panel = getattr(self, "_stream_panel", None)
        if panel is not None:
            panel.set_input_enabled(False)

    def _on_wait_close_checkbox_changed(self, _state: int) -> None:
        """Cancel pending wait if user unchecks the option."""
        if self._wait_close_checkbox.isChecked():
            # Auto-start data fetch if RefreshLoop is not running yet.
            self._ensure_refresh_loop_running()
            self._refresh_last_forming_ts()
            # UX: checking this option implies "submit after the next bar close".
            # Auto-trigger the same flow as clicking 「提交分析」 so the user does not
            # need to click twice.
            if (
                not self._analysis_in_progress
                and not self._pending_submit_after_close
                and not getattr(self, "_switching", False)
                and not getattr(self, "_demo_mode", False)
            ):
                self._begin_submit_analysis(force_incremental=False)
        else:
            if self._pending_submit_after_close:
                self._clear_pending_bar_close_wait()
            self._status_bar.showMessage("已取消等待K线收盘")
        self._update_wait_close_countdown_display()

    def _refresh_last_forming_ts(self) -> None:
        """Snapshot newest forming bar ts_open for countdown display."""
        from pa_agent.data.bar_close_wait import current_forming_ts

        if not getattr(self._ctx, "data_source", None) or not getattr(
            self._ctx.data_source, "_connected", False
        ):
            return
        bars = self._last_frame_ready_bars or []
        ts = current_forming_ts(
            bars,
            self._tf_combo.currentText(),
            symbol=self._symbol_combo.currentText().strip(),
            now_ms=self._reference_now_ms(),
        )
        if ts is not None:
            self._last_forming_ts_open = ts

    def _forming_bar_seconds_remaining(self) -> int | None:
        """Seconds until the relevant forming bar closes."""
        from pa_agent.data.bar_close_wait import seconds_until_bar_closes

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
        return seconds_until_bar_closes(
            int(ts), tf, now_ms=self._reference_now_ms()
        )

    def _update_wait_close_countdown_display(self) -> None:
        """Update checkbox-adjacent countdown label while waiting."""
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

    def _clear_pending_bar_close_wait(self) -> None:
        """Cancel wait-for-bar-close armed by the checkbox."""
        self._pending_submit_after_close = False
        self._pending_force_incremental = False
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
        symbol = self._pending_submit_symbol
        timeframe = self._pending_submit_timeframe
        if not forming_bar_has_closed(
            self._wait_forming_ts,
            bars,
            timeframe,
            symbol=symbol,
            now_ms=self._reference_now_ms(),
        ):
            return
        bar_count = self._pending_submit_bar_count
        force_incremental = self._pending_force_incremental
        leaving_demo = self._demo_mode
        if leaving_demo:
            self._exit_demo_mode(silent=True)
        self._clear_pending_bar_close_wait()
        submit_hint = "提交增量分析" if force_incremental else "提交分析"
        if leaving_demo:
            self._status_bar.showMessage(
                f"最新K线已收盘，已退出演示模式，正在{submit_hint}…"
            )
        elif force_incremental:
            self._status_bar.showMessage("最新K线已收盘，正在提交增量分析…")
        else:
            self._status_bar.showMessage("最新K线已收盘，正在提交分析…")
        self._start_analysis(
            symbol,
            timeframe,
            bar_count,
            force_incremental=force_incremental,
            snapshot_bars=bars,
        )

    def _arm_wait_for_bar_close(
        self,
        symbol: str,
        timeframe: str,
        bar_count: int,
        *,
        force_incremental: bool = False,
    ) -> bool:
        """Wait until bars[0] ts_open changes, then call _start_analysis."""
        from datetime import datetime

        from pa_agent.data.bar_close_wait import current_forming_ts

        data_source = getattr(self._ctx, "data_source", None)
        if data_source is None or not getattr(data_source, "_connected", False):
            logger.warning("_arm_wait_for_bar_close：数据源未连接，放弃")
            self._status_bar.showMessage("数据源未连接")
            return False

        bars_raw = self._bars_for_analysis_submit(bar_count)
        if not bars_raw:
            logger.warning(
                "_arm_wait_for_bar_close：bars 为空（RefreshLoop 尚未推送数据？），放弃"
            )
            self._status_bar.showMessage("数据不足，请等待图表刷新后再提交")
            return False

        forming_ts = current_forming_ts(
            bars_raw,
            timeframe,
            symbol=symbol,
            now_ms=self._reference_now_ms(),
        )
        if forming_ts is None:
            submit_hint = "提交增量分析" if force_incremental else "提交分析"
            self._status_bar.showMessage(f"最新K线已收盘，正在{submit_hint}…")
            self._start_analysis(
                symbol,
                timeframe,
                bar_count,
                force_incremental=force_incremental,
                snapshot_bars=bars_raw,
            )
            return True

        self._pending_submit_after_close = True
        self._pending_force_incremental = force_incremental
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

        submit_hint = "提交增量分析" if force_incremental else "提交分析"
        if secs is not None:
            self._status_bar.showMessage(
                f"等待当前K线收盘…（{ts_hint}，收盘后将自动{submit_hint}）"
            )
        else:
            self._status_bar.showMessage(
                f"等待当前K线收盘…（{ts_hint}，收盘后将自动{submit_hint}）"
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

    def _on_demo_menu_action(self, mode: str) -> None:
        """Handle demo mode selection from the Settings menu."""
        if self._demo_mode:
            self._exit_demo_mode()
        else:
            self._start_demo_mode(mode)

    def _on_keep_analysis_checkbox_changed(self, _state: int) -> None:
        """Handle keep-analysis checkbox state change.

        When enabled, automatically check and lock the wait-for-bar-close checkbox
        so that every analysis cycle waits for the next K-line to close first.
        When disabled, unlock and uncheck the wait-for-bar-close checkbox.
        """
        enabled = self._keep_analysis_checkbox.isChecked()
        # Persist preference to settings
        settings = getattr(self._ctx, "settings", None)
        if settings is not None:
            try:
                settings.general.keep_analysis = enabled
                from pa_agent.config.settings import save_settings
                from pa_agent.config.paths import SETTINGS_JSON_PATH
                save_settings(settings, SETTINGS_JSON_PATH)
            except Exception:  # noqa: BLE001
                pass

        wait_cb = getattr(self, "_wait_close_checkbox", None)
        if wait_cb is None:
            return

        if enabled:
            # Auto-start data fetch if RefreshLoop is not running yet.
            self._ensure_refresh_loop_running()
            # Force-check wait_close and lock it so user can't uncheck independently
            wait_cb.blockSignals(True)
            wait_cb.setChecked(True)
            wait_cb.blockSignals(False)
            wait_cb.setEnabled(False)
            # Resume live chart updates immediately — the chart should show real-time
            # price action (including the forming bar) while waiting for bar close.
            self._set_chart_refresh_paused(False)
            self._status_bar.showMessage(
                "持续跟踪分析已开启：等待K线收盘后将自动开始分析"
            )
            # Reset sentinel so the next RefreshLoop tick initialises it fresh.
            # Do NOT call _begin_submit_analysis here — if the RefreshLoop just
            # started, _last_frame_ready_bars may be empty and the arm would fail
            # with no retry.  Instead, let _check_keep_analysis (called on every
            # RefreshLoop tick) handle the first trigger once bars arrive.
            self._keep_analysis_last_closed_ts = None
            logger.info(
                "持续跟踪分析已开启，重置哨兵，等待 RefreshLoop 推送第一批数据后自动触发"
            )
        else:
            # Unlock wait_close and cancel any pending wait
            wait_cb.setEnabled(True)
            wait_cb.blockSignals(True)
            wait_cb.setChecked(False)
            wait_cb.blockSignals(False)
            if self._pending_submit_after_close:
                self._clear_pending_bar_close_wait()
            self._status_bar.showMessage("持续跟踪分析已关闭")

    def _refresh_keep_analysis_sentinel(self) -> None:
        """Sync keep-analysis sentinel after analysis completes.

        We restore the sentinel to the closed-bar ts that was current *when
        analysis was submitted* (recorded in ``_keep_analysis_submit_closed_ts``).
        This means any bars that closed *during* the analysis run will be seen
        as "new" by ``_check_keep_analysis`` on the very next RefreshLoop tick
        and will trigger a fresh analysis round immediately.

        Falls back to the current latest closed bar only when the submit-time
        snapshot is unavailable.
        """
        # Prefer the snapshot taken at submit time so bars that closed while
        # the AI was running are NOT silently swallowed by the sentinel.
        submit_ts = getattr(self, "_keep_analysis_submit_closed_ts", None)
        if submit_ts is not None:
            self._keep_analysis_last_closed_ts = submit_ts
            self._keep_analysis_submit_closed_ts = None
            return

        # Fallback: derive from the latest RefreshLoop bars
        bars = self._last_frame_ready_bars
        if not bars or len(bars) < 2:
            return
        try:
            from pa_agent.data.bar_close_wait import current_forming_ts

            forming_ts = current_forming_ts(
                bars,
                self._tf_combo.currentText(),
                symbol=self._symbol_combo.currentText().strip(),
                now_ms=self._reference_now_ms(),
            )
            if forming_ts is not None:
                closed_bar = None
                for bar in bars:
                    ts_open = getattr(bar, "ts_open", None) or (bar[0] if hasattr(bar, "__getitem__") else None)
                    if ts_open is not None and int(ts_open) != int(forming_ts):
                        closed_bar = bar
                        break
                if closed_bar is None:
                    return
                ts_open = getattr(closed_bar, "ts_open", None) or (closed_bar[0] if hasattr(closed_bar, "__getitem__") else None)
            else:
                bar = bars[0]
                ts_open = getattr(bar, "ts_open", None) or (bar[0] if hasattr(bar, "__getitem__") else None)
            if ts_open is not None:
                self._keep_analysis_last_closed_ts = int(ts_open)
        except Exception as exc:  # noqa: BLE001
            logger.debug("_refresh_keep_analysis_sentinel error: %s", exc)

    def _check_keep_analysis(self, bars: Any) -> None:
        """Trigger a new analysis round when keep-analysis is enabled and a new bar has closed.

        Uses the ts_open of the most recently closed bar (i.e. the second-to-last
        bar when a forming bar is present, or the last bar otherwise) as the
        sentinel.  If the sentinel changes between ticks a new bar has closed and
        we auto-submit.
        """
        if not getattr(self, "_keep_analysis_checkbox", None):
            return
        if not self._keep_analysis_checkbox.isChecked():
            return
        if self._analysis_in_progress:
            logger.debug("持续跟踪分析：跳过（分析进行中）")
            return
        if self._pending_submit_after_close:
            logger.debug("持续跟踪分析：跳过（等待K线收盘中）")
            return
        if getattr(self, "_demo_mode", False):
            return
        if not bars or len(bars) < 2:
            logger.warning("持续跟踪分析：跳过（bars 数量不足，len=%d）", len(bars) if bars else 0)
            return

        try:
            from pa_agent.data.bar_close_wait import current_forming_ts

            forming_ts = current_forming_ts(
                bars,
                self._tf_combo.currentText(),
                symbol=self._symbol_combo.currentText().strip(),
                now_ms=self._reference_now_ms(),
            )
            # The most recently closed bar is the last bar that is NOT the forming bar.
            # If forming_ts is present, that's bars[-1]; closed bar is bars[-2].
            # If no forming bar, closed bar is bars[-1].
            if forming_ts is not None:
                # bars is ordered newest-first or oldest-first — detect from ts_open
                # The bar whose ts_open == forming_ts is the forming bar; skip it.
                closed_bar = None
                for bar in bars:
                    ts_open = getattr(bar, "ts_open", None) or (bar[0] if hasattr(bar, "__getitem__") else None)
                    if ts_open is not None and int(ts_open) != int(forming_ts):
                        closed_bar = bar
                        break
                if closed_bar is None:
                    return
                ts_open = getattr(closed_bar, "ts_open", None) or (closed_bar[0] if hasattr(closed_bar, "__getitem__") else None)
            else:
                bar = bars[0]
                ts_open = getattr(bar, "ts_open", None) or (bar[0] if hasattr(bar, "__getitem__") else None)

            if ts_open is None:
                return

            closed_ts = int(ts_open)
            if self._keep_analysis_last_closed_ts is None:
                # First tick after enabling — record sentinel and immediately arm
                # a wait for the *current* forming bar to close so the first
                # analysis starts as soon as the bar in progress finishes, rather
                # than waiting for an additional bar after that.
                self._keep_analysis_last_closed_ts = closed_ts
                logger.info(
                    "持续跟踪分析：哨兵初始化 closed_ts=%s，立即 arm 等待当前K线收盘",
                    closed_ts,
                )
                symbol = self._symbol_combo.currentText().strip()
                tf = self._tf_combo.currentText()
                bar_count = self._analysis_bar_count()
                if (
                    not self._analysis_in_progress
                    and not self._pending_submit_after_close
                    and self._bars_sufficient_for_analysis(bars, bar_count)
                ):
                    self._arm_wait_for_bar_close(
                        symbol, tf, bar_count, force_incremental=False
                    )
                return

            if closed_ts == self._keep_analysis_last_closed_ts:
                logger.debug("持续跟踪分析：无新K线（sentinel=%s）", closed_ts)
                return  # No new bar yet

            # New bar has closed — update sentinel and trigger analysis
            self._keep_analysis_last_closed_ts = closed_ts
            logger.info("保持分析：检测到新K线收盘（ts_open=%s），自动触发分析", closed_ts)
            symbol = self._symbol_combo.currentText().strip()
            tf = self._tf_combo.currentText()
            bar_count = self._analysis_bar_count()
            if self._bars_sufficient_for_analysis(bars, bar_count):
                # A bar just closed — submit analysis immediately with the
                # current bars rather than arming another wait-for-close.
                # Arming an extra wait would delay every cycle by one full
                # bar period on top of the AI latency, which is undesirable.
                logger.info("持续跟踪分析：直接提交分析（bars 已足够）")
                self._start_analysis_with_bars(
                    symbol, tf, bar_count, bars, force_incremental=False
                )
            else:
                logger.warning(
                    "持续跟踪分析：bars 数量不足（len=%d，需要=%d），跳过本轮",
                    len(bars), bar_count,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("_check_keep_analysis error: %s", exc, exc_info=True)

    def _start_demo_mode(self, mode: str) -> None:
        """Load a pending JSON record and replay it through the UI."""
        from pathlib import Path

        from pa_agent.config.paths import RECORDS_PENDING_DIR
        from pa_agent.demo.record_loader import (
            is_demo_playable,
            pick_playable_demo_record,
            try_load_analysis_record,
        )

        self._demo_mode_kind = str(mode)
        self._demo_auto_next_armed = False

        skipped_note = ""
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
            path = Path(path_str)
            record = try_load_analysis_record(path)
            if record is None or not is_demo_playable(record):
                alt = pick_playable_demo_record(exclude=path)
                if alt is None:
                    QMessageBox.warning(
                        self,
                        "演示模式",
                        "所选记录无法读取或缺少阶段结果，且目录中没有其它可用记录。",
                    )
                    return
                skipped_note = path.name
                path, record = alt
        else:
            path, record = self._try_load_random_demo_record()
            if record is None:
                QMessageBox.warning(
                    self,
                    "演示模式",
                    f"未找到可读取的演示记录（已跳过损坏或不完整的文件）：\n{RECORDS_PENDING_DIR}",
                )
                return

        if skipped_note:
            QMessageBox.information(
                self,
                "演示模式",
                f"已跳过无法使用的记录「{skipped_note}」，\n"
                f"改用：{path.name}",
            )

        self._enter_demo_mode(path, record)

    def _try_load_random_demo_record(self) -> tuple[Any, Any] | tuple[None, None]:
        """Return (path, record) for a random playable pending record, or (None, None)."""
        from pa_agent.demo.record_loader import pick_playable_demo_record

        last = self._demo_record_path or None
        picked = pick_playable_demo_record(exclude=last)
        if picked is not None:
            return picked
        if last:
            return pick_playable_demo_record(exclude=None) or (None, None)
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

    def _enter_demo_mode(
        self,
        path: Any,
        record: Any,
        *,
        _skip_retry: int = 0,
    ) -> None:
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
        # 同步菜单项状态
        if hasattr(self, "_demo_exit_action"):
            self._demo_exit_action.setEnabled(True)
        if hasattr(self, "_demo_manual_action"):
            self._demo_manual_action.setEnabled(False)
        if hasattr(self, "_demo_auto_action"):
            self._demo_auto_action.setEnabled(False)
        ds_combo = getattr(self, "_data_source_combo", None)
        if ds_combo is not None:
            ds_combo.setEnabled(False)
        self._sync_tv_exchange_visibility()

        meta = record.meta
        self._symbol_combo.blockSignals(True)
        self._tf_combo.blockSignals(True)
        try:
            self._symbol_combo.setCurrentText(meta.symbol)
            self._tf_combo.setCurrentText(meta.timeframe)
        finally:
            self._symbol_combo.blockSignals(False)
            self._tf_combo.blockSignals(False)

        try:
            frame = frame_from_record_klines(
                record.kline_data,
                symbol=meta.symbol,
                timeframe=meta.timeframe,
                snapshot_ts_local_ms=meta.timestamp_local_ms,
            )
        except Exception as exc:  # noqa: BLE001
            self._exit_demo_mode(silent=True)
            if _skip_retry < 8:
                alt = self._try_load_random_demo_record()
                if alt[0] is not None and str(alt[0]) != str(path):
                    self._demo_mode_kind = prev_kind
                    self._enter_demo_mode(alt[0], alt[1], _skip_retry=_skip_retry + 1)
                    return
            QMessageBox.warning(
                self,
                "演示模式",
                f"无法构建 K 线快照，已跳过该记录：\n{Path(path).name}\n{exc}",
            )
            return

        # New record may use a different symbol/TF; drop previous trade overlays first.
        self._chart_widget.reset()
        self._chart_widget.set_frame_now(frame, fit_view=True)
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
        self._future_trend_panel.clear()

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
        self._demo_replayer.content_token.connect(panel.on_content_token)
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
        # 同步菜单项状态
        if hasattr(self, "_demo_exit_action"):
            self._demo_exit_action.setEnabled(False)
        if hasattr(self, "_demo_manual_action"):
            self._demo_manual_action.setEnabled(True)
        if hasattr(self, "_demo_auto_action"):
            self._demo_auto_action.setEnabled(True)
        ds_combo = getattr(self, "_data_source_combo", None)
        if ds_combo is not None:
            ds_combo.setEnabled(True)
        self._sync_tv_exchange_visibility()
        self._demo_mode_label.hide()
        self._analysis_in_progress = False
        self._set_chart_refresh_paused(False)
        self._update_submit_button_state()
        self._decision_badge.setText("")

        if was_demo and not silent:
            if hasattr(self, "_chart_widget"):
                self._chart_widget.reset()
                self._chart_widget.request_fit_on_next_render()
            self._status_bar.showMessage("已退出演示模式")
            self._refresh_chart_once()

    def _on_submit_analysis(self) -> None:
        """Handle the '提交分析' / '增量分析' button click.

        When an eligible prior record is available the button label reads
        '增量分析' and we honour that by passing force_incremental=True so
        the user never needs a separate button.
        """
        self._begin_submit_analysis(force_incremental=self._incremental_available)

    def _on_submit_incremental_analysis(self) -> None:
        """Handle the '增量分析' button click — always try incremental mode."""
        self._begin_submit_analysis(force_incremental=True)

    def _begin_submit_analysis(self, *, force_incremental: bool) -> None:
        """Shared entry for normal and forced-incremental submit buttons."""
        if not self._can_submit():
            return

        # Clear auto-incremental flag — user initiated analysis manually
        self._auto_incremental_pending = False

        # If the user typed a new symbol/tf but hasn't fetched data yet,
        # trigger the switch now before submitting analysis.
        data_source = getattr(self._ctx, "data_source", None)
        if data_source is not None:
            new_symbol_raw = self._symbol_combo.currentText().strip()
            new_tf = self._tf_combo.currentText().strip()
            # 东方财富期货: 下拉框显示 "AO0 主力", 数据源内部存 normalize 后的 "AO0",
            # 需 normalize 后比较, 否则每次提交都误触发切换导致分析无法启动.
            if self._current_data_source_kind() == "eastmoney_futures":
                from pa_agent.data.eastmoney_futures_source import normalize_futures_symbol
                new_symbol = normalize_futures_symbol(new_symbol_raw)
            else:
                new_symbol = new_symbol_raw
            cur_symbol = str(getattr(data_source, "_symbol", "") or "").strip()
            cur_tf = str(getattr(data_source, "_timeframe", "") or "").strip()
            if new_symbol and (new_symbol != cur_symbol or new_tf != cur_tf):
                self._on_symbol_or_tf_changed(new_symbol_raw, new_tf)
                # Switch resets everything; user needs to click 提交分析 again
                # once data arrives.
                return

        # Cancel any existing worker before starting a new one
        self._cancel_analysis_worker()

        symbol = self._symbol_combo.currentText().strip()
        timeframe = self._tf_combo.currentText()
        bar_count = self._analysis_bar_count()

        if self._wait_close_checkbox.isChecked():
            if not self._arm_wait_for_bar_close(
                symbol,
                timeframe,
                bar_count,
                force_incremental=force_incremental,
            ):
                return
            return

        self._start_analysis(
            symbol,
            timeframe,
            bar_count,
            force_incremental=force_incremental,
        )

    def _start_analysis(
        self,
        symbol: str,
        timeframe: str,
        bar_count: int,
        *,
        force_incremental: bool = False,
        snapshot_bars: Any = None,
    ) -> None:
        """Build snapshot and run two-stage analysis (after optional bar-close wait)."""
        if snapshot_bars is None:
            snapshot_bars = self._bars_for_analysis_submit(bar_count)
        if snapshot_bars is None or not self._bars_sufficient_for_analysis(
            snapshot_bars, bar_count
        ):
            self._start_analysis_async_fetch(
                symbol,
                timeframe,
                bar_count,
                force_incremental=force_incremental,
            )
            return
        self._start_analysis_with_bars(
            symbol,
            timeframe,
            bar_count,
            snapshot_bars,
            force_incremental=force_incremental,
        )

    def _start_analysis_async_fetch(
        self,
        symbol: str,
        timeframe: str,
        bar_count: int,
        *,
        force_incremental: bool = False,
    ) -> None:
        """Fetch K-lines on a worker thread when no RefreshLoop snapshot is cached yet."""
        data_source = getattr(self._ctx, "data_source", None)
        if data_source is None or not getattr(data_source, "_connected", False):
            self._status_bar.showMessage("数据源未连接")
            return

        # Cancel any previous worker (belt-and-suspenders; normally cleaned
        # up by _on_worker_done, but a rapid re-trigger could race).
        self._cancel_snapshot_fetch_worker()

        from pa_agent.gui.snapshot_worker import SnapshotFetchWorker

        self._status_bar.showMessage("正在后台获取K线…")
        from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS

        worker = SnapshotFetchWorker(
            data_source, bar_count + INDICATOR_WARMUP_BARS + 5, parent=None
        )
        # Use a generation token so that stale callbacks from a cancelled
        # worker are silently ignored (closures can't easily be disconnected).
        fetch_id = object()
        self._snapshot_fetch_id = fetch_id
        self._snapshot_fetch_worker = worker

        def _on_bars(bars: list) -> None:
            if getattr(self, "_snapshot_fetch_id", None) is not fetch_id:
                return  # stale fetch — ignore
            if not _qobject_alive(self):
                return
            self._snapshot_fetch_worker = None
            if not self._bars_sufficient_for_analysis(bars, bar_count):
                self._status_bar.showMessage("数据不足，请等待图表刷新后再提交")
                return
            self._last_frame_ready_bars = list(bars)
            self._start_analysis_with_bars(
                symbol,
                timeframe,
                bar_count,
                bars,
                force_incremental=force_incremental,
            )

        def _on_fail(msg: str) -> None:
            if getattr(self, "_snapshot_fetch_id", None) is not fetch_id:
                return  # stale fetch — ignore
            if not _qobject_alive(self):
                return
            self._snapshot_fetch_worker = None
            self._status_bar.showMessage(msg or "获取K线失败")

        worker.bars_ready.connect(_on_bars)
        worker.failed.connect(_on_fail)
        worker.start()

    def _start_analysis_with_bars(
        self,
        symbol: str,
        timeframe: str,
        bar_count: int,
        snapshot_bars: Any,
        *,
        force_incremental: bool = False,
    ) -> None:
        """Prepare analysis off the UI thread, then launch the AI worker."""
        self._sync_buffer_from_snapshot_bars(snapshot_bars)

        settings = getattr(self._ctx, "settings", None)
        threshold = int(
            getattr(getattr(settings, "general", None), "incremental_max_new_bars", 10)
        )

        self._analysis_in_progress = True
        self._last_analysis_had_error = False
        self._update_submit_button_state()
        self._status_bar.showMessage("准备分析…（构建快照）")
        self._decision_badge.setText("准备中…")

        from pa_agent.gui.analysis_prep_worker import AnalysisPrepWorker

        self._cancel_analysis_prep_worker()
        prep_id = object()
        self._prep_worker_id = prep_id
        prep = AnalysisPrepWorker(
            bars_raw=list(snapshot_bars),
            symbol=symbol,
            timeframe=timeframe,
            bar_count=bar_count,
            now_ms=self._reference_now_ms(),
            force_incremental=force_incremental,
            incremental_threshold=threshold,
            parent=None,
        )
        self._prep_worker = prep

        def _on_ready(result: Any) -> None:
            if getattr(self, "_prep_worker_id", None) is not prep_id:
                return
            if not _qobject_alive(self):
                return
            self._prep_worker = None
            self._launch_analysis_worker(
                symbol=symbol,
                timeframe=timeframe,
                bar_count=bar_count,
                snapshot_bars=snapshot_bars,
                prep=result,
                force_incremental=force_incremental,
            )

        def _on_failed(msg: str) -> None:
            if getattr(self, "_prep_worker_id", None) is not prep_id:
                return
            if not _qobject_alive(self):
                return
            self._prep_worker = None
            self._analysis_in_progress = False
            self._update_submit_button_state()
            self._status_bar.showMessage(msg or "准备分析失败")
            self._decision_badge.setText("")

        prep.ready.connect(_on_ready)
        prep.failed.connect(_on_failed)
        prep.start()

    def _launch_analysis_worker(
        self,
        *,
        symbol: str,
        timeframe: str,
        bar_count: int,
        snapshot_bars: Any,
        prep: Any,
        force_incremental: bool = False,
    ) -> None:
        """UI-thread launch after background prep (frame + incremental lookup)."""
        frame = getattr(prep, "frame", None)
        if frame is None:
            self._analysis_in_progress = False
            self._update_submit_button_state()
            self._status_bar.showMessage("数据不足，请等待图表刷新后再提交")
            return

        self._last_analysis_frame = frame
        previous_record = getattr(prep, "previous_record", None)
        self._analysis_previous_record = previous_record
        incremental_new_bar_count = getattr(prep, "incremental_new_bar_count", None)
        incremental_detail = getattr(prep, "incremental_detail", None)

        if force_incremental and previous_record is None:
            reason = self._incremental_unavailable_reason(frame, symbol, timeframe)
            self._analysis_in_progress = False
            self._update_submit_button_state()
            self._status_bar.showMessage(reason)
            QMessageBox.warning(self, "无法增量分析", reason)
            return

        orchestrator = self._build_orchestrator()
        if orchestrator is None:
            self._analysis_in_progress = False
            self._update_submit_button_state()
            self._status_bar.showMessage("编排器未就绪，请检查设置")
            return

        try:
            from pa_agent.data.bar_close_wait import current_forming_ts as _cft

            _bars_snap = list(snapshot_bars) if snapshot_bars else []
            _forming = _cft(
                _bars_snap,
                timeframe,
                symbol=symbol,
                now_ms=self._reference_now_ms(),
            )
            _submit_closed_ts: int | None = None
            if _forming is not None:
                for _b in _bars_snap:
                    _bts = getattr(_b, "ts_open", None) or (_b[0] if hasattr(_b, "__getitem__") else None)
                    if _bts is not None and int(_bts) != int(_forming):
                        _submit_closed_ts = int(_bts)
                        break
            else:
                _b0 = _bars_snap[0] if _bars_snap else None
                if _b0 is not None:
                    _bts = getattr(_b0, "ts_open", None) or (_b0[0] if hasattr(_b0, "__getitem__") else None)
                    if _bts is not None:
                        _submit_closed_ts = int(_bts)
            self._keep_analysis_submit_closed_ts = _submit_closed_ts
        except Exception:  # noqa: BLE001
            self._keep_analysis_submit_closed_ts = None

        from pa_agent.util.threading import CancelToken

        self._cancel_token = CancelToken()
        worker_id = object()
        self._analysis_worker_id = worker_id

        self._worker = _AnalysisWorker(
            orchestrator=orchestrator,
            frame=frame,
            cancel_token=self._cancel_token,
            previous_record=previous_record,
            incremental_new_bar_count=incremental_new_bar_count,
            parent=None,
        )
        def _on_worker_finished(decision: dict) -> None:
            if getattr(self, "_analysis_worker_id", None) is not worker_id:
                return
            if not _qobject_alive(self):
                return
            try:
                self._on_analysis_finished(decision)
            except Exception as exc:  # noqa: BLE001
                self._last_analysis_had_error = True
                logger.exception("Analysis finished UI update failed: %s", exc)
            finally:
                self._on_worker_done()

        self._worker.record_ready.connect(self._on_record_ready)
        self._worker.finished.connect(_on_worker_finished)
        self._worker.error_occurred.connect(self._on_analysis_error)
        self._worker.status_update.connect(self._on_status_update)
        self._worker.retry_occurred.connect(self._on_retry_occurred)

        panel = getattr(self, "_stream_panel", None)
        if panel is not None:
            self._worker.stage_prompt_ready.connect(panel.on_stage_prompt_ready)
            self._worker.reasoning_token.connect(panel.on_reasoning_token)
            self._worker.content_token.connect(panel.on_content_token)

        self._worker.stage2_files_ready.connect(
            self._on_stage2_files_ready,
            Qt.ConnectionType.UniqueConnection,
        )
        # Start AI worker before heavy chart/UI work so the UI stays responsive.
        self._worker.start()

        self._chart_widget.set_frame_now(frame, fit_view=True)
        self._set_chart_refresh_paused(True)
        self._update_submit_button_state()

        # Reset FlowBar and SummaryStrip at the start of every analysis
        flow = getattr(self, "_flow_bar", None)
        if flow is not None:
            flow.reset_all()
            flow.set_step_status(0, "done")
            flow.set_step_caption(0, "已就绪")
        strip = getattr(self, "_summary_strip", None)
        if strip is not None:
            strip.reset()
        # Clear previous support/resistance lines
        self._chart_widget.clear_support_resistance()
        from pa_agent.ai.decision_stance import stance_label_zh

        stance_raw = "balanced"
        settings = getattr(self._ctx, "settings", None)
        if settings is not None:
            stance_raw = getattr(settings.general, "decision_stance", "balanced")
        stance_label = stance_label_zh(stance_raw)
        if incremental_new_bar_count is not None:
            prefix = "强制增量分析中" if force_incremental else "增量分析中"
            if incremental_new_bar_count > 0:
                detail = incremental_detail or f"新增{incremental_new_bar_count}根已收盘K线"
            else:
                detail = "无新增K线，基于上一轮结论复核"
            self._status_bar.showMessage(
                f"{prefix}…（倾向:{stance_label}，{detail}，图表已冻结）"
            )
            logger.info("Incremental submit: %s", detail)
        else:
            self._status_bar.showMessage(
                f"分析中…（倾向:{stance_label}，图表已冻结，K1=最新已收盘K线）"
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

    def _find_incremental_base_record(
        self,
        frame: Any,
        symbol: str,
        timeframe: str,
        *,
        force_incremental: bool = False,
    ) -> tuple[Any | None, int | None, str | None]:
        """Return a prior record for incremental analysis when configured."""
        settings = getattr(self._ctx, "settings", None)
        threshold = int(
            getattr(getattr(settings, "general", None), "incremental_max_new_bars", 10)
        )
        if not force_incremental and threshold <= 0:
            return None, None, None

        try:
            from pa_agent.records.analysis_history import (
                compute_incremental_bar_delta,
                find_latest_successful_record,
                format_bar_ts,
            )

            previous = find_latest_successful_record(symbol=symbol, timeframe=timeframe)
            if previous is None:
                return None, None, None

            delta = compute_incremental_bar_delta(frame, previous)
            if delta is None:
                logger.info("Incremental analysis skipped: no overlapping prior bar")
                return None, None, None

            new_count = delta.new_count
            if not force_incremental and new_count > threshold:
                logger.info(
                    "Incremental analysis skipped: %d new bars exceeds threshold %d",
                    new_count,
                    threshold,
                )
                return None, None, None

            anchor_label = format_bar_ts(delta.anchor_ts_open)
            if new_count == 0:
                detail = f"锚定K线 {anchor_label}，无新增已收盘K线"
            elif new_count == 1:
                detail = (
                    f"锚定K线 {anchor_label}，新增1根 {format_bar_ts(delta.new_bar_ts_opens[0])}"
                )
            else:
                newest = format_bar_ts(delta.new_bar_ts_opens[0])
                oldest_new = format_bar_ts(delta.new_bar_ts_opens[-1])
                detail = (
                    f"锚定K线 {anchor_label}，新增{new_count}根（{oldest_new} → {newest}）"
                )

            mode = "forced" if force_incremental else "auto"
            logger.info("Incremental analysis enabled (%s): %s", mode, detail)
            return previous, new_count, detail
        except Exception as exc:  # noqa: BLE001
            logger.warning("Incremental base lookup failed: %s", exc)
            return None, None, None

    def _incremental_unavailable_reason(
        self,
        frame: Any,
        symbol: str,
        timeframe: str,
    ) -> str:
        """Explain why forced incremental analysis cannot start."""
        try:
            from pa_agent.records.analysis_history import (
                compute_incremental_bar_delta,
                find_latest_successful_record,
            )

            previous = find_latest_successful_record(symbol=symbol, timeframe=timeframe)
            if previous is None:
                return (
                    f"无法强制增量分析：未找到 {symbol} {timeframe} 的成功分析记录。"
                    "请先完成一次完整分析。"
                )
            if compute_incremental_bar_delta(frame, previous) is None:
                return (
                    "无法强制增量分析：当前 K 线与上一轮记录无法对齐。"
                    "可能缺口过大或 K 线数量/范围变化过大，请改用「提交分析」。"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Incremental unavailable reason lookup failed: %s", exc)
        return "无法强制增量分析：未找到可用的上一轮记录。"

    def _on_stage2_files_ready(self, strategy_files: list) -> None:
        """Update 调试 tab when Stage 2 strategy .txt list is known."""
        pf = getattr(self, "_prompt_files_panel", None)
        if pf is None:
            return
        from pa_agent.ai.prompt_assembler import stage2_prompt_txt_files

        pf.set_stage2_files(stage2_prompt_txt_files(strategy_files))
        pf.set_extras(stage1_builtin=True, stage2_builtin=True)

    def _current_stage1_diagnosis(self) -> dict:
        """Stage-1 diagnosis for the analysis that just finished."""
        record = getattr(self, "_last_analysis_record", None)
        if record is not None:
            s1 = getattr(record, "stage1_diagnosis", None)
            if isinstance(s1, dict):
                return s1
        s1 = getattr(self, "_last_stage1_diagnosis", None)
        return s1 if isinstance(s1, dict) else {}

    def _on_analysis_finished(self, decision: dict) -> None:
        """Called on the main thread when the AI worker completes.

        *decision* is the full stage2 JSON dict (``{"decision": {...},
        "diagnosis_summary": {...}}``).  The chart and panel widgets expect
        the inner ``decision`` sub-dict, so we extract it here.
        """
        if not self._ui_is_alive():
            return
        if decision:
            from pa_agent.gui.stage2_payload import prepare_stage2_for_ui

            stage1_diag = self._current_stage1_diagnosis()
            inner = prepare_stage2_for_ui(
                decision,
                stage1_json=stage1_diag or None,
                skip_next_bar=not bool(
                    getattr(getattr(self._ctx.settings, "general", None), "enable_next_bar_prediction", False)
                ) if self._ctx.settings is not None else False,
            )
            from pa_agent.gui.chart_decision_overlay import enrich_decision_for_chart_overlay

            cooldown = 3
            if self._ctx.settings is not None:
                cooldown = int(
                    getattr(self._ctx.settings.general, "structure_flip_cooldown_bars", 3) or 3
                )
            chart_decision = enrich_decision_for_chart_overlay(
                inner,
                stage2_full=decision if isinstance(decision, dict) else None,
                frame=getattr(self, "_last_analysis_frame", None),
                stage1_json=stage1_diag or None,
                previous_record=getattr(self, "_analysis_previous_record", None),
                cooldown_bars=cooldown,
            )
            self._chart_widget.set_decision(chart_decision)
            if getattr(self, "_demo_mode", False):
                self._chart_widget.fit_view()
            stance = None
            if self._ctx.settings is not None:
                stance = getattr(self._ctx.settings.general, "decision_stance", None)
            self._decision_panel.set_decision(
                inner,
                diagnosis_summary=decision.get("diagnosis_summary"),
                stage1_diagnosis=stage1_diag or None,
                decision_stance=stance,
                confidence_threshold=self._confidence_threshold(),
            )
            self._future_trend_panel.set_prediction(inner)
            self._bind_decision_tree(decision, stage1_diag or None)
            order = inner.get("order_type", "—")
            self._decision_badge.setText(f"决策: {order}")
            if self._maybe_alert_order_opportunity(inner):
                self._spawn_post_order_followup(inner, decision)

            elif getattr(self, "_demo_mode", False):
                self._present_decision_flow_playback(force_play=True)

            # ── FlowBar: mark Stage 2 done ────────────────────────────────────
            flow = getattr(self, "_flow_bar", None)
            if flow is not None:
                flow.set_step_status(3, "done")
                flow.set_step_caption(3, "已完成")
                flow.set_step_status(4, "active")
                flow.set_step_caption(4, "可追问")

            # ── SummaryStrip: populate key metrics ────────────────────────────
            strip = getattr(self, "_summary_strip", None)
            if strip is not None:
                diag = decision.get("diagnosis_summary") or {}

                from pa_agent.ai.cycle_enums import (
                    format_cycle_with_direction,
                    format_trend_label,
                )

                # Current market cycle from diagnosis_summary
                cur_cycle = diag.get("cycle_position") or ""
                # Include direction prefix for cycle labels (e.g. 下跌宽通道 / 震荡交易区间).
                cur_cycle_zh = format_cycle_with_direction(cur_cycle, diag.get("direction"))

                # Next cycle: pick the highest-probability from next_cycle_prediction
                next_cycle_zh = "—"
                ncp = decision.get("next_cycle_prediction") or {}
                probs = ncp.get("probabilities") or {}
                if probs:
                    best_key = _best_probability_key(probs)
                    if best_key:
                        next_cycle_zh = format_cycle_with_direction(best_key, ncp.get("direction"))
                else:
                    cycle_key = ncp.get("cycle")
                    if cycle_key:
                        next_cycle_zh = format_cycle_with_direction(cycle_key, ncp.get("direction"))

                # Current trend: align with decision panel (range cycles → 震荡偏空/偏多).
                cur_trend_zh = format_trend_label(diag.get("direction"), cur_cycle)

                metrics: dict[str, str] = {
                    "当前趋势": cur_trend_zh,
                    "当前市场周期": cur_cycle_zh,
                    "下一个市场周期": next_cycle_zh,
                    "支撑区": "—",
                    "阻力区": "—",
                }
                from pa_agent.gui.support_resistance import (
                    chart_levels_from_stage1_diagnosis,
                    nearest_support_resistance_labels,
                )

                sup_label, res_label = nearest_support_resistance_labels(stage1_diag)
                metrics["支撑区"] = sup_label
                metrics["阻力区"] = res_label

                try:
                    chart_levels = chart_levels_from_stage1_diagnosis(stage1_diag)
                    self._chart_widget.set_support_resistance(chart_levels)
                except Exception:  # noqa: BLE001
                    pass
                strip.set_metrics(metrics)
        else:
            self._chart_widget.clear_decision_overlay()
            self._chart_widget.clear_support_resistance()
            self._decision_panel.clear()
            self._future_trend_panel.clear()
            self._decision_tree_panel.clear()
            if getattr(self, "_decision_flow_viz_panel", None) is not None:
                self._decision_flow_viz_panel.clear()
            self._decision_badge.setText("")
            strip = getattr(self, "_summary_strip", None)
            if strip is not None:
                strip.reset()

    def _build_exception_debug_bundle(
        self,
        exc_info: dict,
        *,
        record: Any = None,
    ) -> str:
        """Full text for validation-failure dialogs (exception + optional raw response)."""
        import json as _json

        parts: list[str] = []
        stage = exc_info.get("stage", "")
        exc_type = exc_info.get("type", "")
        category = exc_info.get("category", "")
        if exc_type == "provider_error" or category == "e":
            parts.append(
                "【说明】API 提供商返回积分/额度不足（402），程序不会自动重试。"
                "请充值 OpenClaw 积分或更换 API 后重新「提交分析」。\n"
            )
        elif stage == "stage2":
            parts.append(
                "【说明】阶段二校验失败时程序会自动重试（格式类错误，见 ValidationSettings）；"
                "请根据下方信息修改提示词/模型输出或手动重新「提交分析」。\n"
            )
        elif stage == "stage1":
            parts.append(
                "【说明】阶段一校验失败时程序会自动重试（格式类错误，见 ValidationSettings）；"
                "请根据下方信息排查后手动重新「提交分析」。\n"
            )

        parts.append("--- Exception JSON ---\n")
        parts.append(_json.dumps(exc_info, ensure_ascii=False, indent=2))

        invalid = exc_info.get("invalid_fields") or []
        if invalid:
            from pa_agent.ai.validation_messages import format_validation_errors

            parts.append("\n--- 规则摘要（全部 invalid_fields）---\n")
            parts.append(
                format_validation_errors(
                    list(invalid),
                    missing_fields=exc_info.get("missing_fields"),
                    max_items=len(invalid),
                )
            )

        raw_text = exc_info.get("raw_text")
        if isinstance(raw_text, str) and raw_text.strip():
            parts.append("\n--- AI 原始正文（截断）---\n")
            parts.append(raw_text[:8000])
            if len(raw_text) > 8000:
                parts.append(f"\n…（共 {len(raw_text)} 字符，完整内容见「原始」页 Raw Response）")

        if record is not None:
            stage_key = f"{stage}_response" if stage in ("stage1", "stage2") else ""
            raw = getattr(record, stage_key, None) if stage_key else None
            if raw:
                parts.append(f"\n--- {stage} API raw（节选）---\n")
                try:
                    parts.append(_json.dumps(raw, ensure_ascii=False, indent=2)[:6000])
                except TypeError:
                    parts.append(str(raw)[:6000])

        return "\n".join(parts).strip()

    def _prompt_debug_report_for_bug_fix(
        self,
        headline: str,
        detail: str = "",
        *,
        exc_info: dict | None = None,
        record: Any = None,
    ) -> None:
        """Switch to 原始 tab and show debug dialog (no automatic API retry)."""
        sidebar = getattr(self, "_ai_sidebar", None)
        debug = getattr(self, "_debug_widget", None)
        if sidebar is not None:
            sidebar.focus_raw()
        if debug is not None:
            debug.focus_exception_turn()

        if exc_info:
            body = self._build_exception_debug_bundle(exc_info, record=record)
            summary = (
                f"{headline}\n\n"
                "已切换到右侧「原始」页，可对照 Raw Response / Validation。\n"
                "下方为完整调试信息（可复制粘贴给 AI）。"
            )
            if detail:
                summary += f"\n\n摘要：{detail}"
            show_validation_debug_dialog(
                self,
                title="分析校验失败",
                summary=summary,
                body=body,
            )
            return

        body = (
            f"{headline}\n\n"
            "已切换到右侧「原始」页。\n"
            "请查看页面最下方的「Validation / Exception」与「Raw Response」，"
            "或点击「复制调试信息」，将完整内容粘贴给 AI，便于排查并修复问题。"
        )
        if detail:
            body += f"\n\n摘要：{detail}"
        QMessageBox.warning(self, "需要排查错误", body)

    def _maybe_show_truncation_help_dialog(self, exc_info: dict | None) -> None:
        """If validation indicates truncation/context shortage, prompt user actions."""
        if not exc_info or not isinstance(exc_info, dict):
            return
        msg = str(exc_info.get("message", "") or "")
        if not msg:
            return

        # Heuristic: two_stage.py enriches messages with clear truncation keywords.
        is_trunc = any(
            token in msg
            for token in (
                "被截断",
                "未闭合对象",
                "正文 content 为空",
                "思考占满输出额度",
                "思考在输出阶段",
            )
        )
        if not is_trunc:
            return

        # Prevent repeated popups for the same error message.
        key = msg[:300]
        if getattr(self, "_last_truncation_hint_key", None) == key:
            return
        self._last_truncation_hint_key = key

        from PyQt6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("模型输出可能被截断")
        box.setText(
            "本次分析的 JSON 正文可能因「模型上下文/输出额度不足」而被截断，"
            "导致校验失败。"
        )
        box.setInformativeText(
            "建议操作：\n"
            "1) 换一个更长上下文/更稳的模型；或\n"
            "2) 在「AI 模型」设置里关闭「Thinking」后重试。\n\n"
            f"诊断摘要：{key}"
        )
        btn_open = box.addButton("打开 AI 模型设置", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("知道了", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == btn_open:
            self._open_ai_model_settings_dialog()

    def _on_analysis_error(self, message: str) -> None:
        """Unhandled exception in the analysis worker thread."""
        if not self._ui_is_alive():
            return
        self._last_analysis_had_error = True
        debug = getattr(self, "_debug_widget", None)
        if debug is not None:
            debug.add_turn({
                "label": "⚠ 程序异常",
                "system_prompt": "",
                "user_prompt": "",
                "raw_response": {},
                "validation_info": message,
            })
        self._prompt_debug_report_for_bug_fix("分析过程发生程序异常", message)

    def _on_retry_occurred(self, stage: str) -> None:
        """Handle retry event: if cancel_keep_analysis_on_retry is enabled, disable keep_analysis."""
        if not self._ui_is_alive():
            return
        settings = getattr(self._ctx, "settings", None)
        if settings is None:
            return
        try:
            cancel_on_retry = getattr(settings.general, "cancel_keep_analysis_on_retry", False)
        except Exception:  # noqa: BLE001
            return
        if not cancel_on_retry:
            return
        keep_cb = getattr(self, "_keep_analysis_checkbox", None)
        if keep_cb is not None and keep_cb.isChecked():
            keep_cb.setChecked(False)
            # Persist to settings (same as _on_keep_analysis_checkbox_changed)
            try:
                settings.general.keep_analysis = False
                from pa_agent.config.settings import save_settings
                from pa_agent.config.paths import SETTINGS_JSON_PATH
                save_settings(settings, SETTINGS_JSON_PATH)
                logger.info(
                    "持续跟踪分析已因 %s 重试自动关闭", stage
                )
            except Exception:  # noqa: BLE001
                pass

    def _on_record_ready(self, record: Any) -> None:
        """Push the full AnalysisRecord to the conversation and debug tabs."""
        if not self._ui_is_alive():
            return
        try:
            self._on_record_ready_impl(record)
        except Exception as exc:  # noqa: BLE001
            self._last_analysis_had_error = True
            logger.exception("Record ready handler failed: %s", exc)

    def _on_record_ready_impl(self, record: Any) -> None:
        self._last_analysis_record = record
        import json as _json

        exc_info = getattr(record, "exception", None)
        exc_json = (
            _json.dumps(exc_info, ensure_ascii=False, indent=2) if exc_info else ""
        )

        # ── Debug tab: add Stage1 and Stage2 turns ────────────────────────────
        debug = getattr(self, "_debug_widget", None)
        if debug is not None:
            # Stage 1 turn
            s1_msgs = getattr(record, "stage1_messages", []) or []
            s1_system = next((m.get("content", "") for m in s1_msgs if m.get("role") == "system"), "")
            s1_user = next((m.get("content", "") for m in s1_msgs if m.get("role") == "user"), "")
            s1_raw = getattr(record, "stage1_response", {}) or {}
            s1_diag = getattr(record, "stage1_diagnosis", None)
            if exc_info and exc_info.get("stage") == "stage1":
                s1_validation = exc_json
            elif s1_diag:
                s1_validation = _json.dumps(s1_diag, ensure_ascii=False, indent=2)
            else:
                s1_validation = "（验证失败或无数据）"
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
            s2_user = next((m.get("content", "") for m in reversed(s2_msgs) if m.get("role") == "user"), "")
            s2_raw = getattr(record, "stage2_response", {}) or {}
            s2_decision = getattr(record, "stage2_decision", None)
            if exc_info and exc_info.get("stage") == "stage2":
                s2_validation = exc_json
            elif s2_decision:
                s2_validation = _json.dumps(s2_decision, ensure_ascii=False, indent=2)
            else:
                s2_validation = "（验证失败或无数据）"
            debug.add_turn({
                "label": "Stage2 决策",
                "system_prompt": s2_system,
                "user_prompt": s2_user,
                "raw_response": s2_raw,
                "validation_info": s2_validation,
            })

        # If the analysis failed due to truncation/context issues, prompt actionable help.
        if exc_info:
            self._maybe_show_truncation_help_dialog(exc_info)

            if exc_info:
                debug.add_turn({
                    "label": "⚠ 异常",
                    "system_prompt": "",
                    "user_prompt": "",
                    "raw_response": {},
                    "validation_info": exc_json,
                })
                self._last_analysis_had_error = True
                err_type = exc_info.get("type", "error")
                category = exc_info.get("category", "")
                msg = exc_info.get("message", "")
                if err_type == "provider_error" or category == "e":
                    headline = "API 积分不足"
                    detail = msg or "OpenClaw 积分不足，请充值或更换 API"
                    self._status_bar.showMessage(detail)
                else:
                    detail = f"{category}: {msg}" if category else (msg or err_type)
                    headline = f"分析未通过（{err_type}）"
                self._prompt_debug_report_for_bug_fix(
                    headline,
                    detail,
                    exc_info=exc_info,
                    record=record,
                )
            else:
                self._last_analysis_had_error = False

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
        self._last_analysis_record = record
        self._last_stage1_diagnosis = s1_diag if isinstance(s1_diag, dict) else None
        s2_full = getattr(record, "stage2_decision", None)
        if s2_full:
            from pa_agent.gui.stage2_payload import prepare_stage2_for_ui

            s1_diag = getattr(record, "stage1_diagnosis", None) or {}
            inner = prepare_stage2_for_ui(
                s2_full if isinstance(s2_full, dict) else {},
                stage1_json=s1_diag if isinstance(s1_diag, dict) else None,
                skip_next_bar=not bool(
                    getattr(getattr(self._ctx.settings, "general", None), "enable_next_bar_prediction", False)
                ) if self._ctx.settings is not None else False,
            )
            meta = getattr(record, "meta", None)
            stance = getattr(meta, "decision_stance", None) if meta is not None else None
            self._decision_panel.set_decision(
                inner,
                diagnosis_summary=s2_full.get("diagnosis_summary"),
                stage1_diagnosis=s1_diag if isinstance(s1_diag, dict) else None,
                decision_stance=stance,
                confidence_threshold=self._confidence_threshold(),
            )
            self._future_trend_panel.set_prediction(inner)
            self._bind_decision_tree(
                s2_full,
                s1_diag if isinstance(s1_diag, dict) else None,
            )

        panel = getattr(self, "_stream_panel", None)
        if panel is not None:
            s1_diag = getattr(record, "stage1_diagnosis", None)
            s1_raw = getattr(record, "stage1_response", {}) or {}
            if s1_diag:
                s1_content = _json.dumps(s1_diag, ensure_ascii=False, indent=2)
                s1_reasoning = reasoning_from_response(
                    s1_raw if isinstance(s1_raw, dict) else None
                )
                panel.show_stage_result("阶段一：市场诊断", s1_content, s1_reasoning)
            # Push per-stage cache hit rate to stats label
            if isinstance(s1_raw, dict):
                s1_usage = s1_raw.get("usage") or {}
                s1_hit_pct = s1_usage.get("cache_hit_rate_pct")
                if s1_hit_pct is not None and hasattr(panel, "set_stage_cache_hit"):
                    panel.set_stage_cache_hit("stage1", s1_hit_pct)

            s2_decision = getattr(record, "stage2_decision", None)
            s2_raw = getattr(record, "stage2_response", {}) or {}
            if s2_decision:
                s2_content = _json.dumps(s2_decision, ensure_ascii=False, indent=2)
                s2_reasoning = reasoning_from_response(
                    s2_raw if isinstance(s2_raw, dict) else None
                )
                panel.show_stage_result("阶段二：交易决策", s2_content, s2_reasoning)
            if isinstance(s2_raw, dict):
                s2_usage = s2_raw.get("usage") or {}
                s2_hit_pct = s2_usage.get("cache_hit_rate_pct")
                if s2_hit_pct is not None and hasattr(panel, "set_stage_cache_hit"):
                    panel.set_stage_cache_hit("stage2", s2_hit_pct)

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
            flow_viz.schedule_refit_view(delay_ms=150)
        if has_path and flow_viz is not None:
            decision_inner = stage2_full.get("decision")
            skip_flow_viz = (
                self._order_opportunity_alert_enabled()
                and isinstance(decision_inner, dict)
                and self._has_order_opportunity(decision_inner)
            )
            # 演示模式：等 finished 回调后再切「决策树可视化」，与真实流式结束顺序一致
            if getattr(self, "_demo_mode", False):
                pass
            elif skip_flow_viz:
                pass
            elif flow_viz.should_auto_play_after_load():
                self._present_decision_flow_playback(force_play=False)

    def _order_opportunity_alert_enabled(self) -> bool:
        settings = self._ctx.settings
        if settings is None:
            return True
        return bool(getattr(settings.general, "alert_on_order_opportunity", True))

    def _confidence_threshold(self) -> int:
        """Return the decision confidence threshold from settings (default 0 = no gate)."""
        settings = self._ctx.settings
        if settings is None:
            return 0
        return int(getattr(settings.general, "decision_confidence_threshold", 0))

    def _has_order_opportunity(self, decision_inner: dict) -> bool:
        from pa_agent.gui.order_opportunity import has_order_opportunity

        return has_order_opportunity(
            decision_inner,
            confidence_threshold=self._confidence_threshold(),
        )

    def _spawn_post_order_followup(self, inner: dict, decision: dict) -> None:
        """Run trade CSV/chart + notifications off the UI thread (can take seconds)."""
        import threading

        settings = getattr(self._ctx, "settings", None)
        model_name = ""
        meta_symbol = ""
        meta_timeframe = ""
        decision_stance = ""
        if settings is not None:
            model_name = getattr(settings.provider, "model", "") or ""
            meta_symbol = getattr(settings.general, "last_symbol", "") or ""
            meta_timeframe = getattr(settings.general, "last_timeframe", "") or ""
            decision_stance = getattr(settings.general, "decision_stance", "") or ""
        stage1_diag = self._current_stage1_diagnosis() or None
        frame = getattr(self, "_last_analysis_frame", None)

        def _run() -> None:
            try:
                from pa_agent.records.trade_logger import save_trade_record

                save_trade_record(
                    decision_inner=inner,
                    stage2_full=decision,
                    stage1_diagnosis=stage1_diag,
                    frame=frame,
                    meta_symbol=meta_symbol,
                    meta_timeframe=meta_timeframe,
                    decision_stance=decision_stance,
                    model_name=model_name,
                    structure_flip_cooldown_bars=int(
                        getattr(settings.general, "structure_flip_cooldown_bars", 3) or 3
                    )
                    if settings is not None
                    else 3,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Trade record logging failed: %s", exc)

            try:
                from pa_agent.notify.feishu_notifier import send_order_signal as send_feishu_order
                from pa_agent.notify.pushplus_notifier import send_order_signal as send_pushplus_order
                from pa_agent.records.trade_logger import _TRADE_RECORDS_DIR

                safe_sym = meta_symbol.replace("/", "-").replace("\\", "-")
                safe_tf = meta_timeframe.replace("/", "-")
                img_glob = f"{safe_sym}_{safe_tf}_*.png"
                candidates = sorted(
                    _TRADE_RECORDS_DIR.glob(img_glob),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                latest_img = candidates[0] if candidates else None

                send_feishu_order(
                    decision_inner=inner,
                    stage2_full=decision,
                    symbol=meta_symbol,
                    timeframe=meta_timeframe,
                    chart_image_path=latest_img,
                    settings=settings,
                )
                from pa_agent.notify.pushplus_notifier import pushplus_is_active

                if pushplus_is_active(settings):
                    send_pushplus_order(
                        decision_inner=inner,
                        stage2_full=decision,
                        symbol=meta_symbol,
                        timeframe=meta_timeframe,
                        settings=settings,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("下单信号通知失败（不影响主流程）: %s", exc)

        threading.Thread(
            target=_run,
            name="post-order-followup",
            daemon=True,
        ).start()

    def _maybe_alert_order_opportunity(self, decision_inner: dict) -> bool:
        """Beep, popup, and focus decision tab when stage-2 proposes an order."""
        if not self._order_opportunity_alert_enabled():
            return False
        if not self._has_order_opportunity(decision_inner):
            return False

        from pa_agent.gui.order_opportunity import (
            play_order_alert_sound,
            show_order_opportunity_alert,
        )

        play_order_alert_sound()
        sidebar = getattr(self, "_ai_sidebar", None)
        if sidebar is not None:
            sidebar.focus_decision()
        show_order_opportunity_alert(self, decision_inner)
        return True

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
        flow_viz.schedule_refit_view(delay_ms=0)
        QTimer.singleShot(120, flow_viz.play_path)

    def _on_worker_done(self) -> None:
        """Reset in-progress flag and re-enable the submit button."""
        if not self._ui_is_alive():
            return
        try:
            self._analysis_in_progress = False
            self._auto_incremental_pending = False
            self._worker = None
            self._update_submit_button_state()

            # Reap any zombie workers / refresh loops that finished while busy
            self._reap_zombie_workers()
            self._reap_zombie_loops()

            # 分析结束后刷新持续跟踪分析哨兵，防止因分析期间新K线收盘而立即再次触发
            self._refresh_keep_analysis_sentinel()

            # Keep-analysis mode must always resume the chart so the next tick of
            # RefreshLoop can deliver bars and detect the next bar close.
            keep_analysis_on = (
                getattr(self, "_keep_analysis_checkbox", None) is not None
                and self._keep_analysis_checkbox.isChecked()
            )
            if keep_analysis_on and self._chart_refresh_paused:
                self._set_chart_refresh_paused(False)

            auto_resumed = self._maybe_auto_resume_chart_after_analysis()
            if self._last_analysis_had_error:
                msg = "分析结束（存在错误，请查看「原始」页调试信息）"
                if auto_resumed:
                    msg += "；图表已恢复实时更新"
            elif auto_resumed:
                msg = "分析完成，图表已恢复实时更新"
            else:
                msg = "分析完成"
            self._status_bar.showMessage(msg)
        except RuntimeError as exc:
            logger.debug("MainWindow UI torn down during worker cleanup: %s", exc)

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Stop background work before Qt destroys widgets."""
        self._window_closing = True
        try:
            self._cancel_analysis_worker()
            self._cancel_snapshot_fetch_worker()
            self._stop_refresh_loop()
        except RuntimeError as exc:
            logger.debug("Shutdown cleanup skipped: %s", exc)
        super().closeEvent(event)

    def showEvent(self, event: QShowEvent | None) -> None:
        """On first show, prompt for API Key when missing."""
        super().showEvent(event)
        if self._startup_api_key_check_done:
            return
        self._startup_api_key_check_done = True
        QTimer.singleShot(0, self._on_startup_api_key_check)
        if not self._startup_tv_connectivity_check_done:
            self._startup_tv_connectivity_check_done = True
            QTimer.singleShot(0, self._on_startup_tv_connectivity_check)
    def _on_startup_tv_connectivity_check(self) -> None:
        if self._current_data_source_kind() != "tradingview":
            return
        self._ensure_tradingview_reachable()

    def _on_startup_api_key_check(self) -> None:
        self._refresh_api_key_ui_state()
        if not self._has_api_key_configured():
            QMessageBox.information(
                self,
                "需要配置 API Key",
                "尚未配置 API Key，将打开设置窗口。\n"
                "请填写 API Key 并点击「保存」，才能使用「提交分析」与「增量分析」。",
            )
            self._open_settings_dialog(focus_api_key=True)

    def _has_api_key_configured(self) -> bool:
        from pa_agent.config.settings import provider_api_key_configured

        settings = getattr(self._ctx, "settings", None)
        return provider_api_key_configured(settings)

    def _refresh_api_key_ui_state(self) -> None:
        """Show or hide API Key warning and sync submit button state."""
        configured = self._has_api_key_configured()
        alert = getattr(self, "_api_key_alert_label", None)
        if alert is not None:
            alert.setVisible(not configured)
        self._sync_submit_button_state()
        status_bar = getattr(self, "_status_bar", None)
        if status_bar is None or configured:
            return
        if self._analysis_in_progress:
            return
        cur = status_bar.currentMessage() or ""
        if cur in ("就绪", "") or "API Key" in cur or "提交分析已锁定" in cur:
            status_bar.showMessage(
                "未配置 API Key：请点击左上角「AI 模型」填写后才能分析"
            )

    def _open_settings_dialog(self, *, focus_api_key: bool = False) -> None:
        """内部使用：启动时检测到 API Key 未配置时直接打开 AI 模型设置."""
        self._open_ai_model_settings_dialog(focus_api_key=focus_api_key)

    def _open_ai_model_settings_dialog(self, *, focus_api_key: bool = False) -> None:
        """打开 AI 模型设置对话框."""
        from pa_agent.gui.ai_model_settings_dialog import AIModelSettingsDialog
        from pa_agent.config.settings import Settings
        from pa_agent.util.logging import update_api_key

        settings: Settings = self._ctx.settings  # type: ignore[assignment]
        if settings is None:
            settings = Settings()

        dlg = AIModelSettingsDialog(settings, parent=self)
        if focus_api_key:
            dlg.focus_api_key_field()
        if dlg.exec():
            self._ctx.settings = settings
            from pa_agent.ai.client_factory import create_ai_client

            self._ctx.client = create_ai_client(settings.provider, logger_=logger)
            if settings is not None:
                key = getattr(settings.provider, "api_key", "") or ""
                self._debug_widget._api_key = key
                self._ai_sidebar.bind_settings(settings)
                update_api_key(key)
            self._update_ai_mode_label()
            self._refresh_api_key_ui_state()

    def _open_feishu_settings_dialog(self) -> None:
        """打开飞书机器人设置对话框."""
        from pa_agent.gui.feishu_settings_dialog import FeishuSettingsDialog
        from pa_agent.config.settings import Settings

        settings: Settings = self._ctx.settings  # type: ignore[assignment]
        if settings is None:
            settings = Settings()

        dlg = FeishuSettingsDialog(settings=settings, parent=self)
        dlg.exec()

    def _open_general_settings_dialog(self) -> None:
        """打开通用设置对话框."""
        from pa_agent.gui.general_settings_dialog import GeneralSettingsDialog
        from pa_agent.config.settings import Settings

        settings: Settings = self._ctx.settings  # type: ignore[assignment]
        if settings is None:
            settings = Settings()

        dlg = GeneralSettingsDialog(settings, parent=self)
        dlg.set_decision_flow_play_handler(self._trigger_decision_flow_playback)
        if dlg.exec():
            self._ctx.settings = settings
            self._ai_sidebar.bind_settings(settings)
            self._apply_chart_display_settings()

    def _apply_chart_display_settings(self) -> None:
        """Sync chart label font sizes and decision-flow zoom from persisted settings."""
        chart = getattr(self, "_chart_widget", None)
        settings = getattr(self._ctx, "settings", None)
        if chart is not None and settings is not None:
            chart.set_seq_label_font_pt(
                int(getattr(settings.general, "chart_seq_label_font_pt", 11) or 11)
            )
        flow_viz = getattr(self, "_decision_flow_viz_panel", None)
        if flow_viz is not None:
            flow_viz.refit_view()
            flow_viz.schedule_refit_view()

    def _update_ai_mode_label(self) -> None:
        """Show current thinking / reasoning_effort / model in the toolbar."""
        settings = getattr(self._ctx, "settings", None)
        if settings is None:
            self._ai_mode_label.setText("")
            return
        p = settings.provider
        base = (p.base_url or "").lower()
        if "deepseek.com" in base:
            thinking = "开" if p.thinking else "关"
            self._ai_mode_label.setText(
                f"思考: {thinking} · effort={p.reasoning_effort} · {p.model}"
            )
        elif "kkone.vip" in base:
            thinking = "开" if p.thinking else "关"
            effort = p.reasoning_effort if p.thinking else "—"
            self._ai_mode_label.setText(
                f"KKAI 思考: {thinking} · budget≈{effort} · {p.model}"
            )
        elif "yunwu.ai" in base:
            thinking = "开" if p.thinking else "关"
            effort = p.reasoning_effort if p.thinking else "—"
            mode = "adaptive" if "opus-4-7" in p.model or "opus-4-6" in p.model else "effort"
            self._ai_mode_label.setText(
                f"云雾 思考: {thinking} · {mode}={effort} · {p.model}"
            )
        elif "packyapi.com" in base:
            thinking = "开" if p.thinking else "关"
            effort = p.reasoning_effort if p.thinking else "—"
            mode = "adaptive" if "opus-4-7" in p.model or "opus-4-6" in p.model else "effort"
            self._ai_mode_label.setText(
                f"PackyAPI 思考: {thinking} · {mode}={effort} · {p.model}"
            )
        else:
            self._ai_mode_label.setText(
                f"模型: {p.model} · 思考={('开' if p.thinking else '关')}"
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _can_submit(self) -> bool:
        """Return True if the submit button should be enabled."""
        return self._submit_block_reason() is None

    def _submit_block_reason(self) -> str | None:
        """Human-readable reason when submit is disabled, or None if allowed."""
        if not self._has_api_key_configured():
            return "未配置 API Key，请点击左上角「AI 模型」填写后才能分析"
        if self._demo_mode:
            return "演示模式中，请退出演示后再提交真实分析"
        if self._analysis_in_progress:
            return "分析进行中"
        if self._pending_submit_after_close:
            return "等待最新K线收盘"
        if self._switching:
            return "正在切换品种/周期"
        return None

    def _sync_submit_button_state(self) -> None:
        """Enable submit button, update its label, and surface why it may be locked."""
        if not hasattr(self, "_submit_btn"):
            return
        reason = self._submit_block_reason()
        can = reason is None
        self._submit_btn.setEnabled(can)
        if hasattr(self, "_incremental_submit_btn"):
            self._incremental_submit_btn.setEnabled(can)
            if can:
                self._incremental_submit_btn.setToolTip(
                    "强制基于同品种/周期最近一条成功记录做增量分析，"
                    "不受「增量分析最大新增K线」阈值限制；"
                    "若无可用上一轮记录或 K 线无法对齐，将提示失败。"
                )
            else:
                self._incremental_submit_btn.setToolTip(reason or "")
        if can:
            self._submit_btn.setToolTip("")
        else:
            self._submit_btn.setToolTip(reason or "")

        status_bar = getattr(self, "_status_bar", None)
        if status_bar is not None and reason:
            cur = status_bar.currentMessage() or ""
            if cur in ("就绪", "") or "提交分析已锁定" in cur:
                status_bar.showMessage(f"提交分析已锁定：{reason}")

        # Update submit button label to reflect incremental availability.
        self._refresh_incremental_label()

    def _refresh_incremental_label(self) -> None:
        """Check whether an incremental base record is available and update the
        submit button label accordingly ('增量分析' vs '提交分析').

        This is a lightweight heuristic check — we only look up the prior record
        when bars are already cached; if not cached we default to '提交分析'.
        """
        if not hasattr(self, "_submit_btn"):
            return
        # Don't change label while analysis is running or button is disabled.
        if self._analysis_in_progress or not self._submit_btn.isEnabled():
            self._submit_btn.setText("提交分析")
            self._incremental_available = False
            return

        bars = self._last_frame_ready_bars
        if not bars:
            self._submit_btn.setText("提交分析")
            self._incremental_available = False
            return

        symbol = self._symbol_combo.currentText().strip() if hasattr(self, "_symbol_combo") else ""
        timeframe = self._tf_combo.currentText() if hasattr(self, "_tf_combo") else ""
        if not symbol or not timeframe:
            self._submit_btn.setText("提交分析")
            self._incremental_available = False
            return

        try:
            from pa_agent.records.analysis_history import (
                compute_incremental_bar_delta,
                find_latest_successful_record,
            )
            from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS

            bar_count = self._analysis_bar_count()
            frame = self._build_chart_frame_from_bars(
                bars,
                include_forming=False,
                bar_count=bar_count,
            )
            if frame is None:
                raise ValueError("frame is None")

            previous = find_latest_successful_record(symbol=symbol, timeframe=timeframe)
            if previous is None:
                raise ValueError("no prior record")

            settings = getattr(self._ctx, "settings", None)
            threshold = int(
                getattr(getattr(settings, "general", None), "incremental_max_new_bars", 10)
            )

            delta = compute_incremental_bar_delta(frame, previous)
            if delta is None:
                raise ValueError("cannot align")

            if threshold > 0 and delta.new_count <= threshold:
                self._incremental_available = True
                self._submit_btn.setText("增量分析")
                return
        except Exception:  # noqa: BLE001
            pass

        self._incremental_available = False
        self._submit_btn.setText("提交分析")

    def _update_submit_button_state(self) -> None:
        """Enable or disable the submit button based on current state."""
        self._sync_submit_button_state()

    def _build_chart_frame_from_bars(
        self,
        bars_raw: Any,
        *,
        bar_count: int | None = None,
        include_forming: bool = False,
    ) -> Any:
        """Build chart KlineFrame.

        - include_forming=True: forming + N closed (legacy; causes chart to shrink on submit)
        - include_forming=False: N closed only (chart + AI; K1 = newest closed bar)
        """
        from pa_agent.data.snapshot import build_display_frame, build_live_frame

        n = bar_count if bar_count is not None else self._analysis_bar_count()
        symbol = self._symbol_combo.currentText().strip()
        timeframe = self._tf_combo.currentText()
        now_ms = self._reference_now_ms()
        if not bars_raw:
            return None
        if include_forming:
            return build_live_frame(
                bars_raw, n, symbol, timeframe, now_ms=now_ms
            )
        return build_display_frame(
            bars_raw, n, symbol, timeframe, now_ms=now_ms
        )

    def _take_snapshot(
        self,
        symbol: str,
        timeframe: str,
        bar_count: int,
        *,
        bars_raw: Any = None,
    ) -> Any:
        """Snapshot for analysis: *bar_count* closed bars (newest forming bar excluded)."""
        try:
            if bars_raw is None:
                bars_raw = self._bars_for_analysis_submit(bar_count)
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
        """Return a callable that refreshes/freeze chart then exports its K-line table."""

        def _snapshot() -> str:
            return self.snapshot_klines_for_followup()

        return _snapshot

    def _build_orchestrator(self) -> Any:
        """Build a TwoStageOrchestrator from ctx components, or return None."""
        try:
            from pa_agent.orchestrator.two_stage import TwoStageOrchestrator

            client = getattr(self._ctx, "client", None)
            assembler = getattr(self._ctx, "assembler", None)
            router = getattr(self._ctx, "router", None)
            validator = getattr(self._ctx, "validator", None)
            pending_writer = getattr(self._ctx, "pending_writer", None)
            exp_reader = getattr(self._ctx, "exp_reader", None)
            settings = getattr(self._ctx, "settings", None)

            if any(
                x is None
                for x in [client, assembler, router, validator,
                           pending_writer, exp_reader]
            ):
                return None

            return TwoStageOrchestrator(
                client=client,
                assembler=assembler,
                router=router,
                validator=validator,
                pending_writer=pending_writer,
                exp_reader=exp_reader,
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not build orchestrator: %s", exc)
            return None
