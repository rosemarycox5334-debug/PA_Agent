"""A-share K-line data via East Money (东方财富) built-in HTTP API."""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Any

from pa_agent.data.ashare_common import (
    PRESET_SYMBOLS as _PRESET_SYMBOLS,
    ashare_after_close_today as _ashare_after_close_today,
    ashare_head_bar_live as _ashare_head_bar_live,
    ashare_session_open as _ashare_session_open,
    ashare_trading_day as _ashare_trading_day,
    cn_now as _cn_now,
    ensure_today_closed_daily_bar,
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
)
from pa_agent.data.eastmoney_client import (
    EastMoneyTransientError,
    fetch_hot_stock_codes,
    fetch_index_daily,
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


# ── 盘口/现价的预算与熔断 ────────────────────────────────────────────────────
# 东方财富 quote 域（push2*.eastmoney.com）在部分网络下全部超时：实测单次
# ten-depth 约 18s、quote 约 17s、整次盘口约 90s；而一根日线快照会先后调用
# 盘口 2~3 次（补当日K线 / 作用到 forming 根 / 收盘补根），一次刷新就卡 100s。
# 因此给单次调用加预算：超预算立刻熔断，改用免费备用源（腾讯）盘口，
# 其字段形状与东方财富 StockOrderBook 一致，当日 OHLCV 仍然拿得到。
_QUOTE_BUDGET_S = 4.0
_QUOTE_COOLDOWN_S = 600.0
_quote_down_until = 0.0

#: K 线拉取的单段预算（腾讯 / 东方财富）。超过即换下一个源，避免被限流时空等。
_KLINE_BUDGET_S = 8.0
#: Baostock 只作最后兜底，且同样限时（实测 94s，不能让它无限拖）。
_BAOSTOCK_BUDGET_S = 25.0


def _quote_cooling_down() -> bool:
    return time.monotonic() < _quote_down_until


def _mark_quote_down(reason: str) -> None:
    global _quote_down_until
    if not _quote_cooling_down():
        logger.info(
            "东方财富盘口接口暂不可用（%s）：%d 秒内改用免费备用源",
            reason,
            int(_QUOTE_COOLDOWN_S),
        )
    _quote_down_until = time.monotonic() + _QUOTE_COOLDOWN_S


def _call_within_budget(fn: Any, budget_s: float) -> tuple[bool, Any]:
    """在守护线程里调用 *fn*；超预算返回 ``(False, None)``，不阻塞调用方。"""
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    worker = threading.Thread(target=_run, daemon=True, name="pa-em-quote")
    worker.start()
    worker.join(budget_s)
    if worker.is_alive():
        return False, None
    if "error" in box:
        logger.debug("quote call failed: %s", box["error"])
        return True, None
    return True, box.get("value")


def _free_order_book(symbol: str) -> Any:
    """免费备用源盘口（腾讯 → zzshare），约 0.2s。"""
    from pa_agent.data.eastmoney_client import _fallback_order_book

    return _fallback_order_book(symbol)


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
        self._symbol = code
        self._timeframe = timeframe
        logger.info("EastMoneySource subscribed: %s %s", code, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        self._snap_cache_bars = []
        self._snap_cache_n = 0
        logger.info("EastMoneySource unsubscribed")

    def _order_book(self) -> Any:
        """盘口（五档）：东方财富优先；超预算/熔断时改用免费备用源。"""
        if _quote_cooling_down():
            return _free_order_book(self._symbol)
        from pa_agent.data.eastmoney_client import fetch_stock_order_book

        ok, book = _call_within_budget(
            lambda: fetch_stock_order_book(self._symbol), _QUOTE_BUDGET_S
        )
        if not ok:
            _mark_quote_down("盘口超时")
            return _free_order_book(self._symbol)
        return book

    def _spot_price(self) -> float | None:
        """最新价：东方财富优先；超预算/熔断时用免费备用源盘口价。"""
        if not _quote_cooling_down():
            from pa_agent.data.eastmoney_client import fetch_spot_price

            ok, price = _call_within_budget(
                lambda: fetch_spot_price(self._symbol), _QUOTE_BUDGET_S
            )
            if ok:
                return price
            _mark_quote_down("现价超时")
        book = _free_order_book(self._symbol)
        if book is not None and getattr(book, "price", 0) and book.price > 0:
            return float(book.price)
        return None

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

        if self._timeframe == "1d" and _ashare_trading_day():
            book = None
            if not is_index_symbol(self._symbol):
                book = self._order_book()
            spot = (
                float(book.price)
                if book is not None and book.price > 0
                else self._spot_price()
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

        # 收盘后（15:00之后）若数据源未返回当天日线，用盘口数据补一根已收盘K线
        if self._timeframe == "1d" and _ashare_after_close_today():
            self._ensure_today_closed_bar(rows_asc)

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
            rows = self._minute_rows(symbol, timeframe, "1", fetch_n + 8)
            return rows[-fetch_n:] if rows else []
        if timeframe == "4h":
            rows_60 = self._minute_rows(symbol, timeframe, "60", n * 4 + 8)
            if rows_60:
                return _resample_rows_to_4h(rows_60)[-n:]
            return []
        period = _TF_MINUTE_PERIOD.get(timeframe)
        if period:
            rows = self._minute_rows(symbol, timeframe, period, n)
            return rows[-n:] if rows else []
        return []

    def _minute_rows(
        self, symbol: str, timeframe: str, period: str, want: int
    ) -> list[dict[str, Any]]:
        """分钟线：腾讯（≤320 根）→ 东方财富 → Baostock，每段都有时间预算。

        原先东财滚动窗口不够就直接转 Baostock，而 Baostock 现在单次要 90s 上下
        （实测 4h n=60 走了 88s），所以把 Baostock 挪到最后并限时。
        """
        from pa_agent.data.ashare_fallback import TENCENT_MINUTE_MAX_BARS

        if want <= TENCENT_MINUTE_MAX_BARS:
            rows = self._try_tencent_minute(symbol, period, want)
            if rows:
                return rows
        rows = self._try_eastmoney_minute(symbol, period, want)
        if rows:
            return rows
        if is_index_symbol(symbol):
            return []
        ok, rows_bs = _call_within_budget(
            lambda: fetch_minute_history_baostock(symbol, timeframe, max(want, 1)),
            _BAOSTOCK_BUDGET_S,
        )
        if not ok:
            logger.info("Baostock %s 分钟线超预算（%.0fs），放弃", timeframe, _BAOSTOCK_BUDGET_S)
            return []
        return list(rows_bs or [])

    def _try_tencent_minute(
        self, symbol: str, period: str, want: int
    ) -> list[dict[str, Any]]:
        from pa_agent.data.ashare_fallback import tencent_minute

        ok, raw = _call_within_budget(
            lambda: tencent_minute(symbol, period=period, start_date="", end_date=""),
            _KLINE_BUDGET_S,
        )
        if not ok or not raw:
            return []
        rows = _em_rows_to_bars_asc(raw)
        # 腾讯只给最近 320 根；不够就用东财（区间更全）
        return rows if len(rows) >= min(want, 20) else []

    def _try_eastmoney_minute(
        self, symbol: str, period: str, want: int
    ) -> list[dict[str, Any]]:
        end_dt = _cn_now()
        days = max(30, (want // 4) + 15)
        start_dt = end_dt - timedelta(days=days)
        code = normalize_ashare_symbol(symbol)
        idx = is_index_symbol(symbol)
        adjust = get_kline_adjust()
        ok, raw = _call_within_budget(
            lambda: fetch_stock_minute(
                _index_symbol_for_api(symbol) if idx else code,
                period=period,
                start_date=start_dt.strftime("%Y-%m-%d 09:30:00"),
                end_date=end_dt.strftime("%Y-%m-%d 15:00:00"),
                adjust=adjust,
                is_index=idx,
            ),
            _KLINE_BUDGET_S,
        )
        if not ok:
            logger.info("东方财富 %s 分钟线超预算（%.0fs）", period, _KLINE_BUDGET_S)
            return []
        if not raw:
            return []
        return _em_rows_to_bars_asc(raw)

    def _fetch_daily(
        self,
        symbol: str,
        n: int,
        *,
        timeframe: str = "1d",
    ) -> list[dict[str, Any]]:
        """日/周/月 K 线：腾讯 → 东方财富 → Baostock，每段都有时间预算。

        实测（2026-11-02，300369 日线）：腾讯三入口 0.2~0.9s、东方财富 0.4s、
        Baostock 94.7s。Baostock 曾经最快，现在慢 200 倍，只能当最后兜底。
        """
        adjust = get_kline_adjust()
        want = n + 5

        rows = self._try_tencent_daily(symbol, timeframe, want, adjust)
        if rows:
            return rows

        rows = self._try_eastmoney_daily(symbol, timeframe, want, adjust)
        if rows:
            return rows

        if adjust == "qfq":
            rows = self._try_baostock_daily(symbol, want)
            if rows:
                return rows

        raise DataSourceTransientError(
            "K线拉取失败（腾讯、东方财富、Baostock 均不可用），请检查网络后重试"
        )

    def _try_tencent_daily(
        self, symbol: str, timeframe: str, want: int, adjust: str
    ) -> list[dict[str, Any]]:
        """腾讯日/周/月线（三入口轮换，不封 IP）。超过 640 根时交给东财。"""
        from pa_agent.data.ashare_fallback import tencent_period

        if want > 640:
            return []
        ok, raw = _call_within_budget(
            lambda: tencent_period(
                symbol, timeframe=timeframe, count=want, adjust=adjust
            ),
            _KLINE_BUDGET_S,
        )
        if not ok:
            logger.info("腾讯 K 线超预算（%.0fs），改用东方财富", _KLINE_BUDGET_S)
            return []
        if not raw:
            return []
        rows = _em_rows_to_bars_asc(raw)
        if len(rows) < min(want, 10):
            return []
        logger.debug("腾讯 K 线命中 %s %s（%d 根）", symbol, timeframe, len(rows))
        return rows[-want:]

    def _try_eastmoney_daily(
        self, symbol: str, timeframe: str, want: int, adjust: str
    ) -> list[dict[str, Any]]:
        end = _cn_now().strftime("%Y%m%d")
        cal_days = min(max(int(want * 1.45) + 25, 75), 3000)
        start = (_cn_now() - timedelta(days=cal_days)).strftime("%Y%m%d")

        if is_index_symbol(symbol):
            idx = _index_symbol_for_api(symbol)
            ok, raw = _call_within_budget(
                lambda: fetch_index_daily(idx, start_date=start, end_date=end),
                _KLINE_BUDGET_S,
            )
            if not ok or not raw:
                return []
            return _em_rows_to_bars_asc(raw)[-(want):]

        code = normalize_ashare_symbol(symbol)
        if timeframe in ("1w", "1M"):
            ok, raw = _call_within_budget(
                lambda: fetch_stock_period_recent(
                    code, timeframe=timeframe, n=want, adjust=adjust
                ),
                _KLINE_BUDGET_S,
            )
            if not ok or not raw:
                return []
            return _em_rows_to_bars_asc(raw)[-(want):]

        ok, raw = _call_within_budget(
            lambda: fetch_stock_daily(code, start_date=start, end_date=end, adjust=adjust),
            _KLINE_BUDGET_S,
        )
        if not ok:
            logger.info("东方财富 K 线超预算（%.0fs）", _KLINE_BUDGET_S)
            return []
        if not raw:
            return []
        return _em_rows_to_bars_asc(raw)[-(want):]

    def _try_baostock_daily(self, symbol: str, want: int) -> list[dict[str, Any]]:
        """Baostock 兜底（TCP，历史长但很慢：实测 94s+）。"""
        ok, rows = _call_within_budget(
            lambda: fetch_daily_history_baostock(symbol, want),
            _BAOSTOCK_BUDGET_S,
        )
        if not ok:
            logger.info("Baostock 日线超预算（%.0fs），放弃", _BAOSTOCK_BUDGET_S)
            return []
        return list(rows or [])

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

            book = self._order_book()
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
            price = self._spot_price()
            if price is None:
                return
            apply_session_quote_to_forming_row(last, price=price, daily=True)
            return

        price = self._spot_price()
        if price is None:
            return
        from pa_agent.data.ashare_common import apply_session_quote_to_forming_row

        apply_session_quote_to_forming_row(last, price=price, daily=False)

    def _ensure_today_closed_bar(self, rows_asc: list[dict[str, Any]]) -> None:
        """收盘后若数据源（Baostock等）未包含当日日线，从东方财富盘口补一根已收盘K线。

        Baostock 数据通常在收盘后有数小时延迟，导致 15:00-18:00 之间
        分析时最后一根 K 线停在昨天。此方法通过东方财富五档/现价接口获取
        当日完整 OHLCV 并补充为已收盘 K 线。
        """
        from pa_agent.data.ashare_common import bar_trade_date

        if not rows_asc:
            return
        today = _cn_now().date()
        # 如果最后一根 bar 已经是今天的，无需补充
        if bar_trade_date(int(rows_asc[-1]["ts_open"])) >= today:
            return

        # 尝试从东方财富盘口获取当日 OHLCV
        book = None
        if not is_index_symbol(self._symbol):
            book = self._order_book()

        if book is not None and book.price > 0:
            ensure_today_closed_daily_bar(
                rows_asc,
                symbol=self._symbol,
                session_open=float(book.open) if book.open else 0.0,
                session_high=float(book.high) if book.high else 0.0,
                session_low=float(book.low) if book.low else 0.0,
                session_close=float(book.price),
                session_volume_lots=float(book.volume) if book.volume else 0.0,
                session_amount=float(book.amount) if book.amount else 0.0,
            )
            return

        # 指数无盘口五档，尝试现价
        spot = self._spot_price()
        if spot and spot > 0:
            ensure_today_closed_daily_bar(
                rows_asc,
                symbol=self._symbol,
                session_close=float(spot),
            )
