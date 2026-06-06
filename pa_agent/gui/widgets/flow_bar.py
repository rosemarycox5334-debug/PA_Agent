"""5-step horizontal flow indicator."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


_COLORS = {
    "idle": {"bg": "#1c2128", "border": "#484f58", "glow": None},
    "done": {"bg": "#22c55e", "border": "#22c55e", "glow": "rgba(34,197,94,0.18)"},
    "active": {"bg": "#1c2128", "border": "#38bdf8", "glow": "rgba(56,189,248,0.20)"},
    "error": {"bg": "#ef4444", "border": "#ef4444", "glow": "rgba(239,68,68,0.18)"},
}

_DEFAULT_NAMES = ["数据", "快照", "诊断", "决策", "追问"]
_DEFAULT_CAPTIONS = ["等待连接", "未获取", "等待阶段一", "等待阶段二", "等待完成"]


class _StepWidget(QFrame):
    """Single step panel: dot + name + caption."""

    def __init__(
        self,
        name: str,
        caption: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setContentsMargins(0, 0, 0, 0)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 12, 0)
        outer.setSpacing(8)
        outer.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # Glow wrapper
        self._glow = QFrame(self)
        self._glow.setFixedSize(30, 30)
        self._glow.setStyleSheet("background: transparent; border-radius: 15px;")
        glow_layout = QHBoxLayout(self._glow)
        glow_layout.setContentsMargins(0, 0, 0, 0)
        glow_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Dot
        self._dot = QLabel(self._glow)
        self._dot.setFixedSize(20, 20)
        self._dot.setStyleSheet(
            "background-color: #1c2128; border: 2px solid #484f58; border-radius: 10px;"
        )
        glow_layout.addWidget(self._dot)

        outer.addWidget(self._glow)

        # Text column
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        text_col.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._name = QLabel(name)
        self._name.setStyleSheet("font-size: 12px; font-weight: bold; color: #e6edf3;")
        self._name.setMinimumWidth(0)
        text_col.addWidget(self._name)

        self._caption = QLabel(caption)
        self._caption.setStyleSheet("font-size: 11px; color: #8b949e;")
        self._caption.setMinimumWidth(0)
        self._caption.setWordWrap(False)
        self._caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        text_col.addWidget(self._caption)

        outer.addLayout(text_col, stretch=1)

    def set_status(self, status: str) -> None:
        """Update dot appearance for the given status."""
        spec = _COLORS.get(status, _COLORS["idle"])
        dot_ss = (
            f"background-color: {spec['bg']}; "
            f"border: 2px solid {spec['border']}; "
            f"border-radius: 10px;"
        )
        self._dot.setStyleSheet(dot_ss)

        if spec["glow"]:
            self._glow.setStyleSheet(
                f"background: {spec['glow']}; border-radius: 15px;"
            )
        else:
            self._glow.setStyleSheet("background: transparent; border-radius: 15px;")

    def set_caption(self, text: str) -> None:
        """Update the caption label."""
        self._caption.setText(text)


class FlowBar(QWidget):
    """5-step horizontal flow indicator.

    Parameters
    ----------
    parent:
        Optional parent widget.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("flowBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self._steps: list[_StepWidget] = []
        for name, caption in zip(_DEFAULT_NAMES, _DEFAULT_CAPTIONS, strict=True):
            step = _StepWidget(name, caption)
            layout.addWidget(step, stretch=1)
            self._steps.append(step)

    def set_step_status(self, index: int, status: str) -> None:
        """Set the visual status of a step.

        Parameters
        ----------
        index:
            Zero-based step index.
        status:
            One of ``"idle"``, ``"done"``, ``"active"``, ``"error"``.
        """
        if 0 <= index < len(self._steps):
            self._steps[index].set_status(status)

    def set_step_caption(self, index: int, text: str) -> None:
        """Update the caption text of a step.

        Parameters
        ----------
        index:
            Zero-based step index.
        text:
            New caption string.
        """
        if 0 <= index < len(self._steps):
            self._steps[index].set_caption(text)

    def reset_all(self) -> None:
        """Reset every step to *idle* and restore default captions."""
        for step, caption in zip(self._steps, _DEFAULT_CAPTIONS, strict=True):
            step.set_status("idle")
            step.set_caption(caption)
