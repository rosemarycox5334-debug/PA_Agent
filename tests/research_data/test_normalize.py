from decimal import Decimal

import pytest

from pa_agent.research_data.normalize import (
    DataSchemaError,
    normalize_contract_rules,
    normalize_funding_rate,
    normalize_price_kline,
    normalize_trade_kline,
)

TRADE_ROW = [
    0,
    "100.10",
    "101.20",
    "99.90",
    "100.50",
    "3.000",
    59_999,
    "301.500",
    2,
    "1.000",
    "100.500",
    "0",
]


def test_normalize_trade_kline_preserves_decimal_and_closed_status():
    bar = normalize_trade_kline(
        TRADE_ROW,
        symbol="BTCUSDT",
        interval="1m",
        source_server_time_utc_ms=60_000,
    )

    assert bar.close == Decimal("100.50")
    assert bar.trade_count == 2
    assert bar.is_closed is True
    assert bar.stream == "trade"
    assert bar.schema_version == "BINANCE_KLINE_V1_EXACT_12"


def test_normalize_price_kline_is_a_separate_zero_volume_stream():
    bar = normalize_price_kline(
        TRADE_ROW,
        stream="mark",
        symbol="BTCUSDT",
        interval="1m",
        source_server_time_utc_ms=60_000,
    )

    assert bar.stream == "mark"
    assert bar.base_volume == Decimal("0")
    assert bar.is_closed is True


@pytest.mark.parametrize("stream", ["trade", "mark", "index"])
def test_unclosed_kline_fails_closed_against_exchange_server_time(stream):
    normalizer = normalize_trade_kline if stream == "trade" else normalize_price_kline
    kwargs = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "source_server_time_utc_ms": 59_999,
    }
    if stream != "trade":
        kwargs["stream"] = stream

    with pytest.raises(DataSchemaError, match="UNCLOSED_BAR"):
        normalizer(TRADE_ROW, **kwargs)


def test_normalize_funding_rate_requires_mark_price():
    item = normalize_funding_rate(
        {"symbol": "BTCUSDT", "fundingTime": 1, "fundingRate": "0.0001", "markPrice": "100"},
        expected_symbol="BTCUSDT",
    )

    assert item.funding_rate == Decimal("0.0001")
    assert item.schema_version == "BINANCE_FUNDING_V1"
    with pytest.raises(DataSchemaError):
        normalize_funding_rate(
            {"symbol": "BTCUSDT", "fundingTime": 1, "fundingRate": "0"},
            expected_symbol="BTCUSDT",
        )


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (
            {"symbol": "ETHUSDT", "fundingTime": 1, "fundingRate": "0", "markPrice": "100"},
            "symbol",
        ),
        (
            {"symbol": "BTCUSDT", "fundingTime": "1.5", "fundingRate": "0", "markPrice": "100"},
            "funding_time",
        ),
        (
            {"symbol": "BTCUSDT", "fundingTime": -1, "fundingRate": "0", "markPrice": "100"},
            "funding_time",
        ),
        (
            {"symbol": "BTCUSDT", "fundingTime": 1, "fundingRate": "0", "markPrice": "0"},
            "mark_price",
        ),
        (
            {"symbol": "BTCUSDT", "fundingTime": 1, "fundingRate": "NaN", "markPrice": "100"},
            "funding_rate",
        ),
    ],
)
def test_funding_rate_rejects_wrong_identity_and_invalid_values(item, message):
    with pytest.raises(DataSchemaError, match=message):
        normalize_funding_rate(item, expected_symbol="BTCUSDT")


def test_normalize_contract_rules_marks_current_snapshot_only():
    payload = {
        "symbols": [
                {
                    "symbol": "BTCUSDT",
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
                {
                    "symbol": "ETHUSDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
                {"symbol": "OTHER", "status": "TRADING", "filters": []},
        ]
    }

    rules = normalize_contract_rules(
        payload,
        symbols=("BTCUSDT", "ETHUSDT"),
        acquired_at_utc_ms=10,
        source_hash="hash",
    )

    assert len(rules) == 2
    assert rules[0].price_tick == Decimal("0.10")
    assert rules[0].validity == "CURRENT_SNAPSHOT_ONLY"
    assert rules[0].schema_version == "CONTRACT_RULE_SNAPSHOT_V1"


@pytest.mark.parametrize("row", [[], [0, "bad"], TRADE_ROW[:-1]])
def test_malformed_kline_schema_is_rejected(row):
    with pytest.raises(DataSchemaError):
        normalize_trade_kline(
            row,
            symbol="BTCUSDT",
            interval="1m",
            source_server_time_utc_ms=60_000,
        )


@pytest.mark.parametrize("row", [[*TRADE_ROW, "extra"], TRADE_ROW[:-1]])
def test_kline_v1_requires_exactly_twelve_fields(row):
    with pytest.raises(DataSchemaError, match="exactly 12"):
        normalize_trade_kline(
            row,
            symbol="BTCUSDT",
            interval="1m",
            source_server_time_utc_ms=60_000,
        )


@pytest.mark.parametrize(
    ("index", "value"),
    [
        (5, "-1"),
        (7, "-1"),
        (8, "1.5"),
        (8, -1),
        (9, "-1"),
        (10, "-1"),
    ],
)
def test_trade_kline_rejects_negative_volume_count_and_nonintegral_count(index, value):
    row = list(TRADE_ROW)
    row[index] = value
    with pytest.raises(DataSchemaError):
        normalize_trade_kline(
            row,
            symbol="BTCUSDT",
            interval="1m",
            source_server_time_utc_ms=60_000,
        )


@pytest.mark.parametrize(
    ("index", "value"),
    [(2, "99"), (3, "101"), (0, 1), (6, 60_000)],
)
def test_trade_kline_rejects_invalid_ohlc_or_interval_boundary(index, value):
    row = list(TRADE_ROW)
    row[index] = value
    with pytest.raises(DataSchemaError):
        normalize_trade_kline(
            row,
            symbol="BTCUSDT",
            interval="1m",
            source_server_time_utc_ms=60_000,
        )
