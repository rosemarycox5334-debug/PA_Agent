from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QSizePolicy

from pa_agent.gui.widgets.flow_bar import FlowBar


def test_flow_bar_captions_can_enter_completed_state(qtbot) -> None:
    flow = FlowBar()
    qtbot.addWidget(flow)

    flow.set_step_status(2, "done")
    flow.set_step_caption(2, "阶段一完成")
    flow.set_step_status(3, "done")
    flow.set_step_caption(3, "阶段二完成")
    flow.set_step_status(4, "active")
    flow.set_step_caption(4, "可继续追问")

    assert flow._steps[2]._caption.text() == "阶段一完成"
    assert flow._steps[3]._caption.text() == "阶段二完成"
    assert flow._steps[4]._caption.text() == "可继续追问"


def test_flow_bar_caption_does_not_force_step_width(qtbot) -> None:
    flow = FlowBar()
    qtbot.addWidget(flow)

    flow.set_step_caption(2, "阶段一分析中，非常长的状态文本也不应该撑爆顶部流程条")

    assert flow._steps[2]._caption.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
