"""Compact East Money level-2-style order-book display."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
)


_BID_COLOR = "#3fb950"
_ASK_COLOR = "#f85149"
_MUTED_COLOR = "#8b949e"


def _format_price(value: object) -> str:
    try:
        return f"{float(value):.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "—"


def _format_volume(value: object) -> str:
    try:
        volume = max(0, int(value))
    except (TypeError, ValueError):
        return "—"
    if volume >= 100_000_000:
        return f"{volume / 100_000_000:.2f}亿手"
    if volume >= 10_000:
        return f"{volume / 10_000:.2f}万手"
    return f"{volume}手"


class EastMoneyOrderBookPanel(QFrame):
    """Show free five-level bid/ask depth returned by East Money."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName("eastMoneyOrderBook")
        self.setStyleSheet(
            "QFrame#eastMoneyOrderBook {"
            "background-color: #161b22; border: 1px solid #30363d;"
            "border-radius: 8px;"
            "}"
        )
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )
        self._setup_ui()
        self.clear()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        self._title_label = QLabel("东方财富盘口")
        self._title_label.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #79c0ff;"
        )
        self._depth_label = QLabel("五档")
        self._depth_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._depth_label.setStyleSheet(f"color: {_MUTED_COLOR}; font-size: 12px;")
        title_row.addWidget(self._title_label)
        title_row.addStretch(1)
        title_row.addWidget(self._depth_label)
        layout.addLayout(title_row)

        self._quote_label = QLabel("等待获取盘口")
        self._quote_label.setWordWrap(True)
        self._quote_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #c9d1d9;"
        )
        layout.addWidget(self._quote_label)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(5)

        for column, text, color in (
            (0, "买方", _BID_COLOR),
            (1, "价格", _BID_COLOR),
            (2, "委托量", _BID_COLOR),
            (3, "卖方", _ASK_COLOR),
            (4, "价格", _ASK_COLOR),
            (5, "委托量", _ASK_COLOR),
        ):
            header = QLabel(text)
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setStyleSheet(f"font-weight: bold; color: {color};")
            grid.addWidget(header, 0, column)

        self._bid_rows: list[tuple[QLabel, QLabel, QLabel]] = []
        self._ask_rows: list[tuple[QLabel, QLabel, QLabel]] = []
        for index in range(5):
            bid_row = self._make_level_row(f"买{index + 1}", _BID_COLOR)
            ask_row = self._make_level_row(f"卖{index + 1}", _ASK_COLOR)
            for column, label in enumerate((*bid_row, *ask_row)):
                grid.addWidget(label, index + 1, column)
            self._bid_rows.append(bid_row)
            self._ask_rows.append(ask_row)
        layout.addLayout(grid)

        self._summary_label = QLabel("买盘 —  ·  卖盘 —  ·  委比 —")
        self._summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._summary_label.setWordWrap(True)
        self._summary_label.setStyleSheet(
            "padding: 7px; background-color: #21262d; border-radius: 6px;"
            "color: #c9d1d9; font-size: 12px;"
        )
        layout.addWidget(self._summary_label)
        layout.addStretch(1)

    @staticmethod
    def _make_level_row(name: str, color: str) -> tuple[QLabel, QLabel, QLabel]:
        name_label = QLabel(name)
        price_label = QLabel("—")
        volume_label = QLabel("—")
        for label in (name_label, price_label, volume_label):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet(f"color: {color}; font-size: 12px;")
        name_label.setStyleSheet(
            f"color: {color}; font-weight: bold; font-size: 12px;"
        )
        return name_label, price_label, volume_label

    @staticmethod
    def _level_values(level: Any) -> tuple[object, object]:
        if isinstance(level, dict):
            return level.get("price"), level.get("volume")
        return getattr(level, "price", None), getattr(level, "volume", None)

    def _set_levels(
        self,
        rows: list[tuple[QLabel, QLabel, QLabel]],
        levels: object,
    ) -> int:
        items = list(levels) if isinstance(levels, (list, tuple)) else []
        total = 0
        for index, (_, price_label, volume_label) in enumerate(rows):
            if index < len(items):
                price, volume = self._level_values(items[index])
                price_label.setText(_format_price(price))
                volume_label.setText(_format_volume(volume))
                try:
                    total += max(0, int(volume))
                except (TypeError, ValueError):
                    pass
            else:
                price_label.setText("—")
                volume_label.setText("—")
        return total

    def set_order_book(self, book: Any | None) -> None:
        if book is None:
            self.clear("暂无盘口数据")
            return

        code = str(getattr(book, "code", "") or "")
        name = str(getattr(book, "name", "") or "")
        price = _format_price(getattr(book, "price", None))
        try:
            pct = float(getattr(book, "pct_chg", 0.0) or 0.0)
            pct_text = f"{pct:+.2f}%"
            pct_color = _BID_COLOR if pct >= 0 else _ASK_COLOR
        except (TypeError, ValueError):
            pct_text = "—"
            pct_color = _MUTED_COLOR

        identity = " ".join(part for part in (name, code) if part).strip()
        prefix = f"{identity} · " if identity else ""
        self._quote_label.setText(f"{prefix}最新 {price} · {pct_text}")
        self._quote_label.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {pct_color};"
        )

        depth = int(getattr(book, "depth_levels", 5) or 5)
        source = str(getattr(book, "depth_source", "") or "")
        self._depth_label.setText(
            f"{depth}档{' · L2' if source == 'push2_l2' else ' · 免费行情'}"
        )

        bid_total = self._set_levels(self._bid_rows, getattr(book, "bids", None))
        ask_total = self._set_levels(self._ask_rows, getattr(book, "asks", None))
        total = bid_total + ask_total
        imbalance = ((bid_total - ask_total) / total * 100.0) if total else 0.0
        self._summary_label.setText(
            f"买盘 {_format_volume(bid_total)}  ·  "
            f"卖盘 {_format_volume(ask_total)}  ·  委比 {imbalance:+.1f}%"
        )

    def clear(self, message: str = "等待获取盘口") -> None:
        self._quote_label.setText(message)
        self._quote_label.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {_MUTED_COLOR};"
        )
        self._depth_label.setText("五档")
        self._set_levels(self._bid_rows, [])
        self._set_levels(self._ask_rows, [])
        self._summary_label.setText("买盘 —  ·  卖盘 —  ·  委比 —")
