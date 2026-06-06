"""Overlay horizontal lines for entry / TP / SL on a pyqtgraph PlotWidget."""
from __future__ import annotations

from typing import TYPE_CHECKING

import pyqtgraph as pg
from PyQt6.QtGui import QColor

if TYPE_CHECKING:
    from pyqtgraph import PlotItem

# Line colors
_COLOR_ENTRY = QColor(30, 144, 255)   # dodger blue
_COLOR_TP = QColor(0, 200, 80)        # green
_COLOR_SL = QColor(220, 50, 50)       # red


class OverlayLines:
    """Manages entry / TP / SL horizontal lines on a PlotWidget.

    Each line is an ``pg.InfiniteLine`` (angle=0, i.e. horizontal) paired
    with a ``pg.TextItem`` label positioned at the left edge of the view.

    Usage::

        overlay = OverlayLines()
        overlay.set_lines(plot_item, entry=1900.0, tp=1920.0, sl=1880.0)
        # … later …
        overlay.clear_lines(plot_item)
    """

    def __init__(self) -> None:
        self._items: list[pg.GraphicsItem] = []
        self._labels: list[pg.TextItem] = []
        self._plot: "PlotItem | None" = None
        self._range_conn = None

    # ── Public API ────────────────────────────────────────────────────────────

    def set_lines(
        self,
        plot: "PlotItem",
        entry: float,
        tp: float,
        sl: float,
    ) -> None:
        """Draw (or redraw) the three horizontal price lines.

        Clears any previously drawn lines first. Labels are anchored to the
        left edge of the current view and stay there on pan/zoom.
        """
        self.clear_lines(plot)
        self._plot = plot

        specs = [
            (entry, _COLOR_ENTRY, "Entry"),
            (tp, _COLOR_TP, "TP"),
            (sl, _COLOR_SL, "SL"),
        ]

        for price, color, label_text in specs:
            line = pg.InfiniteLine(
                pos=price,
                angle=0,
                pen=pg.mkPen(color=color, width=1, style=pg.QtCore.Qt.PenStyle.DashLine),
                movable=False,
            )
            label = pg.TextItem(
                text=f"{label_text}: {price:.5g}",
                color=color,
                anchor=(0.0, 1.0),
            )

            plot.addItem(line)
            plot.addItem(label)
            self._items.extend([line, label])
            self._labels.append(label)

        # Position labels at the left edge of the current view
        self._update_label_positions()

        # Keep labels anchored to the left edge when the view changes
        vb = plot.getViewBox()
        self._range_conn = vb.sigRangeChanged.connect(self._update_label_positions)

    def clear_lines(self, plot: "PlotItem") -> None:
        """Remove all managed lines and labels from the plot."""
        if self._range_conn is not None:
            try:
                plot.getViewBox().sigRangeChanged.disconnect(self._range_conn)
            except Exception:
                pass
            self._range_conn = None

        for item in self._items:
            plot.removeItem(item)
        self._items.clear()
        self._labels.clear()
        self._plot = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _update_label_positions(self) -> None:
        """Move all labels to the left edge of the current X view range."""
        if self._plot is None or not self._labels:
            return
        try:
            x_min = self._plot.getViewBox().viewRange()[0][0]
        except Exception:
            return
        for label in self._labels:
            # Extract price from text (format: "Label: price")
            text = label.textItem.toPlainText()
            try:
                price = float(text.split(":")[-1].strip())
            except (ValueError, IndexError):
                continue
            label.setPos(x_min, price)
