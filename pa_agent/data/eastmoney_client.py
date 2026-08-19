"""Low-level HTTP client for East Money (东方财富) public quote APIs.

Directly calls the same JSON endpoints used by quote.eastmoney.com
(``push2his.eastmoney.com/api/qt/stock/kline/get`` etc.).
Uses ``curl_cffi`` when available to pass TLS fingerprint checks.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)

# CDN mirrors — ``push2delay`` is reachable when ``push2`` / ``push2his`` reset TLS.
# K-line: connect to delay mirror but send ``Host: push2his.eastmoney.com``.
_DELAY_MIRROR_HOST = "push2delay.eastmoney.com"
_HIS_LOGICAL_HOST = "push2his.eastmoney.com"
_KLINE_HOSTS: tuple[str, ...] = (
    _DELAY_MIRROR_HOST,
    "push2his.eastmoney.com",
    "33.push2his.eastmoney.com",
    "63.push2his.eastmoney.com",
    "7.push2his.eastmoney.com",
    "38.push2his.eastmoney.com",
    "48.push2his.eastmoney.com",
)
_QUOTE_HOSTS: tuple[str, ...] = (
    _DELAY_MIRROR_HOST,
    "push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "91.push2.eastmoney.com",
    "38.push2.eastmoney.com",
    "48.push2.eastmoney.com",
    "62.push2.eastmoney.com",
    "63.push2.eastmoney.com",
    "7.push2.eastmoney.com",
    "33.push2.eastmoney.com",
)
# Web front-end token (quote.eastmoney.com gridlist / kline pages)
_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_WBP2U = "|0|0|0|web"
_REFERER_CLIST = "https://quote.eastmoney.com/center/gridlist.html"
_REFERER_KLINE = "https://quote.eastmoney.com/"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

_THROTTLE_NORMAL_S = 0.45
_THROTTLE_BULK_S = 0.95
_throttle_interval_s = _THROTTLE_NORMAL_S
_last_fetch_mono: float = 0.0
_threading = __import__("threading")
_request_lock = _threading.Lock()
_host_rr_lock = _threading.Lock()
_request_slots = _threading.Semaphore(1)
_host_rr: dict[str, int] = {"kline": 0, "quote": 0}

try:
    from curl_cffi import requests as _http
    _IMPERSONATE_OPTIONS: tuple[str | None, ...] = (
        "chrome120",
        "chrome116",
        "chrome131",
        "edge101",
        "safari15_5",
        None,
    )
except ImportError:
    import requests as _http  # type: ignore[no-redef]
    _IMPERSONATE_OPTIONS = (None,)


def _is_transient_http_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    needles = (
        "connection closed",
        "connection reset",
        "connection aborted",
        "curl: (56)",
        "curl: (52)",
        "curl: (55)",
        "curl: (28)",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "502",
        "503",
        "504",
        "429",
        "ssl",
        "eof",
        "broken pipe",
        "reset by peer",
    )
    return any(n in text for n in needles)


def _rotate_hosts(hosts: tuple[str, ...], kind: str) -> tuple[str, ...]:
    """Round-robin host order so a bad CDN node is not always tried first."""
    if not hosts:
        return hosts
    with _host_rr_lock:
        start = _host_rr.get(kind, 0) % len(hosts)
        _host_rr[kind] = start + 1
    return hosts[start:] + hosts[:start]


def _prioritize_primary_host(
    hosts: tuple[str, ...],
    primary: str,
) -> tuple[str, ...]:
    """Always try the reachable delay mirror first (rotation must not skip it)."""
    if primary not in hosts:
        return hosts
    return (primary,) + tuple(h for h in hosts if h != primary)


def is_transient_http_error(exc: BaseException) -> bool:
    """Public wrapper for retry / user messaging."""
    return _is_transient_http_error(exc)


class EastMoneyError(Exception):
    """Base error from East Money client."""


class EastMoneyTransientError(EastMoneyError):
    """Retryable network / rate-limit error."""


def stock_market_code(symbol: str) -> int:
    """1 = Shanghai (6/9), 0 = Shenzhen."""
    return 1 if symbol[:1] in ("6", "9") else 0


def stock_secid(symbol: str) -> str:
    code = symbol[-6:] if len(symbol) > 6 else symbol
    return f"{stock_market_code(code)}.{code}"


def index_secid(symbol: str) -> str:
    sym = symbol.lower()
    if sym.startswith("sh"):
        return f"1.{sym[2:]}"
    if sym.startswith("sz"):
        return f"0.{sym[2:]}"
    if sym.startswith("399"):
        return f"0.{sym}"
    return f"1.{sym}"


def set_screener_bulk_mode(enabled: bool) -> None:
    """Slower throttle + limited HTTP concurrency for bulk screening."""
    global _throttle_interval_s, _request_slots
    _throttle_interval_s = _THROTTLE_BULK_S if enabled else _THROTTLE_NORMAL_S
    _request_slots = _threading.Semaphore(2 if enabled else 1)


def _throttle(*, backoff_s: float = 0.0) -> None:
    global _last_fetch_mono
    now = time.monotonic()
    wait = max(_throttle_interval_s, backoff_s) - (now - _last_fetch_mono)
    if wait > 0:
        time.sleep(wait)
    _last_fetch_mono = time.monotonic()


def _http_get_once(
    url: str,
    params: dict[str, Any],
    *,
    timeout: float,
    impersonate: str | None,
    referer: str,
    host_header: str | None = None,
) -> dict[str, Any]:
    headers = {
        **_DEFAULT_HEADERS,
        "Referer": referer,
    }
    if host_header:
        headers["Host"] = host_header
    kwargs: dict[str, Any] = {
        "params": params,
        "headers": headers,
        "timeout": timeout,
    }
    if impersonate:
        kwargs["impersonate"] = impersonate
    resp = _http.get(url, **kwargs)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise EastMoneyTransientError("东方财富返回非 JSON 对象")
    rc = payload.get("rc")
    if rc not in (None, 0, "0"):
        raise EastMoneyTransientError(f"东方财富业务错误 rc={rc}")
    return payload


def _http_get(
    url: str,
    params: dict[str, Any],
    *,
    timeout: float,
    referer: str,
    host_header: str | None = None,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt, impersonate in enumerate(_IMPERSONATE_OPTIONS):
        if attempt > 0:
            _throttle(backoff_s=0.35 * attempt)
        try:
            return _http_get_once(
                url,
                params,
                timeout=timeout,
                impersonate=impersonate,
                referer=referer,
                host_header=host_header,
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_transient_http_error(exc):
                raise
            logger.debug(
                "East Money GET retry impersonate=%s: %s",
                impersonate,
                exc,
            )
    raise EastMoneyTransientError(str(last_exc or "HTTP request failed"))


def _kline_host_header(host: str) -> str | None:
    """Delay mirror serves push2his routes when the logical Host header is set."""
    if host == _DELAY_MIRROR_HOST:
        return _HIS_LOGICAL_HOST
    return None


def _get_json_on_hosts(
    hosts: tuple[str, ...],
    path: str,
    params: dict[str, Any],
    *,
    timeout: float = 20.0,
    host_kind: str = "kline",
    referer: str = _REFERER_KLINE,
    max_rounds: int = 2,
    max_hosts: int | None = None,
    host_header: str | None = None,
) -> dict[str, Any]:
    """Try CDN hosts with retries; serialised to avoid parallel connection drops.

    Use ``max_rounds=1`` and ``max_hosts=3`` for startup / UI paths so a dead
    network cannot block the main thread for minutes.
    """
    last_exc: Exception | None = None
    ordered = _prioritize_primary_host(
        _rotate_hosts(hosts, host_kind),
        _DELAY_MIRROR_HOST,
    )
    if max_hosts is not None:
        ordered = ordered[: max(1, int(max_hosts))]
    with _request_slots:
        for round_idx in range(max_rounds):
            for host_idx, host in enumerate(ordered):
                url = f"https://{host}{path}"
                effective_host_header = host_header
                if effective_host_header is None and host_kind == "kline":
                    effective_host_header = _kline_host_header(host)
                try:
                    if host_idx or round_idx:
                        _throttle(backoff_s=0.4 * round_idx + 0.15 * host_idx)
                    return _http_get(
                        url,
                        params,
                        timeout=timeout,
                        referer=referer,
                        host_header=effective_host_header,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if not _is_transient_http_error(exc):
                        raise EastMoneyTransientError(str(exc)) from exc
                    logger.debug(
                        "East Money host %s round=%d failed: %s",
                        host,
                        round_idx,
                        exc,
                    )
            if round_idx + 1 < max_rounds:
                time.sleep(min(4.0, 0.8 * (2**round_idx)))
    msg = str(last_exc or "all hosts failed")
    if _is_transient_http_error(last_exc or Exception(msg)):
        raise EastMoneyTransientError(
            "东方财富接口连接中断（筛选请求过多时易触发）。"
            "请等待 1–2 分钟后重试，或减少「行情页数」。"
        ) from last_exc
    raise EastMoneyTransientError(msg) from last_exc


def _parse_klines(raw: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in raw:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        time_text = parts[0].strip()
        try:
            if len(time_text) == 10:
                bar_time = datetime.strptime(time_text, "%Y-%m-%d")
            elif len(time_text) == 16:
                bar_time = datetime.strptime(time_text, "%Y-%m-%d %H:%M")
            else:
                bar_time = datetime.strptime(time_text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        row: dict[str, Any] = {
            "time": bar_time,
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": float(parts[5]),
            "amount": 0.0,
            "pct_chg": None,
        }
        if len(parts) > 6 and parts[6].strip():
            try:
                row["amount"] = float(parts[6])
            except ValueError:
                pass
        if len(parts) > 8 and parts[8].strip():
            try:
                row["pct_chg"] = float(parts[8])
            except ValueError:
                pass
        rows.append(row)
    return rows


def _fetch_kline(params: dict[str, Any], *, resilient: bool = False) -> list[dict[str, Any]]:
    kw: dict[str, Any] = {}
    if resilient:
        kw = {"max_rounds": 4, "timeout": 30.0}
    data = _get_json_on_hosts(
        _KLINE_HOSTS, "/api/qt/stock/kline/get", params, **kw
    )
    klines = (data.get("data") or {}).get("klines") or []
    return _parse_klines(klines)


def _fetch_trends(params: dict[str, Any]) -> list[dict[str, Any]]:
    data = _get_json_on_hosts(_KLINE_HOSTS, "/api/qt/stock/trends2/get", params)
    trends = (data.get("data") or {}).get("trends") or []
    return _parse_klines(trends)


_KLT_MAP = {
    "1d": "101",
    "1w": "102",
    "1M": "103",
}


def _daily_kline_params(
    symbol: str,
    *,
    adjust: str,
    klt: str = "101",
    beg: str | None = None,
    end: str | None = None,
    lmt: int | None = None,
) -> dict[str, Any]:
    adjust_map = {"qfq": "1", "hfq": "2", "": "0", "none": "0"}
    params: dict[str, Any] = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": _UT,
        "klt": klt,
        "fqt": adjust_map.get(adjust, "1"),
        "secid": stock_secid(symbol),
    }
    if lmt is not None:
        params["end"] = end or "20500101"
        params["lmt"] = str(max(5, min(int(lmt), 1200)))
    else:
        params["beg"] = beg or "19900101"
        params["end"] = end or "20500101"
    return params


def fetch_stock_daily(
    symbol: str,
    *,
    start_date: str,
    end_date: str,
    adjust: str = "qfq",
) -> list[dict[str, Any]]:
    params = _daily_kline_params(
        symbol,
        adjust=adjust,
        beg=start_date,
        end=end_date,
    )
    return _fetch_kline(params)


def _fetch_kline_resilient(params: dict[str, Any]) -> list[dict[str, Any]]:
    return _fetch_kline(params, resilient=True)


def fetch_stock_daily_recent(
    symbol: str,
    *,
    n: int = 120,
    adjust: str = "qfq",
) -> list[dict[str, Any]]:
    """Recent daily bars via official ``lmt`` param (same as quote.eastmoney.com kline)."""
    params = _daily_kline_params(symbol, adjust=adjust, klt="101", lmt=n)
    return _fetch_kline_resilient(params)


def fetch_stock_period_recent(
    symbol: str,
    *,
    timeframe: str,
    n: int = 120,
    adjust: str = "qfq",
) -> list[dict[str, Any]]:
    """Recent daily / weekly / monthly bars (klt 101/102/103)."""
    klt = _KLT_MAP.get(timeframe, "101")
    params = _daily_kline_params(symbol, adjust=adjust, klt=klt, lmt=n)
    return _fetch_kline_resilient(params)


def fetch_stock_period(
    symbol: str,
    *,
    timeframe: str,
    start_date: str,
    end_date: str,
    adjust: str = "qfq",
    is_index: bool = False,
) -> list[dict[str, Any]]:
    """Fetch daily / weekly / monthly bars inside an inclusive date range."""
    klt = _KLT_MAP.get(timeframe, "101")
    params = _daily_kline_params(
        symbol,
        adjust=adjust,
        klt=klt,
        beg=start_date,
        end=end_date,
    )
    if is_index:
        params["secid"] = index_secid(symbol)
        params["fqt"] = "0"
    return _fetch_kline(params)


def fetch_stock_daily_resilient(
    symbol: str,
    *,
    n: int = 120,
    adjust: str = "qfq",
    max_attempts: int = 4,
) -> list[dict[str, Any]]:
    """Retry daily kline fetch — for screener / backtest loops."""
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fetch_stock_daily_recent(symbol, n=n, adjust=adjust)
        except EastMoneyTransientError as exc:
            last_exc = exc
            if attempt + 1 < max_attempts:
                time.sleep(min(5.0, 0.8 * (2**attempt)))
    raise last_exc or EastMoneyTransientError("日线拉取失败")


def _secid(symbol: str, *, is_index: bool) -> str:
    return index_secid(symbol) if is_index else stock_secid(symbol)


def fetch_stock_minute(
    symbol: str,
    *,
    period: str,
    start_date: str,
    end_date: str,
    adjust: str = "qfq",
    is_index: bool = False,
) -> list[dict[str, Any]]:
    adjust_map = {"qfq": "1", "hfq": "2", "": "0", "none": "0"}
    secid = _secid(symbol, is_index=is_index)
    fqt = "0" if is_index else adjust_map.get(adjust, "1")
    if period == "1" and not is_index:
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "ut": _UT,
            "ndays": "5",
            "iscr": "0",
            "secid": secid,
        }
        rows = _fetch_trends(params)
    else:
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "ut": _UT,
            "klt": period,
            "fqt": fqt,
            "secid": secid,
            "beg": "0",
            "end": "20500000",
        }
        rows = _fetch_kline(params)

    start_dt = datetime.strptime(start_date[:19], "%Y-%m-%d %H:%M:%S")
    end_dt = datetime.strptime(end_date[:19], "%Y-%m-%d %H:%M:%S")
    return [r for r in rows if start_dt <= r["time"] <= end_dt]


def fetch_index_daily(
    symbol: str,
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    params = {
        "secid": index_secid(symbol),
        "fields1": "f1,f2,f3,f4,f5",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": "101",
        "fqt": "0",
        "beg": start_date,
        "end": end_date,
    }
    return _fetch_kline(params)


def fetch_spot_price(symbol: str) -> float | None:
    quote = fetch_stock_quote(symbol)
    return quote.get("price") if quote else None


# A-share: 沪深主板 + 创业板 + 科创板 (same fs as quote.eastmoney.com gridlist)
_CLIST_FS_A_SHARE = (
    "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
)
_CLIST_FIELDS_UNIVERSE = (
    "f12,f14,f2,f3,f4,f5,f6,f7,f15,f16,f17,f18,f8,f9,f10,f20,f21,f23"
)
# clist 单页上限；全市场用分页拉取（比 pz=5000 单请求更稳、进度可更新）
_CLIST_PAGE_SIZE_DEFAULT = 100


def _parse_clist_rows(diff: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in diff:
        if not isinstance(item, dict):
            continue
        code = str(item.get("f12", "")).strip()
        if not (code.isdigit() and len(code) == 6):
            continue
        rows.append(
            {
                "code": code,
                "name": str(item.get("f14", "") or code),
                "price": _safe_float(item.get("f2")),
                "pct_chg": _safe_float(item.get("f3")),
                "volume": _safe_float(item.get("f5")),
                "amount": _safe_float(item.get("f6")),
                "turnover_pct": _safe_float(item.get("f8")),
                "volume_ratio": _safe_float(item.get("f10")),
                "total_cap": _safe_float(item.get("f20")),
                "float_cap": _safe_float(item.get("f21")),
            }
        )
    return rows


def fetch_stock_universe_page(
    *,
    page: int = 1,
    page_size: int = 100,
    sort_field: str = "f3",
) -> tuple[list[dict[str, Any]], int | None]:
    """Fetch one page of A-share spot rows from East Money ``/api/qt/clist/get``.

    Returns (rows, total_count). Each row has:
    code, name, price, pct_chg, volume, amount, turnover_pct, volume_ratio,
    total_cap, float_cap (amounts in yuan when fltt=2).
    """
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    params = {
        "pn": str(page),
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "dect": "1",
        "ut": _UT,
        "wbp2u": _WBP2U,
        "fltt": "2",
        "invt": "2",
        "fid": sort_field,
        "fs": _CLIST_FS_A_SHARE,
        "fields": _CLIST_FIELDS_UNIVERSE,
    }
    data = _get_json_on_hosts(
        _QUOTE_HOSTS,
        "/api/qt/clist/get",
        params,
        timeout=12.0,
        host_kind="quote",
        referer=_REFERER_CLIST,
        max_rounds=2,
        max_hosts=1,
    )
    payload = data.get("data") or {}
    diff = payload.get("diff") or []
    total = payload.get("total")
    rows = _parse_clist_rows(diff if isinstance(diff, list) else [])
    try:
        total_n = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_n = None
    return rows, total_n


def _fetch_universe_page_resilient(
    *,
    page: int,
    page_size: int,
    max_attempts: int = 3,
    on_retry: Callable[[int, int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fetch_stock_universe_page(page=page, page_size=page_size)
        except EastMoneyTransientError as exc:
            last_exc = exc
            if attempt + 1 < max_attempts:
                if on_retry is not None:
                    on_retry(page, attempt + 1, max_attempts)
                time.sleep(min(4.0, 0.6 * (2**attempt)))
    raise last_exc or EastMoneyTransientError("行情列表拉取失败")


def iter_stock_universe(
    *,
    page_size: int = _CLIST_PAGE_SIZE_DEFAULT,
    max_pages: int | None = None,
    on_page: Callable[[int, int, int], None] | None = None,
    on_retry: Callable[[int, int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Fetch A-share universe from East Money clist (paginated, progress-friendly)."""
    step = max(1, min(100, int(page_size)))
    all_rows: list[dict[str, Any]] = []
    total_pages = max_pages if max_pages is not None else 60
    page = 1

    while True:
        if cancel_check and cancel_check():
            break
        if max_pages is not None and page > max(1, int(max_pages)):
            break

        rows, total = _fetch_universe_page_resilient(
            page=page,
            page_size=step,
            on_retry=on_retry,
        )
        if not rows:
            break

        all_rows.extend(rows)
        if total is not None:
            total_pages = max(1, (int(total) + step - 1) // step)
            if max_pages is not None:
                total_pages = min(total_pages, int(max_pages))

        if on_page is not None:
            on_page(page, total_pages, len(all_rows))

        if total is not None and len(all_rows) >= int(total):
            break
        if max_pages is not None and page >= int(max_pages):
            break
        if len(rows) < step:
            if total is None:
                break
            last_page = max(1, (int(total) + step - 1) // step)
            if page >= last_page:
                break
        page += 1

    return all_rows


def _safe_float(value: Any) -> float | None:
    if value is None or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_stock_spot_row(symbol: str) -> dict[str, Any] | None:
    """Single-stock spot row from clist (price, 换手, 量比, 市值)."""
    code = symbol[-6:] if len(symbol) > 6 else symbol.strip()
    if not (code.isdigit() and len(code) == 6):
        return None
    market = 1 if code.startswith(("5", "6", "9")) else 0
    fs = f"m:{market}+t:2+s:{code}" if market == 1 else f"m:0+t:6+s:{code}"
    params = {
        "pn": "1",
        "pz": "1",
        "po": "1",
        "np": "1",
        "dect": "1",
        "ut": _UT,
        "wbp2u": _WBP2U,
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": fs,
        "fields": _CLIST_FIELDS_UNIVERSE,
    }
    try:
        data = _get_json_on_hosts(
            _QUOTE_HOSTS,
            "/api/qt/clist/get",
            params,
            timeout=8.0,
            host_kind="quote",
            referer=_REFERER_CLIST,
            max_rounds=1,
            max_hosts=2,
        )
        diff = (data.get("data") or {}).get("diff") or []
        rows = _parse_clist_rows(diff if isinstance(diff, list) else [])
        return rows[0] if rows else None
    except EastMoneyTransientError as exc:
        logger.debug("East Money spot row failed for %s: %s", code, exc)
        return None


def fetch_hot_stock_codes(*, limit: int = 30) -> list[str]:
    """Return liquid A-share codes from East Money clist (for symbol picker)."""
    limit = max(5, min(100, int(limit)))
    params = {
        "pn": "1",
        "pz": str(limit),
        "po": "1",
        "np": "1",
        "dect": "1",
        "ut": _UT,
        "wbp2u": _WBP2U,
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": _CLIST_FS_A_SHARE,
        "fields": "f12",
    }
    try:
        data = _get_json_on_hosts(
            _QUOTE_HOSTS,
            "/api/qt/clist/get",
            params,
            timeout=8.0,
            host_kind="quote",
            referer=_REFERER_CLIST,
            max_rounds=1,
            max_hosts=3,
        )
        diff = (data.get("data") or {}).get("diff") or []
        codes = [str(row.get("f12", "")).strip() for row in diff]
        return [c for c in codes if c.isdigit() and len(c) == 6]
    except EastMoneyTransientError as exc:
        logger.debug("East Money clist failed: %s", exc)
        return []


def fetch_stock_quote_payload(symbol: str) -> dict[str, Any] | None:
    """Full ``/api/qt/stock/get`` payload (五档需完整响应，不可缩 fields)."""
    code = symbol[-6:] if len(symbol) > 6 else symbol
    params = {
        "fltt": "2",
        "invt": "2",
        "ut": _UT,
        "wbp2u": _WBP2U,
        "secid": stock_secid(code),
    }
    try:
        data = _get_json_on_hosts(
            _QUOTE_HOSTS,
            "/api/qt/stock/get",
            params,
            timeout=8.0,
            host_kind="quote",
            referer=_REFERER_KLINE,
            max_rounds=1,
            max_hosts=3,
        )
        payload = data.get("data") or {}
        if not payload:
            return None
        return payload
    except EastMoneyTransientError as exc:
        logger.debug("East Money stock quote failed: %s", exc)
        return None


def fetch_stock_ten_depth_payload(symbol: str) -> dict[str, Any] | None:
    """十档协议 ``stock/get``（``fltt=1`` + vendor.js fields，L2 未开通时仅五档有值）。"""
    from pa_agent.data.eastmoney_quote_api import TEN_DEPTH_FIELDS

    code = symbol[-6:] if len(symbol) > 6 else symbol
    params = {
        "fltt": "1",
        "invt": "2",
        "ut": _UT,
        "wbp2u": _WBP2U,
        "dect": "1",
        "secid": stock_secid(code),
        "fields": TEN_DEPTH_FIELDS,
    }
    try:
        data = _get_json_on_hosts(
            _QUOTE_HOSTS,
            "/api/qt/stock/get",
            params,
            timeout=10.0,
            host_kind="quote",
            referer=_REFERER_KLINE,
            max_rounds=1,
            max_hosts=3,
        )
        payload = data.get("data") or {}
        return payload or None
    except EastMoneyTransientError as exc:
        logger.debug("East Money ten-depth quote failed: %s", exc)
        return None


def fetch_stock_order_book(symbol: str):
    """盘口（五档免费 + 六~十档需超级 L2，见 ``eastmoney_quote_api``）。"""
    from pa_agent.data.eastmoney_quote import parse_order_book_payload

    payload = fetch_stock_ten_depth_payload(symbol)
    fltt = 1
    if not payload:
        payload = fetch_stock_quote_payload(symbol)
        fltt = 2
    if not payload:
        return None
    return parse_order_book_payload(payload, fltt=fltt)


def fetch_stock_tick_details(
    symbol: str,
    *,
    tail: int = 40,
) -> list:
    """当日逐笔成交（``/api/qt/stock/details/get``）。"""
    from pa_agent.data.eastmoney_quote import TickTrade, parse_tick_lines

    code = symbol[-6:] if len(symbol) > 6 else symbol
    params = {
        "secid": stock_secid(code),
        "ut": _UT,
        "fields1": "f1",
        "fields2": "f51,f52,f53,f54,f55",
        "pos": "0",
        "lmt": str(max(20, min(int(tail), 2000))),
    }
    try:
        data = _get_json_on_hosts(
            _QUOTE_HOSTS,
            "/api/qt/stock/details/get",
            params,
            timeout=10.0,
            host_kind="quote",
            referer=_REFERER_KLINE,
            max_rounds=1,
            max_hosts=3,
        )
        lines = (data.get("data") or {}).get("details") or []
        if not isinstance(lines, list):
            return []
        return parse_tick_lines([str(x) for x in lines], tail=tail)
    except EastMoneyTransientError as exc:
        logger.debug("East Money tick details failed: %s", exc)
        return []


def fetch_stock_intraday_trends(symbol: str, *, ndays: int = 1) -> list[dict[str, Any]]:
    """当日/多日分时（``/api/qt/stock/trends2/get``）。"""
    code = symbol[-6:] if len(symbol) > 6 else symbol
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ut": _UT,
        "ndays": str(max(1, min(int(ndays), 5))),
        "iscr": "0",
        "secid": stock_secid(code),
    }
    try:
        return _fetch_trends(params)
    except EastMoneyTransientError as exc:
        logger.debug("East Money trends failed: %s", exc)
        return []


def fetch_stock_quote(symbol: str) -> dict[str, Any] | None:
    """Return code, name, and last price from East Money quote API."""
    code = symbol[-6:] if len(symbol) > 6 else symbol
    payload = fetch_stock_quote_payload(symbol)
    if not payload:
        return None
    name = payload.get("f58")
    price_raw = payload.get("f43")
    if not name and price_raw is None:
        return None
    result: dict[str, Any] = {
        "code": str(payload.get("f57") or code),
        "name": str(name or code),
    }
    if price_raw is not None:
        result["price"] = float(price_raw)
    return result
