"""Refresh interval, cache TTL, and zombie-loop timing for data sources."""

from __future__ import annotations

# Sources that poll a public REST API and therefore need a floor on the refresh
# interval (and a longer grace period before a worker is declared a zombie).
# Crypto venues are included: Binance/OKX weight limits are generous but not
# infinite, and the extra interval keeps the UI smooth.
HTTP_POLL_SOURCES: frozenset[str] = frozenset({"eastmoney", "akshare", "tushare", "binance", "okx"})

HTTP_MIN_REFRESH_MS = 2500
HTTP_SNAPSHOT_CACHE_TTL_S = 8.0
HTTP_SNAPSHOT_CACHE_TTL_1D_S = 12.0
HTTP_ZOMBIE_JOIN_MS = 15_000
DEFAULT_ZOMBIE_JOIN_MS = 5000


def is_http_poll_source(kind: str) -> bool:
    return kind in HTTP_POLL_SOURCES


def effective_refresh_interval_ms(
    kind: str,
    user_ms: int,
    *,
    timeframe: str = "",
) -> int:
    """Clamp user refresh interval for slow HTTP/Baostock sources.

    This is the single source of truth for the refresh floor: the GUI used to
    inline a partial copy of this rule (which missed the 1d floor and treated
    ``tushare`` inconsistently) — see the 2026-10 code review.
    """
    ms = max(500, int(user_ms or 1000))
    if kind in HTTP_POLL_SOURCES:
        ms = max(ms, HTTP_MIN_REFRESH_MS)
    if kind in HTTP_POLL_SOURCES and timeframe == "1d":
        ms = max(ms, 3000)
    return ms


def snapshot_cache_ttl_s(timeframe: str) -> float:
    if timeframe in ("1d", "1w", "1M"):
        return HTTP_SNAPSHOT_CACHE_TTL_1D_S
    if timeframe == "1m":
        return 4.0
    return HTTP_SNAPSHOT_CACHE_TTL_S


def zombie_join_timeout_ms(kind: str) -> int:
    if kind in HTTP_POLL_SOURCES:
        return HTTP_ZOMBIE_JOIN_MS
    return DEFAULT_ZOMBIE_JOIN_MS
