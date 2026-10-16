"""Binance public market-data source (spot + USDⓈ-M perpetual swap).

No API key is required: only public ``/api/v3`` (spot) and ``/fapi/v1``
(futures) market-data endpoints are used. The tool never places orders.

Symbols
-------
* Spot — ``BTCUSDT`` (the native Binance concatenated form; ``BTC/USDT`` and
  ``BTC-USDT`` are accepted and normalised).
* USDⓈ-M perpetual — append ``.perp``: ``BTCUSDT.perp`` (routed to ``fapi``).

Hosts
-----
``api.binance.com`` is commonly unreachable from mainland China while Binance's
official market-data domain ``data-api.binance.vision`` still answers, so the
spot host list tries both (see :mod:`pa_agent.data.crypto_rest` for the failover
and cooldown rules). Behind a proxy, export ``HTTPS_PROXY`` — httpx honours it.

Candle payload (``GET /api/v3/klines``), oldest first, 12 fields:
``[openTime, open, high, low, close, volume, closeTime, quoteAssetVolume,
tradeCount, takerBuyBase, takerBuyQuote, ignore]``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from pa_agent.data.base import (
    DataSource,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)
from pa_agent.data.crypto_rest import DEFAULT_TIMEOUT_S, CryptoRestClient
from pa_agent.data.crypto_symbols import (
    binance_symbol_is_perp,
    normalize_binance_symbol,
    strip_binance_perp_suffix,
)

logger = logging.getLogger(__name__)

#: Our timeframe strings → Binance ``interval`` values (identical spellings).
TF_MAP: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1d",
    "3d": "3d",
    "1w": "1w",
    "1M": "1M",
}

SPOT_HOSTS: tuple[str, ...] = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
)

PERP_HOSTS: tuple[str, ...] = ("https://fapi.binance.com",)

SPOT_KLINES_PATH = "/api/v3/klines"
SPOT_TIME_PATH = "/api/v3/time"
PERP_KLINES_PATH = "/fapi/v1/klines"
PERP_TIME_PATH = "/fapi/v1/time"

#: Binance caps a single klines request at 1000 bars.
_MAX_LIMIT = 1000
_MAX_PAGES = 8

#: Ignore a serverTime offset beyond a day (bogus clock / captive portal stub).
_MAX_CLOCK_OFFSET_MS = 24 * 60 * 60 * 1000

#: Popular USDT pairs offered in the symbol dropdown (a venue-wide list would
#: be thousands of entries; the UI fills a combo box).
DEFAULT_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "TONUSDT",
    "DOTUSDT",
    "TRXUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "NEARUSDT",
    "APTUSDT",
    "ARBUSDT",
    "OPUSDT",
    "SUIUSDT",
    "PEPEUSDT",
    "BTCUSDT.perp",
    "ETHUSDT.perp",
    "SOLUSDT.perp",
    "BNBUSDT.perp",
    "XRPUSDT.perp",
    "DOGEUSDT.perp",
)


def is_perp_symbol(symbol: str) -> bool:
    """True when *symbol* selects the USDⓈ-M perpetual endpoint."""
    return binance_symbol_is_perp(normalize_binance_symbol(symbol) or symbol)


class BinanceSource(DataSource):
    """K-line data from Binance public REST API (spot or USDⓈ-M perpetual)."""

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: CryptoRestClient | None = None,
        spot_hosts: Sequence[str] = SPOT_HOSTS,
        perp_hosts: Sequence[str] = PERP_HOSTS,
    ) -> None:
        self._timeout_s = float(timeout_s)
        # Spot and USDⓈ-M are served by different host sets (the public mirror
        # has /api/v3 but no /fapi), so each market keeps its own list.
        self._spot_hosts: tuple[str, ...] = tuple(spot_hosts)
        self._perp_hosts: tuple[str, ...] = tuple(perp_hosts)
        self._client = client or CryptoRestClient(
            self._spot_hosts + self._perp_hosts, timeout_s=self._timeout_s, logger_=logger
        )
        self._symbol = ""
        self._api_symbol = ""
        self._timeframe = ""
        self._perp = False
        self._connected = False
        #: serverTime - local wall clock (ms), refreshed opportunistically.
        self._clock_offset_ms: int | None = None
        self._clock_refreshed_mono: float = 0.0

    def _market_hosts(self) -> tuple[str, ...]:
        return self._perp_hosts if self._perp else self._spot_hosts

    def _klines_path(self) -> str:
        return PERP_KLINES_PATH if self._perp else SPOT_KLINES_PATH

    def _time_path(self) -> str:
        return PERP_TIME_PATH if self._perp else SPOT_TIME_PATH

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        ok = self._client.probe(
            SPOT_TIME_PATH, hosts=self._spot_hosts, timeout_s=float(self._timeout_s)
        )
        if not ok:
            ok = self._client.probe(
                PERP_TIME_PATH, hosts=self._perp_hosts, timeout_s=float(self._timeout_s)
            )
        if not ok:
            raise DataSourceTransientError(
                "Binance 公共行情接口不可达（现货主机 "
                f"{len(self._spot_hosts)} 个、永续主机 {len(self._perp_hosts)} 个均已尝试）。"
                "如在中国大陆：现货可用官方行情域 data-api.binance.vision；"
                "永续 (fapi.binance.com) 无公共镜像，需配置代理——"
                "设置环境变量 HTTPS_PROXY=http://127.0.0.1:端口 后重启。"
            )
        self._connected = True
        self._refresh_clock_offset()
        logger.info(
            "BinanceSource connected (transport=%s, host=%s)",
            self._client.transport_name,
            self._client.preferred_host,
        )

    def disconnect(self) -> None:
        self._connected = False
        logger.info("BinanceSource disconnected")

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        return list(DEFAULT_SYMBOLS)

    def supported_timeframes(self) -> list[str]:
        return list(TF_MAP.keys())

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in TF_MAP:
            raise ValueError(f"Unsupported timeframe: {timeframe!r}. Use one of {list(TF_MAP)}")
        api_symbol = normalize_binance_symbol(symbol)
        if not api_symbol:
            raise ValueError(
                f"无法识别的币安交易对: {symbol!r}；示例 BTCUSDT、BTC/USDT、ETHUSDT.perp"
            )
        self._symbol = symbol
        self._api_symbol = api_symbol
        self._perp = binance_symbol_is_perp(api_symbol)
        self._timeframe = timeframe
        logger.info(
            "BinanceSource subscribed: %s (%s, %s)",
            symbol,
            "USDⓈ-M 永续" if self._perp else "现货",
            timeframe,
        )

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._api_symbol = ""
        self._timeframe = ""
        logger.info("BinanceSource unsubscribed")

    # ── Data fetch ────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """Return *n* bars newest-first; ``bars[0]`` is the forming bar."""
        return self._fetch_newest_first(n, end_time_ms=None)

    def historical_snapshot(self, n: int, end_time_ms: int) -> list[KlineBar]:
        """Return *n* bars newest-first, ending **at or before** *end_time_ms*.

        Walk-forward replay helper: the request carries ``endTime``, so no bar
        after *end_time_ms* is ever fetched — a replay physically cannot see the
        future. Every returned bar is already closed, hence ``seq`` starts at 1.
        """
        return self._fetch_newest_first(n, end_time_ms=int(end_time_ms))

    def _fetch_newest_first(self, n: int, *, end_time_ms: int | None) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("Not connected — call connect() first")
        if not self._api_symbol or not self._timeframe:
            raise DataSourceTransientError("Not subscribed — call subscribe() first")

        want = max(1, int(n))
        path = self._klines_path()
        hosts = self._market_hosts()
        interval = TF_MAP[self._timeframe]

        raw_pages: list[list] = []
        end_time: int | None = end_time_ms
        fetched = 0
        for _ in range(_MAX_PAGES):
            limit = min(_MAX_LIMIT, want - fetched)
            if limit <= 0:
                break
            params: dict[str, object] = {
                "symbol": strip_binance_perp_suffix(self._api_symbol),
                "interval": interval,
                "limit": limit,
            }
            if end_time is not None:
                params["endTime"] = end_time
            page = self._client.get_json(path, params, timeout_s=self._timeout_s, hosts=hosts)
            if not isinstance(page, list) or not page:
                break
            raw_pages.append(page)
            fetched += len(page)
            # Binance returns oldest-first; walk further back from the oldest bar.
            end_time = int(page[0][0]) - 1
            if len(page) < limit:
                break

        if not raw_pages:
            raise DataSourceTransientError(
                f"币安返回空 K 线: {strip_binance_perp_suffix(self._api_symbol)} {interval}"
            )

        self._refresh_clock_offset()
        now_ms = self._now_ms()

        bars: list[KlineBar] = []
        seen: set[int] = set()
        # ``raw_pages`` is collected newest-window first; inside a page Binance
        # returns oldest-first, so reverse within the page but keep the page
        # order as fetched (reversing the pages too put the oldest window at the
        # front — caught by the deep-history unit test).
        for page in raw_pages:
            for record in reversed(page):
                bar = _parse_kline(record, now_ms=now_ms)
                if bar is None or int(bar.ts_open) in seen:
                    continue
                seen.add(int(bar.ts_open))
                bars.append(bar)
                if len(bars) >= want:
                    break
            if len(bars) >= want:
                break

        if not bars:
            raise DataSourceTransientError("币安 K 线解析结果为空")

        # Newest bar keeps closed=False (still forming) → seq 0, like the other
        # sources; every older bar is numbered 1..N.
        return _assign_seq(bars)

    # ── Clock ─────────────────────────────────────────────────────────────────

    def server_time_ms(self) -> int | None:
        """Server clock as local wall-clock equivalent, from the cached offset.

        Deliberately *non-blocking*: it never issues a request, so callers on
        the GUI thread (``reference_now_ms``) cannot stall the UI. The offset is
        refreshed inside :meth:`latest_snapshot`, which runs on the refresh
        worker thread.
        """
        if self._clock_offset_ms is None:
            return None
        return int(time.time() * 1000) + int(self._clock_offset_ms)

    def _now_ms(self) -> int:
        server = self.server_time_ms()
        return server if server is not None else int(time.time() * 1000)

    def _refresh_clock_offset(self, *, min_interval_s: float = 60.0) -> None:
        """Best-effort sync of the local clock against Binance serverTime."""
        mono = time.monotonic()
        if self._clock_offset_ms is not None and mono - self._clock_refreshed_mono < min_interval_s:
            return
        path = self._time_path()
        try:
            payload = self._client.get_json(
                path, hosts=self._market_hosts(), timeout_s=min(self._timeout_s, 8.0)
            )
        except Exception as exc:  # noqa: BLE001 — clock sync must never break a fetch
            logger.debug("Binance serverTime unavailable: %s", exc)
            return
        server_ms = payload.get("serverTime") if isinstance(payload, dict) else None
        try:
            offset = int(server_ms) - int(time.time() * 1000)
        except (TypeError, ValueError):
            return
        # Sanity guard: a bogus serverTime (captive portal, proxy error page, a
        # test stub) would otherwise make every bar look "still forming".
        if abs(offset) > _MAX_CLOCK_OFFSET_MS:
            logger.warning(
                "Binance serverTime offset %s ms is implausible — ignoring it",
                offset,
            )
            return
        self._clock_offset_ms = offset
        self._clock_refreshed_mono = mono


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_kline(record: list, *, now_ms: int) -> KlineBar | None:
    """Parse one Binance kline array into a KlineBar (closed flag resolved)."""
    if not isinstance(record, (list, tuple)) or len(record) < 8:
        return None
    try:
        ts_open = int(record[0])
        close_time = int(record[6])
        volume = float(record[5])
        amount = float(record[7])
    except (TypeError, ValueError):
        return None

    closed = close_time < now_ms
    return normalize_kline_bar(
        KlineBar(
            seq=0 if not closed else 1,
            ts_open=ts_open,
            open=float(record[1]),
            high=float(record[2]),
            low=float(record[3]),
            close=float(record[4]),
            volume=volume,
            amount=amount,
            closed=closed,
        )
    )


def _assign_seq(bars: list[KlineBar]) -> list[KlineBar]:
    """Number bars newest-first: forming bar → seq 0, then 1..N."""
    out: list[KlineBar] = []
    idx = 0
    for bar in bars:
        if not bar.closed:
            out.append(bar)
            continue
        idx += 1
        if bar.seq == idx:
            out.append(bar)
        else:
            out.append(
                normalize_kline_bar(
                    KlineBar(
                        seq=idx,
                        ts_open=bar.ts_open,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                        amount=bar.amount,
                        pct_chg=bar.pct_chg,
                        closed=True,
                    )
                )
            )
    return out
