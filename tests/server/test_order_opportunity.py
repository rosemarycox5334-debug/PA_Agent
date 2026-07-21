"""notify.order_opportunity 纯函数（无 Qt 依赖）测试."""
import sys


def test_import_without_qt():
    """服务端模块 import 后不得引入 PyQt6."""
    for mod in list(sys.modules):
        if mod.startswith("PyQt6"):
            del sys.modules[mod]
    from pa_agent.notify.order_opportunity import has_order_opportunity  # noqa: F401

    assert not any(m.startswith("PyQt6") for m in sys.modules)


def test_has_order_opportunity_basic():
    from pa_agent.notify.order_opportunity import has_order_opportunity

    assert has_order_opportunity({"order_type": "限价单"})
    assert not has_order_opportunity({"order_type": "观望"})
    assert not has_order_opportunity(None)


def test_confidence_threshold_gate():
    from pa_agent.notify.order_opportunity import has_order_opportunity

    d = {"order_type": "市价单", "trade_confidence": 55}
    assert has_order_opportunity(d, confidence_threshold=50)
    assert not has_order_opportunity(d, confidence_threshold=60)
    # 无 confidence 字段且设了阈值 → 拒绝
    assert not has_order_opportunity({"order_type": "市价单"}, confidence_threshold=50)


def test_format_order_alert_message_fields():
    from pa_agent.notify.order_opportunity import format_order_alert_message

    text = format_order_alert_message(
        {"order_direction": "做多", "order_type": "限价单", "entry_price": 2400.5}
    )
    assert "方向：做多" in text and "入场：2400.5" in text


def test_gui_reexport_compat():
    """GUI 旧路径必须仍然可导入同一函数（需要 PyQt6 环境）."""
    from pa_agent.gui.order_opportunity import has_order_opportunity as gui_fn
    from pa_agent.notify.order_opportunity import has_order_opportunity as pure_fn

    assert gui_fn is pure_fn
