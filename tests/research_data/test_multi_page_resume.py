from pa_agent.research_data.cli import run_first_batch

MINUTE_MS = 60_000
DAY_MS = 86_400_000
THREE_DAYS_MS = 3 * DAY_MS


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


def rows_for_period(minutes):
    return [
        raw_bar(i * MINUTE_MS, 100 + i, 101 + i, 99 + i, 100.5 + i, 1)
        for i in range(minutes)
    ]


def native_rows(total_minutes, count):
    return [
        raw_bar(
            start * MINUTE_MS,
            100 + start,
            100 + start + count,
            99 + start,
            99.5 + start + count,
            count,
        )
        for start in range(0, total_minutes, count)
    ]


class PagingClient:
    def __init__(self, *, interrupt_on_call=None, exchange_nonce=0):
        self.interrupt_on_call = interrupt_on_call
        self.exchange_nonce = exchange_nonce
        self.calls = 0
        self.minutes = THREE_DAYS_MS // MINUTE_MS
        self.one_minute = rows_for_period(self.minutes)
        self.four_hour = native_rows(self.minutes, 240)
        self.one_day = native_rows(self.minutes, 1_440)

    def _page(self, rows, params, timestamp):
        start = int(params["startTime"])
        end = int(params["endTime"])
        limit = int(params["limit"])
        return [row for row in rows if start <= timestamp(row) <= end][:limit]

    def get_json(self, path, params):
        if path == "/fapi/v1/time":
            return {"serverTime": THREE_DAYS_MS + 1}
        self.calls += 1
        if self.calls == self.interrupt_on_call:
            raise OSError("intentional page interruption")
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
                ],
            }
        if path == "/fapi/v1/fundingRate":
            rows = [
                {
                    "symbol": params["symbol"],
                    "fundingTime": timestamp,
                    "fundingRate": "0.0001",
                    "markPrice": "100",
                }
                for timestamp in range(0, THREE_DAYS_MS, 8 * 60 * 60 * 1_000)
            ]
            return self._page(rows, params, lambda row: row["fundingTime"])
        if path in {"/fapi/v1/markPriceKlines", "/fapi/v1/indexPriceKlines"}:
            return self._page(self.one_minute, params, lambda row: row[0])
        if path == "/fapi/v1/klines":
            rows = {"1m": self.one_minute, "4h": self.four_hour, "1d": self.one_day}[
                params["interval"]
            ]
            return self._page(rows, params, lambda row: row[0])
        raise AssertionError(path)


def test_three_day_page_three_interruption_resumes_without_content_change(tmp_path):
    common = {
        "symbols": ("BTCUSDT", "ETHUSDT"),
        "start_time_ms": 0,
        "end_time_ms": THREE_DAYS_MS - 1,
        "page_limit": 1_000,
        "include_index": True,
    }
    interrupted_output = tmp_path / "resume"
    try:
        run_first_batch(
            client=PagingClient(interrupt_on_call=3, exchange_nonce=1),
            output_dir=interrupted_output,
            clock_ms=lambda: THREE_DAYS_MS + 1,
            **common,
        )
    except OSError as exc:
        assert str(exc) == "intentional page interruption"
    else:
        raise AssertionError("expected page-three interruption")

    resumed = run_first_batch(
        client=PagingClient(exchange_nonce=2),
        output_dir=interrupted_output,
        clock_ms=lambda: THREE_DAYS_MS + 2,
        **common,
    )
    clean = run_first_batch(
        client=PagingClient(exchange_nonce=3),
        output_dir=tmp_path / "clean",
        clock_ms=lambda: THREE_DAYS_MS + 3,
        **common,
    )

    assert resumed["dataset_manifests"]["BTCUSDT_trade_1m"]["resumed"] is True
    assert resumed["dataset_content_hash"] == clean["dataset_content_hash"]
    assert resumed["acquisition_manifest_hash"] != clean["acquisition_manifest_hash"]
    for symbol in ("BTCUSDT", "ETHUSDT"):
        assert resumed["aggregation"][symbol]["4h"]["valid"] is True
        assert resumed["aggregation"][symbol]["1d"]["valid"] is True
        assert all(
            report["status"] == "COMPLETE"
            for report in resumed["gap_reports"][symbol].values()
        )
