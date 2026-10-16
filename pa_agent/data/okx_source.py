"""OKX public market-data source (spot + USDT-margined perpetual swap).

No API key is required: only public ``/api/v5/market`` endpoints are used.
The tool never places orders.

Symbols (OKX ``instId``)
------------------------
* Spot — ``BTC-USDT``.
* Perpetual swap — ``BTC-USDT-SWAP``.

Concatenated input (``BTCUSDT``), slashed input (``BTC/USDT``) and the Binance
``.perp`` marker are all normalised to the canonical ``instId``
(:mod:`pa_agent.data.crypto_symbols`).

Endpoints
---------
* ``GET /api/v5/market/candles`` — most recent candles, newest first, ≤300/req.
* ``GET /api/v5/market/history-candles`` — older candles, ≤100/req (paged with
  the ``after`` cursor for deep history such as ``analysis_bar_count`` > 300).
* ``GET /api/v5/public/time`` — server clock used for the countdown helper.

Candle row (newest first), 9 fields::

    [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]

``confirm`` is ``"1"`` once the candle is closed and ``"0"`` while it is still
forming. Volume units differ by instrument: spot ``vol`` is base-currency volume
and ``volCcyQuote`` the quote-currency turnover; for swaps ``vol`` counts
contracts, so base-currency volume comes from ``volCcy``.

.. note::
   ``okx.com`` is unreachable from some networks (mainland China). The unit
   tests for this source run against recorded fixture payloads that follow the
   documented schema; the live path needs ``HTTPS_PROXY`` (httpx honours it) or
   an unblocked network — see ``tools/smoke_crypto_sources.py``.
"""

from __future__ import annotations

import logging
import time

from pa_agent.data.base import (
    DataSource,
    DataSourceError,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)
from pa_agent.data.crypto_rest import DEFAULT_TIMEOUT_S, CryptoRestClient
from pa_agent.data.crypto_symbols import (
    normalize_okx_inst_id,
    okx_inst_is_swap,
    okx_inst_to_display,
)

logger = logging.getLogger(__name__)

#: Our timeframe strings → OKX ``bar`` values. OKX spells minutes lowercase and
#: hours/days/weeks/months uppercase, so ``1m`` (minute) and ``1M`` (month) stay
#: distinct.
TF_MAP: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "6h": "6H",
    "12h": "12H",
    "1d": "1D",
    "1w": "1W",
    "1M": "1M",
}

HOSTS: tuple[str, ...] = ("https://www.okx.com", "https://aws.okx.com")

CANDLES_PATH = "/api/v5/market/candles"
HISTORY_CANDLES_PATH = "/api/v5/market/history-candles"
TIME_PATH = "/api/v5/public/time"

#: OKX limits: 300 rows for /candles, 100 rows for /history-candles.
_MAX_RECENT = 300
_MAX_HISTORY = 100
_MAX_PAGES = 10

#: Business codes that mean "this request can never succeed".
_PERMANENT_ERROR_CODES = frozenset(
    {
        "51000",  # parameter error
        "51001",  # instrument ID does not exist
        "51008",  # insufficient balance (should not happen for public data)
        "51014",  # instrument not listed
        "51101",  # bar is invalid for this instrument
    }
)

DEFAULT_SYMBOLS: tuple[str, ...] = (
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "BNB-USDT",
    "XRP-USDT",
    "DOGE-USDT",
    "ADA-USDT",
    "AVAX-USDT",
    "LINK-USDT",
    "TON-USDT",
    "DOT-USDT",
    "TRX-USDT",
    "LTC-USDT",
    "BCH-USDT",
    "NEAR-USDT",
    "APT-USDT",
    "ARB-USDT",
    "OP-USDT",
    "SUI-USDT",
    "PEPE-USDT",
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "BNB-USDT-SWAP",
    "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP",
)


class OkxSource(DataSource):
    """K-line data from OKX public REST API (spot or USDT perpetual swap)."""

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: CryptoRestClient | None = None,
    ) -> None:
        self._timeout_s = float(timeout_s)
        self._client = client or CryptoRestClient(HOSTS, timeout_s=self._timeout_s, logger_=logger)
        self._symbol = ""
        self._inst_id = ""
        self._timeframe = ""
        self._swap = False
        self._connected = False
        self._clock_offset_ms: int | None = None
        self._clock_refreshed_mono: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        if not self._client.probe(TIME_PATH, timeout_s=float(self._timeout_s)):
            raise DataSourceTransientError(
                "OKX 公共行情接口不可达（已尝试 "
                f"{', '.join(self._client.hosts)}）。"
                "okx.com 在部分网络被拦截，请配置代理：设置环境变量 "
                "HTTPS_PROXY=http://127.0.0.1:端口 后重启。"
            )
        self._connected = True
        self._refresh_clock_offset()
        logger.info(
            "OkxSource connected (transport=%s, host=%s)",
            self._client.transport_name,
            self._client.preferred_host,
        )

    def disconnect(self) -> None:
        self._connected = False
        logger.info("OkxSource disconnected")

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        return list(DEFAULT_SYMBOLS)

    def supported_timeframes(self) -> list[str]:
        return list(TF_MAP.keys())

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in TF_MAP:
            raise ValueError(f"Unsupported timeframe: {timeframe!r}. Use one of {list(TF_MAP)}")
        inst_id = normalize_okx_inst_id(symbol)
        if not inst_id:
            raise ValueError(
                f"无法识别的 OKX 交易对: {symbol!r}；示例 BTC-USDT、BTCUSDT、" "ETH-USDT-SWAP"
            )
        self._symbol = symbol
        self._inst_id = inst_id
        self._swap = okx_inst_is_swap(inst_id)
        self._timeframe = timeframe
        logger.info(
            "OkxSource subscribed: %s (%s, %s)",
            okx_inst_to_display(inst_id),
            "永续" if self._swap else "现货",
            timeframe,
        )

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._inst_id = ""
        self._timeframe = ""
        logger.info("OkxSource unsubscribed")

    # ── Data fetch ────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """Return *n* bars newest-first; ``bars[0]`` is the forming bar."""
        if not self._connected:
            raise DataSourceTransientError("Not connected — call connect() first")
        if not self._inst_id or not self._timeframe:
            raise DataSourceTransientError("Not subscribed — call subscribe() first")

        want = max(1, int(n))
        bar = TF_MAP[self._timeframe]

        rows: list[list] = []
        seen: set[int] = set()
        after: int | None = None
        for page_index in range(_MAX_PAGES):
            remaining = want - len(rows)
            if remaining <= 0:
                break
            if page_index == 0:
                limit = min(_MAX_RECENT, remaining)
                path = CANDLES_PATH
            else:
                limit = min(_MAX_HISTORY, remaining)
                path = HISTORY_CANDLES_PATH
            params: dict[str, object] = {
                "instId": self._inst_id,
                "bar": bar,
                "limit": limit,
            }
            if after is not None:
                params["after"] = after
            data = self._get_rows(path, params)
            if not data:
                break

            fresh = [r for r in data if _row_ts(r) not in seen]
            for row in fresh:
                seen.add(_row_ts(row))
            rows.extend(fresh)
            if not fresh or len(data) < limit:
                break
            # ``after`` = go back in time from the oldest row of this page.
            after = min(_row_ts(r) for r in fresh)

        if not rows:
            raise DataSourceTransientError(f"OKX 返回空 K 线: {self._inst_id} {bar}")

        # Caches the venue clock for bar_close_wait; ``confirm`` is authoritative
        # for the closed flag, so no local wall clock is needed here.
        self._refresh_clock_offset()

        bars: list[KlineBar] = []
        for row in _dedupe_newest_first(rows):
            parsed = _parse_candle(row, swap=self._swap)
            if parsed is None:
                continue
            if not parsed.closed:
                # Sanity: an unconfirmed candle can only be the newest one. The
                # API is authoritative, but a stale ``confirm=0`` from a halted
                # market must not be trusted forever (see bar_close_wait).
                parsed = normalize_kline_bar(
                    KlineBar(
                        seq=0,
                        ts_open=parsed.ts_open,
                        open=parsed.open,
                        high=parsed.high,
                        low=parsed.low,
                        close=parsed.close,
                        volume=parsed.volume,
                        amount=parsed.amount,
                        pct_chg=parsed.pct_chg,
                        closed=False,
                    )
                )
            bars.append(parsed)
            if len(bars) >= want:
                break

        if not bars:
            raise DataSourceTransientError("OKX K 线解析结果为空")
        return _assign_seq(bars)

    # ── Clock ─────────────────────────────────────────────────────────────────

    def server_time_ms(self) -> int | None:
        """Server clock (local equivalent) from the cached offset — never blocking."""
        if self._clock_offset_ms is None:
            return None
        return int(time.time() * 1000) + int(self._clock_offset_ms)

    def _refresh_clock_offset(self, *, min_interval_s: float = 60.0) -> None:
        mono = time.monotonic()
        if self._clock_offset_ms is not None and mono - self._clock_refreshed_mono < min_interval_s:
            return
        try:
            payload = self._client.get_json(TIME_PATH, timeout_s=min(self._timeout_s, 8.0))
        except Exception as exc:  # noqa: BLE001
            logger.debug("OKX server time unavailable: %s", exc)
            return
        try:
            rows = payload.get("data") or []
            server_ms = int(rows[0]["ts"])
            self._clock_offset_ms = server_ms - int(time.time() * 1000)
            self._clock_refreshed_mono = mono
        except (KeyError, IndexError, TypeError, ValueError):
            return

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get_rows(self, path: str, params: dict[str, object]) -> list[list]:
        payload = self._client.get_json(path, params, timeout_s=self._timeout_s)
        if not isinstance(payload, dict):
            raise DataSourceTransientError(f"OKX 返回了非 JSON 对象: {path}")
        code = str(payload.get("code") or "")
        if code and code != "0":
            message = str(payload.get("msg") or "").strip()
            text = f"OKX 返回错误 code={code} msg={message or '(空)'} ({path})"
            if code in _PERMANENT_ERROR_CODES:
                raise DataSourceError(text)
            raise DataSourceTransientError(text)
        data = payload.get("data")
        return data if isinstance(data, list) else []


# ── Helpers ───────────────────────────────────────────────────────────────────


def _row_ts(row: list) -> int:
    try:
        return int(row[0])
    except (IndexError, TypeError, ValueError):
        return -1


def _dedupe_newest_first(rows: list[list]) -> list[list]:
    """Sort rows newest-first and drop duplicate timestamps (stable)."""
    valid = [r for r in rows if isinstance(r, (list, tuple)) and len(r) >= 6]
    valid.sort(key=_row_ts, reverse=True)
    out: list[list] = []
    seen: set[int] = set()
    for row in valid:
        ts = _row_ts(row)
        if ts in seen:
            continue
        seen.add(ts)
        out.append(list(row))
    return out


def _parse_candle(row: list, *, swap: bool) -> KlineBar | None:
    """Parse one OKX candle row into a KlineBar.

    ``vol``/``volCcy`` semantics depend on the instrument, so the base-currency
    volume is taken from ``vol`` for spot and from ``volCcy`` for swaps;
    ``volCcyQuote`` is the quote-currency turnover either way.
    """
    if not isinstance(row, (list, tuple)) or len(row) < 6:
        return None
    try:
        ts_open = int(row[0])
        opened = float(row[1])
        high = float(row[2])
        low = float(row[3])
        close = float(row[4])
        vol = float(row[5])
    except (TypeError, ValueError):
        return None

    vol_ccy = _safe_float(row[6]) if len(row) > 6 else None
    vol_ccy_quote = _safe_float(row[7]) if len(row) > 7 else None
    confirm = str(row[8]).strip() if len(row) > 8 else "1"
    closed = confirm != "0"

    volume = (vol_ccy if swap and vol_ccy is not None else vol) or 0.0
    amount = vol_ccy_quote if vol_ccy_quote is not None else 0.0

    return normalize_kline_bar(
        KlineBar(
            seq=0 if not closed else 1,
            ts_open=ts_open,
            open=opened,
            high=high,
            low=low,
            close=close,
            volume=float(volume),
            amount=float(amount),
            closed=closed,
        )
    )


def _safe_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
            continue
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
