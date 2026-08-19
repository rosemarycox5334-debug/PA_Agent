"""Baostock fallback for A-share minute history beyond East Money rolling window."""
from __future__ import annotations

import contextlib
import io
import logging
import threading
from datetime import timedelta
from typing import Any, Callable, TypeVar

from pa_agent.data.ashare_common import (
    cn_now as _cn_now,
    df_to_bars_asc as _df_to_bars_asc,
    is_index_symbol,
    normalize_ashare_symbol,
    normalize_ohlcv_df as _normalize_ohlcv_df,
    resample_rows_to_4h as _resample_rows_to_4h,
)
from pa_agent.data.base import DataSourceTransientError

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_RETRYABLE_BS_MARKERS = (
    "网络接收",
    "接收数据",
    "recv",
    "decode",
    "utf-8",
    "codec",
    "连接",
    "socket",
)

# East Money push2his rolling caps (bars, same date window regardless of lmt)
_EM_ROLLING_CAP: dict[str, int] = {
    "1": 240,
    "5": 1536,
    "15": 512,
    "30": 256,
    "60": 128,
}

_BAOSTOCK_FREQ: dict[str, str] = {
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
}


def eastmoney_rolling_cap(period: str) -> int:
    return _EM_ROLLING_CAP.get(period, 128)


def needs_baostock_history(timeframe: str, period: str, n: int) -> bool:
    """True when requested bar count exceeds East Money minute rolling window."""
    if timeframe == "4h":
        return (n * 4 + 8) > eastmoney_rolling_cap("60")
    return (n + 8) > eastmoney_rolling_cap(period)


def _baostock_code(symbol: str) -> str:
    sym = normalize_ashare_symbol(symbol)
    if sym.startswith(("sh", "sz")):
        return f"{sym[:2]}.{sym[2:]}"
    if sym.startswith(("5", "6", "9")):
        return f"sh.{sym}"
    return f"sz.{sym}"


def _calendar_days_for_bars(n: int, *, bars_per_day: int = 4) -> int:
    # A-share ~4 hourly bars per trading day; add calendar buffer for holidays.
    trading_days = (n // max(bars_per_day, 1)) + 30
    return max(365, int(trading_days * 1.55))


def _is_retryable_baostock_error(msg: str) -> bool:
    text = (msg or "").lower()
    return any(marker in text for marker in _RETRYABLE_BS_MARKERS)


def _collect_baostock_rows(rs: Any) -> list[list[str]]:
    data: list[list[str]] = []
    while rs.error_code == "0" and rs.next():
        data.append(rs.get_row_data())
    if rs.error_code != "0":
        msg = str(rs.error_msg or "未知错误")
        if _is_retryable_baostock_error(msg):
            raise DataSourceTransientError(f"Baostock: {msg}")
        raise DataSourceTransientError(f"Baostock: {msg}")
    return data


class _BaostockSession:
    """Serialise Baostock calls — its socket singleton is not thread-safe."""

    _logged_in: bool = False
    _lock = threading.Lock()

    @classmethod
    def login(cls) -> None:
        with cls._lock:
            cls._ensure_login_locked()

    @classmethod
    def logout(cls) -> None:
        with cls._lock:
            cls._logout_locked()

    @classmethod
    def execute(cls, label: str, fn: Callable[[], _T]) -> _T:
        with cls._lock:
            last_exc: Exception | None = None
            for attempt in range(2):
                try:
                    cls._ensure_login_locked()
                    return fn()
                except DataSourceTransientError as exc:
                    last_exc = exc
                    if attempt == 0 and _is_retryable_baostock_error(str(exc)):
                        logger.info(
                            "Baostock %s failed, resetting session: %s",
                            label,
                            exc,
                        )
                        cls._force_reset_locked()
                        continue
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if attempt == 0:
                        logger.info(
                            "Baostock %s error, resetting session: %s",
                            label,
                            exc,
                        )
                        cls._force_reset_locked()
                        continue
                    raise DataSourceTransientError(
                        f"Baostock {label} 失败: {exc}"
                    ) from exc
            raise DataSourceTransientError(
                f"Baostock {label} 失败: {last_exc}"
            ) from last_exc

    @classmethod
    def _ensure_login_locked(cls) -> None:
        if cls._logged_in:
            return
        import baostock as bs

        with contextlib.redirect_stdout(io.StringIO()):
            lg = bs.login()
        if lg.error_code != "0":
            raise DataSourceTransientError(f"Baostock 登录失败: {lg.error_msg}")
        cls._logged_in = True

    @classmethod
    def _logout_locked(cls) -> None:
        if not cls._logged_in:
            return
        try:
            import baostock as bs

            with contextlib.redirect_stdout(io.StringIO()):
                bs.logout()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Baostock logout: %s", exc)
        cls._logged_in = False

    @classmethod
    def _force_reset_locked(cls) -> None:
        try:
            import baostock as bs
            import baostock.common.context as ctx

            sock = getattr(ctx, "default_socket", None)
            if sock is not None:
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass
                setattr(ctx, "default_socket", None)
            with contextlib.redirect_stdout(io.StringIO()):
                bs.logout()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Baostock force reset: %s", exc)
        cls._logged_in = False


def fetch_daily_history_baostock(symbol: str, n: int) -> list[dict[str, Any]]:
    """Daily OHLCV fallback when East Money HTTP is unstable."""
    if is_index_symbol(symbol):
        raise DataSourceTransientError("Baostock 指数日线请仍走 East Money")

    code = _baostock_code(symbol)
    end = _cn_now().strftime("%Y-%m-%d")
    cal_days = min(max(int(n * 1.45) + 25, 75), 420)
    start = (_cn_now() - timedelta(days=cal_days)).strftime("%Y-%m-%d")

    import baostock as bs

    def _query() -> list[list[str]]:
        rs = bs.query_history_k_data_plus(
            code,
            "date,code,open,high,low,close,volume",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="2",
        )
        return _collect_baostock_rows(rs)

    data = _BaostockSession.execute("日线", _query)
    if not data:
        return []

    import pandas as pd

    cols = [x.strip() for x in "date,code,open,high,low,close,volume".split(",")]
    df = pd.DataFrame(data, columns=cols)
    norm = _normalize_ohlcv_df(df, time_col="date")
    return _df_to_bars_asc(norm, time_col="date")[-n:]


def fetch_minute_history_baostock(
    symbol: str,
    timeframe: str,
    n: int,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch ascending OHLCV dict rows (ts_open ms) from Baostock."""
    if is_index_symbol(symbol):
        raise DataSourceTransientError("Baostock 不提供指数分钟线，请使用日线 1d")

    if timeframe == "4h":
        hourly = fetch_minute_history_baostock(
            symbol,
            "1h",
            n * 4 + 8,
            start_date=start_date,
            end_date=end_date,
        )
        return _resample_rows_to_4h(hourly)[-n:]

    freq = _BAOSTOCK_FREQ.get(timeframe)
    if freq is None:
        raise DataSourceTransientError(f"Baostock 不支持周期: {timeframe}")

    code = _baostock_code(symbol)
    end = end_date or _cn_now().strftime("%Y-%m-%d")
    if start_date:
        start = start_date
    else:
        days = _calendar_days_for_bars(n if timeframe != "4h" else n * 4)
        start = (_cn_now() - timedelta(days=days)).strftime("%Y-%m-%d")

    import baostock as bs

    fields = (
        "date,time,code,open,high,low,close,volume"
        if freq != "d"
        else "date,code,open,high,low,close,volume"
    )

    def _query() -> list[list[str]]:
        rs = bs.query_history_k_data_plus(
            code,
            fields,
            start_date=start,
            end_date=end,
            frequency=freq,
            adjustflag="2",
        )
        return _collect_baostock_rows(rs)

    data = _BaostockSession.execute(f"{timeframe} 分钟线", _query)
    if not data:
        return []

    import pandas as pd

    cols = [x.strip() for x in fields.split(",")]
    df = pd.DataFrame(data, columns=cols)
    if freq != "d":
        time_digits = df["time"].astype(str).str.replace(r"\D", "", regex=True)
        # Baostock time: YYYYMMDDHHMMSSmmm (e.g. 20260302103000000)
        bar_time = pd.to_datetime(time_digits.str.slice(0, 14), format="%Y%m%d%H%M%S", errors="coerce")
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
    else:
        norm = _normalize_ohlcv_df(df, time_col="date")
        rows = _df_to_bars_asc(norm, time_col="date")

    return rows[-n:]
