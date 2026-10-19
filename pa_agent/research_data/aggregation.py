from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from pa_agent.research_data.models import GapInterval, Kline

ONE_MINUTE_MS = 60_000
INTERVAL_NAMES = {
    4 * 60 * ONE_MINUTE_MS: "4h",
    24 * 60 * ONE_MINUTE_MS: "1d",
}


@dataclass(frozen=True, slots=True)
class AggregationResult:
    bars: tuple[Kline, ...]
    incomplete_intervals: tuple[GapInterval, ...]


def aggregate_klines(bars: Iterable[Kline], *, interval_ms: int) -> AggregationResult:
    if interval_ms <= 0 or interval_ms % ONE_MINUTE_MS:
        raise ValueError("Aggregation interval must be a positive whole number of minutes")
    ordered = sorted(bars, key=lambda bar: bar.open_time_utc_ms)
    if not ordered:
        return AggregationResult((), ())
    identity = {(bar.source, bar.stream, bar.symbol, bar.interval) for bar in ordered}
    if len(identity) != 1 or ordered[0].interval != "1m":
        raise ValueError("Aggregation requires one homogeneous 1m stream")
    grouped: dict[int, list[Kline]] = defaultdict(list)
    for bar in ordered:
        bucket = (bar.open_time_utc_ms // interval_ms) * interval_ms
        grouped[bucket].append(bar)

    expected_count = interval_ms // ONE_MINUTE_MS
    complete: list[Kline] = []
    incomplete: list[GapInterval] = []
    for bucket, items in sorted(grouped.items()):
        items.sort(key=lambda bar: bar.open_time_utc_ms)
        expected_times = list(range(bucket, bucket + interval_ms, ONE_MINUTE_MS))
        actual_times = [bar.open_time_utc_ms for bar in items]
        if (
            len(items) != expected_count
            or actual_times != expected_times
            or not all(bar.is_closed for bar in items)
        ):
            incomplete.append(GapInterval(bucket, bucket + interval_ms - 1))
            continue
        complete.append(
            Kline(
                source=items[0].source,
                stream=items[0].stream,
                symbol=items[0].symbol,
                interval=INTERVAL_NAMES.get(interval_ms, f"{interval_ms}ms"),
                open_time_utc_ms=bucket,
                close_time_utc_ms=bucket + interval_ms - 1,
                open=items[0].open,
                high=max(bar.high for bar in items),
                low=min(bar.low for bar in items),
                close=items[-1].close,
                base_volume=sum((bar.base_volume for bar in items), start=items[0].base_volume * 0),
                quote_volume=sum((bar.quote_volume for bar in items), start=items[0].quote_volume * 0),
                trade_count=sum(bar.trade_count for bar in items),
                taker_buy_base_volume=sum(
                    (bar.taker_buy_base_volume for bar in items),
                    start=items[0].taker_buy_base_volume * 0,
                ),
                taker_buy_quote_volume=sum(
                    (bar.taker_buy_quote_volume for bar in items),
                    start=items[0].taker_buy_quote_volume * 0,
                ),
                is_closed=True,
            )
        )
    return AggregationResult(tuple(complete), tuple(incomplete))
