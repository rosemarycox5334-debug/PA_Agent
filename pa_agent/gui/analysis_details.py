"""Compact disclosure: neutral summary surface, accessible source details."""
from PyQt6.QtWidgets import QLabel, QToolButton, QVBoxLayout, QWidget


class AnalysisDetails(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.button = QToolButton()
        self.button.setText("分析详情")
        self.button.setCheckable(True)
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setStyleSheet("color: #8b949e; font-size: 12px;")
        self.body.setVisible(False)
        self.button.toggled.connect(self.body.setVisible)
        layout.addWidget(self.button)
        layout.addWidget(self.body)
        self.setVisible(False)

    def set_details(self, text):
        self.button.setChecked(False)
        self.body.setText(text)
        self.body.setVisible(False)
        self.setVisible(bool(text))
