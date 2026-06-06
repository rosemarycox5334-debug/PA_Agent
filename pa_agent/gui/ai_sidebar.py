"""Right-hand sidebar: live stream, raw I/O, prompt files debug, and decision."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PyQt6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from pa_agent.gui.ai_stream_window import AIStreamPanel
from pa_agent.gui.debug_widget import DebugWidget
from pa_agent.gui.decision_panel import DecisionPanel
from pa_agent.gui.decision_flow_viz import DecisionFlowVizPanel
from pa_agent.gui.decision_tree_panel import DecisionTreePanel
from pa_agent.gui.prompt_files_panel import PromptFilesPanel

if TYPE_CHECKING:
    from pa_agent.config.settings import Settings


class AISidebar(QWidget):
    """Workbench sidebar tabs: 实时 | 决策树 | 决策树可视化 | 决策 | 原始 | 调试."""

    def __init__(
        self,
        api_key: str = "",
        settings: Optional["Settings"] = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tabs = QTabWidget()

        self.stream = AIStreamPanel()
        self.debug = DebugWidget(api_key=api_key)
        self.prompt_files = PromptFilesPanel()
        self.decision = DecisionPanel()
        self.decision_tree = DecisionTreePanel()
        self.decision_flow_viz = DecisionFlowVizPanel()

        self._tabs.addTab(self.stream, "实时")
        self._tabs.addTab(self.decision_tree, "决策树")
        self._tabs.addTab(self.decision_flow_viz, "决策树可视化")
        self._tabs.addTab(self.decision, "决策")
        self._tabs.addTab(self.debug, "原始")
        self._tabs.addTab(self.prompt_files, "调试")

        if settings is not None:
            self.bind_settings(settings)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tabs)

    TAB_STREAM = 0
    TAB_DECISION_TREE = 1
    TAB_DECISION_FLOW = 2
    TAB_DECISION = 3
    TAB_RAW = 4
    TAB_DEBUG = 5

    def focus_stream(self) -> None:
        """Switch to the live AI output tab (index 0)."""
        self._tabs.setCurrentIndex(self.TAB_STREAM)

    def focus_decision_flow_viz(self) -> None:
        """Switch to the decision flow visualization tab."""
        self._tabs.setCurrentIndex(self.TAB_DECISION_FLOW)

    def focus_raw(self) -> None:
        """Switch to the raw I/O tab (原始)."""
        self._tabs.setCurrentIndex(self.TAB_RAW)

    def bind_settings(self, settings: Optional["Settings"]) -> None:
        self.stream.bind_settings(settings)
        self.decision_flow_viz.bind_settings(settings)
