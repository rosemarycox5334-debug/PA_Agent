"""PA Agent utility package."""

from pa_agent.util.threading import CancelToken, OrchestratorEvent
from pa_agent.util.logging import configure_logging, update_api_key

__all__ = ["CancelToken", "OrchestratorEvent", "EventBus", "configure_logging", "update_api_key"]


def __getattr__(name: str):
    # EventBus 依赖 PyQt6；惰性导入使 headless 服务端（无 Qt 环境）可用本包
    if name == "EventBus":
        from pa_agent.util.event_bus import EventBus

        return EventBus
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
