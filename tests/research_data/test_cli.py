
import hashlib
import json
import subprocess
import sys

import pytest

from pa_agent.research_data.binance_public import PublicTransportError
from pa_agent.research_data.cli import run_first_batch
from pa_agent.research_data.normalize import DataSchemaError

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
    def __init__(
        self,
        exchange_nonce=0,
        server_time=DAY_MS + 1,
        index_nonce=0,
        contract_tick="0.10",
    ):
        self.exchange_nonce = exchange_nonce
        self.server_time = server_time
        self.index_nonce = index_nonce
        self.contract_tick = contract_tick
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "/fapi/v1/time":
            return {"serverTime": self.server_time}
        if path == "/fapi/v1/exchangeInfo":
            return {
                "serverTime": self.exchange_nonce,
                "symbols": [
                    {
                        "symbol": symbol,
                        "status": "TRADING",
                        "filters": [
                            {
                                "filterType": "PRICE_FILTER",
                                "tickSize": self.contract_tick,
                            },
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
        if path == "/fapi/v1/indexPriceKlines":
            rows = minute_rows()
            if self.index_nonce:
                rows[0][1:5] = ["200", "201", "199", "200.5"]
            return rows
        if path == "/fapi/v1/klines":
            return {"1m": minute_rows(), "4h": native_rows(240), "1d": native_rows(1_440)}[
                params["interval"]
            ]
        raise AssertionError(f"unexpected public path: {path}")


def test_first_batch_orchestration_writes_data_and_validates_native_periods(tmp_path):
    client = FakeBinanceClient()
    summary = run_first_batch(
        client=client,
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
    assert {
        "schedule_status",
        "coverage_status",
        "gap_intervals",
        "observed_steps_ms",
    }.issubset(summary["gap_reports"]["BTCUSDT"]["funding"])
    assert summary["contract_rules"][0]["validity"] == "CURRENT_SNAPSHOT_ONLY"
    snapshot = summary["contract_rule_snapshot"]
    assert snapshot["requested_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert snapshot["returned_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert snapshot["missing_symbols"] == []
    assert snapshot["validity"] == "CURRENT_SNAPSHOT_ONLY"
    assert snapshot["review_status"] == "REVIEW_REQUIRED"
    assert client.calls[0] == ("/fapi/v1/time", {})
    assert summary["source_server_time_utc_ms"] == DAY_MS + 1
    assert (tmp_path / "canonical/BTCUSDT_trade_1m.jsonl").exists()
    assert (tmp_path / "summary.json").exists()
    acquisition = json.loads(
        (tmp_path / "acquisition_manifest.json").read_text(encoding="utf-8")
    )
    assert "exchange_info" in acquisition["dataset_page_hashes"]
    assert acquisition["source_server_time_utc_ms"] == DAY_MS + 1
    assert len(acquisition["source_server_time_raw_payload_sha256"]) == 64
    source_time_page = json.loads(
        (tmp_path / "raw/source_server_time/page-000000.json").read_text(
            encoding="utf-8"
        )
    )
    assert source_time_page["payload"] == {"serverTime": DAY_MS + 1}
    assert source_time_page["metadata"]["raw_payload_sha256"] == acquisition[
        "source_server_time_raw_payload_sha256"
    ]
    assert all(
        hashes and all(len(value) == 64 for value in hashes)
        for hashes in acquisition["dataset_page_hashes"].values()
    )


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


def test_canonical_records_persist_versioned_schema_identities(tmp_path):
    summary = run_first_batch(
        client=FakeBinanceClient(),
        output_dir=tmp_path,
        symbols=("BTCUSDT", "ETHUSDT"),
        start_time_ms=0,
        end_time_ms=DAY_MS - 1,
        page_limit=2_000,
        include_index=False,
        clock_ms=lambda: 999_999_999,
    )

    kline = json.loads(
        (tmp_path / "canonical/BTCUSDT_trade_1m.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    funding = json.loads(
        (tmp_path / "canonical/BTCUSDT_funding.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    contract_rule = json.loads(
        (tmp_path / "canonical/contract_rules_current.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert kline["schema_version"] == "BINANCE_KLINE_V1_EXACT_12"
    assert funding["schema_version"] == "BINANCE_FUNDING_V1"
    assert contract_rule["schema_version"] == "CONTRACT_RULE_SNAPSHOT_V1"
    assert summary["dataset_manifests"]["BTCUSDT_trade_1m"][
        "canonical_schema_version"
    ] == "BINANCE_KLINE_V1_EXACT_12"
    assert summary["dataset_manifests"]["BTCUSDT_funding"][
        "canonical_schema_version"
    ] == "BINANCE_FUNDING_V1"
    assert summary["contract_rule_dataset_manifest"][
        "canonical_schema_version"
    ] == "CONTRACT_RULE_SNAPSHOT_V1"
    assert (
        tmp_path / "manifests/contract_rules_current.json"
    ).exists()


def test_unclosed_native_bar_fails_before_canonical_persistence(tmp_path):
    with pytest.raises(DataSchemaError, match="UNCLOSED_BAR"):
        run_first_batch(
            client=FakeBinanceClient(server_time=DAY_MS - 1),
            output_dir=tmp_path,
            symbols=("BTCUSDT", "ETHUSDT"),
            start_time_ms=0,
            end_time_ms=DAY_MS - 1,
            page_limit=2_000,
            include_index=False,
            clock_ms=lambda: 10 * DAY_MS,
        )

    assert not (tmp_path / "canonical/BTCUSDT_trade_1d.jsonl").exists()


def test_wrong_funding_symbol_fails_before_canonical_persistence(tmp_path):
    class WrongFundingSymbolClient(FakeBinanceClient):
        def get_json(self, path, params):
            payload = super().get_json(path, params)
            if path == "/fapi/v1/fundingRate":
                payload[0]["symbol"] = "ETHUSDT" if params["symbol"] == "BTCUSDT" else "BTCUSDT"
            return payload

    with pytest.raises(DataSchemaError, match="symbol"):
        run_first_batch(
            client=WrongFundingSymbolClient(),
            output_dir=tmp_path,
            symbols=("BTCUSDT", "ETHUSDT"),
            start_time_ms=0,
            end_time_ms=DAY_MS - 1,
            page_limit=2_000,
            include_index=False,
            clock_ms=lambda: DAY_MS + 1,
        )

    assert not (tmp_path / "canonical/BTCUSDT_funding.jsonl").exists()


def test_wrapper_script_can_import_package_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/download_binance_research.py", "--help"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Download public Binance research data" in result.stdout


def test_missing_requested_contract_symbol_raises_clear_validation_failure(tmp_path):
    class MissingEthClient(FakeBinanceClient):
        def get_json(self, path, params):
            payload = super().get_json(path, params)
            if path == "/fapi/v1/exchangeInfo":
                payload["symbols"] = [
                    item for item in payload["symbols"] if item["symbol"] == "BTCUSDT"
                ]
            return payload

    with pytest.raises(DataSchemaError, match="ContractRuleValidationFailure") as exc_info:
        run_first_batch(
            client=MissingEthClient(),
            output_dir=tmp_path,
            symbols=("BTCUSDT", "ETHUSDT"),
            start_time_ms=0,
            end_time_ms=DAY_MS - 1,
            page_limit=2_000,
            include_index=False,
            clock_ms=lambda: DAY_MS + 1,
        )
    assert exc_info.value.missing_symbols == ("ETHUSDT",)
    failure = tmp_path / "contract_rule_validation_failure.json"
    assert failure.exists()


def test_completed_orchestration_requires_explicit_reuse_policy(tmp_path):
    common = dict(
        client=FakeBinanceClient(),
        output_dir=tmp_path,
        symbols=("BTCUSDT", "ETHUSDT"),
        start_time_ms=0,
        end_time_ms=DAY_MS - 1,
        page_limit=2_000,
        include_index=False,
        clock_ms=lambda: DAY_MS + 1,
    )
    first = run_first_batch(**common)
    with pytest.raises(ValueError, match="completed raw directory"):
        run_first_batch(**common)

    reused = run_first_batch(**common, existing_data_policy="reuse")
    assert reused["dataset_content_hash"] == first["dataset_content_hash"]
    assert all(
        manifest["reused_existing"]
        for manifest in reused["dataset_manifests"].values()
    )


def test_completed_output_reject_preflight_is_read_only_and_uses_zero_client_calls(
    tmp_path,
):
    common = dict(
        output_dir=tmp_path,
        symbols=("BTCUSDT", "ETHUSDT"),
        start_time_ms=0,
        end_time_ms=DAY_MS - 1,
        page_limit=2_000,
        include_index=False,
    )
    run_first_batch(
        client=FakeBinanceClient(),
        clock_ms=lambda: DAY_MS + 1,
        **common,
    )

    def directory_hashes():
        return {
            path.relative_to(tmp_path).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(tmp_path.rglob("*"))
            if path.is_file()
        }

    before = directory_hashes()

    class ZeroCallClient:
        calls = 0

        def get_json(self, _path, _params):
            self.calls += 1
            raise AssertionError("preflight must reject before network access")

    client = ZeroCallClient()

    def forbidden_clock():
        raise AssertionError("preflight must reject before acquisition clock access")

    with pytest.raises(ValueError, match="completed raw directory"):
        run_first_batch(client=client, clock_ms=forbidden_clock, **common)

    assert client.calls == 0
    assert directory_hashes() == before


def test_source_time_and_exchange_info_use_shared_retry_and_persist_counts(tmp_path):
    class RetryOneOffClient(FakeBinanceClient):
        def __init__(self):
            super().__init__()
            self.time_attempts = 0
            self.exchange_attempts = 0

        def get_json(self, path, params):
            if path == "/fapi/v1/time":
                self.time_attempts += 1
                if self.time_attempts < 3:
                    raise PublicTransportError("429", retryable=True)
            if path == "/fapi/v1/exchangeInfo":
                self.exchange_attempts += 1
                if self.exchange_attempts < 3:
                    raise PublicTransportError("503", retryable=True)
            return super().get_json(path, params)

    sleeps = []
    client = RetryOneOffClient()
    run_first_batch(
        client=client,
        output_dir=tmp_path,
        symbols=("BTCUSDT", "ETHUSDT"),
        start_time_ms=0,
        end_time_ms=DAY_MS - 1,
        page_limit=2_000,
        include_index=False,
        clock_ms=lambda: DAY_MS + 1,
        sleep=sleeps.append,
    )

    source_page = json.loads(
        (tmp_path / "raw/source_server_time/page-000000.json").read_text(
            encoding="utf-8"
        )
    )
    exchange_page = json.loads(
        (tmp_path / "raw/exchange_info/page-000000.json").read_text(
            encoding="utf-8"
        )
    )
    assert source_page["metadata"]["retry_count"] == 2
    assert exchange_page["metadata"]["retry_count"] == 2
    assert client.time_attempts == 3
    assert client.exchange_attempts == 3
    assert sleeps == [0.5, 1.0, 0.5, 1.0]


def test_content_hashes_separate_strategy_execution_audit_and_contract(tmp_path):
    common = dict(
        symbols=("BTCUSDT", "ETHUSDT"),
        start_time_ms=0,
        end_time_ms=DAY_MS - 1,
        page_limit=2_000,
        clock_ms=lambda: DAY_MS + 1,
    )
    base = run_first_batch(
        client=FakeBinanceClient(),
        output_dir=tmp_path / "base",
        include_index=False,
        **common,
    )
    with_index = run_first_batch(
        client=FakeBinanceClient(index_nonce=1),
        output_dir=tmp_path / "index",
        include_index=True,
        **common,
    )
    changed_contract = run_first_batch(
        client=FakeBinanceClient(contract_tick="0.20"),
        output_dir=tmp_path / "contract",
        include_index=False,
        **common,
    )

    required = {
        "acquisition_bundle_content_hash",
        "strategy_data_content_hash",
        "execution_data_content_hash",
        "audit_data_content_hash",
        "contract_rule_content_hash",
    }
    assert required.issubset(base)
    assert all(len(base[field]) == 64 for field in required)
    assert base["strategy_data_content_hash"] == with_index[
        "strategy_data_content_hash"
    ] == changed_contract["strategy_data_content_hash"]
    assert base["execution_data_content_hash"] == with_index[
        "execution_data_content_hash"
    ] == changed_contract["execution_data_content_hash"]
    assert base["audit_data_content_hash"] != with_index["audit_data_content_hash"]
    assert base["audit_data_content_hash"] == changed_contract[
        "audit_data_content_hash"
    ]
    assert base["contract_rule_content_hash"] == with_index[
        "contract_rule_content_hash"
    ]
    assert base["contract_rule_content_hash"] != changed_contract[
        "contract_rule_content_hash"
    ]
    assert base["acquisition_bundle_content_hash"] != with_index[
        "acquisition_bundle_content_hash"
    ]
    assert base["acquisition_bundle_content_hash"] != changed_contract[
        "acquisition_bundle_content_hash"
    ]
    assert base["dataset_content_hash"] == base["acquisition_bundle_content_hash"]


def test_index_gap_report_includes_requested_leading_and_trailing_boundaries(
    tmp_path,
):
    class MissingIndexEdgesClient(FakeBinanceClient):
        def get_json(self, path, params):
            payload = super().get_json(path, params)
            if path == "/fapi/v1/indexPriceKlines":
                return payload[1:-1]
            return payload

    summary = run_first_batch(
        client=MissingIndexEdgesClient(),
        output_dir=tmp_path,
        symbols=("BTCUSDT", "ETHUSDT"),
        start_time_ms=0,
        end_time_ms=DAY_MS - 1,
        page_limit=2_000,
        include_index=True,
        clock_ms=lambda: DAY_MS + 1,
    )

    report = summary["gap_reports"]["BTCUSDT"]["index"]
    assert report["status"] == "GAPS_DETECTED"
    assert report["gap_intervals"] == (
        {"start_utc_ms": 0, "end_utc_ms": MINUTE_MS - 1},
        {"start_utc_ms": DAY_MS - MINUTE_MS, "end_utc_ms": DAY_MS - 1},
    )
