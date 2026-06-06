"""EnhancedStatusBar — custom status bar with message, progress bar, and label."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QWidget


class EnhancedStatusBar(QWidget):
    """A status bar replacement showing a message and a colour-aware progress pill.

    The progress bar can switch between three colours at runtime:
    *normal* (cyan), *yellow* (warning), and *red* (danger).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("enhancedStatusBar")
        self._color = "normal"

        self.setFixedHeight(28)
        self.setStyleSheet("background-color: #161b22;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(10)

        self._message = QLabel("")
        self._message.setStyleSheet(
            "font-size: 11px; color: #8b949e;"
            "border: none; background: transparent;"
        )
        layout.addWidget(self._message, stretch=1)

        right = QHBoxLayout()
        right.setSpacing(8)

        ctx_label = QLabel("上下文")
        ctx_label.setStyleSheet(
            "font-size: 11px; color: #8b949e;"
            "border: none; background: transparent;"
        )
        right.addWidget(ctx_label)

        self._progress = QProgressBar()
        self._progress.setTextVisible(False)
        self._progress.setMinimum(0)
        self._progress.setMaximum(100)
        self._progress.setValue(0)
        self._progress.setFixedWidth(128)
        self._progress.setMaximumHeight(6)
        self._progress.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        right.addWidget(self._progress)

        self._progress_label = QLabel("0% · 0 / 0")
        self._progress_label.setStyleSheet(
            "font: 11px monospace; color: #8b949e;"
            "border: none; background: transparent;"
        )
        right.addWidget(self._progress_label)

        self._tps_label = QLabel("")
        self._tps_label.setStyleSheet(
            "font: 11px monospace; color: #2dd4bf;"
            "border: none; background: transparent;"
        )
        right.addWidget(self._tps_label)

        layout.addLayout(right)
        self._apply_progress_style()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_message(self, text: str) -> None:
        """Update the left-hand status message."""
        self._message.setText(text)

    def set_progress(self, pct: float, label: str = "") -> None:
        """Set the progress value (0–100) and optional label text.

        Parameters
        ----------
        pct:
            Percentage in the range ``0..100``.
        label:
            Human-readable string such as ``"6.3% · 126,165 / 1,997,000"``.
            If omitted, the percentage alone is shown.
        """
        self._progress.setValue(int(pct))
        display = label if label else f"{pct:.1f}%"
        self._progress_label.setText(display)

    def set_progress_color(self, color: str) -> None:
        """Change the progress bar chunk colour.

        Parameters
        ----------
        color:
            One of ``"normal"`` (cyan), ``"yellow"`` (amber), ``"red"`` (danger).
        """
        self._color = color
        self._apply_progress_style()

    def set_tps(self, tps: float, label: str = "") -> None:
        """Update the TPS (tokens per second) read-out.

        Parameters
        ----------
        tps:
            Tokens-per-second value.  Negative or zero hides the label.
        label:
            Optional override text.  If empty, ``f"{tps:.1f} TPS"`` is shown.
        """
        if tps > 0:
            display = label if label else f"{tps:.1f} TPS"
            self._tps_label.setText(display)
            self._tps_label.show()
        else:
            self._tps_label.hide()

    def showMessage(self, text: str) -> None:
        """QStatusBar compatibility alias."""
        self.set_message(text)

    def currentMessage(self) -> str:
        """QStatusBar compatibility alias."""
        return self._message.text()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _apply_progress_style(self) -> None:
        chunk_colors = {
            "normal": "#38bdf8",
            "yellow": "#f59e0b",
            "red": "#ef4444",
        }
        fill = chunk_colors.get(self._color, chunk_colors["normal"])
        self._progress.setStyleSheet(
            "QProgressBar {"
            "  border: 1px solid #30363d;"
            "  border-radius: 999px;"
            "  background-color: #0a0e14;"
            "  max-height: 6px;"
            "  text-align: center;"
            "}"
            f"QProgressBar::chunk {{"
            f"  background-color: {fill};"
            "  border-radius: 3px;"
            "}"
        )
