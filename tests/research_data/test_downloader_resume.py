from pa_agent.research_data.downloader import DatasetDownloader
from pa_agent.research_data.storage import AtomicDatasetStore


class InterruptingClient:
    def __init__(self):
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        if len(self.calls) == 1:
            return [[0, "a"], [60_000, "b"]]
        raise OSError("interrupted")


class ResumeClient:
    def __init__(self):
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        assert params["startTime"] == 60_000
        return [[60_000, "b"], [120_000, "c"]]


def test_download_resumes_with_boundary_overlap_and_deduplicates(tmp_path):
    store = AtomicDatasetStore(tmp_path)
    first = DatasetDownloader(InterruptingClient(), store, clock_ms=lambda: 10)

    try:
        first.download_pages(
            dataset_name="btc_trade_1m",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )
    except OSError as exc:
        assert str(exc) == "interrupted"
    else:
        raise AssertionError("expected interruption")

    resumed_client = ResumeClient()
    resumed = DatasetDownloader(resumed_client, store, clock_ms=lambda: 20).download_pages(
        dataset_name="btc_trade_1m",
        path="/fapi/v1/klines",
        params={"symbol": "BTCUSDT", "interval": "1m"},
        start_time_ms=0,
        end_time_ms=120_000,
        limit=2,
        timestamp_extractor=lambda row: int(row[0]),
    )

    assert [row[0] for row in resumed.records] == [0, 60_000, 120_000]
    assert resumed.resumed is True
    assert resumed_client.calls[0][1]["startTime"] == 60_000


def test_fresh_download_stops_on_short_page_and_is_sorted(tmp_path):
    class Client:
        def get_json(self, _path, params):
            if params["startTime"] == 0:
                return [[60_000], [0]]
            return [[120_000]]

    result = DatasetDownloader(
        Client(), AtomicDatasetStore(tmp_path), clock_ms=lambda: 1
    ).download_pages(
        dataset_name="data",
        path="/fapi/v1/klines",
        params={},
        start_time_ms=0,
        end_time_ms=120_000,
        limit=2,
        timestamp_extractor=lambda row: int(row[0]),
    )

    assert [row[0] for row in result.records] == [0, 60_000, 120_000]
    assert result.resumed is False
