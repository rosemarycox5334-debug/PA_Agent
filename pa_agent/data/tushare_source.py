"""Tushare Pro-based A-share K-line data source.

Requires ``tushare`` (>=1.12) and a valid Tushare Pro API token.

积分门槛
--------
- 120 分: ``daily``, ``index_daily``, ``stock_basic``, ``trade_cal``
- 2000 分: ``stk_mins``（分钟 K 线）, ``stk_limit``（涨跌停）,
  ``pro_bar``（复权）, ``daily_basic`` 等

The source auto-detects the token level and adjusts which APIs it uses.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pa_agent.data.base import (
    DataSource,
    DataSourceTransientError,
    KlineBar,
)
from pa_agent.data.ashare_common import (
    cn_now,
    normalize_ashare_symbol,
    is_index_symbol,
    index_symbol_for_api,
    df_to_bars_asc,
    normalize_ohlcv_df,
    resample_rows_to_4h,
    rows_to_kline_bars,
)

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")

# Tushare API 调用间隔（避免被限流）
_TS_MIN_INTERVAL_S = 1.0
_last_ts_fetch_mono: float = 0.0

_SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")

# PA Agent timeframe → tushare stk_mins freq
_MINUTE_FREQ_MAP: dict[str, str] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "60min",
}

_PRESET_SYMBOLS: tuple[str, ...] = (
    "000001",  # 平安银行
    "600519",  # 贵州茅台
    "000300",  # 沪深300
    "399006",  # 创业板指
)

_STOCK_CODE_RE = re.compile(r"^\d{6}$")

# ── Helpers ──────────────────────────────────────────────────────────────────


def _ts_code(symbol: str) -> str:
    """Convert 6-digit symbol to Tushare ts_code (e.g. 000001 → 000001.SZ)."""
    raw = normalize_ashare_symbol(symbol)
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    code = digits[-6:] if len(digits) >= 6 else digits.zfill(6)
    if code.startswith(("6", "9", "5")):
        return f"{code}.SH"
    if code in ("000016", "000300", "000905", "000852"):
        return f"{code}.SH"
    return f"{code}.SZ"


def _ts_index_code(symbol: str) -> str:
    """Convert index symbol to Tushare index ts_code."""
    idx = index_symbol_for_api(symbol)
    code = re.sub(r"\D", "", idx)
    return f"{code}.SH" if code.startswith(("000", "1", "880")) else f"{code}.SZ"


def _dt_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def _dt_min_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── TushareSource ─────────────────────────────────────────────────────────────


class TushareSource(DataSource):
    """A-share K-line data via Tushare Pro.

    Token priority:
        1. ``config/tushare_token.txt``
        2. Environment variable ``TUSHARE_TOKEN``
        3. ``~/.tushare/token.txt``

    Rate limits
    -----------
    - ``daily`` / ``index_daily``: up to 200 req/min (2000+ credits)
    - ``stk_mins``: **1 req/min** — calls are sequential and throttled
    """

    def __init__(self) -> None:
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._pro: Any = None  # tushare pro_api instance
        self._token: str = ""
        self._minute_access_known: bool = False  # True once probed
        self._has_minute_access: bool = False  # 2000+ 积分才有分钟线

    # ── Token resolution ──────────────────────────────────────────────────────

    def _resolve_token(self) -> str:
        """Resolve Tushare token from config, env, or file."""
        # 1. Config (set via UI or settings.json)
        from pa_agent.config.paths import CONFIG_DIR

        cfg_path = CONFIG_DIR / "tushare_token.txt"
        if cfg_path.exists():
            token = cfg_path.read_text(encoding="utf-8").strip()
            if token:
                return token

        # 2. Environment variable
        env_token = os.environ.get("TUSHARE_TOKEN", "").strip()
        if env_token:
            return env_token

        # 3. User home
        home_token = Path.home() / ".tushare" / "token.txt"
        if home_token.exists():
            token = home_token.read_text(encoding="utf-8").strip()
            if token:
                return token

        return ""

    # ── DataSource interface ──────────────────────────────────────────────────

    def connect(self) -> None:
        try:
            import tushare as ts  # noqa: F401
        except ImportError as exc:
            raise DataSourceTransientError(
                "未安装 tushare，请执行: pip install tushare"
            ) from exc

        self._token = self._resolve_token()
        if not self._token:
            raise DataSourceTransientError(
                "Tushare token 未配置。请设置环境变量 TUSHARE_TOKEN 或 "
                "在 config/tushare_token.txt 中填入 token。"
            )

        try:
            import tushare as ts_mod

            ts_mod.set_token(self._token)
            self._pro = ts_mod.pro_api()
            # Probe token level by trying a lightweight call
            self._pro_limit_test()
            logger.info(
                "TushareSource connected (minute_access=%s)",
                self._has_minute_access,
            )
        except Exception as exc:
            # Reset state on failure
            self._pro = None
            raise DataSourceTransientError(f"Tushare 连接失败: {exc}") from exc

        self._connected = True

    def _pro_limit_test(self) -> None:
        """Probe token validity with daily (cheap).

        Does **not** call ``stk_mins`` to avoid triggering the 1 req/min rate limit.
        Minute access is assumed available if daily works; the first actual minute
        fetch will confirm and cache the result.
        """
        try:
            df = self._pro.daily(ts_code="000001.SZ", start_date="20260101", end_date="20260110")
            if df is None or df.empty:
                logger.warning("Tushare daily probe returned empty — token may be invalid")
                return
        except Exception as exc:
            logger.warning("Tushare daily probe failed: %s", exc)
            raise  # token invalid

    def disconnect(self) -> None:
        self._pro = None
        self._connected = False
        self._token = ""
        logger.info("TushareSource disconnected")

    def list_symbols(self) -> list[str]:
        return list(_PRESET_SYMBOLS)

    def supported_timeframes(self) -> list[str]:
        # If minute access is confirmed absent, only daily
        if self._minute_access_known and not self._has_minute_access:
            return ["1d"]
        return list(_SUPPORTED_TIMEFRAMES)

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"Unsupported timeframe: {timeframe!r}. "
                f"Use one of {list(_SUPPORTED_TIMEFRAMES)}"
            )
        if timeframe != "1d" and self._minute_access_known and not self._has_minute_access:
            raise ValueError(
                f"Tushare 积分不足：分钟数据（{timeframe}）需要 2000+ 积分。"
                f"请升级积分或切换到 1d 周期。"
            )
        code = normalize_ashare_symbol(symbol)
        if not code:
            raise ValueError("A股代码无效，请输入 6 位数字（如 600519）或指数 sh000300")
        self._symbol = code
        self._timeframe = timeframe
        logger.info("TushareSource subscribed: %s %s", code, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        logger.info("TushareSource unsubscribed")

    def is_symbol_available(self, symbol: str) -> bool:
        code = normalize_ashare_symbol(symbol)
        return bool(_STOCK_CODE_RE.match(code) or code.startswith(("sh", "sz")))

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected or self._pro is None:
            raise DataSourceTransientError("Tushare 未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("Tushare 未订阅品种/周期")

        fetch_n = max(n + 8, 40)
        try:
            rows_asc = self._fetch_history(self._symbol, self._timeframe, fetch_n)
        except DataSourceTransientError:
            raise
        except Exception as exc:
            logger.warning("Tushare fetch failed: %s", exc)
            raise DataSourceTransientError(f"Tushare 拉取失败: {exc}") from exc

        if not rows_asc:
            raise DataSourceTransientError(
                f"Tushare 未返回数据: {self._symbol} {self._timeframe}"
            )

        # Tushare provides completed bars only (no real-time streaming).
        # Mark all bars as closed; rows_to_kline_bars defaults closed=False
        # for the first bar, so we override via the 'closed' key.
        from pa_agent.data.ashare_common import ashare_session_open

        rows_newest = list(reversed(rows_asc[-fetch_n:]))
        session_open = ashare_session_open()
        for i, row in enumerate(rows_newest):
            # Mark the first (newest) bar as forming if market is open
            row["closed"] = not (i == 0 and session_open)

        return rows_to_kline_bars(rows_newest, n)

    # ── Fetch helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _throttle() -> None:
        global _last_ts_fetch_mono
        now = time.monotonic()
        wait = _TS_MIN_INTERVAL_S - (now - _last_ts_fetch_mono)
        if wait > 0:
            time.sleep(wait)
        _last_ts_fetch_mono = time.monotonic()

    @staticmethod
    def _call_with_retries(
        label: str,
        fn: Any,
        *,
        attempts: int = 3,
        max_wait_s: float = 8.0,
    ) -> Any:
        last_exc: Exception | None = None
        waited = 0.0
        for i in range(attempts):
            TushareSource._throttle()
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

    def _fetch_history(
        self, symbol: str, timeframe: str, n: int
    ) -> list[dict[str, Any]]:
        if timeframe == "1d":
            return self._fetch_daily(symbol, n)
        if timeframe == "4h":
            rows_60 = self._fetch_minute(symbol, "60min", n * 4 + 8)
            return resample_rows_to_4h(rows_60)[-n:]
        freq = _MINUTE_FREQ_MAP.get(timeframe)
        if freq is None:
            raise DataSourceTransientError(f"Unsupported timeframe: {timeframe}")
        return self._fetch_minute(symbol, freq, n)

    def _fetch_daily(self, symbol: str, n: int) -> list[dict[str, Any]]:
        assert self._pro is not None
        now = cn_now()
        end = _dt_str(now)
        # Tushare daily has comprehensive history; request enough
        start = _dt_str(now - timedelta(days=max(n * 2, 400)))

        if is_index_symbol(symbol):
            code = _ts_index_code(symbol)

            def _pull() -> Any:
                return self._pro.index_daily(
                    ts_code=code,
                    start_date=start,
                    end_date=end,
                )

            df = self._call_with_retries(f"index_daily {code}", _pull)
        else:
            code = _ts_code(symbol)

            # Note: ``daily()`` does NOT support the ``adj`` parameter (silently ignored).
            # For forward-adjusted data, use ``pro_bar(adj='qfq')`` (requires 2000+ credits).
            def _pull() -> Any:
                return self._pro.daily(
                    ts_code=code,
                    start_date=start,
                    end_date=end,
                )

            df = self._call_with_retries(f"daily {code}", _pull)

        if df is None or df.empty:
            return []
        norm = normalize_ohlcv_df(df, time_col="trade_date")
        if norm.empty:
            return []
        return df_to_bars_asc(norm.tail(n + 5), time_col="trade_date")

    def _fetch_minute(
        self, symbol: str, freq: str, n: int
    ) -> list[dict[str, Any]]:
        assert self._pro is not None
        now = cn_now()
        end_s = _dt_min_str(now)
        # ~4 bars per trading day for 60min; ~240 for 1min
        if freq == "1min":
            days = max(5, (n // 240) + 3)
        elif freq == "5min":
            days = max(8, (n // 48) + 3)
        else:
            days = max(15, (n // 8) + 5)
        start_s = _dt_min_str(now - timedelta(days=days))

        if is_index_symbol(symbol):
            code = _ts_index_code(symbol)
        else:
            code = _ts_code(symbol)

        def _pull() -> Any:
            return self._pro.stk_mins(
                ts_code=code,
                freq=freq,
                start_date=start_s,
                end_date=end_s,
            )

        try:
            df = self._call_with_retries(f"stk_mins {code} {freq}", _pull)
        except Exception as exc:
            msg = str(exc).lower()
            if "频率超限" in msg or "频次" in msg:
                # Rate-limited — cache and fall back to daily only
                self._minute_access_known = True
                self._has_minute_access = True  # have access, just rate-limited
                logger.warning("Tushare stk_mins rate limited; wait 60s before retry")
                raise DataSourceTransientError(
                    f"Tushare 分钟接口频率超限（1次/分钟），请等待后重试: {exc}"
                ) from exc
            if "权限" in msg or "积分" in msg or "not allowed" in msg:
                self._minute_access_known = True
                self._has_minute_access = False
                logger.warning("Tushare stk_mins not available (积分不足2000)")
                raise DataSourceTransientError(
                    f"Tushare 分钟数据不可用（需2000+积分）: {exc}"
                ) from exc
            raise

        if df is None or df.empty:
            return []

        # First successful minute fetch confirms 2000+ access
        self._minute_access_known = True
        self._has_minute_access = True

        norm = normalize_ohlcv_df(df, time_col="trade_time")
        if norm.empty:
            return []
        return df_to_bars_asc(norm.tail(n + 8), time_col="trade_time")


# ── Make sure Path is importable ──────────────────────────────────────────────
from pathlib import Path  # noqa: E402, F811
