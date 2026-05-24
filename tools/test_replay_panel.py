"""Quick test for ReplayPanel initialization and signal emission."""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta
from PyQt6.QtWidgets import QApplication
from pa_agent.gui.replay_panel import ReplayPanel

app = QApplication(sys.argv)
panel = ReplayPanel()

# Test initial state
assert panel._load_btn.text() == "加载历史数据"
assert panel._next_btn.isEnabled() is False
assert panel._submit_btn.isEnabled() is False
assert panel._exit_btn.isEnabled() is False

# Test set_replay_active
panel.set_replay_active(True)
assert panel._next_btn.isEnabled() is True
assert panel._submit_btn.isEnabled() is True
assert panel._exit_btn.isEnabled() is True
assert panel._load_btn.isEnabled() is False

# Test set_replay_active(False)
panel.set_replay_active(False)
assert panel._next_btn.isEnabled() is False

# Test set_loading
panel.set_loading(True)
assert panel._load_btn.text() == "加载中…"
panel.set_loading(False)
assert panel._load_btn.text() == "加载历史数据"

# Test update_progress
panel.update_progress(5, 100)
assert "5" in panel._progress_label.text()
assert "100" in panel._progress_label.text()

# Test set_status
panel.set_status("测试状态")
assert panel._status_label.text() == "测试状态"

# Test get_datetime_range
start, end = panel.get_datetime_range()
assert isinstance(start, datetime)
assert isinstance(end, datetime)
assert start < end

# Test signal emission: load_history_requested
received = []

def on_load(s, e):
    received.append((s, e))

panel.load_history_requested.connect(on_load)
# Simulate click on load button
panel._on_load()
assert len(received) == 1
assert received[0][0] < received[0][1]

# Test signal emission: next_bar_requested
received2 = []

def on_next():
    received2.append(True)

panel.next_bar_requested.connect(on_next)
panel.next_bar_requested.emit()
assert len(received2) == 1

# Test signal emission: exit_replay_requested
received3 = []

def on_exit():
    received3.append(True)

panel.exit_replay_requested.connect(on_exit)
panel.exit_replay_requested.emit()
assert len(received3) == 1

# Test signal emission: submit_analysis_requested
received4 = []

def on_submit():
    received4.append(True)

panel.submit_analysis_requested.connect(on_submit)
panel.submit_analysis_requested.emit()
assert len(received4) == 1

# Test set_next_enabled / set_submit_enabled
panel.set_replay_active(True)
panel.set_next_enabled(False)
assert panel._next_btn.isEnabled() is False
panel.set_next_enabled(True)
assert panel._next_btn.isEnabled() is True
panel.set_submit_enabled(False)
assert panel._submit_btn.isEnabled() is False
panel.set_submit_enabled(True)
assert panel._submit_btn.isEnabled() is True

print("=" * 60)
print("All ReplayPanel tests PASSED!")
print("=" * 60)