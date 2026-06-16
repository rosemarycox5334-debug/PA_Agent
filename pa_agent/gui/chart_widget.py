"""ChartWidget — pyqtgraph-based K-line chart with EMA20 and overlay lines.

Tasks 14.2 + 14.5:
  - Renders N candles, EMA20 line, and sequence-number labels.
  - Draws entry/TP/SL horizontal lines when order_type != "不下单".
  - 30 Hz QTimer throttles redraws so the 1 Hz data thread never blocks the UI.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QEvent, Qt, QTimer

from pa_agent.gui.widgets.candle_item import CandleItem
from pa_agent.gui.widgets.overlay_lines import OverlayLines
from pa_agent.gui.widgets.seq_label_item import SeqLabelItem
from pa_agent.util.trade_metrics import is_long_direction

if TYPE_CHECKING:
    from pa_agent.data.base import KlineFrame

# ── Constants ─────────────────────────────────────────────────────────────────

_TIMER_INTERVAL_MS = 33  # ~30 Hz
_EMA_COLOR = (255, 200, 0)  # amber
_NO_ORDER_TEXT = "不下单"
_X_MARGIN_BARS = 0.65
_Y_PADDING_RATIO = 0.07
_Y_TOP_EXTRA_RATIO = 0.04
_FIT_VISIBLE_BARS = 20
_AXIS_RESIZE_MIN_WIDTH = 40
_AXIS_RESIZE_EDGE_PX = 8


class ChartWidget(pg.PlotWidget):
    """Interactive K-line chart widget.

    Parameters
    ----------
    parent:
        Optional Qt parent widget.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent=parent)

        # Configure plot appearance
        self.setBackground("#0d1117")
        self.showGrid(x=False, y=True, alpha=0.3)
        self.getPlotItem().setLabel("left", "Price")

        # Internal state
        self._latest_frame: KlineFrame | None = None
        self._dirty: bool = False
        self._candle_items: list[CandleItem] = []
        self._seq_labels: list[SeqLabelItem] = []
        self._ema_line: pg.PlotDataItem | None = None
        self._overlay = OverlayLines()
        self._sr_items: list[pg.GraphicsItem] = []  # support/resistance level lines
        self._pending_decision: dict | None = None
        self._direction_items: list[pg.GraphicsItem] = []
        self._seq_label_font_pt: int = 7
        self._fit_on_next_render: bool = False
        self._first_frame_fitted: bool = False

        # Price-axis resize state
        self._axis_resizing: bool = False
        self._axis_drag_origin_x: float = 0.0
        self._axis_drag_origin_w: float = 0.0

        vb = self.getViewBox()
        vb.enableAutoRange(x=False, y=False)

        # 30 Hz redraw timer (task 14.5)
        self._timer = QTimer(self)
        self._timer.setInterval(_TIMER_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_seq_label_font_pt(self, point_size: int) -> None:
        """Set K-line sequence label font size and refresh the chart if needed."""
        point_size = max(6, min(24, int(point_size)))
        if point_size == self._seq_label_font_pt:
            return
        self._seq_label_font_pt = point_size
        if self._latest_frame is not None:
            self._dirty = True

    def set_frame(self, frame: "KlineFrame", *, fit_view: bool = False) -> None:
        """Cache the latest KlineFrame; actual redraw happens on the timer."""
        if self._should_skip_redraw(frame):
            self._latest_frame = frame
            if fit_view or not self._first_frame_fitted:
                self._fit_on_next_render = True
            return
        self._latest_frame = frame
        if fit_view or not self._first_frame_fitted:
            self._fit_on_next_render = True
        self._dirty = True

    def set_frame_now(self, frame: "KlineFrame", *, fit_view: bool = False) -> None:
        """Apply *frame* to the chart immediately (bypass 30 Hz throttle)."""
        if self._should_skip_redraw(frame):
            self._latest_frame = frame
            if fit_view and not self._first_frame_fitted:
                self.fit_view()
            return
        self._latest_frame = frame
        self._dirty = False
        self._render_frame(frame)
        if fit_view:
            self.fit_view()

    def _should_skip_redraw(self, frame: "KlineFrame") -> bool:
        """Skip repaint when the screen already shows the same closed-only snapshot."""
        from pa_agent.data.snapshot import frame_is_pure_closed, frames_equal_for_chart

        current = self._latest_frame
        if current is None or not self._candle_items:
            return False
        if not frame_is_pure_closed(current) or not frame_is_pure_closed(frame):
            return False
        return frames_equal_for_chart(current, frame)

    def request_fit_on_next_render(self) -> None:
        """Zoom/pan to fit the next rendered frame (or now if one is already shown)."""
        self._fit_on_next_render = True
        if self._latest_frame is not None:
            self._dirty = True

    def fit_view(self) -> None:
        """Set view range to show all bars and a comfortable price span."""
        frame = self._latest_frame
        if frame is None or not frame.bars:
            return
        x_range, y_range = self._view_ranges_for_frame(frame)
        self.getViewBox().setRange(
            xRange=x_range,
            yRange=y_range,
            padding=0,
        )
        self._first_frame_fitted = True

    def displayed_frame(self) -> "KlineFrame | None":
        """Return the KlineFrame currently shown on the chart."""
        return self._latest_frame

    def set_decision(self, decision: dict) -> None:
        """Draw or clear entry/TP/SL lines and direction marker from the AI decision."""
        self._pending_decision = decision
        order_type = decision.get("order_type", _NO_ORDER_TEXT)
        if order_type == _NO_ORDER_TEXT:
            self._overlay.clear_lines(self)
            self._clear_direction_marker()
            self._pending_decision = None
            return

        entry = decision.get("entry_price")
        tp = decision.get("take_profit_price")
        sl = decision.get("stop_loss_price")

        if entry is not None and tp is not None and sl is not None:
            try:
                self._overlay.set_lines(self, float(entry), float(tp), float(sl))
            except (TypeError, ValueError):
                self._overlay.clear_lines(self)
        else:
            self._overlay.clear_lines(self)

        self._update_direction_marker()

    def clear_decision_overlay(self) -> None:
        """Remove entry/TP/SL lines and direction marker; keep the current K-line frame."""
        self._overlay.clear_lines(self)
        self._clear_direction_marker()
        self._pending_decision = None

    def set_support_resistance(self, levels: list) -> None:
        """Draw horizontal support/resistance lines from StructureLevel objects.

        Parameters
        ----------
        levels:
            List of ``StructureLevel`` objects (from ``pa_agent.gui.support_resistance``).
            Supports are drawn in green, resistances in red/amber.
        """
        plot = self.getPlotItem()
        for item in self._sr_items:
            plot.removeItem(item)
        self._sr_items.clear()

        for level in levels:
            kind = getattr(level, "kind", "support")
            price = getattr(level, "price", None)
            low = getattr(level, "low", price)
            high = getattr(level, "high", price)
            label_text = getattr(level, "label", kind)
            if price is None:
                continue

            if kind == "support":
                color = (34, 197, 94, 180)    # green
                text_color = (134, 239, 172)   # light green
            else:
                color = (245, 158, 11, 180)    # amber
                text_color = (251, 191, 36)    # yellow

            # Draw the midline
            line = pg.InfiniteLine(
                pos=price,
                angle=0,
                pen=pg.mkPen(color=color, width=1,
                             style=pg.QtCore.Qt.PenStyle.DashLine),
                movable=False,
            )
            plot.addItem(line)
            self._sr_items.append(line)

            # Draw a zone fill if it's a range (high != low)
            is_zone = abs((high or price) - (low or price)) > 1e-9
            if is_zone and low is not None and high is not None:
                zone_color = (*color[:3], 28)  # very transparent fill
                fill = pg.LinearRegionItem(
                    values=(low, high),
                    orientation="horizontal",
                    movable=False,
                    brush=pg.mkBrush(color=zone_color),
                    pen=pg.mkPen(None),
                )
                plot.addItem(fill)
                self._sr_items.append(fill)

            # Label
            label = pg.TextItem(
                text=f"{label_text}: {price:.5g}",
                color=text_color,
                anchor=(0.0, 0.5),
            )
            plot.addItem(label)
            self._sr_items.append(label)
            label._sr_price = float(price)  # type: ignore[attr-defined]

        # Position labels at left edge (use exact price, not rounded display text)
        if self._sr_items:
            try:
                x_min = self.getViewBox().viewRange()[0][0]
                for item in self._sr_items:
                    if isinstance(item, pg.TextItem):
                        p = getattr(item, "_sr_price", None)
                        if p is not None:
                            item.setPos(x_min, float(p))
            except Exception:  # noqa: BLE001
                pass

    def clear_support_resistance(self) -> None:
        """Remove all support/resistance lines from the chart."""
        plot = self.getPlotItem()
        for item in self._sr_items:
            plot.removeItem(item)
        self._sr_items.clear()

    # ── Price-axis resize via viewportEvent ──────────────────────────────────

    def _axis_right_edge_wx(self) -> float:
        """Right edge x of the left price axis in viewport coordinates."""
        axis = self.getPlotItem().getAxis("left")
        geom = axis.geometry()  # layout-managed rect (not sceneBoundingRect!)
        return float(self.mapFromScene(geom.bottomRight()).x())

    def _axis_vertical_range_wy(self) -> tuple[float, float]:
        """Top/bottom y of the left price axis in viewport coordinates."""
        axis = self.getPlotItem().getAxis("left")
        geom = axis.geometry()
        return (
            float(self.mapFromScene(geom.topLeft()).y()),
            float(self.mapFromScene(geom.bottomRight()).y()),
        )

    def _in_axis_resize_zone(self, vx: float, vy: float) -> bool:
        """True when (vx, vy) is within ``_AXIS_RESIZE_EDGE_PX`` of the axis right edge."""
        edge = self._axis_right_edge_wx()
        top, bot = self._axis_vertical_range_wy()
        return abs(vx - edge) < _AXIS_RESIZE_EDGE_PX and top <= vy <= bot

    def viewportEvent(self, ev):  # noqa: N802
        """Intercept viewport mouse events to handle price-axis width resizing.

        This is the canonical entry-point for viewport events in
        ``QAbstractScrollArea`` (parent of ``QGraphicsView``).  We check
        whether the event is inside the price-axis resize zone; if so, we
        handle the drag ourselves and return ``True`` to prevent the event
        from reaching ``QGraphicsView::viewportEvent`` (and thus the scene).
        Otherwise we delegate to the superclass so normal pan/zoom/drag
        on the ViewBox works as usual.
        """
        et = ev.type()

        if et == QEvent.Type.MouseMove:
            pos = ev.position()
            if self._axis_resizing:
                dx = pos.x() - self._axis_drag_origin_x
                new_w = max(
                    _AXIS_RESIZE_MIN_WIDTH,
                    int(self._axis_drag_origin_w + dx),
                )
                self.getPlotItem().getAxis("left").setWidth(new_w)
                ev.accept()
                return True  # consume event — don't forward to scene
            # Cursor hint (on the viewport, not the QGraphicsView)
            vp = self.viewport()
            if self._in_axis_resize_zone(pos.x(), pos.y()):
                vp.setCursor(Qt.CursorShape.SplitHCursor)
            else:
                vp.unsetCursor()

        elif et == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            pos = ev.position()
            if self._in_axis_resize_zone(pos.x(), pos.y()):
                self._axis_resizing = True
                self._axis_drag_origin_x = pos.x()
                self._axis_drag_origin_w = self.getPlotItem().getAxis("left").width()
                ev.accept()
                return True

        elif et == QEvent.Type.MouseButtonRelease and self._axis_resizing:
            self._axis_resizing = False
            ev.accept()
            return True

        return super().viewportEvent(ev)

    def reset(self) -> None:
        """Clear all chart items (candles, labels, EMA, overlay lines)."""
        self.clear_decision_overlay()
        self._clear_candles_and_labels()
        if self._ema_line is not None:
            self.removeItem(self._ema_line)
            self._ema_line = None
        self._latest_frame = None
        self._dirty = False
        self._fit_on_next_render = False
        self._first_frame_fitted = False

    # ── Timer slot ────────────────────────────────────────────────────────────

    def _on_timer(self) -> None:
        """Called every ~33 ms; redraws only when a new frame is available."""
        if not self._dirty or self._latest_frame is None:
            return
        self._dirty = False
        self._render_frame(self._latest_frame)

    # ── Internal rendering ────────────────────────────────────────────────────

    def _render_frame(self, frame: "KlineFrame") -> None:
        """Rebuild all candle items, EMA line, and sequence labels."""
        self._clear_candles_and_labels()
        if self._ema_line is not None:
            self.removeItem(self._ema_line)
            self._ema_line = None
        bars = frame.bars
        n = len(bars)
        if n == 0:
            return

        # bars[0] is newest (seq=1); we want x=0 for oldest, x=n-1 for newest
        # so x_pos for bars[i] = (n - 1 - i)
        ema_x: list[float] = []
        ema_y: list[float] = []

        for i, bar in enumerate(bars):
            x_pos = n - 1 - i  # oldest bar at x=0, newest at x=n-1

            forming = not bar.closed

            # Candle (forming bar: semi-transparent dashed outline)
            candle = CandleItem(bar, x_pos, forming=forming)
            self.addItem(candle)
            self._candle_items.append(candle)

            # Price label — show close price above every other bar
            # Skip forming bar (seq=0); show odd display-index bars only
            if bar.seq > 0:
                display_seq = n - i  # 1..N from left to right
                if display_seq % 2 == 1:
                    label_y = bar.high
                    seq_label = SeqLabelItem(
                        bar.close,
                        x_pos,
                        label_y,
                        font_pt=self._seq_label_font_pt,
                        forming=forming,
                    )
                    self.addItem(seq_label)
                    self._seq_labels.append(seq_label)

            # EMA20 point (skip NaN)
            ema_val = frame.indicators.ema20[i]
            if not math.isnan(ema_val):
                ema_x.append(float(x_pos))
                ema_y.append(ema_val)

        # EMA20 line (slightly dimmed through forming bar)
        if ema_x:
            newest_forming = len(bars) > 0 and not bars[0].closed
            ema_color: tuple[int, ...] = _EMA_COLOR
            if newest_forming:
                ema_color = (255, 200, 0, 140)
            self._ema_line = pg.PlotDataItem(
                x=np.array(ema_x),
                y=np.array(ema_y),
                pen=pg.mkPen(color=ema_color, width=1),
            )
            self.addItem(self._ema_line)

        self._update_direction_marker()

        if self._fit_on_next_render:
            self._fit_on_next_render = False
            self.fit_view()

    def _view_ranges_for_frame(
        self,
        frame: "KlineFrame",
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Compute (x_range, y_range) for the newest ``_FIT_VISIBLE_BARS`` bars."""
        bars = frame.bars
        n = len(bars)
        visible_count = min(_FIT_VISIBLE_BARS, n)
        visible_bars = bars[:visible_count]
        visible_ema = frame.indicators.ema20[:visible_count]

        y_min = min(b.low for b in visible_bars)
        y_max = max(b.high for b in visible_bars)

        for ema_val in visible_ema:
            if not math.isnan(ema_val):
                y_min = min(y_min, ema_val)
                y_max = max(y_max, ema_val)

        decision = self._pending_decision
        if decision is not None:
            for key in ("entry_price", "take_profit_price", "stop_loss_price"):
                raw = decision.get(key)
                if raw is None:
                    continue
                try:
                    price = float(raw)
                except (TypeError, ValueError):
                    continue
                y_min = min(y_min, price)
                y_max = max(y_max, price)

        span = y_max - y_min
        if span <= 0:
            mid = y_max if y_max != 0 else 1.0
            span = abs(mid) * 0.01 or 1.0
        y_pad = span * _Y_PADDING_RATIO
        y_top = span * _Y_TOP_EXTRA_RATIO

        # x=0 is oldest; newest bar is at x=n-1 — show only the rightmost window.
        x_left = float(max(0, n - _FIT_VISIBLE_BARS))
        x_min = x_left - _X_MARGIN_BARS
        x_max = float(n - 1) + _X_MARGIN_BARS
        return (
            (x_min, x_max),
            (y_min - y_pad, y_max + y_pad + y_top),
        )

    def _clear_direction_marker(self) -> None:
        for item in self._direction_items:
            self.removeItem(item)
        self._direction_items.clear()

    def _update_direction_marker(self) -> None:
        """Draw ▲/▼ at newest bar × entry price for long/short."""
        self._clear_direction_marker()
        decision = self._pending_decision
        frame = self._latest_frame
        if decision is None or frame is None:
            return
        if decision.get("order_type", _NO_ORDER_TEXT) == _NO_ORDER_TEXT:
            return

        entry = decision.get("entry_price")
        if entry is None:
            return
        try:
            entry_f = float(entry)
        except (TypeError, ValueError):
            return

        n = len(frame.bars)
        if n == 0:
            return

        long = is_long_direction(decision.get("order_direction"))
        if long is True:
            symbol, color = "▲", (63, 185, 80)
            anchor = (0.5, 1.0)
        elif long is False:
            symbol, color = "▼", (248, 81, 73)
            anchor = (0.5, 0.0)
        else:
            return

        x_pos = float(n - 1)
        marker = pg.TextItem(
            text=symbol,
            color=color,
            anchor=anchor,
        )
        from PyQt6.QtGui import QFont

        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        marker.setFont(font)
        marker.setPos(x_pos, entry_f)
        self.addItem(marker)
        self._direction_items.append(marker)

    def _clear_candles_and_labels(self) -> None:
        """Remove all candle and label items from the plot."""
        for item in self._candle_items:
            self.removeItem(item)
        self._candle_items.clear()

        for item in self._seq_labels:
            self.removeItem(item)
        self._seq_labels.clear()
