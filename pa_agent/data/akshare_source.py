"""AkShare-based A-share K-line data source (polling, no broker required).

Primary APIs: East Money minute/daily history + spot snapshot for forming bars.
Optional fallback: Baostock when env ``PA_AGENT_BAOSTOCK_FALLBACK=1`` (off by default).
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pa_agent.data.base import DataSource, DataSourceTransientError, KlineBar, normalize_kline_bar
from pa_agent.data.datetime_ts import datetime_to_ts_ms

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")

# East Money throttles burst requests; space out AkShare HTTP calls.
_AK_MIN_INTERVAL_S = 0.9
_last_ak_fetch_mono: float = 0.0

# PA Agent timeframe → AkShare minute period (stock_zh_a_hist_min_em)
_MINUTE_PERIOD: dict[str, str] = {
    "1h": "60",
}

_SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("1h", "4h", "1d")

# Preset symbols for the combo; user may type any 6-digit code or sh/sz index id.
_PRESET_SYMBOLS: tuple[str, ...] = (
    "000001",  # 平安银行
    "600519",  # 贵州茅台
    "000300",  # 沪深300（指数）
    "399006",  # 创业板指
)

_STOCK_CODE_RE = re.compile(r"^\d{6}$")
_INDEX_PREFIX_RE = re.compile(r"^(sh|sz)(\d{6})$", re.IGNORECASE)
_FUTURES_CODE_RE = re.compile(r"^[A-Za-z]{1,3}(?:\d{3,4}|0)$")


def normalize_ashare_symbol(symbol: str) -> str:
    """Normalize user input to AkShare stock code (6 digits) or index id (sh000300)."""
    raw = (symbol or "").strip()
    if not raw:
        return ""
    m = _INDEX_PREFIX_RE.match(raw)
    if m:
        prefix, digits = m.group(1).lower(), m.group(2)
        if _is_index_digits(digits):
            return f"{prefix}{digits}"
        return digits
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 6:
        return digits[-6:]
    return ""


def normalize_futures_symbol(symbol: str) -> str:
    """Normalize China futures symbols accepted by AkShare/Sina, e.g. JD2607."""
    raw = (symbol or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"[^0-9A-Za-z]", "", raw)
    if _FUTURES_CODE_RE.match(compact):
        return compact.upper()
    return ""


def normalize_akshare_symbol(symbol: str) -> str:
    """Normalize to either an A-share/index code or a China futures contract."""
    return normalize_ashare_symbol(symbol) or normalize_futures_symbol(symbol)


def is_futures_symbol(symbol: str) -> bool:
    return bool(normalize_futures_symbol(symbol))


def _is_index_digits(digits: str) -> bool:
    return digits in {
        "000300",
        "000016",
        "000905",
        "000852",
        "399001",
        "399006",
        "399300",
    }


def is_index_symbol(symbol: str) -> bool:
    """True for sh/sz-prefixed index codes or common CSI/ChiNext codes."""
    sym = normalize_ashare_symbol(symbol)
    if sym.startswith(("sh", "sz")) and len(sym) >= 8:
        return True
    if _STOCK_CODE_RE.match(sym):
        return _is_index_digits(sym)
    return False


def _index_symbol_for_api(symbol: str) -> str:
    sym = normalize_ashare_symbol(symbol)
    if sym.startswith(("sh", "sz")):
        return sym
    if sym.startswith("399"):
        return f"sz{sym}"
    return f"sh{sym}"


def _cn_now() -> datetime:
    return datetime.now(tz=_CN_TZ)


def _ashare_session_open(now: datetime | None = None) -> bool:
    """True during A-share cash session (Mon–Fri, 09:30–11:30 & 13:00–15:00 CN)."""
    now = now or _cn_now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    morning = 9 * 60 + 30 <= t < 11 * 60 + 30
    afternoon = 13 * 60 <= t < 15 * 60
    return morning or afternoon


def _row_time_to_ts_ms(value: Any) -> int:
    if value is None:
        return int(_cn_now().timestamp() * 1000)
    try:
        import pandas as pd

        if isinstance(value, pd.Timestamp):
            ts = value
            if ts.tz is None:
                ts = ts.tz_localize(_CN_TZ)
            else:
                ts = ts.tz_convert(_CN_TZ)
            return int(ts.timestamp() * 1000)
    except ImportError:
        pass
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_CN_TZ)
        return int(value.timestamp() * 1000)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text[: len(fmt)], fmt).replace(tzinfo=_CN_TZ)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return datetime_to_ts_ms(text)


def _df_to_bars_asc(df: Any, *, time_col: str) -> list[dict[str, Any]]:
    """Convert normalized ascending OHLCV rows to dicts."""
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        ts = _row_time_to_ts_ms(row[time_col])
        o = float(row["open"])
        h = float(row["high"])
        lo = float(row["low"])
        c = float(row["close"])
        vol = float(row.get("volume", 0.0) or 0.0)
        rows.append(
            {"ts_open": ts, "open": o, "high": h, "low": lo, "close": c, "volume": vol}
        )
    return rows


def _normalize_ohlcv_df(df: Any, *, time_col: str) -> Any:
    import pandas as pd

    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    rename: dict[Any, str] = {}
    time_mapped = False
    for col in out.columns:
        c = str(col).strip()
        if c in ("时间", "日期", "date", "datetime", "time"):
            if not time_mapped and str(col) != time_col:
                rename[col] = time_col
                time_mapped = True
            elif str(col) != time_col:
                pass  # drop duplicate time-like columns after rename
        elif c in ("开盘", "open", "Open"):
            rename[col] = "open"
        elif c in ("收盘", "close", "Close"):
            rename[col] = "close"
        elif c in ("最高", "high", "High"):
            rename[col] = "high"
        elif c in ("最低", "low", "Low"):
            rename[col] = "low"
        elif c in ("成交量", "volume", "Volume"):
            rename[col] = "volume"
    out = out.rename(columns=rename)
    drop_cols = [
        c
        for c in out.columns
        if str(c).strip() in ("时间", "日期", "date", "datetime", "time")
        and c != time_col
    ]
    if drop_cols:
        out = out.drop(columns=drop_cols, errors="ignore")
    if time_col not in out.columns:
        return pd.DataFrame()
    for req in ("open", "high", "low", "close"):
        if req not in out.columns:
            return pd.DataFrame()
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out = out.sort_values(time_col).reset_index(drop=True)
    return out


def _resample_rows_to_4h(rows_asc: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows_asc:
        return []
    buckets: list[dict[str, Any]] = []
    chunk: list[dict[str, Any]] = []
    for row in rows_asc:
        chunk.append(row)
        if len(chunk) == 4:
            buckets.append(_merge_ohlcv(chunk))
            chunk = []
    if chunk:
        buckets.append(_merge_ohlcv(chunk))
    return buckets


def _merge_ohlcv(chunk: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ts_open": chunk[0]["ts_open"],
        "open": chunk[0]["open"],
        "high": max(r["high"] for r in chunk),
        "low": min(r["low"] for r in chunk),
        "close": chunk[-1]["close"],
        "volume": sum(r["volume"] for r in chunk),
    }


def _rows_to_kline_bars(rows_newest_first: list[dict[str, Any]], n: int) -> list[KlineBar]:
    bars: list[KlineBar] = []
    for i, row in enumerate(rows_newest_first[:n]):
        ts_ms = int(row["ts_open"])
        bars.append(
            normalize_kline_bar(
                KlineBar(
                    seq=i + 1,
                    ts_open=float(ts_ms),
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                    closed=row.get("closed", i != 0),
                )
            )
        )
    return bars


class AkShareSource(DataSource):
    """A-share quotes via AkShare (East Money); polls on each snapshot."""

    def __init__(self) -> None:
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._baostock_ok: bool = False
        self._baostock_logged_in: bool = False

    def connect(self) -> None:
        # Avoid tqdm progress bars on stderr (blocks some IDE consoles).
        os.environ.setdefault("TQDM_DISABLE", "1")
        try:
            import akshare  # noqa: F401
        except ImportError as exc:
            raise DataSourceTransientError(
                "未安装 akshare，请执行: pip install akshare"
            ) from exc
        self._baostock_ok = False
        if os.environ.get("PA_AGENT_BAOSTOCK_FALLBACK", "").strip() in ("1", "true", "yes"):
            try:
                import baostock  # noqa: F401

                self._baostock_ok = True
            except ImportError:
                logger.debug("PA_AGENT_BAOSTOCK_FALLBACK=1 but baostock not installed")
        self._connected = True
        logger.info("AkShareSource connected (baostock_fallback=%s)", self._baostock_ok)

    def disconnect(self) -> None:
        self._baostock_logout()
        self._connected = False
        logger.info("AkShareSource disconnected")

    def list_symbols(self) -> list[str]:
        return list(_PRESET_SYMBOLS) + ["JD2607", "CU2607", "AU2607", "AG2607", "RB2607"]

    def supported_timeframes(self) -> list[str]:
        return list(_SUPPORTED_TIMEFRAMES)

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"Unsupported timeframe: {timeframe!r}. "
                f"Use one of {list(_SUPPORTED_TIMEFRAMES)}"
            )
        code = normalize_akshare_symbol(symbol)
        if not code or not (
            _STOCK_CODE_RE.match(code)
            or code.startswith(("sh", "sz"))
            or is_futures_symbol(code)
        ):
            raise ValueError("AkShare 代码无效，请输入 6 位 A 股代码、指数 sh000300，或期货合约 JD2607")
        self._symbol = code
        self._timeframe = timeframe
        logger.info("AkShareSource subscribed: %s %s", code, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        logger.info("AkShareSource unsubscribed")

    def is_symbol_available(self, symbol: str) -> bool:
        code = normalize_akshare_symbol(symbol)
        return bool(
            _STOCK_CODE_RE.match(code)
            or code.startswith(("sh", "sz"))
            or is_futures_symbol(code)
        )

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("AkShare 未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("AkShare 未订阅品种/周期")

        fetch_n = max(n + 5, 30)
        try:
            rows_asc = self._fetch_history(self._symbol, self._timeframe, fetch_n)
        except DataSourceTransientError:
            raise
        except Exception as exc:
            logger.warning("AkShare fetch failed: %s", exc)
            raise DataSourceTransientError(f"AkShare 拉取失败: {exc}") from exc

        if not rows_asc:
            raise DataSourceTransientError(
                f"AkShare 未返回数据: {self._symbol} {self._timeframe}"
            )

        if not is_futures_symbol(self._symbol) and _ashare_session_open():
            self._apply_spot_to_forming(rows_asc)

        rows_newest = list(reversed(rows_asc[-fetch_n:]))
        for i, row in enumerate(rows_newest):
            row["closed"] = not (
                i == 0
                and not is_futures_symbol(self._symbol)
                and _ashare_session_open()
            )

        return _rows_to_kline_bars(rows_newest, n)

    # ── Fetch ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _throttle_akshare() -> None:
        global _last_ak_fetch_mono
        now = time.monotonic()
        wait = _AK_MIN_INTERVAL_S - (now - _last_ak_fetch_mono)
        if wait > 0:
            time.sleep(wait)
        _last_ak_fetch_mono = time.monotonic()

    @staticmethod
    def _call_with_retries(
        label: str,
        fn: Any,
        *,
        attempts: int = 4,
        max_wait_s: float = 12.0,
    ) -> Any:
        last_exc: Exception | None = None
        waited = 0.0
        for i in range(attempts):
            AkShareSource._throttle_akshare()
            try:
                return fn()
            except Exception as exc:
                last_exc = exc
                if i + 1 >= attempts:
                    break
                delay = min(3.0, max(1.0, max_wait_s - waited))
                if delay <= 0:
                    break
                time.sleep(delay)
                waited += delay
                logger.debug("%s retry %d/%d: %s", label, i + 2, attempts, exc)
        assert last_exc is not None
        raise last_exc

    def _fetch_history(self, symbol: str, timeframe: str, n: int) -> list[dict[str, Any]]:
        if is_futures_symbol(symbol):
            return self._fetch_futures_history_ak(symbol, timeframe, n)
        try:
            if timeframe == "1d":
                return self._fetch_daily_ak(symbol, n)
            if timeframe == "1h":
                return self._fetch_minute_ak(symbol, "60", n)
            if timeframe == "4h":
                rows_60 = self._fetch_minute_ak(symbol, "60", n * 4 + 8)
                return _resample_rows_to_4h(rows_60)[-n:]
        except Exception as exc:
            try:
                rows = _fetch_history_eastmoney_browser(symbol, timeframe, n)
                logger.debug(
                    "AkShare 主源失败，已使用 EastMoney browser fallback (%s): %s",
                    symbol,
                    exc,
                )
                return rows
            except Exception as em_exc:
                logger.warning("AkShare 主源失败 (%s): %s", symbol, exc)
                logger.warning("EastMoney browser fallback failed (%s): %s", symbol, em_exc)
            if self._baostock_ok:
                try:
                    return self._fetch_history_baostock(symbol, timeframe, n)
                except Exception as bs_exc:
                    logger.warning("Baostock 备用源失败 (%s): %s", symbol, bs_exc)
                    raise DataSourceTransientError(
                        f"AkShare 与 Baostock 均失败: {exc}; 备用: {bs_exc}"
                    ) from bs_exc
            raise DataSourceTransientError(f"AkShare 拉取失败: {exc}") from exc
        if self._baostock_ok:
            return self._fetch_history_baostock(symbol, timeframe, n)
        return []

    def _fetch_futures_history_ak(
        self,
        symbol: str,
        timeframe: str,
        n: int,
    ) -> list[dict[str, Any]]:
        import akshare as ak

        code = normalize_futures_symbol(symbol)
        if timeframe == "1d":
            df = self._call_with_retries(
                f"futures_daily {code}",
                lambda: ak.futures_zh_daily_sina(symbol=code),
            )
            norm = _normalize_ohlcv_df(df, time_col="date")
            if norm.empty:
                return []
            return _df_to_bars_asc(norm.tail(n + 5), time_col="date")
        if timeframe in {"1h", "4h"}:
            fetch_n = n if timeframe == "1h" else n * 4 + 8
            df = self._call_with_retries(
                f"futures_min {code} 60",
                lambda: ak.futures_zh_minute_sina(symbol=code, period="60"),
            )
            norm = _normalize_ohlcv_df(df, time_col="datetime")
            if norm.empty:
                return []
            rows = _df_to_bars_asc(norm.tail(fetch_n + 8), time_col="datetime")
            if timeframe == "4h":
                return _resample_rows_to_4h(rows)[-n:]
            return rows[-(n + 8) :]
        raise ValueError(
            f"Unsupported timeframe for futures: {timeframe!r}. Use one of {list(_SUPPORTED_TIMEFRAMES)}"
        )

    def _fetch_daily_ak(self, symbol: str, n: int) -> list[dict[str, Any]]:
        import akshare as ak

        end = _cn_now().strftime("%Y%m%d")
        start = (_cn_now() - timedelta(days=max(n * 2, 400))).strftime("%Y%m%d")
        if is_index_symbol(symbol):
            idx = _index_symbol_for_api(symbol)
            df = self._call_with_retries(
                f"index_daily {idx}",
                lambda: ak.stock_zh_index_daily_em(symbol=idx),
            )
            if df is None or df.empty:
                return []
            df = df.tail(n + 5)
            norm = _normalize_ohlcv_df(df, time_col="date")
            if norm.empty:
                return []
            return _df_to_bars_asc(norm, time_col="date")
        code = normalize_ashare_symbol(symbol)
        df = self._call_with_retries(
            f"daily {code}",
            lambda: ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="qfq",
            ),
        )
        norm = _normalize_ohlcv_df(df, time_col="date")
        if norm.empty:
            return []
        return _df_to_bars_asc(norm.tail(n + 5), time_col="date")

    def _fetch_minute_ak(self, symbol: str, period: str, n: int) -> list[dict[str, Any]]:
        import akshare as ak

        end_dt = _cn_now()
        # ~4 bars per trading day for 60m
        days = max(30, (n // 4) + 15)
        start_dt = end_dt - timedelta(days=days)
        start_s = start_dt.strftime("%Y-%m-%d 09:30:00")
        end_s = end_dt.strftime("%Y-%m-%d 15:00:00")
        if is_index_symbol(symbol):
            idx = _index_symbol_for_api(symbol)
            df = self._call_with_retries(
                f"index_min {idx}",
                lambda: ak.index_zh_a_hist_min_em(
                    symbol=idx,
                    period=period,
                    start_date=start_s,
                    end_date=end_s,
                ),
            )
        else:
            code = normalize_ashare_symbol(symbol)

            def _pull() -> Any:
                return ak.stock_zh_a_hist_min_em(
                    symbol=code,
                    period=period,
                    start_date=start_s,
                    end_date=end_s,
                    adjust="qfq",
                )

            df = self._call_with_retries(f"min {code} {period}", _pull)
        norm = _normalize_ohlcv_df(df, time_col="time")
        if norm.empty:
            return []
        return _df_to_bars_asc(norm.tail(n + 8), time_col="time")

    def _fetch_history_baostock(
        self, symbol: str, timeframe: str, n: int
    ) -> list[dict[str, Any]]:
        import baostock as bs

        if is_index_symbol(symbol) and timeframe != "1d":
            raise DataSourceTransientError("Baostock 不提供指数分钟线，请稍后重试 AkShare")
        code = _baostock_code(symbol)
        freq = {"1d": "d", "1h": "60", "4h": "60"}.get(timeframe, "d")
        end = _cn_now().strftime("%Y-%m-%d")
        start = (_cn_now() - timedelta(days=max(n * 2, 400))).strftime("%Y-%m-%d")
        self._baostock_login()
        try:
            fields = (
                "date,time,code,open,high,low,close,volume"
                if freq != "d"
                else "date,code,open,high,low,close,volume"
            )
            rs = bs.query_history_k_data_plus(
                code,
                fields,
                start_date=start,
                end_date=end,
                frequency=freq,
                adjustflag="2",
            )
            data: list[list[str]] = []
            while rs.error_code == "0" and rs.next():
                data.append(rs.get_row_data())
            if rs.error_code != "0":
                raise DataSourceTransientError(f"Baostock: {rs.error_msg}")
        except Exception:
            raise
        finally:
            pass  # keep session for RefreshLoop; logout on disconnect()

        if not data:
            return []
        import pandas as pd

        cols = [x.strip() for x in fields.split(",")]
        df = pd.DataFrame(data, columns=cols)
        if freq != "d":
            tcol = df["time"].astype(str).str.replace(r"\D", "", regex=True).str.slice(0, 14)
            bar_time = pd.to_datetime(
                df["date"].astype(str) + tcol,
                format="%Y-%m-%d%H%M%S",
                errors="coerce",
            )
            slim = pd.DataFrame(
                {
                    "bar_time": bar_time,
                    "open": pd.to_numeric(df["open"], errors="coerce"),
                    "high": pd.to_numeric(df["high"], errors="coerce"),
                    "low": pd.to_numeric(df["low"], errors="coerce"),
                    "close": pd.to_numeric(df["close"], errors="coerce"),
                    "volume": pd.to_numeric(df["volume"], errors="coerce"),
                }
            ).dropna(subset=["bar_time"])
            norm = _normalize_ohlcv_df(slim, time_col="bar_time")
            rows = _df_to_bars_asc(norm, time_col="bar_time")
            if timeframe == "4h":
                return _resample_rows_to_4h(rows)[-n:]
            return rows[-(n + 8) :]
        norm = _normalize_ohlcv_df(df, time_col="date")
        return _df_to_bars_asc(norm, time_col="date")[-(n + 5) :]

    def _apply_spot_to_forming(self, rows_asc: list[dict[str, Any]]) -> None:
        """Refresh last bar close from a single-symbol quote (never full-market spot)."""
        if not _ashare_session_open():
            return
        price = self._fetch_spot_price(self._symbol)
        if price is None or not rows_asc:
            return
        last = rows_asc[-1]
        last["close"] = price
        last["high"] = max(last["high"], price)
        last["low"] = min(last["low"], price)

    def _fetch_spot_price(self, symbol: str) -> float | None:
        try:
            import akshare as ak

            if is_index_symbol(symbol):
                return None
            code = normalize_ashare_symbol(symbol)
            df = self._call_with_retries(
                f"spot {code}",
                lambda: ak.stock_individual_info_em(symbol=code),
                attempts=2,
                max_wait_s=6.0,
            )
            if df is None or df.empty:
                return None
            item_col = "item" if "item" in df.columns else df.columns[0]
            val_col = "value" if "value" in df.columns else df.columns[1]
            for item, val in zip(df[item_col], df[val_col], strict=False):
                if str(item).strip() in ("最新", "最新价"):
                    return float(val)
            return None
        except Exception as exc:
            logger.debug("AkShare spot fetch failed: %s", exc)
            return None


    def _baostock_login(self) -> None:
        if self._baostock_logged_in:
            return
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            raise DataSourceTransientError(f"Baostock 登录失败: {lg.error_msg}")
        self._baostock_logged_in = True

    def _baostock_logout(self) -> None:
        if not self._baostock_logged_in:
            return
        try:
            import baostock as bs

            bs.logout()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Baostock logout: %s", exc)
        self._baostock_logged_in = False


def _baostock_code(symbol: str) -> str:
    sym = normalize_ashare_symbol(symbol)
    if sym.startswith(("sh", "sz")):
        return f"{sym[:2]}.{sym[2:]}"
    if sym.startswith(("5", "6", "9")):
        return f"sh.{sym}"
    return f"sz.{sym}"


def _eastmoney_secid(symbol: str) -> str:
    sym = normalize_ashare_symbol(symbol)
    if sym.startswith("sh"):
        return f"1.{sym[2:]}"
    if sym.startswith("sz"):
        return f"0.{sym[2:]}"
    if sym.startswith(("5", "6", "9")) or sym in {"000001", "000016", "000300", "000905", "000852"}:
        return f"1.{sym}"
    return f"0.{sym}"


def _fetch_history_eastmoney_browser(
    symbol: str,
    timeframe: str,
    n: int,
) -> list[dict[str, Any]]:
    """Fetch EastMoney K-lines with browser-like TLS when AkShare requests fail."""
    if timeframe not in {"1d", "1h", "4h"}:
        raise DataSourceTransientError(f"Unsupported timeframe for EastMoney fallback: {timeframe}")

    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise DataSourceTransientError("curl_cffi not installed for EastMoney fallback") from exc

    klt = "101" if timeframe == "1d" else "60"
    fetch_n = n if timeframe != "4h" else n * 4 + 8
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": klt,
        "fqt": "1",
        "secid": _eastmoney_secid(symbol),
        "beg": "0",
        "end": "20500000",
    }
    response = curl_requests.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params=params,
        timeout=20,
        impersonate="chrome",
        proxies={},
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    klines = data.get("klines") or []
    if not klines:
        raise DataSourceTransientError(f"EastMoney returned no data for {symbol} {timeframe}")

    rows: list[dict[str, Any]] = []
    for line in klines[-(fetch_n + 8) :]:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        rows.append(
            {
                "ts_open": _row_time_to_ts_ms(parts[0]),
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5] or 0.0),
            }
        )
    if timeframe == "4h":
        return _resample_rows_to_4h(rows)[-n:]
    return rows[-(n + 8) :]
