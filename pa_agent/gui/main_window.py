"""Main application window for PA Agent."""
from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import QThread, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
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

from pa_agent.gui.widgets.flow_bar import FlowBar
from pa_agent.gui.widgets.summary_strip import SummaryStrip
from pa_agent.gui.widgets.toast import ToastOverlay
from pa_agent.gui.widgets.chart_panel import ChartPanel
from pa_agent.gui.widgets.model_selector import ModelSelector
from pa_agent.gui.widgets.status_bar import EnhancedStatusBar
from pa_agent.gui.support_resistance import (
    extract_structure_levels,
    filter_levels_near_price,
    format_level,
)

from pa_agent.app_context import AppContext
from pa_agent.gui.validation_debug_dialog import show_validation_debug_dialog

logger = logging.getLogger(__name__)

# Zombie timeout in milliseconds (5 seconds)
_WORKER_JOIN_TIMEOUT_MS = 5000


def _ensure_data_source_connected(
    data_source: Any,
    *,
    symbol: str,
    timeframe: str,
    settings: Any = None,
    tv_exchange: str | None = None,
) -> None:
    """Connect and subscribe the current GUI data source if startup left it disconnected."""
    if data_source is None or getattr(data_source, "_connected", False):
        return

    from pa_agent.data.factory import configure_data_source

    kind = getattr(getattr(settings, "general", None), "last_data_source", None)
    configure_data_source(data_source, kind, settings, tv_exchange=tv_exchange)

    data_source.connect()
    data_source.subscribe(symbol, timeframe)


def _format_data_source_error(exc: Exception) -> str:
    """Return a user-facing data-source error message for GUI status/toast."""
    raw = str(exc)
    lowered = raw.lower()
    if "rqdata" in lowered and (
        "license" in lowered or "auth failed" in lowered or "404" in lowered
    ):
        return (
            "RQData License Key 无效或未绑定。"
            "请在设置中更新有效 License，或切换到 TradingView/MT5 数据源。"
        )
    if "not connected" in lowered:
        return "数据源未连接，请检查数据源配置后重新获取数据。"
    return f"数据源连接失败：{raw}"


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
        self.start_time: float | None = None
        self.elapsed_s: float | None = None
        self.stage1_tokens: int = 0
        self.stage2_tokens: int = 0

    def run(self) -> None:
        import time as _time
        from pa_agent.util.threading import OrchestratorEvent

        self.start_time = _time.monotonic()

        _EVENT_LABELS = {
            OrchestratorEvent.Stage1Started: "阶段一分析中…",
            OrchestratorEvent.Stage1Done: "阶段一完成",
            OrchestratorEvent.Stage2Started: "阶段二分析中…",
            OrchestratorEvent.Stage2Done: "阶段二完成",
            OrchestratorEvent.RecordSaved: "记录已保存",
            OrchestratorEvent.Cancelled: "已取消",
            OrchestratorEvent.InsufficientData: "数据不足",
            OrchestratorEvent.Stage1Failed: "阶段一失败",
            OrchestratorEvent.Stage2Failed: "阶段二失败",
        }

        def on_event(event: OrchestratorEvent) -> None:
            label = _EVENT_LABELS.get(event, str(event))
            self.status_update.emit(label)

        def on_stage1_reasoning(chunk: str) -> None:
            self.stage1_tokens += len(chunk)
            self.reasoning_token.emit("stage1", chunk)

        def on_stage1_content(chunk: str) -> None:
            self.stage1_tokens += len(chunk)
            self.content_token.emit("stage1", chunk)

        def on_stage2_reasoning(chunk: str) -> None:
            self.stage2_tokens += len(chunk)
            self.reasoning_token.emit("stage2", chunk)

        def on_stage2_content(chunk: str) -> None:
            self.stage2_tokens += len(chunk)
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
            logger.error("Analysis worker error: %s", exc, exc_info=True)
            decision = {}
            record = None  # type: ignore[assignment]
            self.error_occurred.emit(str(exc))

        self.elapsed_s = _time.monotonic() - self.start_time

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
        self._cancel_token: Any = None
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
        self._free_chat_session: Any = None
        self._last_stage1_diagnosis: dict | None = None
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

        self._toast = ToastOverlay(self)

        self._central = self._build_central_widget()
        self.setCentralWidget(self._central)

        # Legacy demo mode label (not attached to layout in new design)
        self._demo_mode_label = QLabel("")
        self._demo_mode_label.setStyleSheet(
            "color: #e6b800; font-weight: 600; padding-left: 4px;"
        )
        self._demo_mode_label.hide()

        self._status_bar.showMessage("就绪")
        self._refresh_api_key_ui_state()

    def _build_central_widget(self) -> QWidget:
        """Build the new 5-row central widget layout."""
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Row 1: Header ────────────────────────────────────────────────────
        header = QWidget()
        header.setFixedHeight(44)
        header.setStyleSheet(
            "background-color: #161b22; border-bottom: 1px solid #30363d;"
        )
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(14, 0, 14, 0)
        h_layout.setSpacing(10)

        title = QLabel("PA Agent")
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #e6edf3;")
        h_layout.addWidget(title)

        subtitle = QLabel(
            "Trading Terminal · 分析仅供参考，不构成投资建议"
        )
        subtitle.setStyleSheet("font-size: 12px; color: #8b949e;")
        h_layout.addWidget(subtitle)
        h_layout.addStretch()

        self._model_selector = ModelSelector()
        self._model_selector.clicked.connect(self._on_model_selector_clicked)
        h_layout.addWidget(self._model_selector)

        self._header_api_pill = QLabel("API 未配置")
        self._header_api_pill.setStyleSheet(
            "background-color: #3d2a00; color: #ffb86c; padding: 2px 10px; "
            "border-radius: 999px; font-size: 11px;"
        )
        h_layout.addWidget(self._header_api_pill)

        self._settings_btn = QPushButton("设置")
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.setToolTip("打开设置")
        self._settings_btn.setStyleSheet(
            "QPushButton {"
            "  height: 24px;"
            "  padding: 0 10px;"
            "  border: 1px solid #30363d;"
            "  border-radius: 6px;"
            "  background-color: #21262d;"
            "  color: #c9d1d9;"
            "  font-size: 12px;"
            "}"
            "QPushButton:hover {"
            "  background-color: #30363d;"
            "}"
        )
        self._settings_btn.clicked.connect(lambda _checked=False: self._open_settings_dialog())
        h_layout.addWidget(self._settings_btn)

        layout.addWidget(header)

        # ── Row 2: FlowBar ───────────────────────────────────────────────────
        self._flow_bar = FlowBar()
        self._flow_bar.reset_all()
        layout.addWidget(self._flow_bar)

        # ── Row 3: Toolbar ───────────────────────────────────────────────────
        toolbar = QWidget()
        toolbar.setStyleSheet("background-color: #0d1117;")
        t_layout = QHBoxLayout(toolbar)
        t_layout.setContentsMargins(8, 6, 8, 6)
        t_layout.setSpacing(8)

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
        self._active_data_source_kind = _last_ds

        # Left group
        left_group = QWidget()
        left_group.setStyleSheet(
            "background-color: #161b22; border-radius: 6px;"
        )
        lg_layout = QHBoxLayout(left_group)
        lg_layout.setContentsMargins(8, 4, 8, 4)
        lg_layout.setSpacing(6)

        lg_layout.addWidget(QLabel("数据来源:"))
        self._data_source_combo = QComboBox()
        for kind, label in DATA_SOURCE_CHOICES:
            self._data_source_combo.addItem(label, kind)
        ds_index = self._data_source_combo.findData(_last_ds)
        if ds_index >= 0:
            self._data_source_combo.setCurrentIndex(ds_index)
        self._data_source_combo.setMinimumWidth(108)
        self._data_source_combo.setToolTip(
            "K 线数据来源：MT5（需终端登录）、TradingView（tvDatafeed）、RQData（米筐）"
        )
        self._data_source_combo.currentIndexChanged.connect(
            self._on_data_source_combo_changed
        )
        lg_layout.addWidget(self._data_source_combo)

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
        lg_layout.addWidget(self._tv_exchange_label)
        lg_layout.addWidget(self._tv_exchange_combo)

        lg_layout.addWidget(QLabel("品种:"))
        self._symbol_combo = QComboBox()
        self._symbol_combo.setEditable(True)
        self._symbol_combo.setCurrentText(_last_symbol)
        self._symbol_combo.setMinimumWidth(110)
        self._apply_data_source_symbol_placeholder()
        lg_layout.addWidget(self._symbol_combo)
        self._populate_symbol_combo_for_source()

        self._symbol_alert_label = QLabel("")
        self._symbol_alert_label.setStyleSheet("color: #f85149; font-size: 11px;")
        self._symbol_alert_label.setWordWrap(True)
        self._symbol_alert_label.hide()
        lg_layout.addWidget(self._symbol_alert_label)

        lg_layout.addWidget(QLabel("周期:"))
        self._tf_combo = QComboBox()
        self._tf_combo.addItems(["1m", "5m", "15m", "1h", "4h", "1d"])
        self._tf_combo.setCurrentText(_last_tf)
        self._tf_combo.setMinimumWidth(60)
        lg_layout.addWidget(self._tf_combo)
        self._populate_timeframe_combo_for_source()
        self._sync_tv_exchange_visibility()

        self._fetch_data_btn = QPushButton("获取数据")
        self._fetch_data_btn.setObjectName("primaryButton")
        self._fetch_data_btn.setMinimumWidth(90)
        self._fetch_data_btn.setToolTip(
            "开始从当前数据源持续拉取 K 线数据并实时更新图表"
        )
        self._fetch_data_btn.clicked.connect(self._on_fetch_data_clicked)
        lg_layout.addWidget(self._fetch_data_btn)

        t_layout.addWidget(left_group)

        # Middle group
        mid_group = QWidget()
        mid_group.setStyleSheet(
            "background-color: #161b22; border-radius: 6px;"
        )
        mg_layout = QHBoxLayout(mid_group)
        mg_layout.setContentsMargins(8, 4, 8, 4)
        mg_layout.setSpacing(6)

        self._wait_close_checkbox = QCheckBox("等待最新K线收盘后再提交分析")
        self._wait_close_checkbox.setObjectName("waitCloseCheckbox")
        self._wait_close_checkbox.setChecked(False)
        self._wait_close_checkbox.setToolTip(
            "勾选后，点击提交分析将先等待当前未收盘K线走完，再抓取数据并开始分析"
        )
        self._wait_close_checkbox.stateChanged.connect(
            self._on_wait_close_checkbox_changed
        )
        mg_layout.addWidget(self._wait_close_checkbox)

        self._wait_close_countdown_label = QLabel("")
        self._wait_close_countdown_label.setObjectName("mutedLabel")
        self._wait_close_countdown_label.setMinimumWidth(100)
        mg_layout.addWidget(self._wait_close_countdown_label)

        mg_layout.addWidget(QLabel("分析模式:"))
        self._speed_profile_combo = QComboBox()
        self._speed_profile_combo.setMinimumWidth(132)
        self._speed_profile_combo.setToolTip(
            "选择下一次提交分析使用的二阶段分析过程；原始模式保留完整上下文，优化模式减少重复上下文以提速"
        )
        from pa_agent.gui.analysis_modes import analysis_mode_choices, infer_analysis_mode_key

        for key, label in analysis_mode_choices():
            self._speed_profile_combo.addItem(label, key)
        profile_key = infer_analysis_mode_key(getattr(self._ctx, "settings", None))
        profile_idx = self._speed_profile_combo.findData(profile_key)
        if profile_idx >= 0:
            self._speed_profile_combo.setCurrentIndex(profile_idx)
        self._speed_profile_combo.currentIndexChanged.connect(
            self._on_speed_profile_changed
        )
        mg_layout.addWidget(self._speed_profile_combo)

        t_layout.addWidget(mid_group)

        # Right group
        right_group = QWidget()
        right_group.setStyleSheet(
            "background-color: #161b22; border-radius: 6px;"
        )
        rg_layout = QHBoxLayout(right_group)
        rg_layout.setContentsMargins(8, 4, 8, 4)
        rg_layout.setSpacing(6)

        self._submit_btn = QPushButton("提交分析")
        self._submit_btn.setObjectName("primaryButton")
        self._submit_btn.setMinimumWidth(100)
        self._submit_btn.clicked.connect(self._on_submit_analysis)
        rg_layout.addWidget(self._submit_btn)

        self._incremental_submit_btn = QPushButton("增量分析")
        self._incremental_submit_btn.setMinimumWidth(100)
        self._incremental_submit_btn.setToolTip(
            "强制基于同品种/周期最近一条成功记录做增量分析，"
            "不受「增量分析最大新增K线」阈值限制；"
            "若无可用上一轮记录或 K 线无法对齐，将提示失败。"
        )
        self._incremental_submit_btn.clicked.connect(
            self._on_submit_incremental_analysis
        )
        rg_layout.addWidget(self._incremental_submit_btn)

        self._demo_btn = QPushButton("演示模式")
        self._demo_btn.setToolTip(
            "用 records/pending 中已保存的分析记录回放界面"
        )
        self._demo_btn.clicked.connect(self._on_demo_mode_button)
        rg_layout.addWidget(self._demo_btn)

        self._resume_chart_btn = QPushButton("图表实时更新")
        self._resume_chart_btn.setEnabled(False)
        self._resume_chart_btn.setToolTip(
            "恢复 K 线实时刷新；最右侧未收盘 K 线为浅色空心 K 线，不参与 AI 分析"
        )
        self._resume_chart_btn.clicked.connect(self._on_resume_chart_refresh)
        rg_layout.addWidget(self._resume_chart_btn)

        self._fit_chart_btn = QPushButton("恢复图表")
        self._fit_chart_btn.setToolTip(
            "自动调整图表缩放，将 K 线和价格线适配到可视区域"
        )
        self._fit_chart_btn.clicked.connect(self._on_fit_chart)
        rg_layout.addWidget(self._fit_chart_btn)

        t_layout.addWidget(right_group)
        layout.addWidget(toolbar)

        # Hidden legacy widgets for backward compatibility
        self._decision_badge = QLabel("")
        self._decision_badge.setObjectName("mutedLabel")
        self._decision_badge.hide()

        self._ai_mode_label = QLabel("")
        self._ai_mode_label.setObjectName("mutedLabel")
        self._ai_mode_label.hide()

        self._api_key_alert_label = QLabel(
            "未配置 API Key：请点击顶部「设置」按钮，在设置中填写 API Key 后才能进行 AI 分析。"
        )
        self._api_key_alert_label.setWordWrap(True)
        self._api_key_alert_label.setStyleSheet(
            "background-color: #3d2a00; color: #ffb86c; padding: 8px 10px; "
            "border: 1px solid #8a6d2f; border-radius: 4px; font-weight: 600;"
        )
        self._api_key_alert_label.hide()

        self._disclaimer_label = QLabel("分析仅供参考，不构成投资建议")
        self._disclaimer_label.setObjectName("mutedLabel")
        self._disclaimer_label.setWordWrap(True)
        self._disclaimer_label.setStyleSheet(
            "color: #8b949e; font-size: 11px; padding: 2px 0;"
        )
        self._disclaimer_label.hide()

        # ── Row 4: Workspace (ChartPanel + AI Panel) ─────────────────────────
        workspace = QSplitter(Qt.Orientation.Horizontal)

        self._chart_panel = ChartPanel()
        self._chart_widget = self._chart_panel.chart_widget()
        self._chart_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._apply_chart_display_settings()
        workspace.addWidget(self._chart_panel)

        ai_container = QWidget()
        ai_layout = QVBoxLayout(ai_container)
        ai_layout.setContentsMargins(0, 0, 0, 0)
        ai_layout.setSpacing(0)
        self._summary_strip = SummaryStrip()
        ai_layout.addWidget(self._summary_strip)
        ai_layout.addWidget(self._ai_sidebar, stretch=1)

        self._ai_sidebar.setMinimumWidth(400)
        workspace.addWidget(ai_container)

        workspace.setStretchFactor(0, 3)
        workspace.setStretchFactor(1, 2)

        layout.addWidget(workspace, stretch=1)

        # ── Row 5: StatusBar ─────────────────────────────────────────────────
        self._status_bar = EnhancedStatusBar()
        layout.addWidget(self._status_bar)

        # Timers
        from PyQt6.QtCore import QTimer as _QTimer

        self._last_refresh_ts: float = 0.0
        self._elapsed_ticker = _QTimer(central)
        self._elapsed_ticker.setInterval(1000)
        self._elapsed_ticker.timeout.connect(self._update_refresh_elapsed)
        self._elapsed_ticker.start()

        self._symbol_switch_timer = QTimer(self)
        self._symbol_switch_timer.setSingleShot(True)
        self._symbol_switch_timer.setInterval(500)
        self._symbol_switch_timer.timeout.connect(self._flush_deferred_symbol_switch)
        self._symbol_combo.currentTextChanged.connect(
            self._on_symbol_combo_text_changed
        )
        sym_line = self._symbol_combo.lineEdit()
        if sym_line is not None:
            sym_line.editingFinished.connect(self._on_symbol_combo_editing_finished)
        self._tf_combo.currentTextChanged.connect(
            lambda _: self._on_symbol_or_tf_changed(
                self._symbol_combo.currentText(), self._tf_combo.currentText()
            )
        )

        return central

    def _connect_event_bus(self) -> None:
        """Wire EventBus signals to status bar and tab slots (if bus is ready)."""
        bus = self._ctx.event_bus
        if bus is None:
            return
        bus.status.connect(self._on_status_update)

    def _start_refresh_loop(self) -> None:
        """Start the RefreshLoop only when the data source is connected."""
        # Reap any zombie loops before starting a fresh one
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
        if self._current_data_source_kind() == "akshare" and interval_ms < 2500:
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
        if hasattr(data_source, "set_exchange"):
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
        # Stop any running refresh — user must click "获取数据" to re-fetch
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

    def _apply_data_source_symbol_placeholder(self) -> None:
        line = self._symbol_combo.lineEdit()
        if line is None:
            return
        kind = self._current_data_source_kind()
        if kind == "tradingview":
            line.setPlaceholderText(
                "A股 6 位 / 港股 1810 / 名称 小米集团；交易所可自动；或 XAUUSD+OANDA"
            )
        elif kind == "akshare":
            line.setPlaceholderText("A股 6 位代码，如 600519；指数 000300 或 sh000300")
        else:
            line.setPlaceholderText("输入 MT5 品种名，如 XAUUSDm…")

    def _populate_symbol_combo_for_source(self) -> None:
        """Refresh symbol suggestions for the active data source."""
        from pa_agent.data.factory import default_symbol_for_kind

        data_source = getattr(self._ctx, "data_source", None)
        current = self._symbol_combo.currentText().strip()
        kind = self._current_data_source_kind()
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
        from pa_agent.data.factory import (
            configure_data_source,
            create_data_source,
            data_source_label,
        )

        if self._switching:
            return
        self._switching = True
        try:
            if self._worker is not None and self._worker.isRunning():
                if self._cancel_token is not None:
                    self._cancel_token.set()
                self._worker.wait(_WORKER_JOIN_TIMEOUT_MS)
                self._worker = None
            self._analysis_in_progress = False
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
            if hasattr(new_source, "on_probe_status"):
                new_source.on_probe_status = self._on_tv_probe_status
            settings = getattr(self._ctx, "settings", None)
            configure_data_source(new_source, kind, settings)
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

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_symbol_combo_text_changed(self, _text: str = "") -> None:
        """Debounce symbol edits so partial codes (00→600519) do not spam subscribe."""
        self._update_symbol_data_alert()
        sym = self._symbol_combo.currentText()
        tf = self._tf_combo.currentText()
        self._pending_symbol_switch = (sym, tf)
        if self._symbol_switch_timer is not None:
            self._symbol_switch_timer.start()

    def _on_symbol_combo_editing_finished(self) -> None:
        """Apply symbol change immediately when the user leaves the field."""
        if self._symbol_switch_timer is not None:
            self._symbol_switch_timer.stop()
        sym = self._symbol_combo.currentText()
        tf = self._tf_combo.currentText()
        self._pending_symbol_switch = None
        self._on_symbol_or_tf_changed(sym, tf)

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
        self._status_bar.showMessage(text)
        if "阶段一分析中" in text:
            self._flow_bar.set_step_status(2, "active")
            self._flow_bar.set_step_caption(2, text)
        elif "阶段一完成" in text:
            self._flow_bar.set_step_status(2, "done")
            self._flow_bar.set_step_caption(2, "阶段一完成")
            self._flow_bar.set_step_status(3, "active")
            self._flow_bar.set_step_caption(3, "阶段二决策中...")
        elif "阶段二分析中" in text:
            self._flow_bar.set_step_status(3, "active")
            self._flow_bar.set_step_caption(3, text)
        elif "阶段二完成" in text:
            self._flow_bar.set_step_status(3, "done")
            self._flow_bar.set_step_caption(3, "阶段二完成")
        if text == "数据延迟":
            self._update_symbol_data_alert()
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
        self._chart_panel.set_status("live")
        self._toast.show_toast("图表已恢复实时更新", "info")

    def _on_fetch_data_clicked(self) -> None:
        """Start (or restart) continuous data refresh for the current symbol/timeframe."""
        data_source = getattr(self._ctx, "data_source", None)
        if data_source is None:
            self._status_bar.showMessage("数据源未初始化，请先切换数据来源")
            self._toast.show_toast("数据源未初始化", "warning")
            return
        if not getattr(data_source, "_connected", False):
            symbol = self._symbol_combo.currentText().strip()
            timeframe = self._tf_combo.currentText()
            try:
                _ensure_data_source_connected(
                    data_source,
                    symbol=symbol,
                    timeframe=timeframe,
                    settings=getattr(self._ctx, "settings", None),
                    tv_exchange=self._tv_exchange_text(),
                )
            except Exception as exc:  # noqa: BLE001
                msg = _format_data_source_error(exc)
                logger.warning(msg)
                self._status_bar.showMessage(msg)
                self._toast.show_toast(msg, "warning")
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
        # Stop any existing loop first so we can start fresh
        self._stop_refresh_loop()
        self._set_chart_refresh_paused(False)
        self._start_refresh_loop()
        self._flow_bar.set_step_status(0, "done")
        self._flow_bar.set_step_caption(0, "已连接")
        self._toast.show_toast("正在获取数据...", "info")
        self._chart_panel.set_status("live")

    def _on_fit_chart(self) -> None:
        """Auto-fit chart view to show recent bars with proper price range."""
        chart = getattr(self, "_chart_widget", None)
        if chart is None:
            self._toast.show_toast("图表尚未初始化", "warning")
            return
        if chart.fit_view():
            self._status_bar.showMessage("图表已恢复默认缩放")
            self._toast.show_toast("图表已恢复默认缩放", "info")
        else:
            self._status_bar.showMessage("暂无 K 线数据，请先点击「获取数据」")
            self._toast.show_toast("暂无 K 线数据，请先点击「获取数据」", "warning")

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

        text = ""
        style = ""
        if self._pending_submit_after_close:
            secs = self._forming_bar_seconds_remaining()
            if secs is not None:
                text = f"等待K线收盘，还剩 {secs}s"
            else:
                text = "等待最新K线收盘…"
            style = "color: #58a6ff; font-size: 11px;"
        elif self._wait_close_checkbox.isChecked():
            secs = self._forming_bar_seconds_remaining()
            if secs is not None:
                text = f"距最新K线收盘还剩 {secs}s"
            else:
                text = "距最新K线收盘: —"
            style = "color: #58a6ff; font-size: 11px;"
        elif self._chart_refresh_paused:
            text = "图表刷新已暂停"
            style = "color: #e6b800; font-size: 11px;"
        elif self._last_refresh_ts == 0.0:
            text = "距上次刷新: —"
        else:
            elapsed = int(_time.monotonic() - self._last_refresh_ts)
            if elapsed < 60:
                text = f"距上次刷新: {elapsed}s"
            else:
                m, s = divmod(elapsed, 60)
                text = f"距上次刷新: {m}m{s:02d}s"
            if elapsed > 10:
                style = "color: #f85149; font-size: 11px;"
            else:
                style = ""

        label = getattr(self, "_refresh_elapsed_label", None)
        if label is not None:
            label.setText(text)
            if style:
                label.setStyleSheet(style)
            else:
                label.setObjectName("mutedLabel")
                label.setStyleSheet("")

        panel = getattr(self, "_chart_panel", None)
        if panel is not None:
            panel.set_footer_price(text.replace("距上次刷新: ", "距上次刷新 "))

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

        if self._pending_submit_after_close and bars:
            self._check_pending_bar_close(bars)

        if self._chart_refresh_paused:
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
            self._flow_bar.set_step_status(1, "done")
            self._flow_bar.set_step_caption(1, "已获取 K 线")
            symbol = self._symbol_combo.currentText().strip()
            tf = self._tf_combo.currentText()
            self._chart_panel.set_title(symbol, tf)
            bar_count = len(bars) if bars else 0
            self._chart_panel.set_meta(f"{bar_count} 根 K 线 · EMA20")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Frame build skipped: %s", exc)

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

            # ── Step 3: Unsubscribe, clear cached snapshot, re-subscribe ───────
            data_source = getattr(self._ctx, "data_source", None)
            if data_source is not None:
                try:
                    data_source.unsubscribe()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("unsubscribe failed: %s", exc)
            self._last_frame_ready_bars = None
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

            self._status_bar.showMessage(
                self._status_message_after_symbol_switch(new_symbol, new_tf)
            )
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

            # Check for prior analysis record — if found, auto-trigger incremental
            self._check_auto_incremental(new_symbol, new_tf)
            self._flow_bar.reset_all()
            self._summary_strip.reset()
            self._chart_panel.set_title(new_symbol, new_tf)
            self._chart_panel.set_status("live")

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
            self._status_bar.showMessage("数据源未连接")
            return False

        bars_raw = self._bars_for_analysis_submit(bar_count)
        if not bars_raw:
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
                f"等待当前K线收盘…还剩 {secs} 秒（{ts_hint}，收盘后将自动{submit_hint}）"
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
        """Handle the '提交分析' button click."""
        self._begin_submit_analysis(force_incremental=False)

    def _on_submit_incremental_analysis(self) -> None:
        """Handle the '增量分析' button click — always try incremental mode."""
        self._begin_submit_analysis(force_incremental=True)

    def _begin_submit_analysis(self, *, force_incremental: bool) -> None:
        """Shared entry for normal and forced-incremental submit buttons."""
        if not self._can_submit():
            return

        # Clear auto-incremental flag — user initiated analysis manually
        self._auto_incremental_pending = False

        # Cancel any existing worker before starting a new one
        if self._worker is not None and self._worker.isRunning():
            if self._cancel_token is not None:
                self._cancel_token.set()
            self._worker.wait(_WORKER_JOIN_TIMEOUT_MS)
            self._worker = None

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

        self._flow_bar.set_step_status(2, "active")
        self._flow_bar.set_step_caption(2, "阶段一诊断中...")
        self._toast.show_toast("分析已提交", "info")
        self._chart_panel.set_status("snapshot", "快照冻结 · AI 分析中")
        self._summary_strip.reset()
        self._status_bar.set_tps(0)
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
        if data_source is None:
            self._status_bar.showMessage("数据源未初始化")
            return
        if not getattr(data_source, "_connected", False):
            try:
                _ensure_data_source_connected(
                    data_source,
                    symbol=symbol,
                    timeframe=timeframe,
                    settings=getattr(self._ctx, "settings", None),
                    tv_exchange=self._tv_exchange_text(),
                )
            except Exception as exc:  # noqa: BLE001
                msg = _format_data_source_error(exc)
                logger.warning(msg)
                self._status_bar.showMessage(msg)
                self._toast.show_toast(msg, "warning")
                return

        if not getattr(data_source, "_connected", False):
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
        """Continue analysis once K-line bars are available (caller thread = UI)."""
        self._sync_buffer_from_snapshot_bars(snapshot_bars)
        frame = self._take_snapshot(
            symbol, timeframe, bar_count, bars_raw=snapshot_bars
        )
        if frame is None:
            self._status_bar.showMessage("数据不足，请等待图表刷新后再提交")
            return

        orchestrator = self._build_orchestrator()
        if orchestrator is None:
            self._status_bar.showMessage("编排器未就绪，请检查设置")
            return

        previous_record, incremental_new_bar_count, incremental_detail = (
            self._find_incremental_base_record(
                frame,
                symbol,
                timeframe,
                force_incremental=force_incremental,
            )
        )
        if force_incremental and previous_record is None:
            reason = self._incremental_unavailable_reason(frame, symbol, timeframe)
            self._status_bar.showMessage(reason)
            QMessageBox.warning(self, "无法增量分析", reason)
            return

        # Create cancel token
        from pa_agent.util.threading import CancelToken

        self._cancel_token = CancelToken()

        # Start worker in its own QThread (worker IS a QThread subclass)
        self._worker = _AnalysisWorker(
            orchestrator=orchestrator,
            frame=frame,
            cancel_token=self._cancel_token,
            previous_record=previous_record,
            incremental_new_bar_count=incremental_new_bar_count,
            parent=None,
        )
        self._worker.finished.connect(self._on_analysis_finished)
        self._worker.record_ready.connect(self._on_record_ready)
        self._worker.error_occurred.connect(self._on_analysis_error)
        self._worker.status_update.connect(self._on_status_update)
        self._worker.finished.connect(lambda _: self._on_worker_done())

        panel = getattr(self, "_stream_panel", None)
        if panel is not None:
            self._worker.stage_prompt_ready.connect(panel.on_stage_prompt_ready)
            self._worker.reasoning_token.connect(panel.on_reasoning_token)
            self._worker.content_token.connect(panel.on_content_token)

        # Freeze on closed-only frame; immediate redraw so chart matches the AI table.
        self._chart_widget.set_frame_now(frame, fit_view=True)

        self._set_chart_refresh_paused(True)

        self._analysis_in_progress = True
        self._last_analysis_had_error = False
        self._update_submit_button_state()
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

        self._worker.stage2_files_ready.connect(
            self._on_stage2_files_ready,
            Qt.ConnectionType.UniqueConnection,
        )
        self._worker.start()

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

    def _on_analysis_finished(self, decision: dict) -> None:
        """Called on the main thread when the AI worker completes.

        *decision* is the full stage2 JSON dict (``{"decision": {...},
        "diagnosis_summary": {...}}``).  The chart and panel widgets expect
        the inner ``decision`` sub-dict, so we extract it here.
        """
        if decision:
            inner = decision.get("decision", decision)
            # Carry next_bar_prediction from top-level into inner dict
            # so DecisionPanel._apply_next_bar_prediction can find it.
            if "next_bar_prediction" in decision and "next_bar_prediction" not in inner:
                inner = {**inner, "next_bar_prediction": decision["next_bar_prediction"]}
            self._chart_widget.set_decision(inner)
            levels = extract_structure_levels(decision)
            frame = self._chart_widget.displayed_frame()
            if frame is not None:
                levels = filter_levels_near_price(levels, frame.bars)
            self._chart_widget.set_structure_levels(levels)
            if getattr(self, "_demo_mode", False):
                self._chart_widget.fit_view()
            stance = None
            if self._ctx.settings is not None:
                stance = getattr(self._ctx.settings.general, "decision_stance", None)
            self._decision_panel.set_decision(
                inner,
                diagnosis_summary=decision.get("diagnosis_summary"),
                stage1_diagnosis=self._last_stage1_diagnosis,
                decision_stance=stance,
            )
            self._bind_decision_tree(decision, self._last_stage1_diagnosis)
            order = inner.get("order_type", "—")
            self._decision_badge.setText(f"决策: {order}")
            if getattr(self, "_demo_mode", False):
                self._present_decision_flow_playback(force_play=True)

            self._flow_bar.set_step_status(3, "done")
            self._flow_bar.set_step_caption(2, "阶段一完成")
            self._flow_bar.set_step_caption(3, "阶段二完成")
            self._flow_bar.set_step_status(4, "active")
            self._flow_bar.set_step_caption(4, "可继续追问")
            self._chart_panel.set_status("snapshot", "决策已生成")
            self._toast.show_toast("分析完成 · 决策已生成", "ok")

            order_type = inner.get("order_type", "—")
            elapsed = getattr(self._worker, 'elapsed_s', None) if self._worker else None
            total_tokens = (
                (self._worker.stage1_tokens + self._worker.stage2_tokens)
                if self._worker else 0
            )
            tps = (total_tokens / elapsed) if elapsed and elapsed > 0 else 0.0

            # 方向概率：从 next_bar_prediction 提取
            next_pred = inner.get("next_bar_prediction", {})
            probs = next_pred.get("probabilities", {}) if isinstance(next_pred, dict) else {}
            if probs:
                parts = []
                for k, v in probs.items():
                    label = {"bullish": "多", "bearish": "空", "neutral": "中"}.get(k, k)
                    parts.append(f"{label} {v}%")
                direction_prob = " / ".join(parts)
            else:
                direction_prob = inner.get("order_direction", "—") or "—"

            # Prefer structural levels when available; fall back to TP/SL.
            tp_raw = inner.get("take_profit_price")
            sl_raw = inner.get("stop_loss_price")

            resistance = next((lvl for lvl in levels if lvl.kind == "resistance"), None)
            support = next((lvl for lvl in levels if lvl.kind == "support"), None)
            tp_str = format_level(resistance) if resistance is not None else (
                f"{tp_raw:.1f}" if tp_raw is not None else "—"
            )
            sl_str = format_level(support) if support is not None else (
                f"{sl_raw:.1f}" if sl_raw is not None else "—"
            )

            metrics = {
                "最终动作": order_type or "—",
                "方向概率": direction_prob,
                "关键上破": tp_str,
                "支撑区": sl_str,
                "耗时": f"{elapsed:.1f}s" if elapsed else "—",
            }
            self._summary_strip.set_metrics(metrics)
            self._status_bar.set_tps(tps)
        else:
            self._chart_widget.clear_decision_overlay()
            self._chart_widget.clear_structure_levels()
            self._decision_panel.clear()
            self._decision_tree_panel.clear()
            if getattr(self, "_decision_flow_viz_panel", None) is not None:
                self._decision_flow_viz_panel.clear()
            self._decision_badge.setText("")

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
        if stage == "stage2":
            parts.append(
                "【说明】阶段二校验失败后不会自动重试 API；"
                "请根据下方信息修改提示词/模型输出或手动重新「提交分析」。\n"
            )
        elif stage == "stage1":
            parts.append(
                "【说明】阶段一校验失败后不会自动重试 API；"
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
            "2) 在「设置」里关闭「Thinking」后重试。\n\n"
            f"诊断摘要：{key}"
        )
        btn_open = box.addButton("打开设置", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("知道了", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == btn_open:
            self._open_settings_dialog()

    def _on_analysis_error(self, message: str) -> None:
        """Unhandled exception in the analysis worker thread."""
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
        self._flow_bar.set_step_status(
            2 if "stage1" in message.lower() else 3, "error"
        )
        self._toast.show_toast(f"分析异常: {message[:60]}", "warn")
        self._chart_panel.set_status("error", "分析异常")
        self._status_bar.set_tps(0)

    def _on_record_ready(self, record: Any) -> None:
        """Push the full AnalysisRecord to the conversation and debug tabs."""
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
                detail = f"{category}: {msg}" if category else (msg or err_type)
                self._prompt_debug_report_for_bug_fix(
                    f"分析未通过（{err_type}）",
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
        # Cache for _on_analysis_finished (which fires after this)
        self._last_stage1_diagnosis = s1_diag if isinstance(s1_diag, dict) else None
        s2_full = getattr(record, "stage2_decision", None)
        if s2_full:
            inner = s2_full.get("decision", s2_full)
            # Carry next_bar_prediction from top-level into inner dict
            if "next_bar_prediction" in s2_full and "next_bar_prediction" not in inner:
                inner = {**inner, "next_bar_prediction": s2_full["next_bar_prediction"]}
            self._chart_widget.set_decision(inner)
            levels = extract_structure_levels(s2_full)
            frame = self._chart_widget.displayed_frame()
            if frame is not None:
                levels = filter_levels_near_price(levels, frame.bars)
            self._chart_widget.set_structure_levels(levels)
            meta = getattr(record, "meta", None)
            stance = getattr(meta, "decision_stance", None) if meta is not None else None
            self._decision_panel.set_decision(
                inner,
                diagnosis_summary=s2_full.get("diagnosis_summary"),
                stage1_diagnosis=s1_diag if isinstance(s1_diag, dict) else None,
                decision_stance=stance,
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
                    pct = (total_tokens / context_window * 100) if context_window else 0
                    status_bar = getattr(self, "_status_bar", None)
                    if status_bar is not None:
                        status_bar.set_progress(pct, f"{pct:.1f}% · {total_tokens:,} / {context_window:,}")
                        if pct >= 95:
                            status_bar.set_progress_color("red")
                        elif pct >= 80:
                            status_bar.set_progress_color("yellow")
                        else:
                            status_bar.set_progress_color("normal")
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
                pct = (total_tokens / context_window * 100) if context_window else 0
                status_bar = getattr(self, "_status_bar", None)
                if status_bar is not None:
                    status_bar.set_progress(pct, f"{pct:.1f}% · {total_tokens:,} / {context_window:,}")
                    if pct >= 95:
                        status_bar.set_progress_color("red")
                    elif pct >= 80:
                        status_bar.set_progress_color("yellow")
                    else:
                        status_bar.set_progress_color("normal")

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
        self._auto_incremental_pending = False
        self._worker = None
        self._update_submit_button_state()

        # Reap any zombie RefreshLoops that finished while we were busy
        self._reap_zombie_loops()

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
        if self._last_analysis_had_error:
            self._flow_bar.set_step_status(4, "idle")
            self._flow_bar.set_step_caption(4, "等待完成")
        else:
            self._flow_bar.set_step_status(2, "done")
            self._flow_bar.set_step_caption(2, "阶段一完成")
            self._flow_bar.set_step_status(3, "done")
            self._flow_bar.set_step_caption(3, "阶段二完成")
            self._flow_bar.set_step_status(4, "active")
            self._flow_bar.set_step_caption(4, "可继续追问")

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
        if status_bar is not None and not configured:
            if not self._analysis_in_progress:
                cur = status_bar.currentMessage() or ""
                if cur in ("就绪", "") or "API Key" in cur or "提交分析已锁定" in cur:
                    status_bar.showMessage(
                        "未配置 API Key：请点击顶部「设置」填写后才能分析"
                    )
        pill = getattr(self, "_header_api_pill", None)
        if pill is not None:
            if configured:
                pill.setText("API 已配置")
                pill.setStyleSheet(
                    "background-color: #238636; color: #ffffff; padding: 2px 10px; "
                    "border-radius: 999px; font-size: 11px;"
                )
            else:
                pill.setText("API 未配置")
                pill.setStyleSheet(
                    "background-color: #3d2a00; color: #ffb86c; padding: 2px 10px; "
                    "border-radius: 999px; font-size: 11px;"
                )

    def _open_settings_dialog(self, *, focus_api_key: bool = False) -> None:
        """Open the SettingsDialog; import lazily to avoid circular imports."""
        from pa_agent.gui.settings_dialog import SettingsDialog
        from pa_agent.config.settings import Settings
        from pa_agent.util.logging import update_api_key

        settings: Settings = self._ctx.settings  # type: ignore[assignment]
        if settings is None:
            settings = Settings()

        dlg = SettingsDialog(settings, parent=self)
        dlg.set_decision_flow_play_handler(self._trigger_decision_flow_playback)
        if focus_api_key:
            dlg.focus_api_key_field()
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
                self._apply_chart_display_settings()
                update_api_key(key)
            self._update_ai_mode_label()
            self._refresh_api_key_ui_state()

    def _on_model_selector_clicked(self) -> None:
        """Use the model pill as a settings shortcut until model groups exist."""
        selector = getattr(self, "_model_selector", None)
        if selector is not None and getattr(selector, "_groups", None):
            return
        self._open_settings_dialog()

    def _on_speed_profile_changed(self, _index: int) -> None:
        """Apply toolbar analysis mode to the in-memory AI settings."""
        settings = getattr(self._ctx, "settings", None)
        combo = getattr(self, "_speed_profile_combo", None)
        if settings is None or combo is None:
            return
        key = combo.currentData()
        if not key:
            return
        try:
            from pa_agent.gui.analysis_modes import apply_analysis_mode

            mode = apply_analysis_mode(settings, str(key))
        except KeyError:
            return
        self._update_ai_mode_label()
        self._status_bar.showMessage(f"分析模式：{mode.label}")
        toast = getattr(self, "_toast", None)
        if toast is not None:
            toast.show_toast(f"已切换到 {mode.label}", "info")

    def _apply_chart_display_settings(self) -> None:
        """Sync chart label font sizes from persisted general settings."""
        chart = getattr(self, "_chart_widget", None)
        settings = getattr(self._ctx, "settings", None)
        if chart is None or settings is None:
            return
        chart.set_seq_label_font_pt(
            int(getattr(settings.general, "chart_seq_label_font_pt", 7) or 7)
        )

    def _update_ai_mode_label(self) -> None:
        """Show current thinking / reasoning_effort / model in the toolbar."""
        settings = getattr(self._ctx, "settings", None)
        if settings is None:
            self._ai_mode_label.setText("")
            selector = getattr(self, "_model_selector", None)
            if selector is not None:
                selector.set_model_name("—")
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
        selector = getattr(self, "_model_selector", None)
        if selector is not None:
            selector.set_model_name(p.model)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _can_submit(self) -> bool:
        """Return True if the submit button should be enabled."""
        return self._submit_block_reason() is None

    def _submit_block_reason(self) -> str | None:
        """Human-readable reason when submit is disabled, or None if allowed."""
        if not self._has_api_key_configured():
            return "未配置 API Key，请点击顶部「设置」填写后才能分析"
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
        """Enable submit button and surface why it may be locked."""
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
