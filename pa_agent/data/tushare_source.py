"""Tushare-based A-share K-line data source."""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pa_agent.config.settings import Settings
from pa_agent.data.base import (
    DataSource,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")
_SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "1d")
_MINUTE_FREQ_BY_TIMEFRAME: dict[str, str] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "60min",
}
_MINUTE_BARS_PER_DAY: dict[str, int] = {
    "1m": 240,
    "5m": 48,
    "15m": 16,
    "30m": 8,
    "1h": 4,
}
_PRESET_SYMBOLS: tuple[str, ...] = (
    "600519",  # 贵州茅台
    "000001",  # 平安银行
    "300750",  # 宁德时代
    "688981",  # 中芯国际
    "000300",  # 沪深300
)
_TS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)
_DAILY_CACHE_TTL_S = 300.0
_MINUTE_CACHE_TTL_S = 3600.0


def normalize_tushare_symbol(symbol: str) -> str:
    """Normalize common A-share inputs to Tushare ``ts_code``."""
    raw = (symbol or "").strip().upper()
    if _TS_CODE_RE.match(raw):
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 6:
        return digits
    if digits.startswith(("600", "601", "603", "605", "688", "689", "900", "000300")):
        return f"{digits}.SH"
    if digits.startswith(("000", "001", "002", "003", "300", "301", "200", "399")):
        return f"{digits}.SZ"
    if digits.startswith(("8", "4")):
        return f"{digits}.BJ"
    return f"{digits}.SH"


def display_tushare_symbol(ts_code: str) -> str:
    """Return the 6-digit code for combo-box display."""
    return (ts_code or "").strip().upper().split(".", 1)[0]


def _trade_date_to_ts_ms(value: object) -> int:
    text = str(value).strip()
    if not re.match(r"^\d{8}$", text):
        raise ValueError(f"Invalid Tushare trade_date: {value!r}")
    dt = datetime.strptime(text, "%Y%m%d").replace(tzinfo=_CN_TZ)
    return int(dt.timestamp() * 1000)


def _trade_time_to_ts_ms(value: object) -> int:
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=_CN_TZ)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"Invalid Tushare trade_time: {value!r}")


def _number(row: Any, key: str, default: float = 0.0) -> float:
    try:
        value = row[key]
    except Exception:
        return default
    if value is None:
        return default
    try:
        if value != value:
            return default
    except Exception:
        pass
    return float(value)


def _df_to_bars_newest_first(df: Any, n: int) -> list[KlineBar]:
    if df is None or getattr(df, "empty", True):
        return []
    time_col = "trade_time" if "trade_time" in df.columns else "trade_date"
    required = {time_col, "open", "high", "low", "close"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Tushare data missing fields: {', '.join(missing)}")
    out = df.sort_values(time_col, ascending=False).reset_index(drop=True)
    bars: list[KlineBar] = []
    for i, row in out.head(n).iterrows():
        vol = _number(row, "vol")
        ts_open = (
            _trade_time_to_ts_ms(row[time_col])
            if time_col == "trade_time"
            else _trade_date_to_ts_ms(row[time_col])
        )
        bars.append(
            normalize_kline_bar(
                KlineBar(
                    seq=i + 1,
                    ts_open=float(ts_open),
                    open=_number(row, "open"),
                    high=_number(row, "high"),
                    low=_number(row, "low"),
                    close=_number(row, "close"),
                    volume=vol,
                    closed=True,
                )
            )
        )
    return bars


class TushareSource(DataSource):
    """A-share OHLCV bars via Tushare Pro."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected = False
        self._token = ""
        self._cache_key: tuple[str, str, int] | None = None
        self._cache_time = 0.0
        self._cache_bars: list[KlineBar] = []

    def connect(self) -> None:
        token = self._configured_token()
        if not token:
            raise DataSourceTransientError(
                "缺少 TUSHARE_TOKEN。请在通用设置中填写 Tushare Token，"
                "或设置本机环境变量后重启 PA Agent。"
            )
        try:
            import tushare as ts
        except ImportError as exc:
            raise DataSourceTransientError("未安装 tushare，请执行: pip install tushare") from exc
        ts.set_token(token)
        self._token = token
        self._connected = True
        logger.info("TushareSource connected")

    def disconnect(self) -> None:
        self._connected = False
        logger.info("TushareSource disconnected")

    def list_symbols(self) -> list[str]:
        return list(_PRESET_SYMBOLS)

    def supported_timeframes(self) -> list[str]:
        return list(_SUPPORTED_TIMEFRAMES)

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError("Tushare 当前支持 1m / 5m / 15m / 30m / 1h / 1d")
        ts_code = normalize_tushare_symbol(symbol)
        if not _TS_CODE_RE.match(ts_code):
            raise ValueError("A股代码无效，请输入 6 位代码，如 600519 或 000001")
        self._symbol = ts_code
        self._timeframe = timeframe
        self._cache_key = None
        self._cache_bars = []
        logger.info("TushareSource subscribed: %s %s", ts_code, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        logger.info("TushareSource unsubscribed")

    def is_symbol_available(self, symbol: str) -> bool:
        return bool(_TS_CODE_RE.match(normalize_tushare_symbol(symbol)))

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("Tushare 未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("Tushare 未订阅品种/周期")
        fetch_n = max(n + 60, 120)
        key = (self._symbol, self._timeframe, fetch_n)
        now = time.monotonic()
        cache_ttl_s = _MINUTE_CACHE_TTL_S if self._is_minute_timeframe() else _DAILY_CACHE_TTL_S
        if self._cache_key == key and self._cache_bars and now - self._cache_time < cache_ttl_s:
            return list(self._cache_bars[:n])

        try:
            df = (
                self._fetch_minute(fetch_n)
                if self._is_minute_timeframe()
                else self._fetch_daily(fetch_n)
            )
            bars = _df_to_bars_newest_first(df, fetch_n)
        except DataSourceTransientError:
            raise
        except Exception as exc:
            logger.warning("Tushare fetch failed: %s", exc)
            raise DataSourceTransientError(f"Tushare 拉取失败: {exc}") from exc
        if not bars:
            raise DataSourceTransientError(f"Tushare 未返回数据: {self._symbol}")
        self._cache_key = key
        self._cache_time = now
        self._cache_bars = bars
        return list(bars[:n])

    def _configured_token(self) -> str:
        if self._settings is not None:
            token = getattr(getattr(self._settings, "tushare", None), "token", "")
            if str(token or "").strip():
                return str(token).strip()
        return (os.environ.get("TUSHARE_TOKEN") or "").strip()

    def _is_minute_timeframe(self) -> bool:
        return self._timeframe in _MINUTE_FREQ_BY_TIMEFRAME

    def _fetch_daily(self, fetch_n: int) -> Any:
        import tushare as ts

        end = datetime.now(tz=_CN_TZ).strftime("%Y%m%d")
        start_dt = datetime.now(tz=_CN_TZ) - timedelta(days=max(365, int(fetch_n * 2.4)))
        start = start_dt.strftime("%Y%m%d")
        try:
            df = ts.pro_bar(
                ts_code=self._symbol,
                asset="E",
                adj=os.environ.get("TUSHARE_ADJ", "qfq").strip() or "qfq",
                freq="D",
                start_date=start,
                end_date=end,
            )
        except Exception as exc:
            raise DataSourceTransientError(f"Tushare pro_bar 调用失败: {exc}") from exc
        return df

    def _fetch_minute(self, fetch_n: int) -> Any:
        import math
        import tushare as ts

        freq = _MINUTE_FREQ_BY_TIMEFRAME[self._timeframe]
        bars_per_day = _MINUTE_BARS_PER_DAY[self._timeframe]
        lookback_days = max(10, int(math.ceil(fetch_n / bars_per_day * 2.5)) + 7)
        end_dt = datetime.now(tz=_CN_TZ)
        start_dt = end_dt - timedelta(days=lookback_days)
        start = start_dt.strftime("%Y-%m-%d 09:00:00")
        end = end_dt.strftime("%Y-%m-%d 15:30:00")
        limit = min(max(fetch_n, 120), 8000)
        try:
            api = ts.pro_api(self._token)
            return api.stk_mins(
                ts_code=self._symbol,
                start_date=start,
                end_date=end,
                freq=freq,
                limit=limit,
            )
        except Exception as exc:
            msg = str(exc)
            if "频率超限" in msg:
                raise DataSourceTransientError(
                    f"Tushare 分钟线接口限频: {msg}。PA Agent 已缓存同一品种/周期，"
                    "请等待限频窗口结束后重试。"
                ) from exc
            raise DataSourceTransientError(f"Tushare stk_mins 调用失败: {exc}") from exc
