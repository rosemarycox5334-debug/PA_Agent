"""多品种轮巡监控设置对话框.

填写监控品种列表和每轮间隔，保存到 config/settings.json 的 general 段，
并可直接启动/停止轮巡。
"""
from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import Settings, save_settings
from pa_agent.gui.watch_rotation import WatchRotationController, parse_watch_symbols

logger = logging.getLogger(__name__)


class WatchRotationDialog(QDialog):
    """配置并启动/停止多品种轮巡监控的模态对话框."""

    def __init__(
        self,
        settings: Settings,
        controller: WatchRotationController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._controller = controller
        self.setWindowTitle("多品种轮巡监控")
        self.setMinimumWidth(520)
        self._setup_ui()
        self._load_values()
        self._refresh_running_state()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        hint = QLabel(
            "按列表轮流分析各品种（使用当前选中的周期），出现下单信号照常"
            "弹窗并推送飞书。一轮结束后等待设定的间隔再开始下一轮。\n"
            "注意：每个品种每轮都会消耗一次 AI 分析调用；轮巡期间请勿手动"
            "切换品种或提交分析。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        self._symbols_edit = QLineEdit()
        self._symbols_edit.setPlaceholderText("例如：XAUUSD, EURUSD, 600519")
        self._symbols_edit.setToolTip("逗号分隔，使用当前数据源支持的品种代码")
        form.addRow("监控品种：", self._symbols_edit)

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(0, 1440)
        self._interval_spin.setSuffix(" 分钟")
        self._interval_spin.setToolTip("0 = 一轮结束后立即开始下一轮")
        form.addRow("每轮间隔：", self._interval_spin)

        self._status_label = QLabel()
        form.addRow("当前状态：", self._status_label)
        root.addLayout(form)

        btn_box = QDialogButtonBox()
        self._start_btn = QPushButton("保存并启动")
        self._stop_btn = QPushButton("停止轮巡")
        close_btn = QPushButton("关闭")
        btn_box.addButton(self._start_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        btn_box.addButton(self._stop_btn, QDialogButtonBox.ButtonRole.ActionRole)
        btn_box.addButton(close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn.clicked.connect(self._on_stop)
        close_btn.clicked.connect(self.reject)
        root.addWidget(btn_box)

    def _load_values(self) -> None:
        g = self._settings.general
        self._symbols_edit.setText(g.watch_symbols)
        self._interval_spin.setValue(int(g.watch_round_interval_min))

    def _refresh_running_state(self) -> None:
        running = self._controller.active
        self._stop_btn.setEnabled(running)
        self._start_btn.setText("保存并重新启动" if running else "保存并启动")
        self._status_label.setText(self._controller.status_text())

    def _save_values(self) -> None:
        g = self._settings.general
        g.watch_symbols = self._symbols_edit.text().strip()
        g.watch_round_interval_min = int(self._interval_spin.value())
        save_settings(self._settings, SETTINGS_JSON_PATH)

    def _on_start(self) -> None:
        symbols = parse_watch_symbols(self._symbols_edit.text())
        if not symbols:
            QMessageBox.warning(
                self, "监控列表为空", "请填写至少一个品种代码（逗号分隔）。"
            )
            return
        try:
            self._save_values()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, "保存失败", f"写入 config/settings.json 失败：\n{exc}"
            )
            return
        if self._controller.active:
            self._controller.stop()
        error = self._controller.start(symbols, self._interval_spin.value())
        if error:
            QMessageBox.warning(self, "无法启动轮巡", error)
            self._refresh_running_state()
            return
        self.accept()

    def _on_stop(self) -> None:
        self._controller.stop()
        self._refresh_running_state()
