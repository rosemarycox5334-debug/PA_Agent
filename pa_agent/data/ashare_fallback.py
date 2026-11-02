"""Free A-share quote fallbacks when East Money HTTP endpoints fail.

Chain (all free / no paid key):
    1. Tencent  (qt.gtimg.cn / ifzq.gtimg.cn / web.ifzq.gtimg.cn) - plain HTTP
    2. zzshare  (optional dependency, pip install zzshare)         - hosted API
    3. Sina     (hq.sinajs.cn)                                     - last resort (spot only)

Wired into eastmoney_client at four points; when East Money is unreachable
the free chain transparently takes over:
    fetch_stock_quote       -> fallback_spot_quote
    fetch_stock_order_book  -> fallback_order_book
    fetch_stock_daily       -> fallback_daily
    fetch_stock_minute      -> fallback_minute

Public helpers mirror the East Money client contracts:
    fallback_spot_quote(symbol)          -> dict like fetch_stock_quote()
    fallback_order_book(symbol)          -> StockOrderBook | None
    fallback_daily(...)                  -> list[dict] like fetch_stock_daily()
    fallback_minute(...)                 -> list[dict] like fetch_stock_minute()

All OHLCV rows use ascending time order with volume in lots (shou),
amount in CNY - identical to eastmoney_client._parse_klines.
"""
from __future__ import annotations

import contextlib
import logging
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

# 腾讯 K 线三个入口是同一后端、限流各自独立：单入口约 600 次后返回空 JSON，
# 且 web.ifzq 的分钟线路由会 302 到 web3 而失败。某入口失败即冷却 120s 换下一个。
TENCENT_KLINE_HOSTS: tuple[str, ...] = (
    "https://web.ifzq.gtimg.cn",
    "https://proxy.finance.qq.com/ifzqgtimg",
    "https://ifzq.gtimg.cn",
)
_TENCENT_DAILY_PATH = "/appstock/app/fqkline/get"
_TENCENT_MINUTE_PATH = "/appstock/app/kline/mkline"
_TENCENT_HOST_COOLDOWN_S = 120.0
_tencent_host_down_until: dict[str, float] = {}

#: 日/周/月单次可取的根数上限（腾讯侧限制）
TENCENT_DAILY_MAX_BARS = 640
#: 分钟线单次可取的根数上限
TENCENT_MINUTE_MAX_BARS = 320

_TENCENT_RT = "https://qt.gtimg.cn/q="
_SINA_RT = "https://hq.sinajs.cn/list="


def _tencent_kline_hosts_available() -> list[str]:
    """当前未处于冷却期的入口（按优先级）。"""
    now = time.monotonic()
    return [h for h in TENCENT_KLINE_HOSTS if _tencent_host_down_until.get(h, 0.0) <= now]


def _tencent_kline_payload(path: str, param: str) -> dict[str, Any] | None:
    """按顺序尝试腾讯 K 线三个入口，返回 ``data`` 节点。

    空响应、非 200、异常都视为该入口被限流/失效：冷却 ``_TENCENT_HOST_COOLDOWN_S``
    秒后换下一个入口；三个都不可用返回 ``None``（调用方自己降级）。
    """
    requests = _http()
    errors: list[str] = []
    for host in TENCENT_KLINE_HOSTS:
        if _tencent_host_down_until.get(host, 0.0) > time.monotonic():
            continue
        try:
            resp = requests.get(
                host + path, params={"param": param}, headers=_UA, timeout=(8, 20)
            )
            if resp.status_code != 200 or not resp.text.strip():
                raise RuntimeError(f"HTTP {resp.status_code}")
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{host}: {type(exc).__name__}")
            _tencent_host_down_until[host] = time.monotonic() + _TENCENT_HOST_COOLDOWN_S
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and data:
            return data
        errors.append(f"{host}: 空数据")
        _tencent_host_down_until[host] = time.monotonic() + _TENCENT_HOST_COOLDOWN_S
    logger.debug("腾讯 K 线三入口均不可用：%s", "; ".join(errors) or "全部冷却中")
    return None



def _tencent_code(symbol: str) -> str | None:
    """6-digit code -> sh600519 / sz000001; indices keep sh/sz prefix."""
    raw = (symbol or "").strip().lower()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 6:
        return None
    digits = digits[-6:]
    if raw.startswith(("sh", "sz")):
        return raw[:2] + digits
    if digits.startswith(("5", "6", "9")):
        return "sh" + digits
    if digits in {"000300", "000016", "000905", "000852"}:
        return "sh" + digits
    return "sz" + digits


def _zzshare_code(symbol: str) -> str | None:
    """6-digit -> 600519.SH style ts_code."""
    raw = (symbol or "").strip().lower()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 6:
        return None
    digits = digits[-6:]
    if raw.startswith("sh") or (
        not raw.startswith("sz") and digits.startswith(("5", "6", "9"))
    ):
        return f"{digits}.SH"
    if digits in {"000300", "000016", "000905", "000852"}:
        return f"{digits}.SH"
    return f"{digits}.SZ"


def _http():
    """Shared requests getter (requests is pulled in by akshare)."""
    import requests

    return requests


def _f(text: Any) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# Tencent
# --------------------------------------------------------------------------- #
def _tencent_spot_raw(symbol: str) -> list[str] | None:
    code = _tencent_code(symbol)
    if not code:
        return None
    try:
        requests = _http()
        resp = requests.get(_TENCENT_RT + code, headers=_UA, timeout=6)
        resp.encoding = "gbk"
        if resp.status_code != 200 or "~" not in resp.text:
            return None
        body = resp.text.strip().split(";")[0]
        if '="' not in body:
            return None
        payload = body.split('="', 1)[1].rstrip('"')
        parts = payload.split("~")
        return parts if len(parts) > 40 else None
    except Exception as exc:
        logger.debug("Tencent spot failed for %s: %s", symbol, exc)
        return None


def tencent_spot_quote(symbol: str) -> dict[str, Any] | None:
    parts = _tencent_spot_raw(symbol)
    if not parts:
        return None
    price = _f(parts[3])
    if price <= 0:
        return None
    return {"code": parts[2], "name": parts[1] or parts[2], "price": price}


def tencent_order_book(symbol: str):
    from pa_agent.data.eastmoney_quote import OrderBookLevel, StockOrderBook

    parts = _tencent_spot_raw(symbol)
    if not parts:
        return None
    price = _f(parts[3])
    if price <= 0:
        return None
    bids = [
        OrderBookLevel(price=_f(parts[9 + i * 2]), volume=int(_f(parts[10 + i * 2])))
        for i in range(5)
    ]
    asks = [
        OrderBookLevel(price=_f(parts[19 + i * 2]), volume=int(_f(parts[20 + i * 2])))
        for i in range(5)
    ]
    pre_close = _f(parts[4])
    pct = ((price - pre_close) / pre_close * 100.0) if pre_close else 0.0
    return StockOrderBook(
        code=parts[2],
        name=parts[1] or parts[2],
        price=price,
        pct_chg=pct,
        open=_f(parts[5]),
        high=_f(parts[33]),
        low=_f(parts[34]),
        prev_close=pre_close,
        volume=int(_f(parts[36])),
        amount=_f(parts[37]) * 10000.0,
        bids=bids,
        asks=asks,
        depth_levels=5,
        depth_source="tencent_free",
    )


def _tencent_kline_rows(raw: list[Any]) -> list[dict[str, Any]]:
    """Tencent rows: [time, open, close, high, low, volume, ...]."""
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        time_text = str(item[0]).strip()
        try:
            if len(time_text) == 8 and time_text.isdigit():
                bar_time = datetime.strptime(time_text, "%Y%m%d")
            elif len(time_text) == 10:
                bar_time = datetime.strptime(time_text, "%Y-%m-%d")
            elif len(time_text) == 12:
                bar_time = datetime.strptime(time_text, "%Y%m%d%H%M")
            else:
                bar_time = datetime.strptime(time_text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        amount = 0.0
        if len(item) > 7:
            amount = _f(item[7])
        rows.append(
            {
                "time": bar_time,
                "open": _f(item[1]),
                "close": _f(item[2]),
                "high": _f(item[3]),
                "low": _f(item[4]),
                "volume": _f(item[5]),
                "amount": amount,
                "pct_chg": None,
            }
        )
    rows.sort(key=lambda r: r["time"])
    return rows


_TENCENT_PERIODS: dict[str, str] = {"1d": "day", "1w": "week", "1M": "month"}


def tencent_period(
    symbol: str,
    *,
    timeframe: str = "1d",
    count: int = TENCENT_DAILY_MAX_BARS,
    adjust: str = "qfq",
) -> list[dict[str, Any]]:
    """腾讯 日/周/月 K 线（前/后复权 / 不复权），升序 rows；失败返回 []。

    比东财快且不封 IP：实测单次 0.2~0.9s。本接口**没有成交额**，需要成交额时
    用东财；PA 的提示词只用成交量，故日线主路径可以直接用它。
    """
    code = _tencent_code(symbol)
    if not code:
        return []
    period = _TENCENT_PERIODS.get(str(timeframe), "day")
    fq = {"qfq": "qfq", "hfq": "hfq"}.get(adjust, "")
    n = max(1, min(int(count), TENCENT_DAILY_MAX_BARS))
    data = _tencent_kline_payload(_TENCENT_DAILY_PATH, f"{code},{period},,,{n},{fq}")
    if not data:
        return []
    node = data.get(code)
    if not isinstance(node, dict):
        return []
    raw = node.get(f"{fq}{period}") or node.get(period) or []
    if not isinstance(raw, list) or not raw:
        return []
    return _tencent_kline_rows(raw)


def tencent_daily(
    symbol: str, *, start_date: str, end_date: str, adjust: str = "qfq"
) -> list[dict[str, Any]]:
    rows = tencent_period(symbol, timeframe="1d", adjust=adjust)
    if not rows:
        return []
    start = datetime.strptime(start_date[:10], "%Y-%m-%d") if len(start_date) >= 10 else None
    end = datetime.strptime(end_date[:10], "%Y-%m-%d") if len(end_date) >= 10 else None
    return [
        r
        for r in rows
        if (start is None or r["time"] >= start) and (end is None or r["time"] <= end)
    ]


def tencent_minute(
    symbol: str, *, period: str, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    """period: '1' | '5' | '15' | '30' | '60' (East Money klt style)."""
    code = _tencent_code(symbol)
    if not code:
        return []
    p = f"m{period}"
    data = _tencent_kline_payload(
        _TENCENT_MINUTE_PATH, f"{code},{p},,{TENCENT_MINUTE_MAX_BARS}"
    )
    if not data:
        return []
    node = data.get(code)
    if not isinstance(node, dict):
        return []
    raw = node.get(p) or node.get(f"qfq{p}") or []
    if not isinstance(raw, list) or not raw:
        return []
    rows = _tencent_kline_rows(raw)
    start = datetime.strptime(start_date[:19], "%Y-%m-%d %H:%M:%S") if len(start_date) >= 10 else None
    end = datetime.strptime(end_date[:19], "%Y-%m-%d %H:%M:%S") if len(end_date) >= 10 else None
    return [
        r
        for r in rows
        if (start is None or r["time"] >= start) and (end is None or r["time"] <= end)
    ]


# --------------------------------------------------------------------------- #
# zzshare
# --------------------------------------------------------------------------- #
_zzshare_cooldown_until: float = 0.0
_ZZSHARE_COOLDOWN_S = 60.0


def _zzshare_api():
    """Build a zzshare DataApi with blocking retries disabled.

    zzshare retries 429 internally by sleeping on the Retry-After header
    (up to ~55s per attempt, 3 attempts). That would stall the 2.5s polling
    loop, so we replace the retry helper with a single non-blocking GET and
    trip a 60s circuit breaker on any 429.
    """
    global _zzshare_cooldown_until
    import time

    if time.monotonic() < _zzshare_cooldown_until:
        return None
    try:
        import zzshare

        api = zzshare.pro_api()
    except Exception as exc:
        logger.debug("zzshare unavailable: %s", exc)
        return None

    def _fast_request(url, params=None, max_retries=0):
        global _zzshare_cooldown_until
        import time as _t

        import requests

        res = requests.get(
            url, params=params, headers=api.headers, timeout=api.timeout
        )
        if res.status_code == 429:
            _zzshare_cooldown_until = _t.monotonic() + _ZZSHARE_COOLDOWN_S
            logger.info("zzshare rate limited, cooling down %ss", _ZZSHARE_COOLDOWN_S)
        return res

    with contextlib.suppress(Exception):
        api._request_with_retry = _fast_request
    return api


def zzshare_spot_quote(symbol: str) -> dict[str, Any] | None:
    api = _zzshare_api()
    code = _zzshare_code(symbol)
    if api is None or not code:
        return None
    try:
        df = api.rt_k(ts_code=code)
    except Exception as exc:
        logger.debug("zzshare rt_k failed for %s: %s", symbol, exc)
        return None
    if df is None or getattr(df, "empty", True):
        return None
    row = df.iloc[0]
    price = _f(row.get("close"))
    if price <= 0:
        return None
    return {
        "code": str(row.get("ts_code", code))[:6],
        "name": str(row.get("name") or code),
        "price": price,
    }


def zzshare_order_book(symbol: str):
    from pa_agent.data.eastmoney_quote import OrderBookLevel, StockOrderBook

    api = _zzshare_api()
    code = _zzshare_code(symbol)
    if api is None or not code:
        return None
    try:
        df = api.rt_k(ts_code=code)
    except Exception as exc:
        logger.debug("zzshare rt_k failed for %s: %s", symbol, exc)
        return None
    if df is None or getattr(df, "empty", True):
        return None
    row = df.iloc[0]
    price = _f(row.get("close"))
    if price <= 0:
        return None
    pre_close = _f(row.get("pre_close"))
    pct = ((price - pre_close) / pre_close * 100.0) if pre_close else 0.0
    bids = [
        OrderBookLevel(price=_f(row.get("bid_price1")), volume=int(_f(row.get("bid_volume1"))) // 100)
    ]
    asks = [
        OrderBookLevel(price=_f(row.get("ask_price1")), volume=int(_f(row.get("ask_volume1"))) // 100)
    ]
    return StockOrderBook(
        code=str(row.get("ts_code", code))[:6],
        name=str(row.get("name") or code),
        price=price,
        pct_chg=pct,
        open=_f(row.get("open")),
        high=_f(row.get("high")),
        low=_f(row.get("low")),
        prev_close=pre_close,
        volume=int(_f(row.get("vol"))) // 100,
        amount=_f(row.get("amount")),
        bids=bids,
        asks=asks,
        depth_levels=1,
        depth_source="zzshare_free",
    )


def zzshare_daily(
    symbol: str, *, start_date: str, end_date: str, adjust: str = "qfq"
) -> list[dict[str, Any]]:
    import zzshare

    code = _zzshare_code(symbol)
    if not code:
        return []
    beg = start_date.replace("-", "")[:8] or None
    end = end_date.replace("-", "")[:8] or None
    try:
        df = zzshare.daily(ts_code=code, start_date=beg, end_date=end)
    except Exception as exc:
        logger.debug("zzshare daily failed for %s: %s", symbol, exc)
        return []
    if df is None or getattr(df, "empty", True):
        return []
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            bar_time = datetime.strptime(str(row["trade_date"]), "%Y%m%d")
        except Exception:
            continue
        rows.append(
            {
                "time": bar_time,
                "open": _f(row.get("open")),
                "close": _f(row.get("close")),
                "high": _f(row.get("high")),
                "low": _f(row.get("low")),
                "volume": _f(row.get("vol")) / 100.0,
                "amount": _f(row.get("amount")),
                "pct_chg": _f(row.get("pct_chg")) if row.get("pct_chg") is not None else None,
            }
        )
    rows.sort(key=lambda r: r["time"])
    return rows


def zzshare_minute(
    symbol: str, *, period: str, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    api = _zzshare_api()
    code = _zzshare_code(symbol)
    if api is None or not code:
        return []
    freq_map = {"1": "1min", "5": "5min", "15": "15min", "30": "30min", "60": "60min"}
    freq = freq_map.get(str(period))
    if not freq:
        return []
    try:
        df = api.stk_mins(ts_code=code, freq=freq)
    except Exception as exc:
        logger.debug("zzshare stk_mins failed for %s: %s", symbol, exc)
        return []
    if df is None or getattr(df, "empty", True):
        return []
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            bar_time = datetime.strptime(str(row["trade_time"])[:12], "%Y%m%d%H%M")
        except Exception:
            continue
        rows.append(
            {
                "time": bar_time,
                "open": _f(row.get("open")),
                "close": _f(row.get("close")),
                "high": _f(row.get("high")),
                "low": _f(row.get("low")),
                "volume": _f(row.get("vol")) / 100.0,
                "amount": _f(row.get("amount")),
                "pct_chg": None,
            }
        )
    rows.sort(key=lambda r: r["time"])
    start = datetime.strptime(start_date[:19], "%Y-%m-%d %H:%M:%S") if len(start_date) >= 10 else None
    end = datetime.strptime(end_date[:19], "%Y-%m-%d %H:%M:%S") if len(end_date) >= 10 else None
    return [
        r
        for r in rows
        if (start is None or r["time"] >= start) and (end is None or r["time"] <= end)
    ]


# --------------------------------------------------------------------------- #
# Sina (last resort, real-time only)
# --------------------------------------------------------------------------- #
def sina_spot_quote(symbol: str) -> dict[str, Any] | None:
    code = _tencent_code(symbol)
    if not code:
        return None
    try:
        requests = _http()
        resp = requests.get(
            _SINA_RT + code,
            headers={**_UA, "Referer": "https://finance.sina.com.cn/"},
            timeout=6,
        )
        resp.encoding = "gbk"
        if resp.status_code != 200 or '="' not in resp.text:
            return None
        payload = resp.text.split('="', 1)[1].rstrip('";\n')
        parts = payload.split(",")
        if len(parts) < 4:
            return None
        price = _f(parts[3])
        if price <= 0:
            return None
        # Sina: parts[0] is the stock name, not the code
        name = parts[0] or code
        digits = "".join(ch for ch in code if ch.isdigit())[-6:]
        return {"code": digits, "name": name, "price": price}
    except Exception as exc:
        logger.debug("Sina spot failed for %s: %s", symbol, exc)
        return None


# --------------------------------------------------------------------------- #
# Public chained helpers
# --------------------------------------------------------------------------- #
def fallback_spot_quote(symbol: str) -> dict[str, Any] | None:
    for fn in (tencent_spot_quote, zzshare_spot_quote, sina_spot_quote):
        try:
            res = fn(symbol)
        except Exception as exc:
            logger.debug("fallback spot %s failed: %s", fn.__name__, exc)
            continue
        if res:
            logger.info("A-share spot fallback hit: %s", fn.__name__)
            return res
    return None


def fallback_order_book(symbol: str):
    for fn in (tencent_order_book, zzshare_order_book):
        try:
            res = fn(symbol)
        except Exception as exc:
            logger.debug("fallback order book %s failed: %s", fn.__name__, exc)
            continue
        if res is not None:
            logger.info("A-share order book fallback hit: %s", fn.__name__)
            return res
    return None


def fallback_daily(
    symbol: str, *, start_date: str, end_date: str, adjust: str = "qfq"
) -> list[dict[str, Any]]:
    for fn in (tencent_daily, zzshare_daily):
        try:
            rows = fn(symbol, start_date=start_date, end_date=end_date, adjust=adjust)
        except Exception as exc:
            logger.debug("fallback daily %s failed: %s", fn.__name__, exc)
            continue
        if rows:
            logger.info("A-share daily fallback hit: %s (%d rows)", fn.__name__, len(rows))
            return rows
    return []


def fallback_minute(
    symbol: str, *, period: str, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    for fn in (tencent_minute, zzshare_minute):
        try:
            rows = fn(symbol, period=period, start_date=start_date, end_date=end_date)
        except Exception as exc:
            logger.debug("fallback minute %s failed: %s", fn.__name__, exc)
            continue
        if rows:
            logger.info("A-share minute fallback hit: %s (%d rows)", fn.__name__, len(rows))
            return rows
    return []
