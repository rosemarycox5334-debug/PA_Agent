from __future__ import annotations

import sys

import pytest

pytest.importorskip("PyQt6")

from pa_agent.gui.widgets.summary_strip import SummaryStrip
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_summary_strip_keeps_all_cards_on_one_row(qapp) -> None:
    strip = SummaryStrip()

    strip.resize(500, 160)
    strip._relayout()
    assert strip._columns == 5

    strip.resize(700, 160)
    strip._relayout()
    assert strip._columns == 5

    strip.resize(920, 160)
    strip._relayout()
    assert strip._columns == 5

    strip.resize(1180, 120)
    strip._relayout()
    assert strip._columns == 5


def test_summary_strip_is_compact_when_all_cards_fit(qapp) -> None:
    strip = SummaryStrip()

    strip.resize(1180, 90)
    strip._relayout()

    assert strip._columns == 5
    assert all(card.minimumHeight() <= 52 for card in strip._cards)


def test_summary_strip_values_wrap_instead_of_forcing_width(qapp) -> None:
    strip = SummaryStrip()

    strip.set_metrics(
        {
            "最终动作": "不下单",
            "方向概率": "多 40% / 空 35% / 中 25%",
            "支撑区": "7460-7520",
        }
    )
    strip.resize(700, 160)
    strip._relayout()

    assert strip._columns == 5
    assert all(card._value.wordWrap() for card in strip._cards)
