
import subprocess
import sys

from pa_agent.research_data.cli import run_first_batch

MINUTE_MS = 60_000
DAY_MS = 86_400_000


def raw_bar(open_time, open_price, high, low, close, count):
    return [
        open_time,
        str(open_price),
        str(high),
        str(low),
        str(close),
        str(count),
        open_time + count * MINUTE_MS - 1,
        str(count * 2),
        count * 2,
        str(count * 0.4),
        str(count * 0.8),
        "0",
    ]


def minute_rows():
    return [
        raw_bar(i * MINUTE_MS, 100 + i, 101 + i, 99 + i, 100.5 + i, 1)
        for i in range(1_440)
    ]


def native_rows(count):
    rows = []
    for start in range(0, 1_440, count):
        rows.append(
            raw_bar(
                start * MINUTE_MS,
                100 + start,
                101 + start + count - 1,
                99 + start,
                100.5 + start + count - 1,
                count,
            )
        )
    return rows


class FakeBinanceClient:
    def __init__(self, exchange_nonce=0):
        self.exchange_nonce = exchange_nonce

    def get_json(self, path, params):
        if path == "/fapi/v1/exchangeInfo":
            return {
                "serverTime": self.exchange_nonce,
                "symbols": [
                    {
                        "symbol": symbol,
                        "status": "TRADING",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                            {
                                "filterType": "LOT_SIZE",
                                "stepSize": "0.001",
                                "minQty": "0.001",
                            },
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                    for symbol in ("BTCUSDT", "ETHUSDT")
                ]
            }
        if path == "/fapi/v1/fundingRate":
            return [
                {
                    "symbol": params["symbol"],
                    "fundingTime": 28_800_000,
                    "fundingRate": "0.0001",
                    "markPrice": "100",
                }
            ]
        if path == "/fapi/v1/markPriceKlines":
            return minute_rows()
        if path == "/fapi/v1/klines":
            return {"1m": minute_rows(), "4h": native_rows(240), "1d": native_rows(1_440)}[
                params["interval"]
            ]
        raise AssertionError(f"unexpected public path: {path}")


def test_first_batch_orchestration_writes_data_and_validates_native_periods(tmp_path):
    summary = run_first_batch(
        client=FakeBinanceClient(),
        output_dir=tmp_path,
        symbols=("BTCUSDT", "ETHUSDT"),
        start_time_ms=0,
        end_time_ms=DAY_MS - 1,
        page_limit=2_000,
        include_index=False,
        clock_ms=lambda: DAY_MS + 1,
    )

    assert summary["aggregation"]["BTCUSDT"]["4h"]["valid"] is True
    assert summary["aggregation"]["BTCUSDT"]["1d"]["valid"] is True
    assert summary["gap_reports"]["BTCUSDT"]["trade"]["status"] == "COMPLETE"
    assert summary["gap_reports"]["BTCUSDT"]["mark"]["status"] == "COMPLETE"
    assert summary["contract_rules"][0]["validity"] == "CURRENT_SNAPSHOT_ONLY"
    assert (tmp_path / "canonical/BTCUSDT_trade_1m.jsonl").exists()
    assert (tmp_path / "summary.json").exists()


def test_same_content_has_same_dataset_hash_but_different_acquisition_hash(tmp_path):
    common = {
        "symbols": ("BTCUSDT", "ETHUSDT"),
        "start_time_ms": 0,
        "end_time_ms": DAY_MS - 1,
        "page_limit": 2_000,
        "include_index": False,
    }
    first = run_first_batch(
        client=FakeBinanceClient(exchange_nonce=1),
        output_dir=tmp_path / "first",
        clock_ms=lambda: 1,
        **common,
    )
    second = run_first_batch(
        client=FakeBinanceClient(exchange_nonce=2),
        output_dir=tmp_path / "second",
        clock_ms=lambda: 2,
        **common,
    )

    assert first["dataset_content_hash"] == second["dataset_content_hash"]
    assert first["acquisition_manifest_hash"] != second["acquisition_manifest_hash"]
    assert first["acquisition_run_id"] != second["acquisition_run_id"]


def test_wrapper_script_can_import_package_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/download_binance_research.py", "--help"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Download public Binance research data" in result.stdout
