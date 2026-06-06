"""Top-right toast notifications with slide-in / fade-out animation."""
from __future__ import annotations

from PyQt6.QtCore import QPropertyAnimation, Qt, QTimer, pyqtProperty
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

_TOAST_STYLES = {
    "ok": {
        "border": "rgba(34,197,94,0.4)",
        "text": "#86efac",
    },
    "warn": {
        "border": "rgba(245,158,11,0.4)",
        "text": "#fbbf24",
    },
    "info": {
        "border": "rgba(56,189,248,0.4)",
        "text": "#7dd3fc",
    },
}

_DEFAULT_STYLE = _TOAST_STYLES["info"]


class _ToastItem(QFrame):
    """Individual toast bubble with animated offset and opacity."""

    def __init__(
        self,
        message: str,
        type: str = "info",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("toastItem")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        spec = _TOAST_STYLES.get(type, _DEFAULT_STYLE)
        self.setStyleSheet(
            f"""
            QFrame#toastItem {{
                background-color: #1c2128;
                border: 1px solid {spec['border']};
                border-radius: 6px;
            }}
            QLabel {{
                color: {spec['text']};
                font-size: 12px;
            }}
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(0)

        self._label = QLabel(message)
        self._label.setStyleSheet(f"color: {spec['text']}; font-size: 12px;")
        layout.addWidget(self._label)

        # Drop shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 102))
        self.setGraphicsEffect(shadow)

        self._opacity = 1.0
        self._y_offset = 0.0

    # ---- opacity ----

    def get_opacity(self) -> float:
        return self._opacity

    def set_opacity(self, value: float) -> None:
        self._opacity = value
        self.update()

    opacity = pyqtProperty(float, get_opacity, set_opacity)

    # ---- y_offset ----

    def get_y_offset(self) -> float:
        return self._y_offset

    def set_y_offset(self, value: float) -> None:
        self._y_offset = value
        self.update()

    y_offset = pyqtProperty(float, get_y_offset, set_y_offset)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        painter.translate(0, int(self._y_offset))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        super().paintEvent(event)
        painter.end()


class ToastOverlay(QWidget):
    """Manages a vertical stack of toast notifications in the top-right corner.

    Parameters
    ----------
    parent:
        The parent widget (typically the main window). The overlay sizes itself
        to the parent's geometry.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(parent.rect())
        self.raise_()

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 16, 16, 0)
        self._layout.setSpacing(8)
        self._layout.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight
        )

        self._toasts: list[_ToastItem] = []

        parent.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        from PyQt6.QtCore import QEvent

        if event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parentWidget().rect())
        return super().eventFilter(obj, event)

    def show_toast(
        self,
        message: str,
        type: str = "info",
        duration_ms: int = 2200,
    ) -> None:
        """Display a new toast notification.

        Parameters
        ----------
        message:
            Text to display.
        type:
            One of ``"ok"``, ``"warn"``, ``"info"``.
        duration_ms:
            Time before the toast auto-dismisses (milliseconds).
        """
        toast = _ToastItem(message, type, parent=self)
        self._layout.insertWidget(0, toast)
        self._toasts.append(toast)

        toast.y_offset = -20.0
        toast.opacity = 0.0

        # Slide in
        anim_slide = QPropertyAnimation(toast, b"y_offset")
        anim_slide.setDuration(200)
        anim_slide.setStartValue(-20.0)
        anim_slide.setEndValue(0.0)
        anim_slide.start()

        # Fade in
        anim_opacity = QPropertyAnimation(toast, b"opacity")
        anim_opacity.setDuration(200)
        anim_opacity.setStartValue(0.0)
        anim_opacity.setEndValue(1.0)
        anim_opacity.start()

        # Auto-dismiss
        QTimer.singleShot(duration_ms, lambda: self._dismiss(toast))

    def _dismiss(self, toast: _ToastItem) -> None:
        """Play fade-out and remove the toast."""
        if toast not in self._toasts:
            return
        self._toasts.remove(toast)

        anim_opacity = QPropertyAnimation(toast, b"opacity")
        anim_opacity.setDuration(200)
        anim_opacity.setStartValue(1.0)
        anim_opacity.setEndValue(0.0)

        def _on_finished() -> None:
            self._layout.removeWidget(toast)
            toast.deleteLater()

        anim_opacity.finished.connect(_on_finished)
        anim_opacity.start()
