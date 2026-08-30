import json

import pytest

from pa_agent.research_data.binance_public import BinancePublicClient, PublicEndpointError


def test_client_builds_only_public_get_url_without_credential_headers():
    seen = []

    def transport(url: str, timeout: float) -> bytes:
        seen.append((url, timeout))
        return json.dumps([[1, "2"]]).encode()

    client = BinancePublicClient(transport=transport)
    result = client.get_json(
        "/fapi/v1/klines",
        {"symbol": "BTCUSDT", "interval": "1m", "startTime": 1},
    )

    assert result == [[1, "2"]]
    assert seen == [
        (
            "https://fapi.binance.com/fapi/v1/klines?interval=1m&startTime=1&symbol=BTCUSDT",
            30.0,
        )
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://fapi.binance.com",
        "https://evil.example",
        "https://user@fapi.binance.com",
        "https://fapi.binance.com.evil.example",
    ],
)
def test_client_rejects_noncanonical_base_urls(base_url):
    with pytest.raises(PublicEndpointError):
        BinancePublicClient(base_url=base_url)


@pytest.mark.parametrize(
    "path",
    [
        "/fapi/v2/account",
        "/fapi/v1/order",
        "/fapi/v1/openOrders",
        "/sapi/v1/account/status",
        "/fapi/v1/klines/../order",
    ],
)
def test_client_rejects_account_trade_and_unknown_paths(path):
    client = BinancePublicClient(transport=lambda *_: b"[]")

    with pytest.raises(PublicEndpointError):
        client.get_json(path, {})


@pytest.mark.parametrize("key", ["signature", "apiKey", "X-MBX-APIKEY", "secret", "recvWindow"])
def test_client_rejects_authentication_parameters(key):
    client = BinancePublicClient(transport=lambda *_: b"[]")

    with pytest.raises(PublicEndpointError):
        client.get_json("/fapi/v1/klines", {key: "forbidden"})


def test_client_rejects_non_json_collection_response():
    client = BinancePublicClient(transport=lambda *_: b'"scalar"')

    with pytest.raises(PublicEndpointError):
        client.get_json("/fapi/v1/time", {})
