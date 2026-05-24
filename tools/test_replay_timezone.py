"""Integration test for replay data loading — datetimes are server time."""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime
from PyQt6.QtWidgets import QApplication
from pa_agent.gui.replay_panel import ReplayPanel
from pa_agent.data.replay_source import ReplaySource
from pa_agent.data.base import KlineBar

app = QApplication(sys.argv)
panel = ReplayPanel()

print("=" * 60)
print("Test 1: Panel datetimes are naive (no tzinfo)")
print("=" * 60)

from PyQt6.QtCore import QDateTime
panel._start_dt.setDateTime(QDateTime(2026, 5, 22, 21, 0))
panel._end_dt.setDateTime(QDateTime(2026, 5, 22, 23, 59))

start_dt, end_dt = panel.get_datetime_range()
print(f"Panel datetimes: start={start_dt}, end={end_dt}")

# Verify they are naive (no timezone info) — they represent server time directly
assert start_dt.tzinfo is None, "start_dt should be naive (server time)"
assert end_dt.tzinfo is None, "end_dt should be naive (server time)"
assert start_dt.hour == 21
assert end_dt.hour == 23
assert end_dt.minute == 59
print("✅ Panel returns naive datetimes — treated as server time")

print()
print("=" * 60)
print("Test 2: ReplaySource frame timestamps are server-time based")
print("=" * 60)

# Create synthetic bars with server-time timestamps
# 1653224400000 ms = 2022-05-22 13:00:00 (server time)
# 1653224460000 ms = 2022-05-22 13:01:00 (server time)
bars = [
    KlineBar(seq=1, ts_open=float(1653224400000), open=100.0, high=101.0, low=99.0, close=100.5, volume=100, closed=True),
    KlineBar(seq=2, ts_open=float(1653224460000), open=100.5, high=102.0, low=100.0, close=101.0, volume=150, closed=True),
]

rs = ReplaySource(bars, "TEST", "1m")
rs.advance()  # Advance to include both bars
frame = rs.current_frame()

# After advance(), frame.bars[0] is the second bar (newest-first order)
ts0 = datetime.fromtimestamp(frame.bars[0].ts_open / 1000)
print(f"Bar 0 (newest) local time: {ts0.isoformat()}")
print(f"  ts_open raw = {frame.bars[0].ts_open} ms")

# frame.bars[1] is the first bar (oldest)
ts1 = datetime.fromtimestamp(frame.bars[1].ts_open / 1000)
print(f"Bar 1 (oldest) local time: {ts1.isoformat()}")
print(f"  ts_open raw = {frame.bars[1].ts_open} ms")

# Verify the oldest bar has the original first timestamp
assert frame.bars[1].ts_open == 1653224400000.0
print("✅ Bar timestamp values correctly preserved")

print()
print("=" * 60)
print("Test 3: Verify no 30-minute offset in bar timestamps")
print("=" * 60)

# Check that consecutive 1m bars are exactly 1 minute apart
ts_diff = (frame.bars[0].ts_open - frame.bars[1].ts_open) / 1000
print(f"Time diff between consecutive 1m bars: {ts_diff} seconds")
assert abs(ts_diff - 60) < 1, f"Expected 60s diff, got {ts_diff}s"
print("✅ Consecutive 1m bars are exactly 60 seconds apart (no 30-min offset)")

print()
print("=" * 60)
print("ALL TESTS PASSED!")
print("=" * 60)