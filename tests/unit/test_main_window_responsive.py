from __future__ import annotations

import sys

import pytest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QSizePolicy

from pa_agent.app_context import AppContext
from pa_agent.config.settings import Settings
from pa_agent.gui.main_window import (
    MainWindow,
    _fit_dimension_to_screen,
    _ui_width_scale,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _assert_visible_widgets_do_not_overlap(layout):
    geometries = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget()
        if widget is not None and not widget.isHidden():
            geometries.append(item.geometry())
    for left, right in zip(geometries, geometries[1:]):
        assert left.right() < right.left()


def test_ui_width_scale_is_bounded():
    assert _ui_width_scale(3840) == 1.0
    assert _ui_width_scale(1920) == 1.0
    assert _ui_width_scale(1600) == pytest.approx(1600 / 1920)
    assert _ui_width_scale(800) == 0.72


def test_fit_dimension_never_exceeds_available_screen():
    assert _fit_dimension_to_screen(1600, preferred=1440, floor=960) == 1440
    assert _fit_dimension_to_screen(1366, preferred=1440, floor=960) == 1302
    assert _fit_dimension_to_screen(600, preferred=1440, floor=960) == 600


def test_main_window_controls_do_not_force_desktop_wide_minimum(qapp):
    window = MainWindow(AppContext(settings=Settings()))
    window.show()
    qapp.processEvents()

    assert window._controls_compact is True
    assert window.centralWidget().minimumSizeHint().width() <= 1366
    _assert_visible_widgets_do_not_overlap(window._primary_controls_layout)
    _assert_visible_widgets_do_not_overlap(window._action_controls_layout)
    for widget in window._action_control_widgets:
        if isinstance(widget, QAbstractButton) and not widget.isHidden():
            assert widget.width() >= widget.sizeHint().width()

    before = window.centralWidget().minimumSizeHint().width()
    window._ai_mode_label.setText("very-long-model-name-" * 100)
    window._decision_badge.setText("分析状态-" * 100)
    window.centralWidget().layout().activate()

    assert window.centralWidget().minimumSizeHint().width() <= before

    window._arrange_control_rows(1600)
    assert window._controls_compact is True
    assert window._action_controls_layout.count() == len(window._action_control_widgets)
    assert window._primary_controls_layout.indexOf(window._fetch_data_btn) == -1
    assert window._action_controls_layout.indexOf(window._fetch_data_btn) >= 0
    window.close()


def test_eastmoney_date_controls_fit_common_desktop_width(qapp):
    settings = Settings()
    settings.general.last_data_source = "eastmoney"
    settings.general.last_symbol = "600519"
    window = MainWindow(AppContext(settings=settings))
    qapp.processEvents()

    assert not window._eastmoney_date_filter_checkbox.isHidden()
    assert window.centralWidget().minimumSizeHint().width() <= 800
    window.close()


def test_workbench_panels_can_shrink_inside_maximized_viewport(qapp):
    window = MainWindow(AppContext(settings=Settings()))

    panels = (
        window._chart_widget,
        window._eastmoney_order_book_panel,
        window._ai_sidebar,
    )
    for panel in panels:
        assert panel.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Ignored

    assert window._workbench_splitter.minimumSizeHint().height() < 100
    assert window.centralWidget().minimumSizeHint().height() <= 600
    window.close()
