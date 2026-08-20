from decimal import Decimal

from pa_agent.research_data.aggregation import aggregate_klines
from pa_agent.research_data.models import Kline

MINUTE_MS = 60_000
FOUR_HOURS_MS = 4 * 60 * MINUTE_MS


def make_bar(index: int) -> Kline:
    value = Decimal(index + 1)
    return Kline(
        source="binance_fapi",
        stream="trade",
        symbol="BTCUSDT",
        interval="1m",
        open_time_utc_ms=index * MINUTE_MS,
        close_time_utc_ms=(index + 1) * MINUTE_MS - 1,
        open=value,
        high=value + 1,
        low=value - 1,
        close=value + Decimal("0.5"),
        base_volume=Decimal("1.1"),
        quote_volume=Decimal("2.2"),
        trade_count=2,
        taker_buy_base_volume=Decimal("0.4"),
        taker_buy_quote_volume=Decimal("0.8"),
        is_closed=True,
    )

def test_aggregate_1m_to_4h_uses_exact_utc_bucket_and_sums_fields():
    result = aggregate_klines([make_bar(i) for i in range(240)], interval_ms=FOUR_HOURS_MS)

    assert result.incomplete_intervals == ()
    assert len(result.bars) == 1
    bar = result.bars[0]
    assert (bar.open_time_utc_ms, bar.close_time_utc_ms) == (0, FOUR_HOURS_MS - 1)
    assert (bar.open, bar.high, bar.low, bar.close) == (
        Decimal("1"),
        Decimal("241"),
        Decimal("0"),
        Decimal("240.5"),
    )
    assert bar.base_volume == Decimal("264.0")
    assert bar.quote_volume == Decimal("528.0")
    assert bar.trade_count == 480
    assert bar.taker_buy_base_volume == Decimal("96.0")
    assert bar.taker_buy_quote_volume == Decimal("192.0")
    assert bar.interval == "4h"


def test_incomplete_or_nonconsecutive_bucket_is_reported_not_emitted():
    bars = [make_bar(i) for i in range(240)]
    bars.pop(100)

    result = aggregate_klines(bars, interval_ms=FOUR_HOURS_MS)

    assert result.bars == ()
    assert len(result.incomplete_intervals) == 1
    assert result.incomplete_intervals[0].start_utc_ms == 0


def test_input_order_does_not_change_aggregation():
    bars = [make_bar(i) for i in range(240)]
    assert aggregate_klines(bars, interval_ms=FOUR_HOURS_MS) == aggregate_klines(
        list(reversed(bars)), interval_ms=FOUR_HOURS_MS
    )
