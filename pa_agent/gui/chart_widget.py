"""ChartWidget — pyqtgraph-based K-line chart with EMA20 and overlay lines.

Tasks 14.2 + 14.5:
  - Renders N candles, EMA20 line, and sequence-number labels.
  - Draws entry/TP/SL horizontal lines when order_type != "不下单".
  - 30 Hz QTimer throttles redraws so the 1 Hz data thread never blocks the UI.
  - X-axis uses integer bar indices; bottom labels show Beijing timestamps.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal

from pa_agent.gui.widgets.candle_item import CandleItem
from pa_agent.gui.widgets.overlay_lines import OverlayLines
from pa_agent.gui.widgets.seq_label_item import SeqLabelItem
from pa_agent.gui.support_resistance import (
    StructureLevel,
    filter_levels_near_price,
    format_level,
)
from pa_agent.util.trade_metrics import is_long_direction

if TYPE_CHECKING:
    from pa_agent.data.base import KlineBar, KlineFrame

# ── Constants ─────────────────────────────────────────────────────────────────

_TIMER_INTERVAL_MS = 33  # ~30 Hz
_EMA_COLOR = (255, 200, 0)  # amber
_EMA10_COLOR = (125, 211, 252)  # sky blue
_EMA60_COLOR = (251, 146, 60)  # orange-red
_NO_ORDER_TEXT = "不下单"
_X_MARGIN_BARS = 0.5
_Y_PADDING_RATIO = 0.05
_Y_TOP_EXTRA_RATIO = 0.03
_FIT_VISIBLE_BARS = 24
_AXIS_RESIZE_MIN_WIDTH = 40
_AXIS_RESIZE_EDGE_PX = 8
_VOL_WIDTH = 0.6
_SUPPORT_COLOR = (56, 189, 248, 170)
_RESISTANCE_COLOR = (251, 191, 36, 170)


class _TimeAxisItem(pg.AxisItem):
    """AxisItem that shows Beijing timestamps at bar-index positions.

    Works with integer X-axis values (bar indices).  On each redraw the
    caller supplies the full ``bars`` tuple; the axis then picks sensible
    tick positions (first bar of each trading day, night-session starts,
    and a few intermediate labels) and formats them as Beijing time.
    """

    def __init__(self, bars: tuple["KlineBar", ...] = (), **kwargs) -> None:
        super().__init__(**kwargs)
        self._bars = bars
        self._major: list[tuple[float, str]] = []
        self._minor: list[tuple[float, str]] = []

    def set_bars(self, bars: tuple["KlineBar", ...]) -> None:
        """Rebuild tick tables from *bars* and refresh the axis."""
        self._bars = bars
        self._build_ticks()
        self.picture = None
        self.update()

    def _build_ticks(self) -> None:
        major: list[tuple[float, str]] = []
        minor: list[tuple[float, str]] = []
        n = len(self._bars)
        if n == 0:
            self._major = major
            self._minor = minor
            return

        last_day_label: str | None = None
        for i, bar in enumerate(self._bars):
            x_pos = float(n - 1 - i)
            beijing = datetime.fromtimestamp(
                bar.ts_open / 1000.0 + 8 * 3600, tz=timezone.utc
            )
            h, m = beijing.hour, beijing.minute

            # Major tick: first bar of day session (around 09:15 Beijing) → date
            if h == 9 and m <= 15:
                label = beijing.strftime("%m-%d")
                if label != last_day_label:
                    major.append((x_pos, label))
                    last_day_label = label

        # Minor ticks: every 8 bars (~2h for 15m) to keep spacing uniform
        step = max(1, n // 8) if n > 16 else 4
        for i in range(0, n, step):
            x_pos = float(n - 1 - i)
            # Skip if this bar already has a major (date) tick
            if any(abs(x_pos - mx) < 0.5 for mx, _ in major):
                continue
            beijing = datetime.fromtimestamp(
                self._bars[i].ts_open / 1000.0 + 8 * 3600, tz=timezone.utc
            )
            minor.append((x_pos, beijing.strftime("%H:%M")))

        self._major = major
        self._minor = minor

    def tickValues(self, minVal, maxVal, size):  # noqa: N802
        spacing = max(maxVal - minVal, 1e-9)
        visible_major = [v for v, _ in self._major if minVal <= v <= maxVal]
        visible_minor = [v for v, _ in self._minor if minVal <= v <= maxVal]
        levels = []
        if visible_major:
            levels.append((spacing, visible_major))
        if visible_minor:
            levels.append((spacing / 2, visible_minor))
        return levels

    def tickStrings(self, values, scale, spacing):  # noqa: N802
        labels = []
        for v in values:
            label = ""
            for tv, tl in self._major + self._minor:
                if abs(v - tv) < 0.01:
                    label = tl
                    break
            labels.append(label)
        return labels


class ChartWidget(pg.GraphicsLayoutWidget):
    """Interactive K-line chart widget with split-pane price + volume display.

    Uses a GraphicsLayoutWidget with two PlotItems:
      - _price_plot (top, 70%): candles, EMA, labels, overlay lines
      - _volume_plot (bottom, 30%): volume bars

    X-axis values are **integer bar indices** (0 = oldest visible candle).
    The bottom axis shows human-readable Beijing timestamps via custom ticks.

    Parameters
    ----------
    parent:
        Optional Qt parent widget.
    """

    bar_hovered = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent=parent)

        # Appearance
        self.setBackground("#0d1117")

        # Price plot (top)
        self._price_plot = pg.PlotItem()
        self._price_plot.setMenuEnabled(False)
        self._price_plot.showGrid(x=False, y=True, alpha=0.3)
        self._price_plot.setLabel("left", "Price")
        # Hide bottom labels on price plot; time labels shown on volume plot below
        self._price_plot.getAxis("bottom").setStyle(showValues=False)
        self._price_plot.getAxis("bottom").setPen("#30363d")
        self._price_plot.getViewBox().enableAutoRange(x=False, y=False)

        # Volume plot (bottom)
        self._volume_plot = pg.PlotItem()
        self._volume_plot.setMenuEnabled(False)
        self._volume_plot.showGrid(x=False, y=True, alpha=0.15)
        self._volume_plot.setLabel("left", "Vol")
        # Time labels are shown on the volume plot's bottom axis
        self._time_axis = _TimeAxisItem(orientation="bottom")
        self._time_axis.setPen("#30363d")
        self._time_axis.setTextPen("#e6edf3")  # light text on dark background
        self._time_axis.setHeight(30)
        self._volume_plot.setAxisItems({"bottom": self._time_axis})
        self._volume_plot.getViewBox().enableAutoRange(x=False, y=True)

        # Layout: row 0 = price, row 1 = volume
        self.ci.addItem(self._price_plot, row=0, col=0)
        self.ci.addItem(self._volume_plot, row=1, col=0)
        self.ci.layout.setRowStretchFactor(0, 7)
        self.ci.layout.setRowStretchFactor(1, 3)

        # X-axis linkage
        self._volume_plot.setXLink(self._price_plot)

        # Internal state
        self._latest_frame: KlineFrame | None = None
        self._dirty: bool = False
        self._candle_items: list[CandleItem] = []
        self._seq_labels: list[SeqLabelItem] = []
        self._ema_line: pg.PlotDataItem | None = None
        self._ema10_line: pg.PlotDataItem | None = None
        self._ema60_line: pg.PlotDataItem | None = None
        self._overlay = OverlayLines()
        self._structure_levels: list[StructureLevel] = []
        self._structure_items: list[pg.GraphicsItem] = []
        self._structure_labels: list[tuple[pg.TextItem, float]] = []
        self._structure_range_conn = None
        self._pending_decision: dict | None = None
        self._direction_items: list[pg.GraphicsItem] = []
        self._seq_label_font_pt: int = 7
        self._fit_on_next_render: bool = False
        self._first_frame_fitted: bool = False

        # Price-axis resize state
        self._axis_resizing: bool = False
        self._axis_drag_origin_x: float = 0.0
        self._axis_drag_origin_w: float = 0.0
        self._last_hover_summary: str = ""

        # Crosshair + Tooltip (price plot only)
        crosshair_pen = pg.mkPen(
            color=(56, 189, 248), width=1, style=Qt.PenStyle.DotLine
        )
        self._v_crosshair = pg.InfiniteLine(angle=90, movable=False, pen=crosshair_pen)
        self._h_crosshair = pg.InfiniteLine(angle=0, movable=False, pen=crosshair_pen)
        self._v_crosshair.hide()
        self._h_crosshair.hide()
        self._price_plot.addItem(self._v_crosshair)
        self._price_plot.addItem(self._h_crosshair)

        self._tooltip = pg.TextItem(
            text="",
            color=(230, 237, 243),
            anchor=(0, 1),
        )
        self._tooltip.hide()
        self._price_plot.addItem(self._tooltip)

        # Volume bars
        self._vol_items: list[pg.BarGraphItem] = []

        # Mouse tracking
        self.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # 30 Hz redraw timer (task 14.5)
        self._timer = QTimer(self)
        self._timer.setInterval(_TIMER_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()

    # ── Public API ────────────────────────────────────────────────────────────

    def getPlotItem(self):  # noqa: N802
        """Return the price plot for legacy PlotWidget-compatible callers."""
        return self._price_plot

    def getViewBox(self):  # noqa: N802
        """Return the price plot ViewBox for legacy PlotWidget-compatible callers."""
        return self._price_plot.getViewBox()

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

    def fit_view(self) -> bool:
        """Set view range to show all bars and a comfortable price span.

        Returns True when the view was actually adjusted.
        """
        frame = self._latest_frame
        if frame is None or not frame.bars:
            return False
        x_range, y_range = self._view_ranges_for_frame(frame)
        vb = self._price_plot.getViewBox()
        if vb is None:
            return False
        try:
            vb.setRange(
                xRange=list(x_range),
                yRange=list(y_range),
                padding=0,
            )
        except Exception:
            return False
        self._first_frame_fitted = True
        return True

    def displayed_frame(self) -> "KlineFrame | None":
        """Return the KlineFrame currently shown on the chart."""
        return self._latest_frame

    def set_decision(self, decision: dict) -> None:
        """Draw or clear entry/TP/SL lines and direction marker from the AI decision."""
        self._pending_decision = decision
        order_type = decision.get("order_type", _NO_ORDER_TEXT)
        if order_type == _NO_ORDER_TEXT:
            self._overlay.clear_lines(self._price_plot)
            self._clear_direction_marker()
            self._pending_decision = None
            return

        entry = decision.get("entry_price")
        tp = decision.get("take_profit_price")
        sl = decision.get("stop_loss_price")

        if entry is not None and tp is not None and sl is not None:
            try:
                self._overlay.set_lines(self._price_plot, float(entry), float(tp), float(sl))
            except (TypeError, ValueError):
                self._overlay.clear_lines(self._price_plot)
        else:
            self._overlay.clear_lines(self._price_plot)

        self._update_direction_marker()

    def set_structure_levels(self, levels: list[StructureLevel]) -> None:
        """Draw support/resistance levels in the price pane."""
        self.clear_structure_levels()
        frame = self._latest_frame
        if frame is not None:
            levels = filter_levels_near_price(levels, frame.bars)
        self._structure_levels = list(levels)
        for level in self._structure_levels:
            color = _SUPPORT_COLOR if level.kind == "support" else _RESISTANCE_COLOR
            name = level.label or ("支撑" if level.kind == "support" else "阻力")
            prices = [level.low, level.high] if level.is_zone else [level.price]
            for price in prices:
                line = pg.InfiniteLine(
                    pos=price,
                    angle=0,
                    pen=pg.mkPen(color=color, width=1, style=Qt.PenStyle.DotLine),
                    movable=False,
                )
                self._price_plot.addItem(line)
                self._structure_items.append(line)

            label_y = level.high if level.is_zone else level.price
            label = pg.TextItem(
                text=f"{name}: {format_level(level)}",
                color=color,
                anchor=(0.0, 1.0),
            )
            self._price_plot.addItem(label)
            self._structure_items.append(label)
            self._structure_labels.append((label, label_y))

        self._update_structure_label_positions()
        vb = self._price_plot.getViewBox()
        try:
            self._structure_range_conn = vb.sigRangeChanged.connect(
                self._update_structure_label_positions
            )
        except Exception:
            self._structure_range_conn = None
        if self._latest_frame is not None:
            self.fit_view()

    def clear_structure_levels(self) -> None:
        """Remove support/resistance levels from the price pane."""
        if self._structure_range_conn is not None:
            try:
                self._price_plot.getViewBox().sigRangeChanged.disconnect(
                    self._structure_range_conn
                )
            except Exception:
                pass
            self._structure_range_conn = None
        for item in self._structure_items:
            self._price_plot.removeItem(item)
        self._structure_items.clear()
        self._structure_labels.clear()
        self._structure_levels.clear()

    def clear_decision_overlay(self) -> None:
        """Remove entry/TP/SL lines and direction marker; keep the current K-line frame."""
        self._overlay.clear_lines(self._price_plot)
        self._clear_direction_marker()
        self._pending_decision = None

    # ── Price-axis resize via viewportEvent ──────────────────────────────────

    def _axis_right_edge_wx(self) -> float:
        """Right edge x of the left price axis in viewport coordinates."""
        axis = self._price_plot.getAxis("left")
        geom = axis.geometry()  # layout-managed rect (not sceneBoundingRect!)
        return float(self.mapFromScene(geom.bottomRight()).x())

    def _axis_vertical_range_wy(self) -> tuple[float, float]:
        """Top/bottom y of the left price axis in viewport coordinates."""
        axis = self._price_plot.getAxis("left")
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
                self._price_plot.getAxis("left").setWidth(new_w)
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
                self._axis_drag_origin_w = self._price_plot.getAxis("left").width()
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
        self.clear_structure_levels()
        self._clear_candles_and_labels()
        self._clear_ema_lines()
        self._clear_vol_items()
        self._latest_frame = None
        self._dirty = False
        self._fit_on_next_render = False
        self._first_frame_fitted = False
        self._v_crosshair.hide()
        self._h_crosshair.hide()
        self._tooltip.hide()

    # ── Timer slot ────────────────────────────────────────────────────────────

    def _on_timer(self) -> None:
        """Called every ~33 ms; redraws only when a new frame is available."""
        if not self._dirty or self._latest_frame is None:
            return
        self._dirty = False
        self._render_frame(self._latest_frame)

    # ── Mouse interaction ─────────────────────────────────────────────────────

    def _hide_hover(self) -> None:
        self._v_crosshair.hide()
        self._h_crosshair.hide()
        self._tooltip.hide()
        if self._last_hover_summary:
            self._last_hover_summary = ""
            self.bar_hovered.emit("")

    def _format_hover_summary(self, bar: "KlineBar") -> str:
        """Return a compact footer summary for the hovered K-line."""
        beijing = datetime.fromtimestamp(bar.ts_open / 1000.0 + 8 * 3600, tz=timezone.utc)
        time_str = beijing.strftime("%m-%d %H:%M")
        change = bar.close - bar.open
        change_pct = (change / bar.open * 100) if bar.open != 0 else 0.0
        sign = "+" if change >= 0 else ""
        seq = f"K{bar.seq}" if bar.seq > 0 else "未收盘K"
        return (
            f"{seq} · {time_str} · "
            f"O {bar.open:.2f} / H {bar.high:.2f} / L {bar.low:.2f} / C {bar.close:.2f} · "
            f"{sign}{change:.2f} ({sign}{change_pct:.2f}%) · Vol {bar.volume:,.0f}"
        )

    def _on_mouse_moved(self, pos) -> None:
        """Show crosshair and tooltip when hovering over a bar."""
        frame = self._latest_frame
        if frame is None or not frame.bars:
            self._hide_hover()
            return

        if not self._price_plot.vb.sceneBoundingRect().contains(pos):
            self._hide_hover()
            return

        try:
            mouse_point = self._price_plot.vb.mapSceneToView(pos)
        except Exception:
            self._hide_hover()
            return

        x_idx = int(round(mouse_point.x()))
        n = len(frame.bars)
        if x_idx < 0 or x_idx >= n:
            self._hide_hover()
            return

        bar_idx = n - 1 - x_idx
        bar = frame.bars[bar_idx]
        summary = self._format_hover_summary(bar)
        if summary != self._last_hover_summary:
            self._last_hover_summary = summary
            self.bar_hovered.emit(summary)

        self._v_crosshair.setPos(x_idx)
        self._h_crosshair.setPos(mouse_point.y())
        self._v_crosshair.show()
        self._h_crosshair.show()

        # Beijing time + change / amplitude calculations
        beijing = datetime.fromtimestamp(bar.ts_open / 1000.0 + 8 * 3600, tz=timezone.utc)
        time_str = beijing.strftime("%m-%d %H:%M")
        change = bar.close - bar.open
        change_pct = (change / bar.open * 100) if bar.open != 0 else 0.0
        amplitude = bar.high - bar.low
        amp_pct = (amplitude / bar.open * 100) if bar.open != 0 else 0.0
        color = "#22c55e" if change >= 0 else "#ef4444"
        sign = "+" if change >= 0 else ""

        tooltip_html = (
            "<div style='background-color:rgba(13,17,23,0.95); "
            "padding:5px 10px; border:1px solid #30363d; border-radius:5px;'>"
            f"<span style='color:#8b949e'>{time_str}  #{bar.seq}</span><br>"
            f"<span style='color:#e6edf3'>O</span> {bar.open:.2f}  "
            f"<span style='color:#e6edf3'>H</span> {bar.high:.2f}  "
            f"<span style='color:#e6edf3'>L</span> {bar.low:.2f}  "
            f"<span style='color:{color}'>C</span> {bar.close:.2f}<br>"
            f"<span style='color:{color}'>{sign}{change:.2f} ({sign}{change_pct:.2f}%)</span>  "
            f"<span style='color:#8b949e'>振幅</span> {amplitude:.2f} ({amp_pct:.2f}%)  "
            f"<span style='color:#8b949e'>Vol</span> {bar.volume:,.0f}"
            "</div>"
        )
        self._tooltip.setHtml(tooltip_html)

        # Position tooltip near the mouse, keeping inside the view
        vb = self._price_plot.getViewBox()
        x_range, y_range = vb.viewRange()
        tooltip_x = max(x_range[0], min(x_idx + 1.0, x_range[1] - 3))
        tooltip_y = min(y_range[1] - (y_range[1] - y_range[0]) * 0.05, mouse_point.y() + (y_range[1] - y_range[0]) * 0.15)
        self._tooltip.setPos(tooltip_x, tooltip_y)
        self._tooltip.show()

    # ── Internal rendering ────────────────────────────────────────────────────

    def _render_frame(self, frame: "KlineFrame") -> None:
        """Rebuild all candle items, EMA lines, sequence labels, and volume bars."""
        self._clear_candles_and_labels()
        self._clear_ema_lines()
        self._clear_vol_items()
        bars = frame.bars
        n = len(bars)
        if n == 0:
            return

        ema10_x: list[float] = []
        ema10_y: list[float] = []
        ema20_x: list[float] = []
        ema20_y: list[float] = []
        ema60_x: list[float] = []
        ema60_y: list[float] = []

        max_vol = max(b.volume for b in bars) if bars else 1.0

        for i, bar in enumerate(bars):
            x_pos = n - 1 - i  # oldest bar at x=0, newest at x=n-1

            forming = not bar.closed

            # Candle (forming bar: semi-transparent dashed outline)
            candle = CandleItem(bar, x_pos, forming=forming)
            self._price_plot.addItem(candle)
            self._candle_items.append(candle)

            # Sequence label — odd seq only; skip forming bar (seq=0)
            if bar.seq > 0 and bar.seq % 2 == 1:
                label_y = bar.high
                seq_label = SeqLabelItem(
                    bar.seq,
                    x_pos,
                    label_y,
                    font_pt=self._seq_label_font_pt,
                    forming=forming,
                )
                self._price_plot.addItem(seq_label)
                self._seq_labels.append(seq_label)

            # EMA points (skip NaN)
            ema10_val = frame.indicators.ema10[i]
            if not math.isnan(ema10_val):
                ema10_x.append(float(x_pos))
                ema10_y.append(ema10_val)

            ema20_val = frame.indicators.ema20[i]
            if not math.isnan(ema20_val):
                ema20_x.append(float(x_pos))
                ema20_y.append(ema20_val)

            ema60_val = frame.indicators.ema60[i]
            if not math.isnan(ema60_val):
                ema60_x.append(float(x_pos))
                ema60_y.append(ema60_val)

            # Volume bar → volume_plot
            vol_height = bar.volume
            vol_color = (
                (34, 197, 94, 90) if bar.close >= bar.open else (239, 68, 68, 90)
            )
            vol_item = pg.BarGraphItem(
                x=[float(x_pos)],
                height=[vol_height],
                width=_VOL_WIDTH,
                brush=pg.mkBrush(color=vol_color),
                pen=pg.mkPen(color=vol_color[:3] + (120,), width=1),
            )
            self._volume_plot.addItem(vol_item)
            self._vol_items.append(vol_item)

        # Update time-axis ticks
        self._time_axis.set_bars(tuple(bars))

        newest_forming = len(bars) > 0 and not bars[0].closed

        # EMA10 line
        if ema10_x:
            color = _EMA10_COLOR if not newest_forming else (*_EMA10_COLOR, 140)
            self._ema10_line = pg.PlotDataItem(
                x=np.array(ema10_x),
                y=np.array(ema10_y),
                pen=pg.mkPen(color=color, width=1),
            )
            self._price_plot.addItem(self._ema10_line)

        # EMA20 line
        if ema20_x:
            color = _EMA_COLOR if not newest_forming else (*_EMA_COLOR, 140)
            self._ema_line = pg.PlotDataItem(
                x=np.array(ema20_x),
                y=np.array(ema20_y),
                pen=pg.mkPen(color=color, width=1),
            )
            self._price_plot.addItem(self._ema_line)

        # EMA60 line
        if ema60_x:
            color = _EMA60_COLOR if not newest_forming else (*_EMA60_COLOR, 140)
            self._ema60_line = pg.PlotDataItem(
                x=np.array(ema60_x),
                y=np.array(ema60_y),
                pen=pg.mkPen(color=color, width=1),
            )
            self._price_plot.addItem(self._ema60_line)

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
        visible_ema10 = frame.indicators.ema10[:visible_count]
        visible_ema20 = frame.indicators.ema20[:visible_count]
        visible_ema60 = frame.indicators.ema60[:visible_count]

        y_min = min(b.low for b in visible_bars)
        y_max = max(b.high for b in visible_bars)

        for ema_val in visible_ema10:
            if not math.isnan(ema_val):
                y_min = min(y_min, ema_val)
                y_max = max(y_max, ema_val)

        for ema_val in visible_ema20:
            if not math.isnan(ema_val):
                y_min = min(y_min, ema_val)
                y_max = max(y_max, ema_val)

        for ema_val in visible_ema60:
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

        for level in self._structure_levels:
            y_min = min(y_min, level.low)
            y_max = max(y_max, level.high)

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
            self._price_plot.removeItem(item)
        self._direction_items.clear()

    def _update_structure_label_positions(self, *args) -> None:
        if not self._structure_labels:
            return
        try:
            x_min = self._price_plot.getViewBox().viewRange()[0][0]
        except Exception:
            return
        for label, price in self._structure_labels:
            label.setPos(x_min, price)

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
        self._price_plot.addItem(marker)
        self._direction_items.append(marker)

    def _clear_candles_and_labels(self) -> None:
        """Remove all candle and label items from the plot."""
        for item in self._candle_items:
            self._price_plot.removeItem(item)
        self._candle_items.clear()

        for item in self._seq_labels:
            self._price_plot.removeItem(item)
        self._seq_labels.clear()

    def _clear_ema_lines(self) -> None:
        """Remove all EMA line items from the plot."""
        if self._ema_line is not None:
            self._price_plot.removeItem(self._ema_line)
            self._ema_line = None
        if self._ema10_line is not None:
            self._price_plot.removeItem(self._ema10_line)
            self._ema10_line = None
        if self._ema60_line is not None:
            self._price_plot.removeItem(self._ema60_line)
            self._ema60_line = None

    def _clear_vol_items(self) -> None:
        """Remove all volume bar items from the plot."""
        for item in self._vol_items:
            self._volume_plot.removeItem(item)
        self._vol_items.clear()
