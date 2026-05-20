"""Decision flow visualization — branched sci-fi flowchart (gate + strategy path)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import (
    QDialog,
    QGraphicsObject,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import pyqtSignal

from pa_agent.ai.decision_tree import (
    _BRANCH_DISPLAY_ZH,
    format_trace_answer,
    get_node_branch_outcome,
    merge_traces,
    plain_trace_question,
)
from pa_agent.gui.theme import tokens as T

_OUTCOME_ZH = {
    "wait": "等待",
    "reject": "放弃",
    "trade": "交易",
    "proceed": "继续评估",
}
_OUTCOME_COLOR = {
    "wait": T.ACCENT_WARNING,
    "reject": T.ACCENT_DANGER,
    "trade": T.ACCENT_SUCCESS,
    "proceed": T.ACCENT_PRIMARY,
}
_PHASE_ZH = {"gate": "闸门", "decision": "策略"}
_ANSWER_COLOR = {
    "是": T.ACCENT_SUCCESS,
    "否": T.ACCENT_DANGER,
    "中性": T.ACCENT_WARNING,
    "等待": T.ACCENT_WARNING,
    "不适用": T.TEXT_MUTED,
}

# Larger card nodes for readability in the sidebar.
_NODE_W = 580
_NODE_H = 196
_STUB_W = 390
_STUB_H = 132
_TERMINAL_W = 560
_TERMINAL_H = 132
_LEVEL_DY = 270
_BRANCH_DX = 360
_PLAY_TICK_MS = 40
_FX_TICK_MS = 60
_ANIM_PHASE = 0.0

_NEON_CYAN = "#37f8ff"
_NEON_BLUE = "#58a6ff"
_NEON_VIOLET = "#a371f7"
_NEON_AMBER = "#ffcf33"
_GLASS_BG = "#09111d"


def _font_ui(pt: int, *, bold: bool = False, mono: bool = False) -> QFont:
    family = "Consolas" if mono else "Microsoft YaHei UI"
    font = QFont(family, pt)
    if bold:
        font.setBold(True)
    return font


def _answer_color(answer: str) -> str:
    base = str(answer).split("（", 1)[0]
    return _ANSWER_COLOR.get(base, T.ACCENT_PRIMARY)


def _draw_corner_brackets(painter: QPainter, rect: QRectF, color: QColor) -> None:
    """Draw HUD-style card corner brackets."""
    painter.setPen(QPen(color, 2))
    l = 18
    x0, x1 = rect.left(), rect.right()
    y0, y1 = rect.top(), rect.bottom()
    painter.drawLine(QPointF(x0 + 8, y0), QPointF(x0 + 8 + l, y0))
    painter.drawLine(QPointF(x0, y0 + 8), QPointF(x0, y0 + 8 + l))
    painter.drawLine(QPointF(x1 - 8 - l, y0), QPointF(x1 - 8, y0))
    painter.drawLine(QPointF(x1, y0 + 8), QPointF(x1, y0 + 8 + l))
    painter.drawLine(QPointF(x0 + 8, y1), QPointF(x0 + 8 + l, y1))
    painter.drawLine(QPointF(x0, y1 - 8 - l), QPointF(x0, y1 - 8))
    painter.drawLine(QPointF(x1 - 8 - l, y1), QPointF(x1 - 8, y1))
    painter.drawLine(QPointF(x1, y1 - 8 - l), QPointF(x1, y1 - 8))


def _taken_branch_side(item: dict[str, Any]) -> Literal["left", "right", "down"]:
    """Which branch the AI took: left=否, right=是, down=跳过/直线."""
    if item.get("skipped"):
        return "down"
    ans = format_trace_answer(item) or str(item.get("answer", ""))
    base = ans.split("（", 1)[0]
    if base == "否":
        return "left"
    if base in ("是", "等待", "中性"):
        return "right"
    return "down"


@dataclass
class _Placed:
    x: float
    y: float
    kind: str
    item: dict[str, Any] | None = None
    step: int = 0
    active: bool = True
    alt_branch: str | None = None  # yes | no — outcome shown on the untaken side
    alt_node_id: str | None = None


class _FlowScene(QGraphicsScene):
    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        global _ANIM_PHASE
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0, QColor("#020711"))
        grad.setColorAt(0.48, QColor("#07111f"))
        grad.setColorAt(1, QColor("#030409"))
        painter.fillRect(rect, QBrush(grad))

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = rect.center()
        halo = QLinearGradient(center.x() - rect.width() * 0.45, center.y(), center.x() + rect.width() * 0.45, center.y())
        c0 = QColor(_NEON_BLUE)
        c0.setAlpha(0)
        c1 = QColor(_NEON_CYAN)
        c1.setAlpha(24)
        halo.setColorAt(0, c0)
        halo.setColorAt(0.5, c1)
        halo.setColorAt(1, c0)
        painter.fillRect(rect, QBrush(halo))

        step = 32
        left = int(rect.left()) - (int(rect.left()) % step)
        top = int(rect.top()) - (int(rect.top()) % step)
        painter.setPen(QPen(QColor(34, 231, 255, 36)))
        x = left
        while x < rect.right():
            painter.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
            x += step
        y = top
        while y < rect.bottom():
            painter.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
            y += step

        major = step * 4
        left = int(rect.left()) - (int(rect.left()) % major)
        top = int(rect.top()) - (int(rect.top()) % major)
        painter.setPen(QPen(QColor(88, 166, 255, 58)))
        x = left
        while x < rect.right():
            painter.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
            x += major
        y = top
        while y < rect.bottom():
            painter.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
            y += major

        scan_y = rect.top() + ((rect.height() + 220) * ((_ANIM_PHASE * 0.18) % 1.0)) - 110
        scan = QLinearGradient(rect.left(), scan_y - 34, rect.left(), scan_y + 34)
        transparent = QColor(_NEON_CYAN)
        transparent.setAlpha(0)
        bright = QColor(_NEON_CYAN)
        bright.setAlpha(34)
        scan.setColorAt(0, transparent)
        scan.setColorAt(0.5, bright)
        scan.setColorAt(1, transparent)
        painter.fillRect(QRectF(rect.left(), scan_y - 34, rect.width(), 68), QBrush(scan))


class _BranchEdge(QGraphicsObject):
    """Bezier branch between parent and child (scene coordinates)."""

    def __init__(
        self,
        p0: QPointF,
        p1: QPointF,
        label: str,
        *,
        active: bool,
    ) -> None:
        super().__init__()
        self._p0 = p0
        self._p1 = p1
        self._label = label
        self._active = active
        self.setZValue(1)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(self._p0, self._p1).normalized().adjusted(-40, -24, 40, 24)

    def paint(self, painter: QPainter, _option: Any, _widget: Any = None) -> None:
        global _ANIM_PHASE
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = QColor(_NEON_CYAN if self._active else T.TEXT_MUTED)
        if not self._active:
            col.setAlpha(90)
        path = self._curve()

        glow = QPen(QColor(col.red(), col.green(), col.blue(), 72 if self._active else 25))
        glow.setWidth(11 if self._active else 4)
        painter.setPen(glow)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        pen = QPen(col)
        pen.setWidth(3 if self._active else 2)
        if not self._active:
            pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(pen)
        painter.drawPath(path)

        if self._active:
            painter.setPen(Qt.PenStyle.NoPen)
            for i in range(3):
                pct = (_ANIM_PHASE * 0.42 + i * 0.33) % 1.0
                pt = path.pointAtPercent(pct)
                dot = QColor(_NEON_CYAN)
                dot.setAlpha(210 - i * 45)
                painter.setBrush(QBrush(dot))
                painter.drawEllipse(pt, 5.5 - i, 5.5 - i)

        # Arrow
        painter.setBrush(QBrush(col))
        dx = self._p1.x() - self._p0.x()
        dy = self._p1.y() - self._p0.y()
        angle = math.atan2(dy, dx)
        ah = 9
        tip = self._p1
        p2 = QPointF(
            tip.x() - ah * math.cos(angle - 0.45),
            tip.y() - ah * math.sin(angle - 0.45),
        )
        p3 = QPointF(
            tip.x() - ah * math.cos(angle + 0.45),
            tip.y() - ah * math.sin(angle + 0.45),
        )
        tri = QPainterPath(tip)
        tri.lineTo(p2)
        tri.lineTo(p3)
        tri.closeSubpath()
        painter.drawPath(tri)
        if self._label:
            mid = QPointF(
                (self._p0.x() + self._p1.x()) / 2,
                (self._p0.y() + self._p1.y()) / 2 - 6,
            )
            badge_w, badge_h = 56, 26
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(8, 12, 20, 240)))
            painter.drawRoundedRect(
                QRectF(
                    mid.x() - badge_w / 2,
                    mid.y() - badge_h / 2,
                    badge_w,
                    badge_h,
                ),
                8,
                8,
            )
            painter.setPen(QPen(col))
            font = QFont(T.FONT_UI.split(",")[0].strip('"'), 11)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                QRectF(mid.x() - badge_w / 2, mid.y() - badge_h / 2, badge_w, badge_h),
                int(Qt.AlignmentFlag.AlignCenter),
                self._label,
            )

    def _curve(self) -> QPainterPath:
        path = QPainterPath(self._p0)
        dy = max(60.0, abs(self._p1.y() - self._p0.y()) * 0.52)
        c1 = QPointF(self._p0.x(), self._p0.y() + dy)
        c2 = QPointF(self._p1.x(), self._p1.y() - dy)
        path.cubicTo(c1, c2, self._p1)
        return path


class _PhaseBandItem(QGraphicsObject):
    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        self.setZValue(2)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-280, -16, 560, 32)

    def paint(self, painter: QPainter, _option: Any, _widget: Any = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(_NEON_VIOLET))
        painter.setPen(pen)
        painter.drawLine(QPointF(-260, 0), QPointF(-70, 0))
        painter.drawLine(QPointF(70, 0), QPointF(260, 0))
        painter.setFont(_font_ui(11, bold=True))
        painter.setBrush(QBrush(QColor(163, 113, 247, 28)))
        painter.drawRoundedRect(QRectF(-86, -15, 172, 30), 10, 10)
        painter.drawText(QRectF(-70, -12, 140, 24), int(Qt.AlignmentFlag.AlignCenter), self._title)


class _DecisionNode(QGraphicsObject):
    """Large card — active decision on the walk path."""

    def __init__(self, item: dict[str, Any], step: int) -> None:
        super().__init__()
        self._item = item
        self._step = step
        self.setZValue(5)
        self.setAcceptHoverEvents(True)
        self._hover = False
        phase = str(item.get("phase", ""))
        self._phase_zh = _PHASE_ZH.get(phase, phase)
        self._nid = str(item.get("node_id", "?"))
        self._question = plain_trace_question(item)
        self._answer = format_trace_answer(item) or str(item.get("answer", "—"))
        self._branch = item.get("branch")
        self._skipped = bool(item.get("skipped"))
        self._section = str(item.get("section", "") or "")
        self._bar_range = str(item.get("bar_range", "") or "")
        tip = [self._question]
        if item.get("bar_range"):
            tip.append(f"K线：{item.get('bar_range')}")
        if item.get("reason"):
            tip.append(str(item.get("reason")))
        self.setToolTip("\n".join(tip))

    def boundingRect(self) -> QRectF:  # noqa: N802
        pad = 18
        return QRectF(-_NODE_W / 2 - pad, -pad, _NODE_W + pad * 2, _NODE_H + pad * 2)

    def hoverEnterEvent(self, _event: Any) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, _event: Any) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def paint(self, painter: QPainter, _option: Any, _widget: Any = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = _NODE_W, _NODE_H
        accent = QColor(_answer_color(self._answer))
        if self._skipped:
            accent = QColor(T.TEXT_MUTED)
        rect = QRectF(-w / 2, 0, w, h)

        if self._hover:
            glow = QPen(QColor(accent.red(), accent.green(), accent.blue(), 120))
            glow.setWidth(14)
            painter.setPen(glow)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-6, -6, 6, 6), 22, 22)

        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, QColor("#172943"))
        grad.setColorAt(0.5, QColor(_GLASS_BG))
        grad.setColorAt(1, QColor("#030711"))
        painter.setBrush(QBrush(grad))
        painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 175), 2))
        painter.drawRoundedRect(rect, 18, 18)
        _draw_corner_brackets(
            painter,
            rect.adjusted(5, 5, -5, -5),
            QColor(accent.red(), accent.green(), accent.blue(), 180),
        )

        # A status stripe makes the answer visible even when zoomed out.
        painter.setPen(Qt.PenStyle.NoPen)
        stripe = QLinearGradient(-w / 2, 0, -w / 2, h)
        bright = QColor(accent)
        dim = QColor(accent)
        dim.setAlpha(30)
        stripe.setColorAt(0, bright)
        stripe.setColorAt(0.55, dim)
        stripe.setColorAt(1, bright)
        painter.setBrush(QBrush(stripe))
        painter.drawRoundedRect(QRectF(-w / 2, 0, 12, h), 6, 6)

        pad_x = 24
        inner_w = w - pad_x * 2

        painter.setFont(_font_ui(12, bold=True))
        painter.setPen(QPen(QColor(T.TEXT_MUTED)))
        painter.drawText(
            QRectF(-w / 2 + pad_x, 14, inner_w * 0.62, 24),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"#{self._step:02d} · {self._phase_zh}",
        )

        painter.setFont(_font_ui(12, mono=True, bold=True))
        painter.setPen(QPen(QColor(_NEON_CYAN)))
        painter.drawText(
            QRectF(w / 2 - pad_x - 170, 14, 170, 24),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            f"§{self._nid}",
        )

        meta = self._section
        if self._bar_range:
            meta = f"{meta} · {self._bar_range}" if meta else self._bar_range
        if meta:
            painter.setFont(_font_ui(11))
            painter.setPen(QPen(QColor(T.TEXT_SECONDARY)))
            painter.drawText(
                QRectF(-w / 2 + pad_x, 38, inner_w, 22),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                meta,
            )

        question_rect = QRectF(-w / 2 + pad_x, 66, inner_w, 76)
        painter.setFont(_font_ui(16, bold=True))
        painter.setPen(QPen(QColor(T.TEXT_PRIMARY)))
        painter.save()
        painter.setClipRect(question_rect)
        painter.drawText(
            question_rect,
            int(
                Qt.AlignmentFlag.AlignLeft
                | Qt.AlignmentFlag.AlignTop
                | Qt.TextFlag.TextWordWrap
            ),
            self._question,
        )
        painter.restore()

        ans = self._answer
        if self._branch:
            bzh = _BRANCH_DISPLAY_ZH.get(str(self._branch), str(self._branch))
            if bzh and bzh not in ans:
                ans = f"{ans} · {bzh}"
        footer_rect = QRectF(-w / 2 + pad_x, h - 44, inner_w, 30)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), 34)))
        painter.drawRoundedRect(footer_rect, 10, 10)
        painter.setBrush(QBrush(QColor(55, 248, 255, 26)))
        painter.drawRoundedRect(
            QRectF(w / 2 - pad_x - 116, h - 44, 116, 30),
            10,
            10,
        )
        painter.setFont(_font_ui(14, bold=True))
        painter.setPen(QPen(accent))
        painter.save()
        painter.setClipRect(footer_rect.adjusted(12, 0, -12, 0))
        painter.drawText(
            footer_rect.adjusted(12, 0, -12, 0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"结论：{ans}",
        )
        painter.restore()
        painter.setFont(_font_ui(10, mono=True, bold=True))
        painter.setPen(QPen(QColor(_NEON_CYAN)))
        painter.drawText(
            QRectF(w / 2 - pad_x - 108, h - 44, 96, 30),
            int(Qt.AlignmentFlag.AlignCenter),
            "AI NODE",
        )

    def port_bottom(self) -> QPointF:
        return self.scenePos() + QPointF(0, _NODE_H)

    def port_top(self) -> QPointF:
        return self.scenePos()

    def port_left(self) -> QPointF:
        return self.scenePos() + QPointF(-_NODE_W * 0.24, _NODE_H)

    def port_right(self) -> QPointF:
        return self.scenePos() + QPointF(_NODE_W * 0.24, _NODE_H)


class _AltBranchNode(QGraphicsObject):
    """Untaken branch — shows what 是/否 would mean per 二元决策.txt."""

    def __init__(self, branch: str, outcome: str, node_id: str) -> None:
        super().__init__()
        self._branch = branch
        self._outcome = outcome or ("继续" if branch == "yes" else "等待")
        self._node_id = node_id
        self.setZValue(3)
        self._title = "是" if branch == "yes" else "否"
        self._title_color = QColor(
            T.ACCENT_SUCCESS if branch == "yes" else T.ACCENT_DANGER
        )
        self.setToolTip(
            f"§{node_id} · 若选「{self._title}」\n{self._outcome}"
        )

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-_STUB_W / 2 - 6, -6, _STUB_W + 12, _STUB_H + 12)

    def paint(self, painter: QPainter, _option: Any, _widget: Any = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(-_STUB_W / 2, 0, _STUB_W, _STUB_H)
        painter.setBrush(QBrush(QColor(13, 18, 29, 235)))
        border = QColor(self._title_color)
        border.setAlpha(120)
        painter.setPen(QPen(border, 2, Qt.PenStyle.DashLine))
        painter.drawRoundedRect(rect, 16, 16)
        pad = 18
        inner_w = _STUB_W - pad * 2
        painter.setFont(_font_ui(13, bold=True))
        painter.setPen(QPen(self._title_color))
        painter.drawText(
            QRectF(-_STUB_W / 2 + pad, 12, inner_w, 26),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"未走分支：{self._title}",
        )
        painter.setFont(_font_ui(12))
        painter.setPen(QPen(QColor(T.TEXT_SECONDARY)))
        body = QRectF(-_STUB_W / 2 + pad, 42, inner_w, _STUB_H - 54)
        painter.save()
        painter.setClipRect(body)
        painter.drawText(
            body,
            int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap),
            self._outcome,
        )
        painter.restore()

    def port_top(self) -> QPointF:
        return self.scenePos()


class _TerminalNode(QGraphicsObject):
    def __init__(self, terminal: dict[str, Any]) -> None:
        super().__init__()
        self.setZValue(6)
        self._nid = str(terminal.get("node_id", "?"))
        self._outcome = str(terminal.get("outcome", ""))
        self._label = str(terminal.get("label", ""))
        self._outcome_zh = _OUTCOME_ZH.get(self._outcome, self._outcome)
        self._color = QColor(_OUTCOME_COLOR.get(self._outcome, T.ACCENT_PRIMARY))
        self.setToolTip(f"§{self._nid} · {self._outcome_zh}\n{self._label}")

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-_TERMINAL_W / 2 - 8, -8, _TERMINAL_W + 16, _TERMINAL_H + 16)

    def paint(self, painter: QPainter, _option: Any, _widget: Any = None) -> None:
        global _ANIM_PHASE
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = _TERMINAL_W, _TERMINAL_H
        rect = QRectF(-w / 2, 0, w, h)
        pulse = int(70 + 35 * (0.5 + 0.5 * math.sin(_ANIM_PHASE * 2.7)))
        glow = QPen(QColor(self._color.red(), self._color.green(), self._color.blue(), pulse))
        glow.setWidth(16)
        painter.setPen(glow)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(-8, -8, 8, 8), 20, 20)
        ring = QColor(self._color)
        ring.setAlpha(55)
        painter.setPen(QPen(ring, 2))
        painter.drawEllipse(QPointF(0, h / 2), w * 0.45, h * 0.74)
        painter.drawEllipse(QPointF(0, h / 2), w * 0.32, h * 0.52)

        grad = QLinearGradient(-w / 2, 0, w / 2, 0)
        grad.setColorAt(0, QColor("#07111f"))
        grad.setColorAt(0.5, QColor("#1a2844"))
        grad.setColorAt(1, QColor("#07111f"))
        painter.setBrush(QBrush(grad))
        painter.setPen(QPen(self._color, 3))
        painter.drawRoundedRect(rect, 14, 14)
        _draw_corner_brackets(painter, rect.adjusted(5, 5, -5, -5), self._color)

        painter.setFont(_font_ui(11, mono=True, bold=True))
        painter.setPen(QPen(QColor(T.TEXT_MUTED)))
        painter.drawText(
            QRectF(-w / 2, 12, w, 22),
            int(Qt.AlignmentFlag.AlignCenter),
            f"FINAL VERDICT  //  §{self._nid}",
        )
        painter.setFont(_font_ui(20, bold=True))
        painter.setPen(QPen(self._color))
        painter.drawText(
            QRectF(-w / 2, 36, w, 34),
            int(Qt.AlignmentFlag.AlignCenter),
            self._outcome_zh.upper(),
        )
        painter.setFont(_font_ui(12))
        painter.setPen(QPen(QColor(T.TEXT_PRIMARY)))
        body = QRectF(-w / 2 + 22, 72, w - 44, 48)
        painter.save()
        painter.setClipRect(body)
        painter.drawText(
            body,
            int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
            self._label,
        )
        painter.restore()

    def port_top(self) -> QPointF:
        return self.scenePos()


class _EmptyHint(QGraphicsObject):
    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-240, -40, 480, 80)

    def paint(self, painter: QPainter, _option: Any, _widget: Any = None) -> None:
        painter.setPen(QPen(QColor(T.TEXT_MUTED)))
        painter.setFont(_font_ui(13))
        painter.drawText(self.boundingRect(), int(Qt.AlignmentFlag.AlignCenter), self._text)

def _layout_branched_path(
    merged: list[dict[str, Any]],
    terminal: dict[str, Any] | None,
) -> tuple[list[_Placed], list[tuple[QPointF, QPointF, str, bool]], list[_Placed]]:
    """Return nodes, edges (p0,p1,label,active), phase bands."""
    nodes: list[_Placed] = []
    edges: list[tuple[QPointF, QPointF, str, bool]] = []
    bands: list[_Placed] = []

    if not merged:
        return nodes, edges, bands

    x, y = 0.0, 40.0
    last_phase: str | None = None

    for i, item in enumerate(merged):
        phase = str(item.get("phase", ""))
        if last_phase == "gate" and phase == "decision":
            bands.append(_Placed(x=x, y=y - 28, kind="band"))
            y += 20
        last_phase = phase

        nodes.append(_Placed(x=x, y=y, kind="decision", item=item, step=i + 1, active=True))
        ny = y + _LEVEL_DY
        side = _taken_branch_side(item)
        port_l = QPointF(x - _NODE_W * 0.24, y + _NODE_H)
        port_r = QPointF(x + _NODE_W * 0.24, y + _NODE_H)
        port_m = QPointF(x, y + _NODE_H)

        if side == "down":
            child_x = x
            edges.append(
                (
                    port_m,
                    QPointF(child_x, ny),
                    "跳过" if item.get("skipped") else "→",
                    True,
                )
            )
            x, y = child_x, ny
            continue

        lx = x - _BRANCH_DX
        rx = x + _BRANCH_DX
        # Keep the untaken side far enough from the next active card.
        stub_lx = x - _BRANCH_DX * 1.28
        stub_rx = x + _BRANCH_DX * 1.28
        nid = str(item.get("node_id", ""))
        if side == "left":
            edges.append((port_l, QPointF(lx, ny), "否", True))
            edges.append((port_r, QPointF(stub_rx, ny), "是", False))
            nodes.append(
                _Placed(
                    x=stub_rx,
                    y=ny,
                    kind="alt",
                    alt_branch="yes",
                    alt_node_id=nid,
                    active=False,
                )
            )
            x, y = lx, ny
        else:
            edges.append((port_l, QPointF(stub_lx, ny), "否", False))
            edges.append((port_r, QPointF(rx, ny), "是", True))
            nodes.append(
                _Placed(
                    x=stub_lx,
                    y=ny,
                    kind="alt",
                    alt_branch="no",
                    alt_node_id=nid,
                    active=False,
                )
            )
            x, y = rx, ny

    if terminal:
        ty = y + _LEVEL_DY - 20
        edges.append(
            (
                QPointF(x, y + _NODE_H),
                QPointF(x, ty),
                "→",
                True,
            )
        )
        nodes.append(_Placed(x=x, y=ty, kind="terminal", item=terminal, active=True))

    return nodes, edges, bands


def _build_playback_path(placed: list[_Placed], *, total_steps: int) -> list[QPointF]:
    """Dense scene points along the AI walk (decision nodes → terminal)."""
    anchors: list[QPointF] = []
    for p in placed:
        if p.kind == "decision":
            anchors.append(QPointF(p.x, p.y + _NODE_H / 2))
        elif p.kind == "terminal":
            anchors.append(QPointF(p.x, p.y + _TERMINAL_H / 2))
    if len(anchors) < 2:
        return anchors

    seg_count = len(anchors) - 1
    per_seg = max(8, total_steps // seg_count)
    dense: list[QPointF] = []
    for i in range(seg_count):
        a, b = anchors[i], anchors[i + 1]
        for k in range(per_seg):
            t = k / per_seg
            dense.append(
                QPointF(
                    a.x() + (b.x() - a.x()) * t,
                    a.y() + (b.y() - a.y()) * t,
                )
            )
    dense.append(anchors[-1])
    return dense


class DecisionFlowVizPanel(QWidget):
    """Branched flowchart of the AI walk (yes=右 / no=左)."""

    playback_finished = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *, show_controls: bool = True) -> None:
        super().__init__(parent)
        self._settings: Any = None
        self._show_controls = show_controls
        self._last_trace_kw: dict[str, Any] | None = None
        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._on_play_tick)
        self._fx_timer = QTimer(self)
        self._fx_timer.timeout.connect(self._on_fx_tick)
        self._fx_timer.start(_FX_TICK_MS)
        self._play_points: list[QPointF] = []
        self._play_index = 0
        self._play_active = False
        self._last_placed: list[_Placed] = []
        self._last_rect = QRectF(-400, 0, 800, 200)

        self._scene = _FlowScene()
        self._view = QGraphicsView(self._scene)
        self._view.viewport().installEventFilter(self)
        self._view.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self._view.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._view.setBackgroundBrush(QBrush(QColor("#05070b")))

        title = QLabel("决策路径可视化")
        title.setObjectName("toolbarTitle")
        title.setStyleSheet(
            "font-size: 15px; font-weight: 600; letter-spacing: 0.5px;"
        )
        self._fullscreen_btn = QPushButton("全屏推演")
        self._fullscreen_btn.setObjectName("decisionFlowFullscreenButton")
        self._fullscreen_btn.setStyleSheet(
            f"color: {_NEON_CYAN}; border-color: {_NEON_CYAN}; font-weight: 600;"
        )
        self._fullscreen_btn.clicked.connect(self._open_fullscreen)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(title)
        title_row.addStretch(1)
        if self._show_controls:
            title_row.addWidget(self._fullscreen_btn)

        sub = QLabel(
            "卡片 = 判断节点 · 左 = 否 / 右 = 是 · 亮线 = AI 实际路径\n"
            "虚线框 = 未走分支含义（二元决策树） · 拖拽平移 · Ctrl + 滚轮缩放"
        )
        sub.setObjectName("mutedLabel")
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"color: {T.TEXT_SECONDARY}; font-size: 12px; line-height: 1.45;"
        )

        self._hud_label = QLabel("")
        self._hud_label.setTextFormat(Qt.TextFormat.RichText)
        self._hud_label.setStyleSheet(
            f"background-color: rgba(5, 13, 24, 220);"
            f"border: 1px solid {_NEON_CYAN}; border-radius: 8px;"
            f"padding: 8px 10px; color: {T.TEXT_PRIMARY};"
            f"font-family: Consolas, 'Microsoft YaHei UI'; font-size: 12px;"
        )

        self._play_status = QLabel("")
        self._play_status.setObjectName("mutedLabel")
        self._play_status.setStyleSheet(
            f"color: {T.ACCENT_PRIMARY}; font-size: 12px; min-height: 18px;"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        layout.addLayout(title_row)
        layout.addWidget(sub)
        layout.addWidget(self._hud_label)
        layout.addWidget(self._play_status)
        layout.addWidget(self._view, stretch=1)
        self.clear()

    def bind_settings(self, settings: Any) -> None:
        """Receive persisted settings (auto-play toggle)."""
        self._settings = settings

    def should_auto_play_after_load(self) -> bool:
        """Whether to switch tab and play when new trace data is loaded."""
        return self._playback_enabled()

    def _playback_enabled(self) -> bool:
        if self._settings is None:
            return False
        return bool(getattr(self._settings.general, "decision_flow_auto_play", False))

    def _on_fx_tick(self) -> None:
        global _ANIM_PHASE
        _ANIM_PHASE = (_ANIM_PHASE + 0.06) % 1000.0
        self._scene.update(self._scene.sceneRect())

    def play_path(self) -> bool:
        """Play camera animation along the current AI path."""
        if not self._last_placed:
            self._play_status.setText("暂无可播放路径，请先完成一次分析")
            return False
        self._fit_scene(self._last_rect)
        self._start_playback(self._last_placed)
        return True

    def is_playing(self) -> bool:
        """Whether the camera/path playback is currently running."""
        return bool(self._play_active)

    def _play_duration_seconds(self) -> int:
        if self._settings is None:
            return 50
        return int(getattr(self._settings.general, "decision_flow_play_seconds", 50))

    def _default_zoom_factor(self) -> float:
        """Scale applied after fitInView (1.0 = fit size; no upper cap—uses settings % / 100)."""
        if self._settings is None:
            return 5.0
        pct = int(getattr(self._settings.general, "decision_flow_default_zoom_pct", 500))
        return max(0.25, pct / 100.0)

    def eventFilter(self, obj: Any, event: QEvent) -> bool:  # noqa: N802
        if (
            self._play_active
            and obj is self._view.viewport()
            and event.type() == QEvent.Type.MouseButtonPress
        ):
            self._stop_playback(user_cancelled=True)
        return super().eventFilter(obj, event)

    def _stop_playback(self, *, user_cancelled: bool = False) -> None:
        self._play_timer.stop()
        self._play_active = False
        self._play_points = []
        self._play_index = 0
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        if user_cancelled:
            self._play_status.setText("已手动停止播放")
        else:
            self._play_status.setText("")

    def _start_playback(self, placed: list[_Placed]) -> None:
        self._stop_playback()
        secs = max(3, self._play_duration_seconds())
        total_steps = max(40, (secs * 1000) // _PLAY_TICK_MS)
        points = _build_playback_path(placed, total_steps=total_steps)
        if len(points) < 2:
            return
        self._play_points = points
        self._play_index = 0
        self._play_active = True
        self._view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._play_status.setText("路径播放中…（点击画面可停止）")
        self._on_play_tick()
        self._play_timer.start(_PLAY_TICK_MS)

    def _on_play_tick(self) -> None:
        if not self._play_active or not self._play_points:
            return
        self._view.centerOn(self._play_points[self._play_index])
        pct = int((self._play_index + 1) * 100 / len(self._play_points))
        self._play_status.setText(f"路径播放中… {pct}%（点击画面可停止）")
        self._play_index += 1
        if self._play_index >= len(self._play_points):
            self._stop_playback()
            self._play_status.setText("播放完成")
            self.playback_finished.emit()

    def wheelEvent(self, event: Any) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = math.pow(1.1, event.angleDelta().y() / 120.0)
            self._view.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def _fit_scene(self, rect: QRectF) -> None:
        self._view.resetTransform()
        self._view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        z = self._default_zoom_factor()
        if abs(z - 1.0) > 1e-6:
            self._view.scale(z, z)
        self._view.centerOn(rect.center())

    def _render_hud(
        self,
        *,
        merged: list[dict[str, Any]],
        terminal: dict[str, Any] | None,
        gate_result: str | None = None,
        gate_shortcircuited: bool = False,
    ) -> None:
        node_count = len(merged)
        gate_count = sum(1 for x in merged if x.get("phase") == "gate")
        decision_count = max(0, node_count - gate_count)
        outcome = str(terminal.get("outcome", "standby")) if terminal else "standby"
        outcome_zh = _OUTCOME_ZH.get(outcome, "待机")
        terminal_id = str(terminal.get("node_id", "--")) if terminal else "--"
        gate_text = gate_result or ("short" if gate_shortcircuited else "ready")
        status_color = _OUTCOME_COLOR.get(outcome, _NEON_CYAN)
        self._hud_label.setText(
            "<span style='color:{cyan}; font-weight:700;'>AI TACTICAL DECISION MATRIX</span>"
            " &nbsp; <span style='color:{muted};'>|</span> &nbsp; "
            "GATE <span style='color:{amber};'>{gate}</span>"
            " &nbsp; <span style='color:{muted};'>|</span> &nbsp; "
            "NODES <span style='color:{cyan};'>{nodes:02d}</span>"
            " &nbsp; <span style='color:{muted};'>|</span> &nbsp; "
            "G/D <span style='color:{violet};'>{gate_count}/{decision_count}</span>"
            " &nbsp; <span style='color:{muted};'>|</span> &nbsp; "
            "TERMINAL <span style='color:{status};'>§{terminal_id} {outcome}</span>"
            .format(
                cyan=_NEON_CYAN,
                muted=T.TEXT_MUTED,
                amber=_NEON_AMBER,
                violet=_NEON_VIOLET,
                status=status_color,
                gate=gate_text.upper(),
                nodes=node_count,
                gate_count=gate_count,
                decision_count=decision_count,
                terminal_id=terminal_id,
                outcome=outcome_zh,
            )
        )

    def _open_fullscreen(self) -> None:
        if self._last_trace_kw is None:
            self._play_status.setText("暂无可全屏推演路径，请先完成一次分析")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("AI Tactical Decision Matrix — Fullscreen")
        dlg.resize(1500, 950)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        panel = DecisionFlowVizPanel(dlg, show_controls=False)
        panel.bind_settings(self._settings)
        panel.set_trace(**self._last_trace_kw)
        layout.addWidget(panel)
        dlg.showMaximized()
        QTimer.singleShot(180, panel.play_path)
        dlg.exec()

    def clear(self) -> None:
        self._stop_playback()
        self._last_trace_kw = None
        self._last_placed = []
        self._scene.clear()
        self._render_hud(merged=[], terminal=None)
        hint = _EmptyHint("等待分析…\n提交后将显示左右分支决策流程图")
        hint.setPos(-200, 60)
        self._scene.addItem(hint)
        rect = QRectF(-400, 0, 800, 200)
        self._scene.setSceneRect(rect)
        self._fit_scene(rect)

    def set_trace(
        self,
        *,
        gate_trace: list[dict[str, Any]] | None = None,
        decision_trace: list[dict[str, Any]] | None = None,
        terminal: dict[str, Any] | None = None,
        gate_result: str | None = None,
        gate_shortcircuited: bool = False,
    ) -> bool:
        """Build flowchart; return True if there is a path to play."""
        self._last_trace_kw = {
            "gate_trace": gate_trace,
            "decision_trace": decision_trace,
            "terminal": terminal,
            "gate_result": gate_result,
            "gate_shortcircuited": gate_shortcircuited,
        }
        merged = merge_traces(gate_trace, decision_trace)
        self._scene.clear()
        if not merged and not terminal:
            self.clear()
            return False

        self._render_hud(
            merged=merged,
            terminal=terminal,
            gate_result=gate_result,
            gate_shortcircuited=gate_shortcircuited,
        )

        placed, edge_specs, bands = _layout_branched_path(merged, terminal)

        if gate_shortcircuited and merged:
            last = placed[-1] if placed else None
            if last and last.kind == "terminal":
                bands.append(_Placed(x=last.x, y=last.y - 50, kind="band_short"))
            elif placed:
                p = placed[-1]
                bands.append(_Placed(x=p.x, y=p.y + _NODE_H + 30, kind="band_short"))

        for b in bands:
            title = "阶段二 · 策略评估"
            if b.kind == "band_short":
                title = "阶段二已短路"
            band = _PhaseBandItem(title)
            band.setPos(b.x, b.y)
            self._scene.addItem(band)

        for p0, p1, label, active in edge_specs:
            self._scene.addItem(_BranchEdge(p0, p1, label, active=active))

        for p in placed:
            if p.kind == "decision" and p.item:
                node = _DecisionNode(p.item, p.step)
                node.setPos(p.x, p.y)
                self._scene.addItem(node)
            elif p.kind == "alt" and p.alt_branch and p.alt_node_id:
                outcome = get_node_branch_outcome(p.alt_node_id, p.alt_branch)
                alt = _AltBranchNode(p.alt_branch, outcome, p.alt_node_id)
                alt.setPos(p.x, p.y)
                self._scene.addItem(alt)
            elif p.kind == "terminal" and p.item:
                term = _TerminalNode(p.item)
                term.setPos(p.x, p.y)
                self._scene.addItem(term)

        xs = [p.x for p in placed]
        ys = [p.y for p in placed]
        widest = max(_NODE_W, _STUB_W, _TERMINAL_W)
        max_y = max(ys) + _TERMINAL_H + 80 if ys else 400
        min_x = min(xs) - widest / 2 if xs else -400
        max_x = max(xs) + widest / 2 if xs else 400
        rect = QRectF(min_x - 80, 0, max_x - min_x + 160, max_y)
        self._scene.setSceneRect(rect)
        self._last_placed = placed
        self._last_rect = rect
        self._fit_scene(rect)
        self._play_status.setText("")
        return bool(placed)
