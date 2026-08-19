"""AI 模型设置对话框 — 只包含 AI 提供商相关字段."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
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
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.settings import Settings, save_settings
from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.ai.cursor_connector import (
    is_openclaw_cs_model,
    should_use_cursor_provider,
)
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


class AIModelSettingsDialog(QDialog):
    """AI 模型 / 提供商配置对话框."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 模型设置")
        self.setMinimumWidth(520)
        self._settings = settings
        self._setup_ui()
        self._load_values()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        provider_group = QGroupBox("AI 提供商")
        form = QFormLayout(provider_group)

        self._model_edit = QLineEdit()
        form.addRow("模型 (model):", self._model_edit)

        self._base_url_edit = QLineEdit()
        form.addRow("Base URL:", self._base_url_edit)

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
        form.addRow("API Key:", api_key_row)

        self._thinking_check = QCheckBox("启用 Thinking")
        form.addRow("Thinking:", self._thinking_check)

        self._reasoning_effort_combo = QComboBox()
        self._reasoning_effort_combo.addItems(["low", "medium", "high", "max"])
        form.addRow("Reasoning Effort:", self._reasoning_effort_combo)

        self._max_output_tokens_edit = QLineEdit()
        self._max_output_tokens_edit.setPlaceholderText("0 = 自动（按网关/模型）")
        form.addRow("最大输出 Tokens:", self._max_output_tokens_edit)

        self._api_key_help_btn = QPushButton("小白点这里！获取程序无限Token，无限分析")
        self._api_key_help_btn.setStyleSheet(
            "QPushButton { font-size: 13pt; font-weight: bold; "
            "padding: 8px 16px; }"
        )
        self._api_key_help_btn.clicked.connect(self._show_unlimited_token_info)
        form.addRow("", self._api_key_help_btn)

        self._agent_tutorial_btn = QPushButton("智能体使用教程及问题解决方法")
        self._agent_tutorial_btn.setToolTip(_AGENT_TUTORIAL_URL)
        self._agent_tutorial_btn.clicked.connect(self._open_agent_tutorial_url)
        form.addRow("", self._agent_tutorial_btn)

        root.addWidget(provider_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_btn:
            save_btn.setText("保存")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── 加载 / 保存 ────────────────────────────────────────────────────────────

    def _load_values(self) -> None:
        p = self._settings.provider
        self._model_edit.setText(p.model)
        self._base_url_edit.setText(p.base_url)
        self._api_key_edit.setText(p.api_key)
        self._thinking_check.setChecked(p.thinking)
        idx = self._reasoning_effort_combo.findText(p.reasoning_effort)
        if idx >= 0:
            self._reasoning_effort_combo.setCurrentIndex(idx)
        self._max_output_tokens_edit.setText(str(p.max_output_tokens))

    def _on_save(self) -> None:
        p = self._settings.provider
        model = self._model_edit.text().strip()
        base_url = self._base_url_edit.text().strip()
        api_key = self._api_key_edit.text().strip()

        # Explicit model aliases win over stale base_url (openclaw_wb before openclaw).
        if is_openclaw_wb_model(model) or should_use_workbuddy_provider(model, base_url):
            p.api_key = api_key
            err = self._apply_workbuddy_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "WorkBuddy 配置异常", err)
                return
        elif is_openclaw_twc_model(model) or should_use_trae_cn_provider(model, base_url):
            p.api_key = api_key
            err = self._apply_trae_cn_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "TRAE Work CN 配置异常", err)
                return
        elif is_openclaw_qc_model(model) or should_use_qoder_cn_provider(model, base_url):
            p.api_key = api_key
            err = self._apply_qoder_cn_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "Qoder CN 配置异常", err)
                return
        elif is_openclaw_cs_model(model) or should_use_cursor_provider(model, base_url):
            # Cursor route must keep the user-provided Cursor API key (crsr_...).
            p.api_key = api_key
            err = self._apply_cursor_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "Cursor 配置异常", err)
                return
        elif is_openclaw_model(model) or should_use_qclaw_provider(model, base_url):
            p.api_key = api_key
            err = self._apply_qclaw_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "QClaw 配置异常", err)
                return
        else:
            field_err = self._validate_provider_fields(model, base_url)
            if field_err:
                QMessageBox.warning(self, "AI 提供商配置有误", field_err)
                return
            p.model = model
            p.base_url = base_url
            p.api_key = api_key

        p.thinking = self._thinking_check.isChecked()
        p.reasoning_effort = self._reasoning_effort_combo.currentText()  # type: ignore[assignment]

        raw_mot = self._max_output_tokens_edit.text().strip()
        try:
            mot = int(raw_mot) if raw_mot else 0
            if mot < 0:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "最大输出 Tokens 无效", "「最大输出 Tokens」需为非负整数（0 = 自动）。")
            return
        p.max_output_tokens = mot

        save_settings(self._settings, SETTINGS_JSON_PATH)
        self.accept()

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    def focus_api_key_field(self) -> None:
        self._api_key_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._api_key_edit.selectAll()

    def _toggle_api_key_visibility(self, checked: bool) -> None:
        if checked:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_key_btn.setText("显示")
        else:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_key_btn.setText("隐藏")

    def _apply_cursor_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.cursor_connector import apply_cursor_provider_to_settings
        return apply_cursor_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    def _apply_qclaw_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.qclaw_connector import apply_qclaw_provider_to_settings
        return apply_qclaw_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    def _apply_workbuddy_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.workbuddy_connector import apply_workbuddy_provider_to_settings
        return apply_workbuddy_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    def _apply_trae_cn_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.trae_connector import apply_trae_cn_provider_to_settings
        return apply_trae_cn_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    def _apply_qoder_cn_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.qoder_connector import apply_qoder_cn_provider_to_settings
        return apply_qoder_cn_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    @staticmethod
    def _validate_provider_fields(model: str, base_url: str) -> str | None:
        if is_openclaw_cs_model(model) or should_use_cursor_provider(model, base_url):
            return None
        if is_openclaw_model(model) or should_use_qclaw_provider(model, base_url):
            return None
        if is_openclaw_wb_model(model) or should_use_workbuddy_provider(model, base_url):
            return None
        if is_openclaw_twc_model(model) or should_use_trae_cn_provider(model, base_url):
            return None
        if is_openclaw_qc_model(model) or should_use_qoder_cn_provider(model, base_url):
            return None
        if model.startswith(("http://", "https://")) and not base_url.startswith(("http://", "https://")):
            return (
                "「模型」与「Base URL」似乎填反了：\n"
                "• 模型应填模型名，如 deepseek-v4-pro 或 claude-sonnet-4-6\n"
                "• 使用 QClaw 时模型填 openclaw（或 openclaw/main）\n"
                "• 使用 Cursor 订阅时模型填 openclaw_cs\n"
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
                    "• 模型填 openclaw_cs → Cursor 订阅（经 QClaw 网关）\n"
                    "• 模型填 openclaw_wb → WorkBuddy\n"
                    "• 模型填 openclaw_twc → TRAE Work CN\n"
                    "• 模型填 openclaw_qc → Qoder CN"
                )
            if detect_workbuddy():
                return (
                    "请填写 Base URL，或使用 WorkBuddy/TRAE Work CN/Qoder CN：\n"
                    "• 模型填 openclaw_wb → WorkBuddy\n"
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
            "QClaw：模型填 openclaw 后点保存（自动配置本地网关）\n"
            "Cursor：模型填 openclaw_cs 后点保存（经 QClaw 走 Cursor 订阅）\n"
            "WorkBuddy：模型填 openclaw_wb 后点保存（自动配置 WorkBuddy）\n"
            "TRAE Work CN：模型填 openclaw_twc 后点保存（自动配置 TRAE Work CN）\n"
            "Qoder CN：模型填 openclaw_qc 后点保存（自动配置 Qoder CN）"
        )

    def _show_unlimited_token_info(self) -> None:
        from PyQt6.QtWidgets import QDialog as _QDialog
        dlg = _QDialog(self)
        dlg.setWindowTitle("获取无限Token")
        from PyQt6.QtWidgets import QVBoxLayout as _VBox, QDialogButtonBox as _DBB
        layout = _VBox(dlg)
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
        bb = _DBB(_DBB.StandardButton.Ok)
        bb.accepted.connect(dlg.accept)
        layout.addWidget(bb)
        dlg.exec()

    def _open_agent_tutorial_url(self) -> None:
        QDesktopServices.openUrl(QUrl(_AGENT_TUTORIAL_URL))
