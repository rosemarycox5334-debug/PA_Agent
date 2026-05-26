"""Unit tests for YFinance data source (no network)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pa_agent.data.base import DataSourceTransientError, KlineBar
from pa_agent.data.yfinance_source import YFinanceSource, _resample_4h, _row_ts_ms


class TestYFinanceSourceLifecycle:
    def test_connect_without_yfinance_raises(self):
        src = YFinanceSource()
        with patch.dict("sys.modules", {"yfinance": None}):
            with pytest.raises(DataSourceTransientError, match="yfinance not installed"):
                src.connect()

    def test_connect_and_disconnect(self):
        src = YFinanceSource()
        src.connect()
        assert src._connected is True
        src.disconnect()
        assert src._connected is False

    def test_subscribe_valid_timeframe(self):
        src = YFinanceSource()
        src.connect()
        src.subscribe("GC=F", "4h")
        assert src._symbol == "GC=F"
        assert src._timeframe == "4h"

    def test_subscribe_invalid_timeframe_raises(self):
        src = YFinanceSource()
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            src.subscribe("GC=F", "3h")

    def test_unsubscribe_clears_state(self):
        src = YFinanceSource()
        src.connect()
        src.subscribe("GC=F", "1h")
        src.unsubscribe()
        assert src._symbol == ""
        assert src._timeframe == ""

    def test_supported_timeframes(self):
        src = YFinanceSource()
        tfs = src.supported_timeframes()
        assert "1m" in tfs
        assert "4h" in tfs
        assert "1d" in tfs
        assert "1w" in tfs

    def test_list_symbols_returns_expected(self):
        src = YFinanceSource()
        symbols = src.list_symbols()
        assert "GC=F" in symbols
        assert "BTC-USD" in symbols


class TestYFinanceSourceFetch:
    def _make_mock_df(self, n: int = 5) -> pd.DataFrame:
        """Return a mock yfinance DataFrame oldest-first."""
        index = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
        data = {
            "Open": [float(i) for i in range(n)],
            "High": [float(i + 1) for i in range(n)],
            "Low": [float(i - 0.5) for i in range(n)],
            "Close": [float(i + 0.5) for i in range(n)],
            "Volume": [float(i * 100) for i in range(n)],
        }
        return pd.DataFrame(data, index=index)

    def test_latest_snapshot_not_connected_raises(self):
        src = YFinanceSource()
        with pytest.raises(DataSourceTransientError, match="Not connected"):
            src.latest_snapshot(10)

    def test_latest_snapshot_not_subscribed_raises(self):
        src = YFinanceSource()
        src.connect()
        with pytest.raises(DataSourceTransientError, match="Not subscribed"):
            src.latest_snapshot(10)

    @patch("pa_agent.data.yfinance_source.yf.Ticker")
    def test_latest_snapshot_1h(self, mock_ticker_cls):
        src = YFinanceSource()
        src.connect()
        src.subscribe("GC=F", "1h")

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = self._make_mock_df(10)
        mock_ticker_cls.return_value = mock_ticker

        bars = src.latest_snapshot(5)
        assert len(bars) == 5
        assert bars[0].seq == 1
        assert bars[0].closed is False
        assert bars[-1].closed is True
        assert bars[0].close == 9.5  # newest bar (index 9)

    @patch("pa_agent.data.yfinance_source.yf.Ticker")
    def test_latest_snapshot_4h_resampling(self, mock_ticker_cls):
        src = YFinanceSource()
        src.connect()
        src.subscribe("GC=F", "4h")

        # Provide 12 hours of 1h data → should resample to 3 bars
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = self._make_mock_df(12)
        mock_ticker_cls.return_value = mock_ticker

        bars = src.latest_snapshot(2)
        assert len(bars) == 2
        # Resampled 4h bar open should match the first bar of the 4h window
        # (oldest-first df, so last resampled bar is newest)
        assert bars[0].seq == 1
        assert bars[0].high == 12.0  # max of last 4 hours

    @patch("pa_agent.data.yfinance_source.yf.Ticker")
    def test_latest_snapshot_empty_data_raises(self, mock_ticker_cls):
        src = YFinanceSource()
        src.connect()
        src.subscribe("GC=F", "1h")

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker_cls.return_value = mock_ticker

        with pytest.raises(DataSourceTransientError, match="no data"):
            src.latest_snapshot(5)

    @patch("pa_agent.data.yfinance_source.yf.Ticker")
    def test_latest_snapshot_network_error_raises(self, mock_ticker_cls):
        src = YFinanceSource()
        src.connect()
        src.subscribe("GC=F", "1h")

        mock_ticker_cls.side_effect = Exception("network timeout")

        with pytest.raises(DataSourceTransientError, match="yfinance fetch failed"):
            src.latest_snapshot(5)


class TestResample4h:
    def test_resample_1h_to_4h_ohlcv(self):
        index = pd.date_range("2024-01-01 00:00", periods=8, freq="h", tz="UTC")
        df = pd.DataFrame(
            {
                "Open": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
                "High": [11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0],
                "Low": [9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0],
                "Close": [10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5],
                "Volume": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0],
            },
            index=index,
        )
        resampled = _resample_4h(df)
        assert len(resampled) == 2
        # First 4h bar
        assert resampled.iloc[0]["Open"] == 10.0
        assert resampled.iloc[0]["High"] == 14.0
        assert resampled.iloc[0]["Low"] == 9.0
        assert resampled.iloc[0]["Close"] == 13.5
        assert resampled.iloc[0]["Volume"] == 1000.0

    def test_resample_drops_na(self):
        index = pd.date_range("2024-01-01 00:00", periods=3, freq="h", tz="UTC")
        df = pd.DataFrame(
            {"Open": [1.0, 2.0, 3.0], "High": [2.0, 3.0, 4.0], "Low": [0.0, 1.0, 2.0], "Close": [1.5, 2.5, 3.5], "Volume": [10.0, 20.0, 30.0]},
            index=index,
        )
        resampled = _resample_4h(df)
        # 3 rows can't form a full 4h bar → empty after dropna
        assert len(resampled) == 0


class TestRowTsMs:
    def test_row_ts_ms_with_datetime(self):
        dt = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        row = MagicMock()
        row.Datetime = dt
        row.Date = None
        assert _row_ts_ms(row) == int(dt.timestamp() * 1000)

    def test_row_ts_ms_fallback_to_date(self):
        dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        row = MagicMock()
        row.Datetime = None
        row.Date = dt
        assert _row_ts_ms(row) == int(dt.timestamp() * 1000)

    def test_row_ts_ms_via_pandas_timestamp(self):
        import pandas as pd
        ts = pd.Timestamp("2024-01-01 12:00", tz="UTC")
        row = MagicMock()
        row.Datetime = ts
        row.Date = None
        assert _row_ts_ms(row) == int(ts.timestamp() * 1000)
