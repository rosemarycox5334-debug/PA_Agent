"""Unit tests for DataService."""
from __future__ import annotations

from unittest.mock import MagicMock

from pa_agent.web.service.data_service import DataService
from pa_agent.data.base import DataSourceTransientError, KlineFrame, KlineBar


class TestDataService:
    """Tests for DataService."""

    def test_get_frame_returns_kline_frame(self):
        """get_frame() should return a KlineFrame when data is available."""
        source = MagicMock()
        source.latest_snapshot.return_value = [
            KlineBar(seq=1, ts_open=1000, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0, closed=True),
            KlineBar(seq=2, ts_open=2000, open=100.5, high=102.0, low=100.0, close=101.5, volume=2000.0, closed=True),
        ]
        svc = DataService(source, symbol="TEST", timeframe="1h")
        frame = svc.get_frame(n=2)
        assert frame is not None
        assert isinstance(frame, KlineFrame)
        assert frame.symbol == "TEST"
        assert frame.timeframe == "1h"
        assert len(frame.bars) == 2

    def test_get_frame_returns_none_when_no_data(self):
        """get_frame() should return None when no bars available."""
        source = MagicMock()
        source.latest_snapshot.return_value = []
        svc = DataService(source, symbol="TEST", timeframe="1h")
        assert svc.get_frame() is None

    def test_get_snapshot_uses_get_frame(self):
        """get_snapshot() should reuse get_frame() internally."""
        source = MagicMock()
        source.latest_snapshot.return_value = [
            KlineBar(seq=1, ts_open=1000, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0, closed=True),
        ]
        svc = DataService(source, symbol="TEST", timeframe="1h")
        snapshot = svc.get_snapshot(n=1)
        assert snapshot is not None
        assert snapshot["symbol"] == "TEST"
        assert "bars" in snapshot
        assert "indicators" in snapshot

    def test_get_frame_reconnects_before_fetch_when_source_was_not_connected(self):
        """Startup connection failures should be retryable on the next fetch."""
        source = MagicMock()
        source.latest_snapshot.side_effect = [
            DataSourceTransientError("Not connected — call connect() first"),
            [
                KlineBar(seq=1, ts_open=1000, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0, closed=True),
                KlineBar(seq=2, ts_open=2000, open=100.5, high=102.0, low=100.0, close=101.5, volume=2000.0, closed=True),
            ],
        ]
        svc = DataService(source, symbol="TEST", timeframe="1h")

        frame = svc.get_frame(n=2)

        assert frame is not None
        source.connect.assert_called_once_with()
        source.subscribe.assert_called_with("TEST", "1h")
        assert source.latest_snapshot.call_count == 2

    def test_apply_settings_rebuilds_source_when_kind_changes(self, monkeypatch):
        """Changing last_data_source must replace the underlying DataSource."""
        old_source = MagicMock()
        new_source = MagicMock()
        created: list[str] = []

        class General:
            last_data_source = "tradingview"
            last_symbol = "XAUUSD"
            last_timeframe = "1h"
            last_tradingview_exchange = "OANDA"
            rqdata_license_key = ""

        class Settings:
            general = General()

        def _fake_create(kind: str):
            created.append(kind)
            return new_source

        monkeypatch.setattr("pa_agent.web.service.data_service.create_data_source", _fake_create)

        svc = DataService(old_source, symbol="SA2609", timeframe="1h")
        svc.apply_settings(Settings())

        old_source.disconnect.assert_called_once_with()
        assert created == ["tradingview"]
        new_source.set_exchange.assert_called_once_with("OANDA")
        new_source.connect.assert_called_once_with()
        new_source.subscribe.assert_called_once_with("XAUUSD", "1h")
