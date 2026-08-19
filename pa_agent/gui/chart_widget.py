"""ChartWidget — pyqtgraph-based K-line chart with trend indicators and overlays.

Tasks 14.2 + 14.5:
  - Renders N candles, EMA10/20/60, BOLL20, and sequence-number labels.
  - Draws entry/TP/SL horizontal lines when order_type != "不下单".
  - 30 Hz QTimer throttles redraws so the 1 Hz data thread never blocks the UI.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal

from pa_agent.gui.widgets.candle_item import CandleItem
from pa_agent.gui.widgets.overlay_lines import OverlayLines
from pa_agent.gui.widgets.seq_label_item import SeqLabelItem
from pa_agent.indicators.bollinger import (
    DEFAULT_BOLL_PERIOD,
    DEFAULT_BOLL_STDDEV,
    bollinger_full,
)
from pa_agent.indicators.ema import ema_full
from pa_agent.util.trade_metrics import is_long_direction

if TYPE_CHECKING:
    from pa_agent.data.base import KlineBar, KlineFrame

# ── Constants ─────────────────────────────────────────────────────────────────

_TIMER_INTERVAL_MS = 33  # ~30 Hz
_EMA_COLORS = {
    10: (125, 211, 252),  # sky blue
    20: (251, 191, 36),   # amber
    60: (251, 146, 60),   # orange
}
_BOLL_PERIOD = DEFAULT_BOLL_PERIOD
_BOLL_STDDEV = DEFAULT_BOLL_STDDEV
_BOLL_MIDDLE_COLOR = (196, 181, 253)
_BOLL_BAND_COLOR = (129, 140, 248)
_NO_ORDER_TEXT = "不下单"
_X_MARGIN_BARS = 0.65
_Y_PADDING_RATIO = 0.07
_Y_TOP_EXTRA_RATIO = 0.04
_FIT_VISIBLE_BARS = 20
_AXIS_RESIZE_MIN_WIDTH = 40
_AXIS_RESIZE_EDGE_PX = 8


_bollinger_bands = bollinger_full


def _ema_values_oldest_first(
    frame: "KlineFrame",
    closes_oldest_first: list[float],
    period: int,
) -> list[float]:
    """Return an EMA series aligned to the chart's left-to-right x positions."""
    if period == 20 and len(frame.indicators.ema20) == len(frame.bars):
        return list(reversed(frame.indicators.ema20))
    return ema_full(closes_oldest_first, period)


def _format_bar_time_local(ts_ms: float, *, short: bool = True) -> str:
    """Format a bar-open timestamp in the host's *local* timezone for display.

    All data sources store ``ts_open`` as a real UTC epoch (MT5 returns UTC
    seconds; A-share sources ``tz_localize("Asia/Shanghai")`` before converting).
    The previous display path formatted that epoch as-is (UTC), which made
    A-share bars show their session times 8 h early (09:30 → 01:30). Rendering
    in the local timezone restores the actual 09:30-15:00 session on screen.

    Only the chart display (axis ticks + hover) is affected; ``ts_open`` itself,
    bar-close detection, incremental analysis, and AI prompt labels are unchanged.
    """
    sec = float(ts_ms)
    if sec > 1e12:  # ms → s
        sec /= 1000.0
    fmt = "%Y-%m-%d %H:%M" if short else "%Y-%m-%d %H:%M:%S"
    return datetime.fromtimestamp(sec).strftime(fmt)


class DateTimeAxisItem(pg.AxisItem):
    """Bottom axis that maps candle x positions to bar opening timestamps."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._timestamps_ms: tuple[float, ...] = ()
        self._timeframe: str = ""

    def set_bar_times(
        self,
        timestamps_ms: tuple[float, ...],
        timeframe: str,
    ) -> None:
        """Set timestamps ordered from the oldest candle to the newest."""
        self._timestamps_ms = tuple(float(value) for value in timestamps_ms)
        self._timeframe = str(timeframe or "")
        self.picture = None
        self.update()

    def clear_bar_times(self) -> None:
        self.set_bar_times((), "")

    def tickStrings(  # noqa: N802
        self,
        values: list[float],
        scale: float,
        spacing: float,
    ) -> list[str]:
        del scale, spacing
        labels: list[str] = []
        daily_or_higher = self._timeframe in {"1d", "1w", "1M"}
        for value in values:
            index = int(round(value))
            if (
                abs(value - index) > 0.25
                or index < 0
                or index >= len(self._timestamps_ms)
            ):
                labels.append("")
                continue
            text = _format_bar_time_local(self._timestamps_ms[index])
            labels.append(text[:10] if daily_or_higher else text[5:16])
        return labels


class ChartWidget(pg.PlotWidget):
    """Interactive K-line chart widget.

    Parameters
    ----------
    parent:
        Optional Qt parent widget.
    """

    bar_hovered = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        self._time_axis = DateTimeAxisItem(orientation="bottom")
        super().__init__(
            parent=parent,
            axisItems={"bottom": self._time_axis},
        )

        # Configure plot appearance
        self.setBackground("#0d1117")
        self.showGrid(x=False, y=True, alpha=0.3)
        self.getPlotItem().setLabel("left", "Price")
        self.getPlotItem().setLabel("bottom", "日期 / 时间")

        # Internal state
        self._latest_frame: KlineFrame | None = None
        self._dirty: bool = False
        self._candle_items: list[CandleItem] = []
        self._seq_labels: list[SeqLabelItem] = []
        self._ema_line: pg.PlotDataItem | None = None
        self._ema_lines: dict[int, pg.PlotDataItem] = {}
        self._bollinger_lines: dict[str, pg.PlotDataItem] = {}
        self._bollinger_fill: pg.FillBetweenItem | None = None
        self._boll_period = _BOLL_PERIOD
        self._boll_stddev = _BOLL_STDDEV
        self._overlay = OverlayLines()
        self._sr_items: list[pg.GraphicsItem] = []  # support/resistance level lines
        self._pending_decision: dict | None = None
        self._direction_items: list[pg.GraphicsItem] = []
        self._seq_label_font_pt: int = 11
        self._fit_on_next_render: bool = False
        self._first_frame_fitted: bool = False
        self._bars_by_x: tuple[KlineBar, ...] = ()
        self._hover_index: int | None = None
        self._hover_timestamp: float | None = None
        self._hover_locked: bool = False

        # Hover/selection overlay.  These items stay outside the candle rebuild
        # cycle so a live refresh does not destroy a locked selection.
        hover_pen = pg.mkPen(color=(56, 189, 248, 180), width=1)
        self._hover_vline = pg.PlotDataItem(pen=hover_pen)
        self._hover_hline = pg.PlotDataItem(pen=hover_pen)
        self._hover_detail = pg.TextItem(
            anchor=(0.0, 0.0),
            border=pg.mkPen(color=(56, 189, 248, 220), width=1),
            fill=pg.mkBrush(13, 17, 23, 235),
        )
        for item in (self._hover_vline, self._hover_hline, self._hover_detail):
            item.setZValue(1000)
            item.hide()
            self.addItem(item, ignoreBounds=True)

        # Price-axis resize state
        self._axis_resizing: bool = False
        self._axis_drag_origin_x: float = 0.0
        self._axis_drag_origin_w: float = 0.0

        vb = self.getViewBox()
        vb.enableAutoRange(x=False, y=False)
        self.scene().sigMouseMoved.connect(self._on_scene_mouse_moved)
        self.scene().sigMouseClicked.connect(self._on_scene_mouse_clicked)

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

    def set_bollinger_params(self, period: int, stddev: float) -> None:
        """Update BOLL parameters and schedule an immediate chart redraw."""
        period = max(2, min(500, int(period)))
        stddev = max(0.1, min(10.0, float(stddev)))
        if period == self._boll_period and math.isclose(stddev, self._boll_stddev):
            return
        self._boll_period = period
        self._boll_stddev = stddev
        if self._latest_frame is not None:
            self._dirty = True

    def bollinger_params(self) -> tuple[int, float]:
        """Return the BOLL period and standard-deviation multiplier in use."""
        return self._boll_period, self._boll_stddev

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
        order_type = decision.get("order_type", _NO_ORDER_TEXT)
        overlay_active = bool(decision.get("chart_overlay_active"))

        if order_type == _NO_ORDER_TEXT and not overlay_active:
            self._pending_decision = None
            self._overlay.clear_lines(self)
            self._clear_direction_marker()
            return

        self._pending_decision = decision
        entry = decision.get("entry_price")
        tp = decision.get("take_profit_price")
        tp2 = decision.get("take_profit_price_2")
        sl = decision.get("stop_loss_price")

        if entry is not None and tp is not None and sl is not None:
            try:
                tp2_val = float(tp2) if tp2 is not None else None
                self._overlay.set_lines(
                    self,
                    float(entry),
                    float(tp),
                    float(sl),
                    tp2=tp2_val,
                    continuity=overlay_active,
                )
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
        """Clear all chart items (candles, indicators, labels, and overlays)."""
        self.clear_decision_overlay()
        self._clear_candles_and_labels()
        self._clear_indicator_overlays()
        self._latest_frame = None
        self._time_axis.clear_bar_times()
        self._bars_by_x = ()
        self._hover_locked = False
        self._hide_bar_details()
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
        """Rebuild candles, trend indicators, and sequence labels."""
        hovered_ts = self._hover_timestamp
        self._clear_candles_and_labels()
        self._clear_indicator_overlays()
        bars = frame.bars
        n = len(bars)
        if n == 0:
            self._time_axis.clear_bar_times()
            self._bars_by_x = ()
            self._hover_locked = False
            self._hide_bar_details()
            return
        self._bars_by_x = tuple(reversed(bars))
        self._time_axis.set_bar_times(
            tuple(float(bar.ts_open) for bar in reversed(bars)),
            frame.timeframe,
        )

        # bars[0] is newest (seq=1); we want x=0 for oldest, x=n-1 for newest.
        for i, bar in enumerate(bars):
            x_pos = n - 1 - i  # oldest bar at x=0, newest at x=n-1

            forming = not bar.closed

            # Candle (forming bar: semi-transparent dashed outline)
            candle = CandleItem(bar, x_pos, forming=forming)
            self.addItem(candle)
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
                self.addItem(seq_label)
                self._seq_labels.append(seq_label)

        self._render_indicator_overlays(frame)

        self._update_direction_marker()

        if self._fit_on_next_render:
            self._fit_on_next_render = False
            self.fit_view()

        if hovered_ts is not None:
            restored_index = next(
                (
                    index
                    for index, bar in enumerate(self._bars_by_x)
                    if float(bar.ts_open) == hovered_ts
                ),
                None,
            )
            if restored_index is None:
                self._hover_locked = False
                self._hide_bar_details()
            else:
                self._show_bar_details(restored_index, self._bars_by_x[restored_index].close)

    @staticmethod
    def _curve_for_values(
        values: list[float],
        *,
        color: tuple[int, ...],
        width: float = 1.0,
        style: Qt.PenStyle = Qt.PenStyle.SolidLine,
    ) -> pg.PlotDataItem | None:
        y_values = np.asarray(values, dtype=float)
        valid = np.isfinite(y_values)
        if not valid.any():
            return None
        x_values = np.arange(len(values), dtype=float)
        curve = pg.PlotDataItem(
            x=x_values[valid],
            y=y_values[valid],
            pen=pg.mkPen(color=color, width=width, style=style),
        )
        curve.setZValue(10)
        return curve

    def _render_indicator_overlays(self, frame: "KlineFrame") -> None:
        """Draw EMA10/20/60 plus the BOLL20 channel over the candles."""
        bars = frame.bars
        closes = [bar.close for bar in reversed(bars)]
        newest_forming = bool(bars and not bars[0].closed)
        alpha = 150 if newest_forming else 255

        for period, base_color in _EMA_COLORS.items():
            values = _ema_values_oldest_first(frame, closes, period)
            color = (*base_color, alpha)
            curve = self._curve_for_values(values, color=color, width=1.2)
            if curve is None:
                continue
            self.addItem(curve)
            self._ema_lines[period] = curve

        # Retain the original attribute for callers that inspect the EMA20 item.
        self._ema_line = self._ema_lines.get(20)

        middle, upper, lower = _bollinger_bands(
            closes,
            period=self._boll_period,
            stddev=self._boll_stddev,
        )
        boll_specs = {
            "middle": (middle, _BOLL_MIDDLE_COLOR, Qt.PenStyle.DotLine),
            "upper": (upper, _BOLL_BAND_COLOR, Qt.PenStyle.DashLine),
            "lower": (lower, _BOLL_BAND_COLOR, Qt.PenStyle.DashLine),
        }
        for name, (values, base_color, style) in boll_specs.items():
            curve = self._curve_for_values(
                values,
                color=(*base_color, alpha),
                width=1.0,
                style=style,
            )
            if curve is not None:
                self._bollinger_lines[name] = curve

        upper_curve = self._bollinger_lines.get("upper")
        lower_curve = self._bollinger_lines.get("lower")
        if upper_curve is not None and lower_curve is not None:
            fill_alpha = 24 if not newest_forming else 16
            self._bollinger_fill = pg.FillBetweenItem(
                upper_curve,
                lower_curve,
                brush=pg.mkBrush(*_BOLL_BAND_COLOR, fill_alpha),
            )
            self._bollinger_fill.setZValue(-20)
            self.addItem(self._bollinger_fill)

        for curve in self._bollinger_lines.values():
            self.addItem(curve)

    def _clear_indicator_overlays(self) -> None:
        """Remove every moving-average and Bollinger graphics item."""
        if self._bollinger_fill is not None:
            self.removeItem(self._bollinger_fill)
            self._bollinger_fill = None
        for curve in self._bollinger_lines.values():
            self.removeItem(curve)
        self._bollinger_lines.clear()
        for curve in self._ema_lines.values():
            self.removeItem(curve)
        self._ema_lines.clear()
        self._ema_line = None

    # ── K-line hover / selection details ─────────────────────────────────────

    def _bar_index_at_x(self, x_value: float) -> int | None:
        """Return the nearest candle index when *x_value* is over a candle."""
        index = int(round(x_value))
        if index < 0 or index >= len(self._bars_by_x):
            return None
        if abs(x_value - index) > 0.5:
            return None
        return index

    @staticmethod
    def _format_price(value: float) -> str:
        return f"{float(value):,.6f}".rstrip("0").rstrip(".")

    def _bar_detail_parts(self, bar: "KlineBar") -> tuple[str, str]:
        """Build rich chart details and a compact footer summary for *bar*."""
        timeframe = self._latest_frame.timeframe if self._latest_frame is not None else ""
        time_text = _format_bar_time_local(bar.ts_open)
        if timeframe in {"1d", "1w", "1M"}:
            time_text = time_text[:10]

        pct_chg = bar.pct_chg
        if pct_chg is None and bar.open:
            pct_chg = (bar.close / bar.open - 1.0) * 100.0
        pct_text = "—" if pct_chg is None else f"{pct_chg:+.2f}%"
        change_color = "#22c55e" if (pct_chg or 0.0) >= 0 else "#ef4444"
        status = "已收盘" if bar.closed else "形成中"
        amount_row = ""
        if bar.amount:
            amount_row = (
                "<br><span style='color:#8b949e'>成交额</span> "
                f"<span style='color:#e6edf3'>{bar.amount:,.2f}</span>"
            )

        html = (
            "<div style='font-family:monospace; font-size:12px; color:#e6edf3'>"
            f"<b>{time_text}</b> "
            f"<span style='color:#8b949e'>· {status}</span><br>"
            "<span style='color:#8b949e'>开</span> "
            f"{self._format_price(bar.open)}　"
            "<span style='color:#8b949e'>高</span> "
            f"{self._format_price(bar.high)}<br>"
            "<span style='color:#8b949e'>低</span> "
            f"{self._format_price(bar.low)}　"
            "<span style='color:#8b949e'>收</span> "
            f"{self._format_price(bar.close)}<br>"
            "<span style='color:#8b949e'>涨跌</span> "
            f"<span style='color:{change_color}'>{pct_text}</span>　"
            "<span style='color:#8b949e'>成交量</span> "
            f"{bar.volume:,.2f}"
            f"{amount_row}</div>"
        )
        summary = (
            f"{time_text}  开 {self._format_price(bar.open)}  "
            f"高 {self._format_price(bar.high)}  低 {self._format_price(bar.low)}  "
            f"收 {self._format_price(bar.close)}  涨跌 {pct_text}  "
            f"量 {bar.volume:,.2f}"
        )
        return html, summary

    def _show_bar_details(self, index: int, cursor_y: float) -> None:
        if index < 0 or index >= len(self._bars_by_x):
            self._hide_bar_details()
            return

        bar = self._bars_by_x[index]
        self._hover_index = index
        self._hover_timestamp = float(bar.ts_open)

        selected_color = (250, 204, 21, 230) if self._hover_locked else (56, 189, 248, 210)
        pen = pg.mkPen(color=selected_color, width=1)
        self._hover_vline.setPen(pen)
        self._hover_hline.setPen(pen)
        self._hover_detail.border = pg.mkPen(color=selected_color, width=1)
        self._hover_detail.update()

        html, summary = self._bar_detail_parts(bar)
        self._hover_detail.setHtml(html)
        x_range, y_range = self.getViewBox().viewRange()
        self._hover_vline.setData(
            x=(float(index), float(index)),
            y=(y_range[0], y_range[1]),
        )
        self._hover_hline.setData(
            x=(x_range[0], x_range[1]),
            y=(float(cursor_y), float(cursor_y)),
        )
        right_side = float(index) > (x_range[0] + x_range[1]) / 2.0
        self._hover_detail.setAnchor((1.0, 0.0) if right_side else (0.0, 0.0))
        x_offset = -0.25 if right_side else 0.25
        y_top = y_range[1] - (y_range[1] - y_range[0]) * 0.02
        self._hover_detail.setPos(float(index) + x_offset, y_top)

        self._hover_vline.show()
        self._hover_hline.show()
        self._hover_detail.show()
        self.bar_hovered.emit(("已选中 · " if self._hover_locked else "") + summary)

    def _hide_bar_details(self) -> None:
        self._hover_vline.hide()
        self._hover_hline.hide()
        self._hover_detail.hide()
        self._hover_index = None
        self._hover_timestamp = None
        self.bar_hovered.emit("")

    def _on_scene_mouse_moved(self, scene_pos) -> None:
        if self._hover_locked:
            return
        view_box = self.getViewBox()
        if not self._bars_by_x or not view_box.sceneBoundingRect().contains(scene_pos):
            self._hide_bar_details()
            return
        view_pos = view_box.mapSceneToView(scene_pos)
        index = self._bar_index_at_x(float(view_pos.x()))
        if index is None:
            self._hide_bar_details()
            return
        self._show_bar_details(index, float(view_pos.y()))

    def _on_scene_mouse_clicked(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        view_box = self.getViewBox()
        scene_pos = event.scenePos()
        if not self._bars_by_x or not view_box.sceneBoundingRect().contains(scene_pos):
            self._hover_locked = False
            self._hide_bar_details()
            return
        view_pos = view_box.mapSceneToView(scene_pos)
        index = self._bar_index_at_x(float(view_pos.x()))
        if index is None:
            return
        if self._hover_locked and self._hover_index == index:
            self._hover_locked = False
        else:
            self._hover_locked = True
        self._show_bar_details(index, float(view_pos.y()))

    def _view_ranges_for_frame(
        self,
        frame: "KlineFrame",
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Compute (x_range, y_range) for the newest ``_FIT_VISIBLE_BARS`` bars."""
        bars = frame.bars
        n = len(bars)
        visible_count = min(_FIT_VISIBLE_BARS, n)
        visible_bars = bars[:visible_count]
        closes_oldest_first = [bar.close for bar in reversed(bars)]
        boll_middle, boll_upper, boll_lower = _bollinger_bands(
            closes_oldest_first,
            period=self._boll_period,
            stddev=self._boll_stddev,
        )
        visible_indicator_values = [
            *boll_middle[-visible_count:],
            *boll_upper[-visible_count:],
            *boll_lower[-visible_count:],
        ]
        for period in _EMA_COLORS:
            ema_values = _ema_values_oldest_first(
                frame,
                closes_oldest_first,
                period,
            )
            visible_indicator_values.extend(ema_values[-visible_count:])

        y_min = min(b.low for b in visible_bars)
        y_max = max(b.high for b in visible_bars)

        for indicator_value in visible_indicator_values:
            if not math.isnan(indicator_value):
                y_min = min(y_min, indicator_value)
                y_max = max(y_max, indicator_value)

        decision = self._pending_decision
        if decision is not None:
            for key in (
                "entry_price",
                "take_profit_price",
                "take_profit_price_2",
                "stop_loss_price",
            ):
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
        if (
            decision.get("order_type", _NO_ORDER_TEXT) == _NO_ORDER_TEXT
            and not decision.get("chart_overlay_active")
        ):
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
