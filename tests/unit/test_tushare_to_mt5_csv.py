from __future__ import annotations

import pandas as pd

from tools.tushare_to_mt5_csv import dataframe_to_mt5_rows, normalize_ts_code, write_mt5_csv


def test_normalize_ts_code_infers_exchange() -> None:
    assert normalize_ts_code("600519") == "600519.SH"
    assert normalize_ts_code("000001") == "000001.SZ"
    assert normalize_ts_code("688981") == "688981.SH"
    assert normalize_ts_code("430047") == "430047.BJ"
    assert normalize_ts_code("300750.SZ") == "300750.SZ"


def test_dataframe_to_mt5_rows_sorts_ascending_and_converts_volume() -> None:
    df = pd.DataFrame(
        [
            {
                "trade_date": "20240103",
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "vol": 123.4,
            },
            {
                "trade_date": "20240102",
                "open": 8.0,
                "high": 9.0,
                "low": 7.5,
                "close": 8.5,
                "vol": 10,
            },
        ]
    )

    rows = dataframe_to_mt5_rows(df)

    assert [r.date for r in rows] == ["2024.01.02", "2024.01.03"]
    assert rows[0].time == "00:00:00"
    assert rows[0].tick_volume == 10
    assert rows[0].volume == 1000
    assert rows[1].tick_volume == 123
    assert rows[1].volume == 12340


def test_write_mt5_csv_without_header(tmp_path) -> None:
    df = pd.DataFrame(
        [
            {
                "trade_date": "20240102",
                "open": 8.0,
                "high": 9.0,
                "low": 7.5,
                "close": 8.5,
                "vol": 10,
            },
        ]
    )
    rows = dataframe_to_mt5_rows(df)
    out = tmp_path / "mt5.csv"

    count = write_mt5_csv(rows, out)

    assert count == 1
    assert out.read_text(encoding="utf-8").strip() == "2024.01.02,00:00:00,8,9,7.5,8.5,10,1000,0"
