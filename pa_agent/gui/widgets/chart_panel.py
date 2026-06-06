"""ChartPanel — wrapper around ChartWidget with titlebar, legend, and footer."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


class ChartPanel(QWidget):
    """A composite widget that wraps ``ChartWidget`` with chrome UI.

    Layout (vertical):
        - titlebar  (symbol / timeframe / meta / status pill)
        - chart_widget  (``ChartWidget``, stretch=1)
        - legend    (EMA20 + up/down colour key)
        - footer    (usage hints + live price read-out)
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chartPanel")

        # Deferred import breaks the circular dependency via pa_agent.gui.__init__
        from pa_agent.gui.chart_widget import ChartWidget

        # ── Root layout ────────────────────────────────────────────────────────
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Title bar ──────────────────────────────────────────────────────────
        titlebar = QWidget()
        titlebar.setFixedHeight(40)
        titlebar.setStyleSheet(
            "background-color: #161b22;"
            "border-bottom: 1px solid #30363d;"
        )
        title_layout = QHBoxLayout(titlebar)
        title_layout.setContentsMargins(14, 0, 14, 0)
        title_layout.setSpacing(10)

        self._title = QLabel("品种 · 周期")
        self._title.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #e6edf3;"
            "border: none; background: transparent;"
        )
        title_layout.addWidget(self._title)

        self._meta = QLabel("")
        self._meta.setStyleSheet(
            "font-size: 12px; color: #8b949e;"
            "border: none; background: transparent;"
        )
        title_layout.addWidget(self._meta)

        self._status = QLabel("")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_layout.addStretch(1)
        title_layout.addWidget(self._status)

        root.addWidget(titlebar)

        # ── Chart widget ───────────────────────────────────────────────────────
        self._chart = ChartWidget(self)
        self._chart.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        root.addWidget(self._chart, stretch=1)

        # ── Legend ─────────────────────────────────────────────────────────────
        legend = QWidget()
        legend.setFixedHeight(28)
        legend.setStyleSheet(
            "background-color: #161b22;"
            "border-top: 1px solid #30363d;"
        )
        legend_layout = QHBoxLayout(legend)
        legend_layout.setContentsMargins(14, 0, 14, 0)
        legend_layout.setSpacing(16)

        ema10_item = QLabel("EMA10（天蓝线）")
        ema10_item.setStyleSheet(
            "font: 11px monospace; color: #7dd3fc;"
            "border: none; background: transparent;"
        )
        legend_layout.addWidget(ema10_item)

        ema20_item = QLabel("EMA20（金黄线）")
        ema20_item.setStyleSheet(
            "font: 11px monospace; color: #fbbf24;"
            "border: none; background: transparent;"
        )
        legend_layout.addWidget(ema20_item)

        ema60_item = QLabel("EMA60（橙红线）")
        ema60_item.setStyleSheet(
            "font: 11px monospace; color: #fb923c;"
            "border: none; background: transparent;"
        )
        legend_layout.addWidget(ema60_item)

        up_item = QLabel("涨（绿色）")
        up_item.setStyleSheet(
            "font: 11px monospace; color: #22c55e;"
            "border: none; background: transparent;"
        )
        legend_layout.addWidget(up_item)

        down_item = QLabel("跌（红色）")
        down_item.setStyleSheet(
            "font: 11px monospace; color: #ef4444;"
            "border: none; background: transparent;"
        )
        legend_layout.addWidget(down_item)

        legend_layout.addStretch(1)
        root.addWidget(legend)

        # ── Footer ─────────────────────────────────────────────────────────────
        footer = QWidget()
        footer.setFixedHeight(28)
        footer.setStyleSheet(
            "background-color: #161b22;"
            "border-top: 1px solid #30363d;"
        )
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(14, 0, 14, 0)
        footer_layout.setSpacing(10)

        self._footer_hint_text = "滚轮缩放 · 拖拽平移 · 当前为分析快照"
        self._footer_left = QLabel(self._footer_hint_text)
        self._footer_left.setStyleSheet(
            "font-size: 11px; color: #8b949e;"
            "border: none; background: transparent;"
        )
        footer_layout.addWidget(self._footer_left)

        footer_layout.addStretch(1)

        self._footer_right = QLabel("Price — · EMA20 —")
        self._footer_right.setStyleSheet(
            "font: 11px monospace; color: #8b949e;"
            "border: none; background: transparent;"
        )
        footer_layout.addWidget(self._footer_right)

        root.addWidget(footer)

        self._chart.bar_hovered.connect(self._on_bar_hovered)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_title(self, symbol: str, timeframe: str) -> None:
        """Update the primary title text, e.g. ``BTCUSDT · 15m``."""
        self._title.setText(f"{symbol} · {timeframe}")

    def set_meta(self, text: str) -> None:
        """Update the secondary meta label (e.g. bar count / indicator info)."""
        self._meta.setText(text)

    def set_status(self, status: str, text: str = "") -> None:
        """Set the status pill style.

        Parameters
        ----------
        status:
            One of ``"live"``, ``"snapshot"``, ``"error"``.
        text:
            Optional override text.  If empty, defaults are used.
        """
        defaults = {
            "live": "实时刷新中",
            "snapshot": "快照冻结",
            "error": "错误",
        }
        display = text or defaults.get(status, status)

        styles = {
            "live": (
                "color: #86efac;"
                "border: 1px solid rgba(34,197,94,0.35);"
                "background-color: rgba(34,197,94,0.10);"
            ),
            "snapshot": (
                "color: #fde047;"
                "border: 1px solid rgba(245,158,11,0.35);"
                "background-color: rgba(245,158,11,0.10);"
            ),
            "error": (
                "color: #fca5a5;"
                "border: 1px solid rgba(239,68,68,0.35);"
                "background-color: rgba(239,68,68,0.10);"
            ),
        }
        base = (
            "border-radius: 999px;"
            "padding: 2px 10px;"
            "font-size: 12px;"
            "background: transparent;"
        )
        self._status.setText(display)
        self._status.setStyleSheet(base + styles.get(status, styles["error"]))

    def set_footer_price(self, price_text: str) -> None:
        """Update the right-hand footer label (e.g. ``Price 67,420.50 · EMA20 67,210.10``)."""
        self._footer_right.setText(price_text)

    def _on_bar_hovered(self, summary: str) -> None:
        """Show hovered K-line context in the footer."""
        self._footer_left.setText(summary or self._footer_hint_text)

    def chart_widget(self) -> ChartWidget:
        """Return the internal ``ChartWidget`` instance for signal connections."""
        return self._chart
