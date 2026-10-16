"""HTTP JSON client shared by the crypto exchange data sources.

Why this module exists
----------------------
Binance and OKX both expose public market-data REST endpoints that need no API
key, but they differ in host reachability and in error conventions. Rather than
duplicating "build URL → call → decode → map errors" in every source (the
duplication pattern flagged for ``akshare_source``/``ashare_common`` in the
2026-10 code review), both sources build on this one client.

Design points
-------------
* **Host failover with a cooldown.** ``api.binance.com`` is frequently
  unreachable from mainland China while the official market-data domain
  ``data-api.binance.vision`` still answers. Hosts are tried in order; a host
  that fails is parked for ``_HOST_COOLDOWN_S`` so later refreshes skip it
  instead of paying the failure latency on every tick.
* **Pluggable transport.** ``httpx`` is the primary transport (it respects
  ``HTTPS_PROXY``/``HTTP_PROXY`` automatically, which is how CN users reach
  blocked venues), ``curl_cffi`` is the fallback (TLS impersonation may help
  with CDN bot walls), plain ``urllib`` is the last resort so the module works
  even in a bare environment. All three are already project dependencies except
  urllib (stdlib).
* **Error taxonomy.** Network failures, timeouts, 429/418 (rate limit) and 5xx
  are *transient* (``DataSourceTransientError`` → the refresh loop retries with
  backoff); other 4xx are *permanent* (``DataSourceError``) and carry the
  upstream error body so a wrong symbol is diagnosable.
* No module-level mutable state: the selected transport and the parked-host
  table live on the client instance, so two sources never interfere.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

from pa_agent.data.base import DataSourceError, DataSourceTransientError

logger = logging.getLogger(__name__)

#: How long a failing host is skipped before it is retried.
_HOST_COOLDOWN_S = 300.0

#: Retry-After (seconds) is capped to keep refresh ticks responsive.
_MAX_RETRY_AFTER_S = 60.0

DEFAULT_TIMEOUT_S = 15.0

USER_AGENT = "PA-Agent/1.x (price-action analysis; public market data)"

#: ``(status_code, body)`` returning transports, tried in this order.
Transport = Callable[[str, float, dict[str, str]], tuple[int, bytes]]


# ── Default transports ────────────────────────────────────────────────────────


def _transport_httpx(url: str, timeout_s: float, headers: dict[str, str]) -> tuple[int, bytes]:
    import httpx

    with httpx.Client(
        timeout=timeout_s,
        follow_redirects=True,
        headers=headers,
        # trust_env defaults to True → HTTPS_PROXY / HTTP_PROXY are honoured.
    ) as client:
        resp = client.get(url)
        return resp.status_code, resp.content


def _transport_curl_cffi(url: str, timeout_s: float, headers: dict[str, str]) -> tuple[int, bytes]:
    from curl_cffi import requests as curl_requests

    resp = curl_requests.get(url, timeout=timeout_s, headers=headers, impersonate="chrome")
    return int(resp.status_code), resp.content


def _transport_urllib(url: str, timeout_s: float, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body
        return int(exc.code), exc.read()


_DEFAULT_TRANSPORTS: tuple[tuple[str, Transport], ...] = (
    ("httpx", _transport_httpx),
    ("curl_cffi", _transport_curl_cffi),
    ("urllib", _transport_urllib),
)


def resolve_default_transport() -> tuple[str, Transport]:
    """Return the first transport whose backend is importable (never raises)."""
    for name, fn in _DEFAULT_TRANSPORTS:
        backend = {"httpx": "httpx", "curl_cffi": "curl_cffi"}.get(name)
        if backend is None:
            return name, fn
        try:
            __import__(backend)
        except ImportError:
            continue
        return name, fn
    return "urllib", _transport_urllib


# ── Client ────────────────────────────────────────────────────────────────────


class CryptoRestClient:
    """Minimal JSON-over-HTTPS client with per-venue host failover."""

    def __init__(
        self,
        hosts: Sequence[str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: Transport | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        if not hosts:
            raise ValueError("CryptoRestClient requires at least one host")
        self._hosts: tuple[str, ...] = tuple(h.rstrip("/") for h in hosts)
        self._timeout_s = float(timeout_s)
        self._log = logger_ or logger
        if transport is not None:
            self._transport = transport
            self._transport_name = "injected"
        else:
            self._transport_name, self._transport = resolve_default_transport()
        # host -> monotonic deadline until which the host is skipped.
        self._parked: dict[str, float] = {}

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def hosts(self) -> tuple[str, ...]:
        return self._hosts

    @property
    def transport_name(self) -> str:
        return self._transport_name

    @property
    def preferred_host(self) -> str:
        """First host that is currently healthy (may be parked)."""
        return self._live_hosts()[0] if self._live_hosts() else self._hosts[0]

    def _live_hosts(self) -> list[str]:
        now = time.monotonic()
        live = [h for h in self._hosts if self._parked.get(h, 0.0) <= now]
        # Everything parked → give the first host another chance rather than
        # failing without a single network attempt.
        return live or [self._hosts[0]]

    def _park(self, host: str, *, seconds: float = _HOST_COOLDOWN_S) -> None:
        self._parked[host] = time.monotonic() + seconds

    # ── Public API ────────────────────────────────────────────────────────────

    def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
        hosts: Sequence[str] | None = None,
    ) -> Any:
        """GET *path* (+ query) from the first answering host and decode JSON.

        Raises ``DataSourceTransientError`` for retryable failures and
        ``DataSourceError`` for permanent ones (e.g. invalid symbol).

        A *permanent* status only means this request is bad on *that host*:
        Binance's public mirror serves ``/api/v3`` but not ``/fapi``, so the
        remaining hosts are still tried and the permanent error is raised only
        when no host accepted the request (found by the live smoke run).
        """
        if hosts:
            # An explicit host list (e.g. Binance spot vs USDⓈ-M host sets) must
            # still honour the parked-host cooldown, otherwise a dead host is
            # retried on every refresh tick.
            requested = [h.rstrip("/") for h in hosts]
            now = time.monotonic()
            healthy = [h for h in requested if self._parked.get(h, 0.0) <= now]
            candidates = healthy or requested
        else:
            candidates = self._live_hosts()
        timeout = float(timeout_s or self._timeout_s)
        attempts: list[str] = []
        permanent: DataSourceError | None = None

        for host in candidates:
            url = host + path
            if params:
                url = f"{url}?{urllib.parse.urlencode(params)}"
            try:
                status, body = self._transport(
                    url, timeout, {"User-Agent": USER_AGENT, "Accept": "application/json"}
                )
            except Exception as exc:  # noqa: BLE001 — any transport failure is transient
                self._park(host)
                attempts.append(f"{host}: {type(exc).__name__}: {exc}")
                self._log.debug("crypto host %s unreachable: %s", host, exc)
                continue

            if status == 200:
                try:
                    return json.loads(body.decode("utf-8", errors="replace") or "null")
                except json.JSONDecodeError as exc:
                    self._park(host)
                    attempts.append(f"{host}: HTTP 200 but invalid JSON ({exc})")
                    continue

            snippet = _body_snippet(body)
            if _is_transient_status(status):
                self._park(host, seconds=_park_seconds(status))
                attempts.append(f"{host}: HTTP {status} {snippet}")
                continue

            # Permanent for this host — remember it and keep trying the rest.
            attempts.append(f"{host}: HTTP {status} {snippet}")
            permanent = DataSourceError(
                f"{host}{path} rejected the request: HTTP {status} {snippet}"
            )

        detail = "; ".join(attempts) or "no host attempted"
        if permanent is not None:
            raise permanent
        raise DataSourceTransientError(
            "crypto REST request failed on all hosts "
            f"({len(candidates)} tried) for {path}: {detail}"
        )

    def probe(
        self,
        path: str = "/",
        *,
        hosts: Sequence[str] | None = None,
        timeout_s: float = 8.0,
    ) -> bool:
        """Cheap reachability check; never raises.

        A 4xx still proves the venue answered (→ reachable), but a *transient*
        failure (connection refused, timeout, 5xx, rate limit) must return False.
        ``DataSourceTransientError`` subclasses ``DataSourceError``, so the
        transient case has to be handled first — otherwise an unreachable venue
        looks "connected" (caught by the unit tests).
        """
        try:
            self.get_json(path, hosts=hosts, timeout_s=timeout_s)
            return True
        except DataSourceTransientError:
            return False
        except DataSourceError:
            return True  # answered with a permanent error → reachable
        except Exception:  # noqa: BLE001
            return False


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_transient_status(status: int) -> bool:
    # 429 = rate limited, 418 = Binance IP ban, 5xx = upstream problem.
    return status in (408, 418, 425, 429) or 500 <= status < 600


def _park_seconds(status: int) -> float:
    return 60.0 if status in (418, 429) else _HOST_COOLDOWN_S


def _body_snippet(body: bytes, limit: int = 200) -> str:
    text = " ".join(body.decode("utf-8", errors="replace").split())
    return text[:limit]
