"""Test that the replay feature components initialize correctly."""
import os
import sys

# Set offscreen mode before any Qt imports
os.environ["QT_QPA_PLATFORM"] = "offscreen"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication
from pa_agent.app_context import AppContext
from pa_agent.gui.main_window import MainWindow

app = QApplication(sys.argv)
ctx = AppContext.bootstrap()
window = MainWindow(ctx)

# Verify replay components exist
assert hasattr(window, "_replay_btn"), "Missing _replay_btn"
assert window._replay_btn is not None, "_replay_btn is None"
assert window._replay_btn.text() == "逐K回放", f"Unexpected button text: {window._replay_btn.text()}"

assert hasattr(window, "_replay_panel"), "Missing _replay_panel"
assert window._replay_panel is not None, "_replay_panel is None"

assert hasattr(window, "_replay_mode"), "Missing _replay_mode"
assert window._replay_mode is False, "Replay mode should be False initially"

assert hasattr(window, "_replay_source"), "Missing _replay_source"
assert window._replay_source is None, "Replay source should be None initially"

# Verify replay panel signals
panel = window._replay_panel
assert panel.load_history_requested is not None
assert panel.next_bar_requested is not None
assert panel.exit_replay_requested is not None
assert panel.submit_analysis_requested is not None

# Verify submit_block_reason includes replay mode check
reason = window._submit_block_reason()
assert reason is None or "回放模式" not in (reason or ""), (
    f"Submit should not be blocked initially: {reason}"
)

# Test entering replay mode
window._on_enter_replay_mode()
assert window._replay_mode is True, "Should be in replay mode"
assert window._replay_btn.isEnabled() is False, "Replay button should be disabled"
assert not window._replay_panel.isHidden(), "Replay panel should not be hidden (show() was called)"

# Test exiting replay mode
window._on_replay_exit()
assert window._replay_mode is False, "Should have exited replay mode"
assert window._replay_btn.isEnabled() is True, "Replay button should be re-enabled"
assert window._replay_panel.isHidden(), "Replay panel should be hidden"

print("=" * 60)
print("All replay feature startup tests PASSED!")
print("=" * 60)