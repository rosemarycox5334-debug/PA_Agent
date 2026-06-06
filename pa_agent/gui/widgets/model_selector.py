"""ModelSelector — pill button with a custom dropdown for model selection."""
from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class _ModelOption(QWidget):
    """Single model entry inside the dropdown."""

    selected = pyqtSignal(str)

    def __init__(
        self,
        model_id: str,
        model_name: str,
        meta: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._model_id = model_id
        self._selected = False

        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(8)

        self._dot = QLabel()
        self._dot.setFixedSize(6, 6)
        self._dot.setStyleSheet("background-color: #6e7681; border-radius: 3px;")

        self._name = QLabel(model_name)
        self._name.setStyleSheet("color: #c9d1d9; font-size: 12px;")

        self._meta = QLabel(meta)
        self._meta.setStyleSheet(
            "color: #6e7681; font-size: 11px; font-family: monospace;"
        )
        self._meta.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        layout.addWidget(self._dot)
        layout.addWidget(self._name)
        layout.addStretch()
        layout.addWidget(self._meta)

        self._refresh_style()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._refresh_style()

    def _refresh_style(self) -> None:
        if self._selected:
            self.setStyleSheet(
                "_ModelOption {"
                "  background-color: rgba(56,189,248,0.12);"
                "  border-radius: 6px;"
                "}"
                "_ModelOption:hover {"
                "  background-color: rgba(56,189,248,0.18);"
                "}"
            )
            self._dot.setStyleSheet(
                "background-color: #38bdf8; border-radius: 3px;"
            )
        else:
            self.setStyleSheet(
                "_ModelOption {"
                "  border-radius: 6px;"
                "}"
                "_ModelOption:hover {"
                "  background-color: #21262d;"
                "}"
            )
            self._dot.setStyleSheet(
                "background-color: #6e7681; border-radius: 3px;"
            )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.selected.emit(self._model_id)
        super().mousePressEvent(event)


class ModelDropdown(QWidget):
    """Popup dropdown widget for model selection."""

    model_selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowFlags(
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(16)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 120))

        self._frame = QFrame(self)
        self._frame.setGraphicsEffect(shadow)
        self._frame.setStyleSheet(
            "QFrame {"
            "  background-color: #1c2128;"
            "  border: 1px solid #484f58;"
            "  border-radius: 10px;"
            "}"
        )

        self._layout = QVBoxLayout(self._frame)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.addWidget(self._frame)

        self._groups: list = []
        self._options: list[_ModelOption] = []
        self._current_model: str | None = None

    def set_groups(self, groups: list) -> None:
        """Populate the dropdown with grouped model entries."""
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._options.clear()

        for idx, (group_name, models) in enumerate(groups):
            if idx > 0:
                spacer = QWidget()
                spacer.setFixedHeight(4)
                self._layout.addWidget(spacer)

            label = QLabel(group_name)
            label.setStyleSheet(
                "color: #6e7681;"
                "font-size: 10px;"
                "text-transform: uppercase;"
                "letter-spacing: 0.08em;"
                "padding: 6px 8px 2px;"
            )
            self._layout.addWidget(label)

            for model_id, meta in models:
                option = _ModelOption(model_id, model_id, meta)
                option.selected.connect(self._on_selected)
                self._layout.addWidget(option)
                self._options.append(option)

        self._update_selection()

    def set_current_model(self, name: str | None) -> None:
        self._current_model = name
        self._update_selection()

    def _update_selection(self) -> None:
        for opt in self._options:
            opt.set_selected(opt._model_id == self._current_model)

    def _on_selected(self, model_id: str) -> None:
        self.model_selected.emit(model_id)
        self.hide()

    def show_below(self, widget: QWidget) -> None:
        """Position and show the dropdown just below *widget*."""
        self.adjustSize()
        min_width = max(240, widget.width())
        if self.width() < min_width:
            self.setFixedWidth(min_width)

        pos = widget.mapToGlobal(QPoint(0, widget.height() + 4))

        screen = widget.screen()
        if screen:
            geo = screen.availableGeometry()
            right_edge = pos.x() + self.width()
            if right_edge > geo.x() + geo.width():
                pos.setX(geo.x() + geo.width() - self.width())
            if pos.x() < geo.x():
                pos.setX(geo.x())

        self.move(pos)
        self.show()


class ModelSelector(QWidget):
    """A compact pill-shaped button showing the active model name.

    Clicking the button opens a custom popup dropdown grouped by model
    categories.  Selecting an entry updates the displayed name and emits
    :attr:`model_selected`.  The legacy :attr:`clicked` signal is still
    emitted for backward compatibility.
    """

    clicked = pyqtSignal()
    model_selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("modelSelector")

        self._current_name = "—"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._button = QPushButton("🧠  —")
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.setStyleSheet(
            "QPushButton {"
            "  height: 24px;"
            "  padding: 0 10px;"
            "  border: 1px solid rgba(56,189,248,0.35);"
            "  border-radius: 999px;"
            "  background-color: rgba(56,189,248,0.10);"
            "  color: #7dd3fc;"
            "  font-size: 12px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(56,189,248,0.18);"
            "}"
            "QPushButton:pressed {"
            "  background-color: rgba(56,189,248,0.25);"
            "}"
        )
        self._button.clicked.connect(self._on_clicked)
        layout.addWidget(self._button)

        self._dropdown = ModelDropdown(self)
        self._dropdown.model_selected.connect(self._on_model_selected)

        self._groups: list = []

    def _on_clicked(self) -> None:
        self.clicked.emit()
        if self._groups:
            self._dropdown.set_groups(self._groups)
            self._dropdown.set_current_model(self._current_name)
            self._dropdown.show_below(self._button)

    def _on_model_selected(self, name: str) -> None:
        self.set_model_name(name)
        self.model_selected.emit(name)

    def set_model_name(self, name: str) -> None:
        """Update the displayed model name."""
        self._current_name = name
        self._button.setText(f"🧠  {name}  ▼")

    def set_model_groups(self, groups: list) -> None:
        """Set the grouped model list used by the dropdown.

        Expected format::

            [
                ("主模型", [
                    ("MiniMax-M2.7-highspeed", "极速 · 1.2s"),
                    ("MiniMax-M2.7-standard", "均衡 · 2.8s"),
                    ("MiniMax-M2.7-deep", "深度 · 5.1s"),
                ]),
                ("备用模型", [
                    ("GPT-4o-mini", "OpenAI · 1.5s"),
                    ("Claude-3.5-Sonnet", "Anthropic · 2.1s"),
                ]),
            ]
        """
        self._groups = groups
