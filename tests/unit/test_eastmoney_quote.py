from __future__ import annotations

import pytest

from pa_agent.data.eastmoney_quote import parse_order_book_payload, parse_tick_lines


def test_parse_order_book_uses_etf_three_decimal_precision() -> None:
    payload = {
        "f57": "159170",
        "f58": "港股通互联网ETF永赢",
        "f59": 3,
        "f43": 835,
        "f46": 842,
        "f44": 843,
        "f45": 830,
        "f60": 859,
        "f170": -279,
        "f19": 834,
        "f20": 1488,
        "f39": 835,
        "f40": 748,
    }

    book = parse_order_book_payload(payload, fltt=1)

    assert book is not None
    assert book.price == pytest.approx(0.835)
    assert book.open == pytest.approx(0.842)
    assert book.high == pytest.approx(0.843)
    assert book.low == pytest.approx(0.830)
    assert book.prev_close == pytest.approx(0.859)
    assert book.pct_chg == pytest.approx(-2.79)
    assert book.bids[0].price == pytest.approx(0.834)
    assert book.bids[0].volume == 1488
    assert book.asks[0].price == pytest.approx(0.835)
    assert book.asks[0].volume == 748


def test_parse_order_book_keeps_stock_two_decimal_precision() -> None:
    payload = {
        "f57": "600519",
        "f58": "贵州茅台",
        "f59": 2,
        "f43": 145023,
        "f19": 145022,
        "f20": 10,
        "f39": 145024,
        "f40": 20,
    }

    book = parse_order_book_payload(payload, fltt=1)

    assert book is not None
    assert book.price == pytest.approx(1450.23)
    assert book.bids[0].price == pytest.approx(1450.22)
    assert book.asks[0].price == pytest.approx(1450.24)


def test_parse_order_book_fltt_two_prices_are_already_scaled() -> None:
    payload = {
        "f57": "159170",
        "f58": "港股通互联网ETF永赢",
        "f59": 3,
        "f43": 0.835,
        "f19": 0.834,
        "f20": 1488,
        "f39": 0.835,
        "f40": 748,
    }

    book = parse_order_book_payload(payload, fltt=2)

    assert book is not None
    assert book.price == pytest.approx(0.835)
    assert book.bids[0].price == pytest.approx(0.834)
    assert book.asks[0].price == pytest.approx(0.835)


def test_parse_tick_lines_keeps_recent_trade_direction() -> None:
    trades = parse_tick_lines(
        [
            "14:59:57,0.834,1488,1,2",
            "14:59:58,0.835,748,1,1",
        ],
        tail=2,
    )

    assert trades[0].time == "14:59:57"
    assert trades[0].price == pytest.approx(0.834)
    assert trades[0].volume == 1488
    assert trades[0].side_hint == "卖"
    assert trades[1].side_hint == "买"
