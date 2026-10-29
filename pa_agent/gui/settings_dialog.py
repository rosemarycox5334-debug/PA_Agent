"""Settings dialog for PA Agent — edits all Settings fields via a form."""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFont

from pa_agent.config.settings import Settings, save_settings
from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.ai.qclaw_connector import (
    detect_qclaw,
    is_openclaw_model,
    should_use_qclaw_provider,
)
from pa_agent.ai.workbuddy_connector import (
    detect_workbuddy,
    is_openclaw_wb_model,
    should_use_workbuddy_provider,
)
from pa_agent.ai.trae_connector import (
    detect_trae_cn,
    is_openclaw_twc_model,
    should_use_trae_cn_provider,
)
from pa_agent.ai.qoder_connector import (
    detect_qoder_cn,
    is_openclaw_qc_model,
    should_use_qoder_cn_provider,
)

_API_KEY_HELP_URL = "https://my.feishu.cn/wiki/CUV1wUKWxiQGhekQdRvcZQQ2ncf"
_AGENT_TUTORIAL_URL = (
    "https://my.feishu.cn/wiki/BEdFwGJhaiATbukuD2HccSXCnrb?from=from_copylink"
)


class SettingsDialog(QDialog):
    """Modal dialog that exposes all Settings fields as editable form widgets."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        self._settings = settings
        self._setup_ui()
        self._load_values()

    def _setup_ui(self) -> None:
        root_layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        form_layout = QVBoxLayout(container)
        form_layout.setContentsMargins(8, 8, 8, 8)
        scroll.setWidget(container)
        root_layout.addWidget(scroll)

        provider_group = QGroupBox("AI 提供商")
        provider_form = QFormLayout(provider_group)

        self._model_edit = QLineEdit()
        provider_form.addRow("模型 (model):", self._model_edit)

        self._base_url_edit = QLineEdit()
        provider_form.addRow("Base URL:", self._base_url_edit)

        api_key_row = QHBoxLayout()
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        self._api_key_edit.setPlaceholderText("输入 API Key")
        api_key_row.addWidget(self._api_key_edit)
        self._show_key_btn = QPushButton("隐藏")
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.setFixedWidth(52)
        self._show_key_btn.toggled.connect(self._toggle_api_key_visibility)
        api_key_row.addWidget(self._show_key_btn)
        provider_form.addRow("API Key:", api_key_row)

        self._glm_key_label = QLabel("GLM 备用 Key:")
        self._glm_key_edit = QLineEdit()
        self._glm_key_edit.setPlaceholderText("输入 GLM Key")
        self._glm_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        provider_form.addRow(self._glm_key_label, self._glm_key_edit)
        self._base_url_edit.textChanged.connect(self._refresh_glm_key_row)

        self._thinking_check = QCheckBox("启用 Thinking")
        provider_form.addRow("Thinking:", self._thinking_check)

        self._reasoning_effort_combo = QComboBox()
        self._reasoning_effort_combo.addItems(["low", "medium", "high", "max"])
        provider_form.addRow("Reasoning Effort:", self._reasoning_effort_combo)

        self._api_key_help_btn = QPushButton("小白点这里！获取程序无限Token，无限分析")
        self._api_key_help_btn.clicked.connect(self._show_unlimited_token_info)
        provider_form.addRow("", self._api_key_help_btn)

        self._agent_tutorial_btn = QPushButton("智能体使用教程及问题解决方法")
        self._agent_tutorial_btn.setToolTip(_AGENT_TUTORIAL_URL)
        self._agent_tutorial_btn.clicked.connect(self._open_agent_tutorial_url)
        provider_form.addRow("", self._agent_tutorial_btn)

        form_layout.addWidget(provider_group)

        general_group = QGroupBox("通用设置")
        general_form = QFormLayout(general_group)

        self._decision_conf_threshold_spin = QSpinBox()
        self._decision_conf_threshold_spin.setRange(0, 100)
        self._decision_conf_threshold_spin.setToolTip(
            "当阶段二 trade_confidence 低于此阈值时，即使 AI 输出了限价单/突破单/市价单，"
            "也不视为下单机会（不弹窗警报、决策页按「不下单」处理）。"
            "设为 0 可关闭此门槛。"
        )
        general_form.addRow("下单置信度门槛:", self._decision_conf_threshold_spin)

        self._decision_stance_combo = QComboBox()
        self._decision_stance_combo.addItem("保守", "conservative")
        self._decision_stance_combo.addItem("均衡（默认，比保守更愿意下单）", "balanced")
        self._decision_stance_combo.addItem("激进（比均衡更愿意下单）", "aggressive")
        self._decision_stance_combo.addItem(
            "极度激进（强制选方向与进场方式）",
            "extreme_aggressive",
        )
        self._decision_stance_combo.setToolTip(
            "仅影响阶段二交易决策倾向；保守与改版前一致。"
            "均衡、激进逐级提高下单意愿；极度激进在未触犯 §14 硬性禁止时"
            "必须给出具体做多/做空及限价/突破/市价方案。"
        )
        general_form.addRow("交易倾向:", self._decision_stance_combo)

        self._enable_next_bar_check = QCheckBox("开启后在「未来走势预期」中显示下根K线预期")
        self._enable_next_bar_check.setToolTip(
            "开启后，AI 会在每次分析中额外预测下一根K线方向，结果显示在「未来走势预期」中。\n"
            "关闭后 prompt 中移除该任务，不消耗额外 token。"
        )
        self._enable_next_bar_check.stateChanged.connect(self._on_enable_next_bar_changed)
        general_form.addRow("下根K线预期:", self._enable_next_bar_check)

        self._analysis_bar_count_spin = QSpinBox()
        self._analysis_bar_count_spin.setRange(2, 5_000)
        self._analysis_bar_count_spin.setToolTip(
            "提交 AI 分析时使用的已收盘 K 线根数（不含当前未收盘 K 线）。"
            "图表实时刷新也会按此数量拉取显示。"
        )
        general_form.addRow("用于分析的 K 线数量:", self._analysis_bar_count_spin)

        self._refresh_interval_spin = QSpinBox()
        self._refresh_interval_spin.setRange(100, 10_000)
        self._refresh_interval_spin.setSuffix(" ms")
        general_form.addRow("刷新间隔:", self._refresh_interval_spin)

        self._auto_resume_chart_check = QCheckBox("分析完成后自动恢复「图表实时更新」")
        self._auto_resume_chart_check.setToolTip(
            "提交分析时图表会暂停刷新并冻结为已收盘 K 线；"
            "勾选后，分析结束（成功或校验失败但流程已跑完）将自动恢复实时刷新，"
            "并重新显示最右侧未收盘空心 K 线。演示模式不受影响。"
        )
        general_form.addRow("图表:", self._auto_resume_chart_check)

        self._keep_analysis_check = QCheckBox("有新K线收盘时自动开始新一轮分析")
        self._keep_analysis_check.setToolTip(
            "勾选后，每当有新的K线收盘时自动触发分析（与主界面「持续跟踪分析」勾选框同步）"
        )
        general_form.addRow("持续跟踪分析:", self._keep_analysis_check)

        self._cancel_keep_on_retry_check = QCheckBox("重试后取消持续跟踪分析")
        self._cancel_keep_on_retry_check.setToolTip(
            "勾选后，当 AI 输出触发校验重试（stage1/stage2），"
            "自动关闭「持续跟踪分析」开关，停止后续自动分析。\n"
            "每次打开程序默认关闭。"
        )
        general_form.addRow("重试行为:", self._cancel_keep_on_retry_check)

        self._context_warning_spin = QSpinBox()
        self._context_warning_spin.setRange(1, 99_999_999)
        self._context_warning_spin.setSuffix(" %")
        general_form.addRow("上下文警告阈值:", self._context_warning_spin)

        self._stream_font_spin = QSpinBox()
        self._stream_font_spin.setRange(8, 28)
        self._stream_font_spin.setSuffix(" pt")
        self._stream_font_spin.setToolTip(
            "「实时」标签页中思考过程/撰写回答大文本框，以及下方追问输入框的字体大小"
        )
        general_form.addRow("实时窗口字号:", self._stream_font_spin)

        self._chart_seq_font_spin = QSpinBox()
        self._chart_seq_font_spin.setRange(6, 24)
        self._chart_seq_font_spin.setSuffix(" pt")
        self._chart_seq_font_spin.setToolTip("K 线图上 #1、#3… 序号标签的字体大小")
        general_form.addRow("图表K线序号字号:", self._chart_seq_font_spin)

        self._incremental_max_new_bars_spin = QSpinBox()
        self._incremental_max_new_bars_spin.setRange(0, 500)
        self._incremental_max_new_bars_spin.setSuffix(" 根")
        self._incremental_max_new_bars_spin.setToolTip(
            "同品种同周期下，若相对上一条成功记录只新增不超过该数量的已收盘K线，"
            "提交分析时走增量分析；设为 0 可关闭增量分析。"
        )
        general_form.addRow("增量分析最大新增K线:", self._incremental_max_new_bars_spin)

        self._last_symbol_edit = QLineEdit()
        general_form.addRow("上次品种:", self._last_symbol_edit)

        self._last_timeframe_edit = QLineEdit()
        general_form.addRow("上次周期:", self._last_timeframe_edit)

        self._alert_on_order_check = QCheckBox(
            "有下单机会时发出警报音和弹窗，并自动跳转到「决策」页"
        )
        self._alert_on_order_check.setToolTip(
            "当阶段二给出限价单、突破单或市价单时：播放系统提示音、弹出摘要对话框，"
            "并切换到右侧「决策」标签页；不再自动进入「决策树可视化」播放演示。"
            "勾选时会播放一次试听。"
        )
        self._alert_on_order_check.stateChanged.connect(self._on_alert_on_order_changed)
        general_form.addRow("下单提醒:", self._alert_on_order_check)

        self._flow_auto_play_check = QCheckBox("决策树可视化生成后自动播放路径")
        self._flow_auto_play_check.setToolTip(
            "分析完成后自动切换到「决策树可视化」并播放路径动画。"
            "若已开启「下单提醒」且本轮有下单机会，则优先走下单提醒，不播放演示。"
        )
        general_form.addRow("决策树播放:", self._flow_auto_play_check)

        self._flow_play_seconds_spin = QSpinBox()
        self._flow_play_seconds_spin.setRange(3, 120)
        self._flow_play_seconds_spin.setSuffix(" 秒")
        general_form.addRow("播放时长:", self._flow_play_seconds_spin)

        self._flow_default_zoom_spin = QSpinBox()
        self._flow_default_zoom_spin.setRange(10, 9_999_999)
        self._flow_default_zoom_spin.setSuffix(" %")
        self._flow_default_zoom_spin.setToolTip(
            "相对「整图适配」视图：100% 与适配一致，50% 再缩小一半；"
            "可填任意更大百分比以放大（分析完成、播放路径、手动播放均用此比例）"
        )
        general_form.addRow("决策树可视化默认缩放:", self._flow_default_zoom_spin)

        self._flow_play_now_btn = QPushButton("播放决策树可视化")
        self._flow_play_now_btn.setToolTip(
            "使用当前已加载的决策路径重新播放动画（若尚未分析则无可播放内容）"
        )
        self._flow_play_now_btn.clicked.connect(self._on_play_decision_flow_now)
        general_form.addRow("", self._flow_play_now_btn)

        self._decision_flow_play_handler: Callable[[], None] | None = None

        form_layout.addWidget(general_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root_layout.addWidget(buttons)

    def _load_values(self) -> None:
        p = self._settings.provider
        g = self._settings.general

        self._model_edit.setText(p.model)
        self._base_url_edit.setText(p.base_url)
        self._api_key_edit.setText(p.api_key)
        self._thinking_check.setChecked(p.thinking)

        self._glm_key_edit.setText(str(getattr(p, "backup_glm_api_key", "") or ""))
        self._refresh_glm_key_row()

        idx = self._reasoning_effort_combo.findText(p.reasoning_effort)
        if idx >= 0:
            self._reasoning_effort_combo.setCurrentIndex(idx)

        self._analysis_bar_count_spin.setValue(g.analysis_bar_count)
        self._refresh_interval_spin.setValue(g.refresh_interval_ms)
        self._auto_resume_chart_check.setChecked(
            bool(getattr(g, "auto_resume_chart_after_analysis", False))
        )
        self._keep_analysis_check.setChecked(
            bool(getattr(g, "keep_analysis", False))
        )
        self._cancel_keep_on_retry_check.setChecked(
            bool(getattr(g, "cancel_keep_analysis_on_retry", False))
        )
        self._context_warning_spin.setValue(int(g.context_warning_threshold_pct))
        self._stream_font_spin.setValue(int(getattr(g, "stream_pane_font_pt", 11)))
        self._chart_seq_font_spin.setValue(int(getattr(g, "chart_seq_label_font_pt", 11)))
        self._incremental_max_new_bars_spin.setValue(
            int(getattr(g, "incremental_max_new_bars", 10))
        )
        stance = getattr(g, "decision_stance", "conservative")
        stance_idx = self._decision_stance_combo.findData(stance)
        if stance_idx >= 0:
            self._decision_stance_combo.setCurrentIndex(stance_idx)
        self._enable_next_bar_check.blockSignals(True)
        self._enable_next_bar_check.setChecked(
            bool(getattr(g, "enable_next_bar_prediction", False))
        )
        self._enable_next_bar_check.blockSignals(False)
        self._decision_conf_threshold_spin.setValue(
            int(getattr(g, "decision_confidence_threshold", 40))
        )
        self._last_symbol_edit.setText(g.last_symbol)
        self._last_timeframe_edit.setText(g.last_timeframe)
        self._alert_on_order_check.blockSignals(True)
        self._alert_on_order_check.setChecked(
            bool(getattr(g, "alert_on_order_opportunity", True))
        )
        self._alert_on_order_check.blockSignals(False)
        self._flow_auto_play_check.setChecked(
            getattr(g, "decision_flow_auto_play", False)
        )
        self._flow_play_seconds_spin.setValue(
            getattr(g, "decision_flow_play_seconds", 50)
        )
        self._flow_default_zoom_spin.setValue(
            int(getattr(g, "decision_flow_default_zoom_pct", 600))
        )

    @staticmethod
    def _validate_provider_fields(model: str, base_url: str) -> str | None:
        """Return user-facing error text, or None if fields look consistent."""
        if is_openclaw_model(model) or should_use_qclaw_provider(model, base_url):
            return None
        if is_openclaw_wb_model(model) or should_use_workbuddy_provider(model, base_url):
            return None
        if is_openclaw_twc_model(model) or should_use_trae_cn_provider(model, base_url):
            return None
        if is_openclaw_qc_model(model) or should_use_qoder_cn_provider(model, base_url):
            return None
        if model.startswith(("http://", "https://")) and not base_url.startswith(
            ("http://", "https://")
        ):
            return (
                "「模型」与「Base URL」似乎填反了：\n"
                "• 模型应填模型名，如 deepseek-v4-pro 或 claude-sonnet-4-6\n"
                "• 使用 QClaw 时模型填 openclaw（或 openclaw/main）\n"
                "• 使用 WorkBuddy 时模型填 openclaw_wb\n"
                "• 使用 TRAE Work CN 时模型填 openclaw_twc\n"
                "• 使用 Qoder CN 时模型填 openclaw_qc\n"
                "• Base URL 应填接口地址，如 https://api.deepseek.com"
            )
        if base_url.startswith(("http://", "https://")):
            return None
        if not base_url:
            if detect_qclaw():
                return (
                    "请填写 Base URL，或使用 QClaw/WorkBuddy/TRAE Work CN/Qoder CN：\n"
                    "• 模型填 openclaw → QClaw\n"
                    "• 模型填 openclaw_wb → WorkBuddy\n"
                    "• 模型填 openclaw_twc → TRAE Work CN\n"
                    "• 模型填 openclaw_qc → Qoder CN"
                )
            if detect_workbuddy():
                return (
                    "请填写 Base URL，或使用 WorkBuddy/TRAE Work CN/Qoder CN：\n"
                    "• 模型填 openclaw_wb（保存时自动配置 WorkBuddy 端点）\n"
                    "• 模型填 openclaw_twc → TRAE Work CN\n"
                    "• 模型填 openclaw_qc → Qoder CN"
                )
            if detect_trae_cn():
                return (
                    "请填写 Base URL，或使用 TRAE Work CN/Qoder CN：\n"
                    "• 模型填 openclaw_twc → TRAE Work CN（保存时自动配置）\n"
                    "• 模型填 openclaw_qc → Qoder CN（保存时自动配置）"
                )
            if detect_qoder_cn():
                return (
                    "请填写 Base URL，或使用 Qoder CN：\n"
                    "• 模型填 openclaw_qc → Qoder CN（保存时自动配置）"
                )
            return "请填写 Base URL（API 接口地址）。"
        return (
            f"Base URL 不是有效网址（当前：{base_url}）。\n"
            "DeepSeek 示例：https://api.deepseek.com\n"
            "PackyAPI 示例：https://www.packyapi.com/v1\n"
            "MiMo 示例：https://api.xiaomimimo.com/v1\n"
            "QClaw：模型填 openclaw 后点保存（自动配置本地网关）\n"
            "WorkBuddy：模型填 openclaw_wb 后点保存（自动配置 WorkBuddy）\n"
            "TRAE Work CN：模型填 openclaw_twc 后点保存（自动配置 TRAE Work CN）\n"
            "Qoder CN：模型填 openclaw_qc 后点保存（自动配置 Qoder CN）"
        )

    def _apply_qclaw_provider(self, *, preferred_model: str = "") -> str | None:
        """Detect QClaw and write provider fields. Returns error text, or None."""
        from pa_agent.ai.qclaw_connector import apply_qclaw_provider_to_settings

        return apply_qclaw_provider_to_settings(
            self._settings,
            preferred_model=preferred_model or None,
        )

    def _apply_workbuddy_provider(self, *, preferred_model: str = "") -> str | None:
        """Detect WorkBuddy and write provider fields. Returns error text, or None."""
        from pa_agent.ai.workbuddy_connector import apply_workbuddy_provider_to_settings

        return apply_workbuddy_provider_to_settings(
            self._settings,
            preferred_model=preferred_model or None,
        )

    def _apply_trae_cn_provider(self, *, preferred_model: str = "") -> str | None:
        """Detect TRAE Work CN and write provider fields. Returns error text, or None."""
        from pa_agent.ai.trae_connector import apply_trae_cn_provider_to_settings

        return apply_trae_cn_provider_to_settings(
            self._settings,
            preferred_model=preferred_model or None,
        )

    def _apply_qoder_cn_provider(self, *, preferred_model: str = "") -> str | None:
        """Detect Qoder CN and write provider fields. Returns error text, or None."""
        from pa_agent.ai.qoder_connector import apply_qoder_cn_provider_to_settings

        return apply_qoder_cn_provider_to_settings(
            self._settings,
            preferred_model=preferred_model or None,
        )

    def _on_save(self) -> None:
        p = self._settings.provider
        g = self._settings.general

        model = self._model_edit.text().strip()
        base_url = self._base_url_edit.text().strip()
        api_key = self._api_key_edit.text().strip()

        # Explicit model aliases win over stale base_url (openclaw_wb before openclaw).
        if is_openclaw_wb_model(model) or should_use_workbuddy_provider(model, base_url):
            p.api_key = api_key
            wb_err = self._apply_workbuddy_provider(preferred_model=model)
            if wb_err:
                QMessageBox.warning(self, "WorkBuddy 配置异常", wb_err)
                return
        elif is_openclaw_twc_model(model) or should_use_trae_cn_provider(model, base_url):
            p.api_key = api_key
            trae_err = self._apply_trae_cn_provider(preferred_model=model)
            if trae_err:
                QMessageBox.warning(self, "TRAE Work CN 配置异常", trae_err)
                return
        elif is_openclaw_qc_model(model) or should_use_qoder_cn_provider(model, base_url):
            p.api_key = api_key
            qc_err = self._apply_qoder_cn_provider(preferred_model=model)
            if qc_err:
                QMessageBox.warning(self, "Qoder CN 配置异常", qc_err)
                return
        elif is_openclaw_model(model) or should_use_qclaw_provider(model, base_url):
            p.api_key = api_key
            qclaw_err = self._apply_qclaw_provider(preferred_model=model)
            if qclaw_err:
                QMessageBox.warning(self, "QClaw 配置异常", qclaw_err)
                return
        else:
            field_err = self._validate_provider_fields(model, base_url)
            if field_err:
                QMessageBox.warning(self, "AI 提供商配置有误", field_err)
                return

            p.model = model
            p.base_url = base_url
            p.api_key = api_key

        p.backup_glm_api_key = self._glm_key_edit.text().strip()
        if p.backup_glm_api_key:
            from pa_agent.ai.glm_backup import ensure_glm_fallback

            ensure_glm_fallback(p)
        elif str(getattr(p, "base_url", "")).startswith("https://token.sensenova.cn"):
            from pa_agent.ai.glm_backup import GLM_BASE_URL, GLM_MODEL

            p.fallbacks = [
                fb for fb in (getattr(p, "fallbacks", None) or [])
                if not (str(getattr(fb, "model", "")) == GLM_MODEL
                        and str(getattr(fb, "base_url", "")) == GLM_BASE_URL)
            ]

        p.thinking = self._thinking_check.isChecked()
        p.reasoning_effort = self._reasoning_effort_combo.currentText()  # type: ignore[assignment]

        g.analysis_bar_count = self._analysis_bar_count_spin.value()
        g.refresh_interval_ms = self._refresh_interval_spin.value()
        g.auto_resume_chart_after_analysis = self._auto_resume_chart_check.isChecked()
        g.keep_analysis = self._keep_analysis_check.isChecked()
        g.cancel_keep_analysis_on_retry = self._cancel_keep_on_retry_check.isChecked()
        g.context_warning_threshold_pct = float(self._context_warning_spin.value())
        g.stream_pane_font_pt = self._stream_font_spin.value()
        g.chart_seq_label_font_pt = self._chart_seq_font_spin.value()
        g.incremental_max_new_bars = self._incremental_max_new_bars_spin.value()
        g.decision_stance = self._decision_stance_combo.currentData()  # type: ignore[assignment]
        g.enable_next_bar_prediction = self._enable_next_bar_check.isChecked()
        g.last_symbol = self._last_symbol_edit.text().strip()
        g.last_timeframe = self._last_timeframe_edit.text().strip()
        g.alert_on_order_opportunity = self._alert_on_order_check.isChecked()
        g.decision_flow_auto_play = self._flow_auto_play_check.isChecked()
        g.decision_flow_play_seconds = self._flow_play_seconds_spin.value()
        g.decision_flow_default_zoom_pct = self._flow_default_zoom_spin.value()
        g.decision_confidence_threshold = self._decision_conf_threshold_spin.value()

        save_settings(self._settings, SETTINGS_JSON_PATH)
        self.accept()

    def _refresh_glm_key_row(self) -> None:
        """Show the GLM backup key row only when the primary is SenseNova."""
        from pa_agent.ai.glm_backup import is_sensenova_base

        visible = is_sensenova_base(self._base_url_edit.text().strip())
        self._glm_key_label.setVisible(visible)
        self._glm_key_edit.setVisible(visible)

    def focus_api_key_field(self) -> None:
        """Focus the API Key field (e.g. when prompting on first launch)."""
        self._api_key_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._api_key_edit.selectAll()

    def set_decision_flow_play_handler(self, handler: Callable[[], None] | None) -> None:
        """Register callback invoked when user clicks 播放决策树可视化."""
        self._decision_flow_play_handler = handler

    def _on_alert_on_order_changed(self, _state: int) -> None:
        if not self._alert_on_order_check.isChecked():
            return
        from pa_agent.gui.order_opportunity import play_order_alert_sound

        play_order_alert_sound()

    def _on_enable_next_bar_changed(self, state: int) -> None:
        """勾选开启下根K线预期时弹出提示。"""
        from PyQt6.QtCore import Qt as _Qt
        if state == _Qt.CheckState.Checked.value:
            QMessageBox.information(
                self,
                "下根K线预期",
                "下根K线预期难度大，结果仅供参考。\n\n"
                "AI 预测单根K线方向的准确率有限，请勿将其作为交易依据。",
            )

    def _on_play_decision_flow_now(self) -> None:
        # Allow previewing playback without pressing “保存”:
        # sync relevant fields from widgets into the in-memory settings object.
        g = self._settings.general
        g.decision_flow_auto_play = self._flow_auto_play_check.isChecked()
        g.decision_flow_play_seconds = self._flow_play_seconds_spin.value()
        g.decision_flow_default_zoom_pct = self._flow_default_zoom_spin.value()

        if self._decision_flow_play_handler is not None:
            self._decision_flow_play_handler()

    def _show_unlimited_token_info(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("获取无限Token")
        layout = QVBoxLayout(dlg)
        label = QLabel(
            "获取无限Token方法需付费49.9元，付费后你将获得<br>"
            "Deepseek V4 Pro/GLM5.1/Kimi2.6等\"满血\"模型的无限分析方法<br>"
            "注意无限Token只支持使用这个分析软件<br>"
            "如果你愿意付费，请联系QQ：564020069（付费后提供远程协助部署安装服务）<br><br>"
            "如果你不愿意付费，你可以用自己的模型api，如果你不知道模型api是什么<br>"
            "可以直接跟龙虾说：<br>"
            "PA_Agent这个程序的模型api有什么作用，该怎么填？<br>"
            "请教我填上Deepseek官方的模型API接口"
        )
        label.setStyleSheet("font-size: 22pt;")
        layout.addWidget(label)
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btn_box.accepted.connect(dlg.accept)
        layout.addWidget(btn_box)
        dlg.exec()

    def _open_agent_tutorial_url(self) -> None:
        QDesktopServices.openUrl(QUrl(_AGENT_TUTORIAL_URL))

    def _toggle_api_key_visibility(self, checked: bool) -> None:
        if checked:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_key_btn.setText("显示")
        else:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_key_btn.setText("隐藏")
