"""A-share K-line data via East Money (东方财富) built-in HTTP API."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from pa_agent.data.ashare_common import (
    PRESET_SYMBOLS as _PRESET_SYMBOLS,
    ashare_head_bar_live as _ashare_head_bar_live,
    ashare_session_open as _ashare_session_open,
    ashare_trading_day as _ashare_trading_day,
    cn_now as _cn_now,
    ensure_today_forming_daily_bar,
    index_symbol_for_api as _index_symbol_for_api,
    is_index_symbol,
    merge_ohlcv as _merge_ohlcv,
    normalize_ashare_symbol,
    resample_rows_to_4h as _resample_rows_to_4h,
    row_time_to_ts_ms as _row_time_to_ts_ms,
    rows_to_kline_bars as _rows_to_kline_bars,
)
from pa_agent.data.base import DataSource, DataSourceTransientError, KlineBar
from pa_agent.data.refresh_policy import snapshot_cache_ttl_s
from pa_agent.data.eastmoney_baostock import (
    _BaostockSession,
    eastmoney_rolling_cap,
    fetch_daily_history_baostock,
    fetch_minute_history_baostock,
    needs_baostock_history,
)
from pa_agent.data.eastmoney_client import (
    EastMoneyTransientError,
    fetch_hot_stock_codes,
    fetch_index_daily,
    fetch_spot_price,
    fetch_stock_daily,
    fetch_stock_minute,
    fetch_stock_period_recent,
    is_transient_http_error,
)
from pa_agent.data.kline_adjust import get_kline_adjust

logger = logging.getLogger(__name__)

_PRESET_SYMBOLS: tuple[str, ...] = (
    "000001",
    "600519",
    "600345",
    "000858",
    "300750",
    "601318",
    "sh000300",
    "sz399006",
)

_SUPPORTED_TIMEFRAMES: tuple[str, ...] = (
    "1m",
    "5m",
    "15m",
    "30m",
    "1h",
    "4h",
    "1d",
    "1w",
    "1M",
)

_TF_MINUTE_PERIOD: dict[str, str] = {
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
}


def _em_rows_to_bars_asc(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        ts = _row_time_to_ts_ms(row["time"])
        out.append(
            {
                "ts_open": ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0) or 0.0),
                "amount": float(row.get("amount", 0.0) or 0.0),
                "pct_chg": row.get("pct_chg"),
            }
        )
    return out


class EastMoneySource(DataSource):
    """Poll East Money push2his APIs for A-share OHLCV (no AkShare)."""

    def __init__(self) -> None:
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._snap_cache_n: int = 0
        self._snap_cache_ts: float = 0.0
        self._snap_cache_bars: list[KlineBar] = []
        self._latest_order_book: Any | None = None
        self._latest_order_book_ts_ms: int | None = None
        self._latest_trades: list[Any] = []
        self._latest_trades_ts_ms: int | None = None

    def connect(self) -> None:
        self._connected = True
        logger.info("EastMoneySource connected")

    def disconnect(self) -> None:
        _BaostockSession.logout()
        self._connected = False
        logger.info("EastMoneySource disconnected")

    def list_symbols(self) -> list[str]:
        """Preset symbols only — never blocks UI on network (see ``fetch_hot_symbols``)."""
        return list(_PRESET_SYMBOLS)

    def fetch_hot_symbols(self, *, limit: int = 30) -> list[str]:
        """Optional East Money hot list; safe to call off the UI thread or after show."""
        hot = fetch_hot_stock_codes(limit=limit)
        merged: list[str] = []
        for code in (*_PRESET_SYMBOLS, *hot):
            if code and code not in merged:
                merged.append(code)
        return merged

    def supported_timeframes(self) -> list[str]:
        return list(_SUPPORTED_TIMEFRAMES)

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"Unsupported timeframe: {timeframe!r}. "
                f"Use one of {list(_SUPPORTED_TIMEFRAMES)}"
            )
        code = normalize_ashare_symbol(symbol)
        if not code:
            raise ValueError("A股代码无效，请输入 6 位数字（如 600519）或指数 sh000300")
        if code != self._symbol or timeframe != self._timeframe:
            self._snap_cache_bars = []
            self._snap_cache_n = 0
            self._latest_order_book = None
            self._latest_order_book_ts_ms = None
            self._latest_trades = []
            self._latest_trades_ts_ms = None
        self._symbol = code
        self._timeframe = timeframe
        logger.info("EastMoneySource subscribed: %s %s", code, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        self._snap_cache_bars = []
        self._snap_cache_n = 0
        self._latest_order_book = None
        self._latest_order_book_ts_ms = None
        self._latest_trades = []
        self._latest_trades_ts_ms = None
        logger.info("EastMoneySource unsubscribed")

    def latest_order_book(self) -> Any | None:
        """Return the order book cached by the latest background snapshot."""
        return self._latest_order_book

    def latest_trades(self) -> list[Any]:
        """Return recent tick trades cached by the latest background snapshot."""
        return list(self._latest_trades)

    def latest_market_context(self) -> dict[str, Any] | None:
        """Return JSON-safe East Money order-book and tick data for AI prompts."""
        book = self._latest_order_book
        trade_values = list(self._latest_trades[-40:])
        if book is None and not trade_values:
            return None

        def _levels(items: object) -> list[dict[str, int | float]]:
            values = list(items) if isinstance(items, (list, tuple)) else []
            return [
                {
                    "level": index,
                    "price": float(level.price),
                    "volume_lots": int(level.volume),
                }
                for index, level in enumerate(values[:10], start=1)
            ]

        bids = _levels(getattr(book, "bids", None)) if book is not None else []
        asks = _levels(getattr(book, "asks", None)) if book is not None else []
        bid_total = sum(int(item["volume_lots"]) for item in bids)
        ask_total = sum(int(item["volume_lots"]) for item in asks)
        total = bid_total + ask_total
        imbalance = ((bid_total - ask_total) / total * 100.0) if total else 0.0

        trades = [
            {
                "time": str(getattr(trade, "time", "") or ""),
                "price": float(getattr(trade, "price", 0.0) or 0.0),
                "volume_lots": int(getattr(trade, "volume", 0) or 0),
                "side": str(getattr(trade, "side_hint", "") or "—"),
            }
            for trade in trade_values
        ]
        active_buy_lots = sum(
            int(trade["volume_lots"])
            for trade in trades
            if trade["side"] == "买"
        )
        active_sell_lots = sum(
            int(trade["volume_lots"])
            for trade in trades
            if trade["side"] == "卖"
        )
        neutral_lots = sum(
            int(trade["volume_lots"])
            for trade in trades
            if trade["side"] not in {"买", "卖"}
        )
        last_trade_price = float(trades[-1]["price"]) if trades else 0.0
        snapshot_ts_ms = max(
            self._latest_order_book_ts_ms or 0,
            self._latest_trades_ts_ms or 0,
        ) or None
        return {
            "provider": "eastmoney",
            "snapshot_ts_ms": snapshot_ts_ms,
            "trades_snapshot_ts_ms": self._latest_trades_ts_ms,
            "code": str(
                (getattr(book, "code", "") if book is not None else "")
                or self._symbol
            ),
            "name": str(getattr(book, "name", "") if book is not None else ""),
            "last_price": float(
                (getattr(book, "price", 0.0) if book is not None else 0.0)
                or last_trade_price
            ),
            "pct_chg": float(
                getattr(book, "pct_chg", 0.0) if book is not None else 0.0
            ),
            "volume_unit": "手",
            "bids": bids,
            "asks": asks,
            "bid_total_lots": bid_total,
            "ask_total_lots": ask_total,
            "order_imbalance_pct": round(imbalance, 2),
            "depth_levels": int(
                (getattr(book, "depth_levels", 5) if book is not None else 5)
                or 5
            ),
            "depth_source": str(
                getattr(book, "depth_source", "") if book is not None else ""
            ),
            "recent_trades": trades,
            "trade_count": len(trades),
            "active_buy_lots": active_buy_lots,
            "active_sell_lots": active_sell_lots,
            "neutral_trade_lots": neutral_lots,
            "active_net_lots": active_buy_lots - active_sell_lots,
        }

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("东方财富数据源未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("东方财富未订阅品种/周期")

        now = time.monotonic()
        cache_ttl = snapshot_cache_ttl_s(self._timeframe)
        if (
            self._snap_cache_bars
            and self._snap_cache_n == n
            and now - self._snap_cache_ts < cache_ttl
        ):
            return list(self._snap_cache_bars)

        fetch_n = max(n + 5, 30)
        try:
            rows_asc = self._fetch_history(self._symbol, self._timeframe, fetch_n)
        except EastMoneyTransientError as exc:
            raise DataSourceTransientError(str(exc)) from exc
        except DataSourceTransientError:
            raise
        except Exception as exc:
            logger.warning("EastMoney fetch failed: %s", exc)
            if is_transient_http_error(exc):
                raise DataSourceTransientError(
                    "东方财富网络连接中断，请稍后重试（可多点击一次「获取数据」）"
                ) from exc
            raise DataSourceTransientError(f"东方财富拉取失败: {exc}") from exc

        if not rows_asc:
            raise DataSourceTransientError(
                f"东方财富未返回数据: {self._symbol} {self._timeframe}"
            )

        self._latest_order_book = None
        self._latest_order_book_ts_ms = None
        self._latest_trades = []
        self._latest_trades_ts_ms = None
        if not is_index_symbol(self._symbol):
            from pa_agent.data.eastmoney_client import (
                fetch_stock_order_book,
                fetch_stock_tick_details,
            )

            self._latest_order_book = fetch_stock_order_book(self._symbol)
            if self._latest_order_book is not None:
                self._latest_order_book_ts_ms = int(time.time() * 1000)
            self._latest_trades = fetch_stock_tick_details(self._symbol, tail=40)
            if self._latest_trades:
                self._latest_trades_ts_ms = int(time.time() * 1000)

        if self._timeframe == "1d" and _ashare_trading_day():
            book = self._latest_order_book
            spot = (
                float(book.price)
                if book is not None and book.price > 0
                else fetch_spot_price(self._symbol)
            )
            ensure_today_forming_daily_bar(
                rows_asc,
                symbol=self._symbol,
                spot_price=spot,
                session_open=float(book.open) if book else 0.0,
                session_high=float(book.high) if book else 0.0,
                session_low=float(book.low) if book else 0.0,
                session_volume_lots=float(book.volume) if book else 0.0,
                session_amount=float(book.amount) if book else 0.0,
            )
        if self._timeframe == "1d" and _ashare_trading_day():
            self._apply_spot_to_forming(rows_asc)
        elif _ashare_session_open():
            self._apply_spot_to_forming(rows_asc)

        rows_newest = list(reversed(rows_asc[-fetch_n:]))
        for i, row in enumerate(rows_newest):
            row["closed"] = not (i == 0 and _ashare_head_bar_live(self._timeframe))

        bars = _rows_to_kline_bars(rows_newest, n)
        self._snap_cache_n = n
        self._snap_cache_ts = time.monotonic()
        self._snap_cache_bars = list(bars)
        return bars

    def _fetch_history(self, symbol: str, timeframe: str, n: int) -> list[dict[str, Any]]:
        if timeframe in ("1d", "1w", "1M"):
            return self._fetch_daily(symbol, n, timeframe=timeframe)
        if timeframe == "1m":
            if is_index_symbol(symbol):
                cap = eastmoney_rolling_cap("1")
                fetch_n = min(n, cap) if cap else n
            else:
                fetch_n = n
            rows = self._fetch_minute(symbol, "1", fetch_n + 8)
            return rows[-fetch_n:]
        if timeframe == "4h":
            want = n * 4 + 8
            if is_index_symbol(symbol):
                cap = eastmoney_rolling_cap("60") // 4
                want_n = min(n, cap)
                rows = self._fetch_minute(symbol, "60", want_n * 4 + 8)
                return _resample_rows_to_4h(rows)[-want_n:]
            if needs_baostock_history(timeframe, "60", want):
                logger.info(
                    "EastMoney rolling window exceeded (4h n=%d); using Baostock history",
                    n,
                )
                return fetch_minute_history_baostock(symbol, "4h", n)
            rows = self._fetch_minute(symbol, "60", want)
            return _resample_rows_to_4h(rows)[-n:]
        period = _TF_MINUTE_PERIOD.get(timeframe)
        if period:
            if is_index_symbol(symbol):
                cap = eastmoney_rolling_cap(period)
                fetch_n = min(n, cap)
                rows = self._fetch_minute(symbol, period, fetch_n)
                return rows[-fetch_n:]
            if needs_baostock_history(timeframe, period, n):
                logger.info(
                    "EastMoney rolling window exceeded (%s n=%d); using Baostock history",
                    timeframe,
                    n,
                )
                return fetch_minute_history_baostock(symbol, timeframe, n)
            rows = self._fetch_minute(symbol, period, n)
            return rows[-n:]
        return []

    def _fetch_daily(
        self,
        symbol: str,
        n: int,
        *,
        timeframe: str = "1d",
    ) -> list[dict[str, Any]]:
        adjust = get_kline_adjust()
        if is_index_symbol(symbol):
            end = _cn_now().strftime("%Y%m%d")
            cal_days = min(max(int(n * 1.45) + 25, 75), 420)
            start = (_cn_now() - timedelta(days=cal_days)).strftime("%Y%m%d")
            try:
                idx = _index_symbol_for_api(symbol)
                raw = fetch_index_daily(idx, start_date=start, end_date=end)
                return _em_rows_to_bars_asc(raw)[-(n + 5) :]
            except EastMoneyTransientError as exc:
                raise DataSourceTransientError(str(exc)) from exc

        if timeframe in ("1w", "1M"):
            code = normalize_ashare_symbol(symbol)
            raw = fetch_stock_period_recent(
                code, timeframe=timeframe, n=n + 5, adjust=adjust
            )
            return _em_rows_to_bars_asc(raw)[-(n + 5) :]

        # A-share stocks: Baostock daily is faster and avoids East Money curl(56) drops.
        if adjust == "qfq":
            try:
                return fetch_daily_history_baostock(symbol, n + 5)
            except Exception as bs_exc:
                logger.info(
                    "Baostock daily failed for %s (%s), trying East Money",
                    symbol,
                    bs_exc,
                )
        end = _cn_now().strftime("%Y%m%d")
        cal_days = min(max(int(n * 1.45) + 25, 75), 420)
        start = (_cn_now() - timedelta(days=cal_days)).strftime("%Y%m%d")
        try:
            code = normalize_ashare_symbol(symbol)
            raw = fetch_stock_daily(
                code, start_date=start, end_date=end, adjust=adjust
            )
            return _em_rows_to_bars_asc(raw)[-(n + 5) :]
        except EastMoneyTransientError as exc:
            raise DataSourceTransientError(
                "K线拉取失败（Baostock 与东方财富均不可用），请检查网络后重试"
            ) from exc

    def _fetch_minute(self, symbol: str, period: str, n: int) -> list[dict[str, Any]]:
        end_dt = _cn_now()
        days = max(30, (n // 4) + 15)
        start_dt = end_dt - timedelta(days=days)
        start_s = start_dt.strftime("%Y-%m-%d 09:30:00")
        end_s = end_dt.strftime("%Y-%m-%d 15:00:00")
        code = normalize_ashare_symbol(symbol)
        idx = is_index_symbol(symbol)
        adjust = get_kline_adjust()
        raw = self._call_with_retries(
            lambda: fetch_stock_minute(
                _index_symbol_for_api(symbol) if idx else code,
                period=period,
                start_date=start_s,
                end_date=end_s,
                adjust=adjust,
                is_index=idx,
            )
        )
        return _em_rows_to_bars_asc(raw)[-(n + 8) :]

    @staticmethod
    def _call_with_retries(fn: Any, *, attempts: int = 2) -> Any:
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return fn()
            except EastMoneyTransientError as exc:
                last_exc = exc
                if i + 1 >= attempts:
                    break
                time.sleep(min(1.5, 0.4 + i * 0.4))
                logger.debug("EastMoney retry %d/%d: %s", i + 2, attempts, exc)
        assert last_exc is not None
        raise last_exc

    def _apply_spot_to_forming(self, rows_asc: list[dict[str, Any]]) -> None:
        if not rows_asc:
            return
        daily = self._timeframe == "1d"
        if daily:
            if not _ashare_trading_day():
                return
        elif not _ashare_session_open():
            return
        last = rows_asc[-1]

        if daily:
            from pa_agent.data.ashare_common import apply_session_quote_to_forming_row

            book = self._latest_order_book
            if book is not None and book.price > 0:
                apply_session_quote_to_forming_row(
                    last,
                    price=book.price,
                    open_=book.open,
                    high=book.high,
                    low=book.low,
                    volume=float(book.volume),
                    amount=float(book.amount),
                    prev_close=book.prev_close,
                    daily=True,
                    volume_lots=True,
                    symbol=self._symbol,
                )
                return
            price = fetch_spot_price(self._symbol)
            if price is None:
                return
            apply_session_quote_to_forming_row(last, price=price, daily=True)
            return

        book = self._latest_order_book
        price = (
            float(book.price)
            if book is not None and book.price > 0
            else fetch_spot_price(self._symbol)
        )
        if price is None:
            return
        from pa_agent.data.ashare_common import apply_session_quote_to_forming_row

        apply_session_quote_to_forming_row(last, price=price, daily=False)
