"""Unit tests for web data-source catalogue service."""
from __future__ import annotations

from types import SimpleNamespace

from pa_agent.web.service.data_source_service import DataSourceService


def test_list_sources_includes_registered_market_sources() -> None:
    settings = SimpleNamespace(general=SimpleNamespace(last_data_source="akshare"))
    service = DataSourceService(data_service=object(), settings=settings)

    sources = service.list_sources()
    by_id = {source["id"]: source for source in sources}

    assert list(by_id) == ["mt5", "tradingview", "akshare", "rqdata"]
    assert by_id["akshare"]["label"] == "AkShare"
    assert by_id["akshare"]["default_symbol"] == "000001"
    assert by_id["akshare"]["is_active"] is True
    assert by_id["rqdata"]["default_symbol"] == "000001.XSHG"
