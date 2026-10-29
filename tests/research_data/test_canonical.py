from decimal import Decimal

from pa_agent.research_data.canonical import (
    canonical_decimal,
    canonical_dumps,
    canonical_records,
)


def test_canonical_decimal_removes_exponent_trailing_zeroes_and_negative_zero():
    assert canonical_decimal(Decimal("1.2300")) == "1.23"
    assert canonical_decimal(Decimal("1E+3")) == "1000"
    assert canonical_decimal(Decimal("-0.000")) == "0"


def test_canonical_dumps_sorts_keys_and_encodes_decimal_as_string():
    value = {"z": Decimal("0.0100"), "a": {"time": 1_700_000_000_000}}

    assert canonical_dumps(value) == '{"a":{"time":1700000000000},"z":"0.01"}'


def test_canonical_records_sorts_by_declared_primary_key():
    records = [
        {"symbol": "ETHUSDT", "open_time_utc_ms": 2, "close": Decimal("2")},
        {"symbol": "BTCUSDT", "open_time_utc_ms": 2, "close": Decimal("3")},
        {"symbol": "BTCUSDT", "open_time_utc_ms": 1, "close": Decimal("1")},
    ]

    result = canonical_records(records, key_fields=("symbol", "open_time_utc_ms"))

    assert [item["close"] for item in result] == [Decimal("1"), Decimal("3"), Decimal("2")]
