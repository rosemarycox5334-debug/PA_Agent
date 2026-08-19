"""Bollinger Bands — full-series calculation."""
from __future__ import annotations

import math


DEFAULT_BOLL_PERIOD = 20
DEFAULT_BOLL_STDDEV = 2.0


def bollinger_full(
    values: list[float],
    *,
    period: int = DEFAULT_BOLL_PERIOD,
    stddev: float = DEFAULT_BOLL_STDDEV,
) -> tuple[list[float], list[float], list[float]]:
    """Return SMA middle, upper, and lower bands for oldest-first values."""
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if not math.isfinite(stddev) or stddev <= 0:
        raise ValueError(f"stddev must be finite and > 0, got {stddev}")

    middle = [math.nan] * len(values)
    upper = [math.nan] * len(values)
    lower = [math.nan] * len(values)
    for end in range(period - 1, len(values)):
        window = values[end - period + 1 : end + 1]
        mean = sum(window) / period
        variance = sum((value - mean) ** 2 for value in window) / period
        width = math.sqrt(variance) * stddev
        middle[end] = mean
        upper[end] = mean + width
        lower[end] = mean - width
    return middle, upper, lower
