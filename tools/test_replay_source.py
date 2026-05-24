"""Unit tests for ReplaySource — bar-by-bar replay engine."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.data.replay_source import ReplaySource


def _make_bars(n: int) -> list[KlineBar]:
    """Create *n* synthetic KlineBar objects (oldest-first)."""
    bars = []
    for i in range(n):
        bars.append(KlineBar(
            seq=i + 1,
            ts_open=float(1000 + i * 60_000),  # 1 min intervals
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=100.0 + i,
            closed=True,
        ))
    return bars


def test_initial_state():
    """ReplaySource starts at index 0 with has_next=True when n > 1."""
    bars = _make_bars(10)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    assert rs.current_index == 0
    assert rs.has_next is True
    assert rs.total_count == 10
    assert rs.progress_text == "K线 1/10"
    assert rs.symbol == "XAUUSD"
    assert rs.timeframe == "15m"


def test_single_bar():
    """ReplaySource with a single bar has has_next=False."""
    bars = _make_bars(1)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    assert rs.current_index == 0
    assert rs.has_next is False
    assert rs.total_count == 1


def test_advance():
    """advance() increments index and returns True until the end."""
    bars = _make_bars(5)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    for i in range(4):
        assert rs.advance() is True
        assert rs.current_index == i + 1
    # Last advance should fail
    assert rs.advance() is False
    assert rs.current_index == 4
    assert rs.has_next is False


def test_reset():
    """reset() returns to index 0."""
    bars = _make_bars(10)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    rs.advance()
    rs.advance()
    rs.advance()
    assert rs.current_index == 3
    rs.reset()
    assert rs.current_index == 0


def test_current_frame_includes_all_bars():
    """current_frame() returns all bars from start to current_index."""
    bars = _make_bars(10)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    # At index 0, frame should have 1 bar
    frame = rs.current_frame()
    assert isinstance(frame, KlineFrame)
    assert len(frame.bars) == 1
    assert frame.bars[0].seq == 1
    assert frame.bars[0].close == 100.5

    # Advance to index 2, frame should have 3 bars
    rs.advance()
    rs.advance()
    frame = rs.current_frame()
    assert len(frame.bars) == 3
    # Newest bar (seq=1) should be the last bar added
    assert frame.bars[0].close == 102.5


def test_current_frame_newest_first():
    """current_frame() returns bars newest-first (seq=1 = newest)."""
    bars = _make_bars(5)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    rs.advance()
    rs.advance()
    frame = rs.current_frame()
    # bars[0] should be the newest (index 2 in original)
    assert frame.bars[0].close == 102.5
    # bars[-1] should be the oldest (index 0 in original)
    assert frame.bars[-1].close == 100.5


def test_current_frame_all_closed():
    """All bars in current_frame() should have closed=True."""
    bars = _make_bars(5)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    rs.advance()
    frame = rs.current_frame()
    for bar in frame.bars:
        assert bar.closed is True, f"Bar seq={bar.seq} should be closed"


def test_current_frame_has_indicators():
    """current_frame() should compute EMA20 and ATR14 indicators."""
    bars = _make_bars(30)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    for _ in range(29):
        rs.advance()
    frame = rs.current_frame()
    assert frame.indicators is not None
    assert len(frame.indicators.ema20) == 30
    assert len(frame.indicators.atr14) == 30


def test_analysis_frame_all_bars():
    """analysis_frame() with n=None returns all bars."""
    bars = _make_bars(10)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    rs.advance()
    rs.advance()
    frame = rs.analysis_frame()
    assert len(frame.bars) == 3


def test_analysis_frame_with_n():
    """analysis_frame(n=2) returns only the most recent 2 bars."""
    bars = _make_bars(10)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    rs.advance()
    rs.advance()
    rs.advance()
    frame = rs.analysis_frame(n=2)
    assert len(frame.bars) == 2
    # Should be the 2 most recent bars
    assert frame.bars[0].close == 103.5  # newest (index 3)
    assert frame.bars[1].close == 102.5  # second newest (index 2)


def test_empty_bars_raises():
    """ReplaySource with empty bars should raise ValueError."""
    try:
        ReplaySource([], "XAUUSD", "15m")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_progress_text_updates():
    """progress_text should reflect current position."""
    bars = _make_bars(5)
    rs = ReplaySource(bars, "XAUUSD", "15m")
    assert rs.progress_text == "K线 1/5"
    rs.advance()
    assert rs.progress_text == "K线 2/5"
    rs.advance()
    assert rs.progress_text == "K线 3/5"


if __name__ == "__main__":
    test_initial_state()
    print("✓ test_initial_state")
    test_single_bar()
    print("✓ test_single_bar")
    test_advance()
    print("✓ test_advance")
    test_reset()
    print("✓ test_reset")
    test_current_frame_includes_all_bars()
    print("✓ test_current_frame_includes_all_bars")
    test_current_frame_newest_first()
    print("✓ test_current_frame_newest_first")
    test_current_frame_all_closed()
    print("✓ test_current_frame_all_closed")
    test_current_frame_has_indicators()
    print("✓ test_current_frame_has_indicators")
    test_analysis_frame_all_bars()
    print("✓ test_analysis_frame_all_bars")
    test_analysis_frame_with_n()
    print("✓ test_analysis_frame_with_n")
    test_empty_bars_raises()
    print("✓ test_empty_bars_raises")
    test_progress_text_updates()
    print("✓ test_progress_text_updates")
    print("\n" + "=" * 60)
    print("All ReplaySource unit tests PASSED!")
    print("=" * 60)