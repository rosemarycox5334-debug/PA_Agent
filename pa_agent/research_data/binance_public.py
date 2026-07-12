from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class PublicEndpointError(ValueError):
    pass


PUBLIC_BASE_URL = "https://fapi.binance.com"
PUBLIC_PATHS = frozenset(
    {
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/fundingRate",
        "/fapi/v1/indexPriceKlines",
        "/fapi/v1/klines",
        "/fapi/v1/markPriceKlines",
        "/fapi/v1/time",
    }
)
FORBIDDEN_PARAMETER_KEYS = frozenset(
    {"apikey", "secret", "signature", "recvwindow", "x-mbx-apikey"}
)


def _stdlib_get(url: str, timeout: float) -> bytes:
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


class BinancePublicClient:
    def __init__(
        self,
        *,
        base_url: str = PUBLIC_BASE_URL,
        timeout: float = 30.0,
        transport: Callable[[str, float], bytes] | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            base_url != PUBLIC_BASE_URL
            or parsed.scheme != "https"
            or parsed.hostname != "fapi.binance.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise PublicEndpointError("Only the canonical Binance USD-M public host is allowed")
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport or _stdlib_get

    def get_json(self, path: str, params: Mapping[str, Any]) -> dict[str, Any] | list[Any]:
        if path not in PUBLIC_PATHS:
            raise PublicEndpointError(f"Path is not in the public market-data allowlist: {path}")
        forbidden = {str(key).lower() for key in params} & FORBIDDEN_PARAMETER_KEYS
        if forbidden:
            raise PublicEndpointError(f"Authentication parameters are forbidden: {sorted(forbidden)}")
        query = urlencode(sorted((str(key), str(value)) for key, value in params.items()))
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{query}"
        try:
            payload = json.loads(self._transport(url, self._timeout))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicEndpointError("Invalid public endpoint response") from exc
        if not isinstance(payload, (dict, list)):
            raise PublicEndpointError("Public endpoint response must be a JSON object or array")
        return payload
