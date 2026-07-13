import hashlib
import json

import pytest

from pa_agent.research_data.binance_public import PublicTransportError
from pa_agent.research_data.canonical import canonical_dumps
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


def test_retryable_public_errors_use_bounded_exponential_backoff(tmp_path):
    class Client:
        def __init__(self):
            self.attempts = 0

        def get_json(self, _path, _params):
            self.attempts += 1
            if self.attempts < 3:
                raise PublicTransportError("rate limited", retryable=True)
            return [[0]]

    sleeps = []
    client = Client()
    result = DatasetDownloader(
        client,
        AtomicDatasetStore(tmp_path),
        clock_ms=lambda: 1,
        sleep=sleeps.append,
        max_retries=3,
        base_delay_seconds=0.5,
    ).download_pages(
        dataset_name="retry",
        path="/fapi/v1/klines",
        params={},
        start_time_ms=0,
        end_time_ms=0,
        limit=1,
        timestamp_extractor=lambda row: int(row[0]),
    )

    assert client.attempts == 3
    assert sleeps == [0.5, 1.0]
    assert result.manifest["pages"][0]["retry_count"] == 2


def test_nonretryable_public_error_is_not_retried(tmp_path):
    class Client:
        def get_json(self, _path, _params):
            raise PublicTransportError("bad request", retryable=False)

    sleeps = []
    downloader = DatasetDownloader(
        Client(), AtomicDatasetStore(tmp_path), clock_ms=lambda: 1, sleep=sleeps.append
    )

    try:
        downloader.download_pages(
            dataset_name="no_retry",
            path="/fapi/v1/klines",
            params={},
            start_time_ms=0,
            end_time_ms=0,
            limit=1,
            timestamp_extractor=lambda row: int(row[0]),
        )
    except PublicTransportError as exc:
        assert str(exc) == "bad request"
    else:
        raise AssertionError("expected non-retryable error")
    assert sleeps == []


def test_resume_rejects_changed_request_identity(tmp_path):
    store = AtomicDatasetStore(tmp_path)
    with pytest.raises(OSError, match="interrupted"):
        DatasetDownloader(InterruptingClient(), store, clock_ms=lambda: 10).download_pages(
            dataset_name="btc_trade_1m",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )

    with pytest.raises(ValueError, match="request identity"):
        DatasetDownloader(ResumeClient(), store, clock_ms=lambda: 20).download_pages(
            dataset_name="btc_trade_1m",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "5m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )


def test_raw_page_records_identity_payload_hash_and_page_chain(tmp_path):
    store = AtomicDatasetStore(tmp_path)
    with pytest.raises(OSError, match="interrupted"):
        DatasetDownloader(InterruptingClient(), store, clock_ms=lambda: 10).download_pages(
            dataset_name="btc_trade_1m",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )

    page = store.read_raw_pages("btc_trade_1m")[0]
    metadata = page["metadata"]
    assert len(metadata["raw_payload_sha256"]) == 64
    assert len(metadata["request_identity_hash"]) == 64
    assert metadata["request_identity"]["dataset_name"] == "btc_trade_1m"
    assert metadata["first_timestamp"] == 0
    assert metadata["last_timestamp"] == 60_000
    assert metadata["next_start"] == 60_001
    assert metadata["row_count"] == 2
    assert metadata["page_index"] == 0


def test_resume_rejects_tampered_raw_page(tmp_path):
    store = AtomicDatasetStore(tmp_path)
    with pytest.raises(OSError, match="interrupted"):
        DatasetDownloader(InterruptingClient(), store, clock_ms=lambda: 10).download_pages(
            dataset_name="btc_trade_1m",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )
    page_path = tmp_path / "raw/btc_trade_1m/page-000000.json"
    page = json.loads(page_path.read_text(encoding="utf-8"))
    page["payload"][0][1] = "tampered"
    page_path.write_text(json.dumps(page), encoding="utf-8")

    with pytest.raises(ValueError, match="raw payload hash"):
        DatasetDownloader(ResumeClient(), store, clock_ms=lambda: 20).download_pages(
            dataset_name="btc_trade_1m",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )


def test_completed_directory_requires_explicit_reuse(tmp_path):
    class Client:
        def get_json(self, _path, _params):
            return [[0, "same"]]

    kwargs = dict(
        dataset_name="completed",
        path="/fapi/v1/klines",
        params={"symbol": "BTCUSDT", "interval": "1m"},
        start_time_ms=0,
        end_time_ms=0,
        limit=2,
        timestamp_extractor=lambda row: int(row[0]),
    )
    DatasetDownloader(Client(), AtomicDatasetStore(tmp_path), clock_ms=lambda: 1).download_pages(
        **kwargs
    )

    with pytest.raises(ValueError, match="completed raw directory"):
        DatasetDownloader(Client(), AtomicDatasetStore(tmp_path), clock_ms=lambda: 2).download_pages(
            **kwargs
        )


def test_conflicting_duplicate_primary_key_fails_closed(tmp_path):
    class Client:
        def get_json(self, _path, _params):
            return [[0, "first"], [0, "conflict"]]

    with pytest.raises(ValueError, match="ConflictingDuplicateRecord"):
        DatasetDownloader(Client(), AtomicDatasetStore(tmp_path), clock_ms=lambda: 1).download_pages(
            dataset_name="conflict",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=0,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"end_time_ms": 180_000},
        {"limit": 3},
        {"path": "/fapi/v1/markPriceKlines"},
    ],
)
def test_resume_rejects_changed_range_limit_or_path(tmp_path, changed):
    store = AtomicDatasetStore(tmp_path)
    base = dict(
        dataset_name="btc_trade_1m",
        path="/fapi/v1/klines",
        params={"symbol": "BTCUSDT", "interval": "1m"},
        start_time_ms=0,
        end_time_ms=120_000,
        limit=2,
        timestamp_extractor=lambda row: int(row[0]),
    )
    with pytest.raises(OSError, match="interrupted"):
        DatasetDownloader(InterruptingClient(), store, clock_ms=lambda: 10).download_pages(**base)
    with pytest.raises(ValueError, match="request identity"):
        DatasetDownloader(ResumeClient(), store, clock_ms=lambda: 20).download_pages(
            **{**base, **changed}
        )


def test_resume_rejects_corrupt_checkpoint_page_hashes(tmp_path):
    store = AtomicDatasetStore(tmp_path)
    with pytest.raises(OSError, match="interrupted"):
        DatasetDownloader(InterruptingClient(), store, clock_ms=lambda: 10).download_pages(
            dataset_name="btc_trade_1m",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )
    checkpoint_path = tmp_path / "state/btc_trade_1m.checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["raw_page_hashes"] = ["0" * 64]
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    with pytest.raises(ValueError, match="Checkpoint raw page hashes"):
        DatasetDownloader(ResumeClient(), store, clock_ms=lambda: 20).download_pages(
            dataset_name="btc_trade_1m",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )


def test_completed_directory_can_be_explicitly_reused_without_network(tmp_path):
    class InitialClient:
        def get_json(self, _path, _params):
            return [[0, "same"]]

    class NoNetworkClient:
        def get_json(self, _path, _params):
            raise AssertionError("explicit reuse must not download another raw page")

    kwargs = dict(
        dataset_name="completed",
        path="/fapi/v1/klines",
        params={"symbol": "BTCUSDT", "interval": "1m"},
        start_time_ms=0,
        end_time_ms=0,
        limit=2,
        timestamp_extractor=lambda row: int(row[0]),
    )
    DatasetDownloader(
        InitialClient(), AtomicDatasetStore(tmp_path), clock_ms=lambda: 1
    ).download_pages(**kwargs)
    reused = DatasetDownloader(
        NoNetworkClient(), AtomicDatasetStore(tmp_path), clock_ms=lambda: 2
    ).download_pages(**kwargs, existing_data_policy="reuse")

    assert reused.records == ([0, "same"],)
    assert reused.manifest["reused_existing"] is True
    assert len(reused.manifest["pages"]) == 1
    assert all(len(page["raw_payload_sha256"]) == 64 for page in reused.manifest["pages"])


def test_identical_duplicate_primary_key_is_deduplicated(tmp_path):
    class Client:
        def get_json(self, _path, _params):
            return [[0, "same"], [0, "same"]]

    result = DatasetDownloader(
        Client(), AtomicDatasetStore(tmp_path), clock_ms=lambda: 1
    ).download_pages(
        dataset_name="identical_duplicate",
        path="/fapi/v1/klines",
        params={"symbol": "BTCUSDT", "interval": "1m"},
        start_time_ms=0,
        end_time_ms=0,
        limit=2,
        timestamp_extractor=lambda row: int(row[0]),
    )
    assert result.records == ([0, "same"],)


def test_fresh_page_rejects_record_outside_original_request_range_before_raw_commit(
    tmp_path,
):
    class Client:
        def get_json(self, _path, _params):
            return [[-1, "outside"]]

    with pytest.raises(ValueError, match="RAW_RECORD_OUT_OF_REQUEST_RANGE"):
        DatasetDownloader(
            Client(), AtomicDatasetStore(tmp_path), clock_ms=lambda: 1
        ).download_pages(
            dataset_name="outside_original_range",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=60_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )

    assert not (tmp_path / "raw/outside_original_range/page-000000.json").exists()


def test_later_page_rejects_record_before_its_request_start(tmp_path):
    class Client:
        def get_json(self, _path, params):
            if params["startTime"] == 0:
                return [[0, "a"], [60_000, "b"]]
            return [[60_000, "overlap-not-requested"], [120_000, "c"]]

    with pytest.raises(ValueError, match="RAW_RECORD_OUT_OF_REQUEST_RANGE"):
        DatasetDownloader(
            Client(), AtomicDatasetStore(tmp_path), clock_ms=lambda: 1
        ).download_pages(
            dataset_name="outside_page_range",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )

    assert not (tmp_path / "raw/outside_page_range/page-000001.json").exists()


def test_resume_revalidates_restored_raw_records_against_original_range(tmp_path):
    store = AtomicDatasetStore(tmp_path)
    with pytest.raises(OSError, match="interrupted"):
        DatasetDownloader(InterruptingClient(), store, clock_ms=lambda: 10).download_pages(
            dataset_name="btc_trade_1m",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )

    page_path = tmp_path / "raw/btc_trade_1m/page-000000.json"
    page = json.loads(page_path.read_text(encoding="utf-8"))
    page["payload"][0][0] = -1
    page["metadata"]["first_timestamp"] = -1
    payload_hash = hashlib.sha256(
        canonical_dumps(page["payload"]).encode("utf-8")
    ).hexdigest()
    page["metadata"]["raw_payload_sha256"] = payload_hash
    page_path.write_text(json.dumps(page), encoding="utf-8")
    checkpoint_path = tmp_path / "state/btc_trade_1m.checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["raw_page_hashes"] = [payload_hash]
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    with pytest.raises(ValueError, match="RAW_RECORD_OUT_OF_REQUEST_RANGE"):
        DatasetDownloader(ResumeClient(), store, clock_ms=lambda: 20).download_pages(
            dataset_name="btc_trade_1m",
            path="/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1m"},
            start_time_ms=0,
            end_time_ms=120_000,
            limit=2,
            timestamp_extractor=lambda row: int(row[0]),
        )
