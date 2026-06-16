"""Price label item for pyqtgraph — shows a bar's close price above the candle."""
from __future__ import annotations

import pyqtgraph as pg
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QFont


def _fmt_price(value: float) -> str:
    """Format a price value for display, avoiding scientific notation."""
    # Use 8 significant digits — enough for any financial price
    # without producing scientific notation for numbers < 100 million.
    s = f"{value:.8g}"
    return s


class SeqLabelItem(pg.TextItem):
    """A small text label showing a candle's close price above its high.

    The label is positioned above the candle's high price.

    Parameters
    ----------
    price:
        Price value to display (typically the bar's close).
    x_pos:
        Integer x-axis position matching the corresponding CandleItem.
    y_pos:
        Y-axis position (typically the bar's high price).
    """

    _COLOR = QColor(180, 180, 180)  # light grey — unobtrusive

    def __init__(
        self,
        price: float,
        x_pos: int,
        y_pos: float,
        *,
        font_pt: int = 8,
        forming: bool = False,
    ) -> None:
        label = _fmt_price(price)
        color = QColor(120, 200, 220, 200) if forming else self._COLOR
        super().__init__(
            text=label,
            color=color,
            anchor=(0.5, 1.0),  # horizontally centred, bottom of text at y_pos
        )
        self.setFont(QFont("Arial", font_pt))
        self.setPos(QPointF(float(x_pos), y_pos))
