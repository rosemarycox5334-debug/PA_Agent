"""Unit tests for GUI data-source connection helpers."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.gui.main_window import _ensure_data_source_connected


class _FakeSource:
    def __init__(self) -> None:
        self._connected = False
        self.calls: list[tuple[str, str | None, str | None]] = []

    def connect(self) -> None:
        self.calls.append(("connect", None, None))
        self._connected = True

    def subscribe(self, symbol: str, timeframe: str) -> None:
        self.calls.append(("subscribe", symbol, timeframe))


class _FailingSource(_FakeSource):
    def connect(self) -> None:
        raise RuntimeError("connect failed")


def test_ensure_data_source_connected_reconnects_and_subscribes() -> None:
    source = _FakeSource()

    _ensure_data_source_connected(
        source,
        symbol="LH2609",
        timeframe="1h",
        settings=SimpleNamespace(general=SimpleNamespace(rqdata_license_key="")),
    )

    assert source.calls == [
        ("connect", None, None),
        ("subscribe", "LH2609", "1h"),
    ]


def test_ensure_data_source_connected_skips_connected_source() -> None:
    source = _FakeSource()
    source._connected = True

    _ensure_data_source_connected(source, symbol="LH2609", timeframe="1h")

    assert source.calls == []


def test_ensure_data_source_connected_reports_connect_error() -> None:
    with pytest.raises(RuntimeError, match="connect failed"):
        _ensure_data_source_connected(_FailingSource(), symbol="LH2609", timeframe="1h")
