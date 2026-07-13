from __future__ import annotations

import argparse
import hashlib
import time
from dataclasses import asdict
from pathlib import Path
from time import sleep as system_sleep
from typing import Any

from pa_agent.research_data.aggregation import ONE_MINUTE_MS, aggregate_klines
from pa_agent.research_data.binance_public import BinancePublicClient
from pa_agent.research_data.canonical import canonical_dumps
from pa_agent.research_data.downloader import (
    DatasetDownloader,
    JsonClient,
    PublicGetRetrier,
)
from pa_agent.research_data.gaps import (
    FUNDING_SCHEDULE_VERSION,
    detect_funding_gap_intervals,
    detect_gap_intervals,
)
from pa_agent.research_data.hashing import (
    ACQUISITION_BUNDLE_CONTENT_VERSION,
    AUDIT_DATA_CONTENT_VERSION,
    EXECUTION_DATA_CONTENT_VERSION,
    STRATEGY_DATA_CONTENT_VERSION,
    acquisition_manifest_hash,
    acquisition_run_id,
    dataset_content_hash,
    versioned_content_bundle_hash,
)
from pa_agent.research_data.models import (
    CONTRACT_RULE_SCHEMA_VERSION,
    FUNDING_SCHEMA_VERSION,
    KLINE_SCHEMA_VERSION,
    Kline,
    StreamGapReport,
)
from pa_agent.research_data.normalize import (
    ContractRuleValidationFailure,
    contract_rule_validation_snapshot,
    normalize_contract_rules,
    normalize_funding_rate,
    normalize_price_kline,
    normalize_trade_kline,
)
from pa_agent.research_data.storage import AtomicDatasetStore
from pa_agent.research_data.validation import validate_native_bars

FOUR_HOURS_MS = 4 * 60 * ONE_MINUTE_MS
ONE_DAY_MS = 24 * 60 * ONE_MINUTE_MS


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


def _gap_report_dict(report: StreamGapReport) -> dict[str, Any]:
    result = asdict(report)
    result["gap_intervals"] = result["intervals"]
    return result


def _preflight_output_directory(
    output_dir: Path | str, *, existing_data_policy: str
) -> None:
    if existing_data_policy not in {"reject", "reuse"}:
        raise ValueError("existing_data_policy must be reject or reuse")
    if existing_data_policy == "reuse":
        return
    root = Path(output_dir)
    if (root / "summary.json").is_file() or (
        root / "acquisition_manifest.json"
    ).is_file():
        raise ValueError(
            "Existing completed raw directory requires explicit reuse or a new output directory"
        )


def _capture_source_server_time(
    *,
    requester: PublicGetRetrier,
    store: AtomicDatasetStore,
    downloaded_at_utc_ms: int,
) -> tuple[int, str]:
    response, retry_count = requester.get_json("/fapi/v1/time", {})
    if not isinstance(response, dict):
        raise TypeError("Binance server time must return an object")
    server_time = response.get("serverTime")
    if isinstance(server_time, bool) or not isinstance(server_time, int) or server_time < 0:
        raise ValueError("Binance serverTime must be a nonnegative integer")
    raw_hash = _sha256_json(response)
    request_identity = {
        "dataset_name": "source_server_time",
        "downloader_schema_version": "BINANCE_PUBLIC_DOWNLOADER_V2",
        "end_time_ms": None,
        "interval": None,
        "limit": None,
        "normalized_params": {},
        "path": "/fapi/v1/time",
        "pair": None,
        "start_time_ms": None,
        "symbol": None,
        "symbol_or_pair": None,
    }
    store.write_raw_page(
        "source_server_time",
        0,
        {
            "metadata": {
                "downloaded_at_utc_ms": downloaded_at_utc_ms,
                "first_timestamp": server_time,
                "last_timestamp": server_time,
                "next_start": None,
                "page_index": 0,
                "path": "/fapi/v1/time",
                "request": {},
                "request_identity": request_identity,
                "request_identity_hash": _sha256_json(request_identity),
                "retry_count": retry_count,
                "row_count": 1,
            },
            "payload": response,
            "request": {},
        },
    )
    return server_time, raw_hash


def _download_klines(
    *,
    downloader: DatasetDownloader,
    store: AtomicDatasetStore,
    symbol: str,
    interval: str,
    stream: str,
    start_time_ms: int,
    end_time_ms: int,
    page_limit: int,
    source_server_time_utc_ms: int,
    existing_data_policy: str,
) -> tuple[tuple[Kline, ...], dict[str, Any]]:
    if stream == "trade":
        path = "/fapi/v1/klines"
        params = {"interval": interval, "symbol": symbol}

        def normalizer(row):
            return normalize_trade_kline(
                row,
                symbol=symbol,
                interval=interval,
                source_server_time_utc_ms=source_server_time_utc_ms,
            )

    elif stream == "mark":
        path = "/fapi/v1/markPriceKlines"
        params = {"interval": interval, "symbol": symbol}

        def normalizer(row):
            return normalize_price_kline(
                row,
                stream="mark",
                symbol=symbol,
                interval=interval,
                source_server_time_utc_ms=source_server_time_utc_ms,
            )

    elif stream == "index":
        path = "/fapi/v1/indexPriceKlines"
        params = {"interval": interval, "pair": symbol}

        def normalizer(row):
            return normalize_price_kline(
                row,
                stream="index",
                symbol=symbol,
                interval=interval,
                source_server_time_utc_ms=source_server_time_utc_ms,
            )
    else:
        raise ValueError(f"Unsupported kline stream: {stream}")
    name = f"{symbol}_{stream}_{interval}"
    result = downloader.download_pages(
        dataset_name=name,
        path=path,
        params=params,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        limit=min(page_limit, 1_500),
        timestamp_extractor=lambda row: int(row[0]),
        existing_data_policy=existing_data_policy,
    )
    records = tuple(normalizer(row) for row in result.records)
    record_dicts = [asdict(record) for record in records]
    content_hash = dataset_content_hash(
        record_dicts, key_fields=("symbol", "open_time_utc_ms")
    )
    store.write_canonical_jsonl(
        f"canonical/{name}.jsonl",
        record_dicts,
        key_fields=("symbol", "open_time_utc_ms"),
    )
    manifest = {
        **result.manifest,
        "canonical_schema_version": KLINE_SCHEMA_VERSION,
        "dataset_content_hash": content_hash,
    }
    manifest["acquisition_manifest_hash"] = acquisition_manifest_hash(manifest)
    store.write_json_atomic(f"manifests/{name}.json", manifest)
    return records, manifest


def _download_funding(
    *,
    downloader: DatasetDownloader,
    store: AtomicDatasetStore,
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
    page_limit: int,
    existing_data_policy: str,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    name = f"{symbol}_funding"
    result = downloader.download_pages(
        dataset_name=name,
        path="/fapi/v1/fundingRate",
        params={"symbol": symbol},
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        limit=min(page_limit, 1_000),
        timestamp_extractor=lambda row: int(row["fundingTime"]),
        existing_data_policy=existing_data_policy,
    )
    records = tuple(
        normalize_funding_rate(row, expected_symbol=symbol) for row in result.records
    )
    record_dicts = [asdict(record) for record in records]
    content_hash = dataset_content_hash(
        record_dicts, key_fields=("symbol", "funding_time_utc_ms")
    )
    store.write_canonical_jsonl(
        f"canonical/{name}.jsonl",
        record_dicts,
        key_fields=("symbol", "funding_time_utc_ms"),
    )
    manifest = {
        **result.manifest,
        "canonical_schema_version": FUNDING_SCHEMA_VERSION,
        "dataset_content_hash": content_hash,
    }
    manifest["acquisition_manifest_hash"] = acquisition_manifest_hash(manifest)
    store.write_json_atomic(f"manifests/{name}.json", manifest)
    return records, manifest


def run_first_batch(
    *,
    client: JsonClient,
    output_dir: Path | str,
    symbols: tuple[str, ...],
    start_time_ms: int,
    end_time_ms: int,
    page_limit: int,
    include_index: bool,
    clock_ms,
    existing_data_policy: str = "reject",
    sleep=system_sleep,
    max_retries: int = 3,
    base_delay_seconds: float = 0.5,
) -> dict[str, Any]:
    _preflight_output_directory(
        output_dir, existing_data_policy=existing_data_policy
    )
    store = AtomicDatasetStore(output_dir)
    requester = PublicGetRetrier(
        client,
        sleep=sleep,
        max_retries=max_retries,
        base_delay_seconds=base_delay_seconds,
    )
    acquisition_started_at_utc_ms = clock_ms()
    source_server_time_utc_ms, source_server_time_raw_payload_sha256 = (
        _capture_source_server_time(
            requester=requester,
            store=store,
            downloaded_at_utc_ms=acquisition_started_at_utc_ms,
        )
    )
    downloader = DatasetDownloader(
        client, store, clock_ms=clock_ms, requester=requester
    )
    manifests: dict[str, dict[str, Any]] = {}
    acquisition_content_hashes: dict[str, str] = {}
    strategy_content_hashes: dict[str, str] = {}
    execution_content_hashes: dict[str, str] = {}
    audit_content_hashes: dict[str, str] = {}
    gap_reports: dict[str, dict[str, dict[str, Any]]] = {}
    aggregation: dict[str, dict[str, dict[str, Any]]] = {}

    for symbol in symbols:
        trade_1m, trade_manifest = _download_klines(
            downloader=downloader,
            store=store,
            symbol=symbol,
            interval="1m",
            stream="trade",
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            page_limit=page_limit,
            source_server_time_utc_ms=source_server_time_utc_ms,
            existing_data_policy=existing_data_policy,
        )
        native_4h, manifest_4h = _download_klines(
            downloader=downloader,
            store=store,
            symbol=symbol,
            interval="4h",
            stream="trade",
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            page_limit=page_limit,
            source_server_time_utc_ms=source_server_time_utc_ms,
            existing_data_policy=existing_data_policy,
        )
        native_1d, manifest_1d = _download_klines(
            downloader=downloader,
            store=store,
            symbol=symbol,
            interval="1d",
            stream="trade",
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            page_limit=page_limit,
            source_server_time_utc_ms=source_server_time_utc_ms,
            existing_data_policy=existing_data_policy,
        )
        mark_1m, mark_manifest = _download_klines(
            downloader=downloader,
            store=store,
            symbol=symbol,
            interval="1m",
            stream="mark",
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            page_limit=page_limit,
            source_server_time_utc_ms=source_server_time_utc_ms,
            existing_data_policy=existing_data_policy,
        )
        funding, funding_manifest = _download_funding(
            downloader=downloader,
            store=store,
            symbol=symbol,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            page_limit=page_limit,
            existing_data_policy=existing_data_policy,
        )
        symbol_manifests = {
            f"{symbol}_trade_1m": trade_manifest,
            f"{symbol}_trade_4h": manifest_4h,
            f"{symbol}_trade_1d": manifest_1d,
            f"{symbol}_mark_1m": mark_manifest,
            f"{symbol}_funding": funding_manifest,
        }
        index_1m: tuple[Kline, ...] = ()
        if include_index:
            index_1m, index_manifest = _download_klines(
                downloader=downloader,
                store=store,
                symbol=symbol,
                interval="1m",
                stream="index",
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
                page_limit=page_limit,
                source_server_time_utc_ms=source_server_time_utc_ms,
                existing_data_policy=existing_data_policy,
            )
            symbol_manifests[f"{symbol}_index_1m"] = index_manifest
        manifests.update(symbol_manifests)
        symbol_content_hashes = {
            name: manifest["dataset_content_hash"]
            for name, manifest in symbol_manifests.items()
        }
        acquisition_content_hashes.update(symbol_content_hashes)
        strategy_content_hashes.update(
            {
                name: symbol_content_hashes[name]
                for name in (
                    f"{symbol}_trade_1m",
                    f"{symbol}_trade_4h",
                    f"{symbol}_trade_1d",
                )
            }
        )
        execution_content_hashes.update(
            {
                name: symbol_content_hashes[name]
                for name in (
                    f"{symbol}_trade_1m",
                    f"{symbol}_mark_1m",
                    f"{symbol}_funding",
                )
            }
        )
        if include_index:
            index_name = f"{symbol}_index_1m"
            audit_content_hashes[index_name] = symbol_content_hashes[index_name]

        gap_reports[symbol] = {
            "trade": _gap_report_dict(
                detect_gap_intervals(
                    stream="trade",
                    timestamps=(bar.open_time_utc_ms for bar in trade_1m),
                    expected_step_ms=ONE_MINUTE_MS,
                    expected_start_ms=start_time_ms,
                    expected_end_ms=end_time_ms - (end_time_ms % ONE_MINUTE_MS),
                )
            ),
            "mark": _gap_report_dict(
                detect_gap_intervals(
                    stream="mark",
                    timestamps=(bar.open_time_utc_ms for bar in mark_1m),
                    expected_step_ms=ONE_MINUTE_MS,
                    expected_start_ms=start_time_ms,
                    expected_end_ms=end_time_ms - (end_time_ms % ONE_MINUTE_MS),
                )
            ),
            "funding": _gap_report_dict(
                detect_funding_gap_intervals(
                    (item.funding_time_utc_ms for item in funding),
                    expected_start_ms=start_time_ms,
                    expected_end_ms=end_time_ms,
                    schedule_version=FUNDING_SCHEDULE_VERSION,
                )
            ),
            "index": _gap_report_dict(
                detect_gap_intervals(
                    stream="index",
                    timestamps=(bar.open_time_utc_ms for bar in index_1m),
                    expected_step_ms=ONE_MINUTE_MS,
                    expected_start_ms=start_time_ms,
                    expected_end_ms=end_time_ms - (end_time_ms % ONE_MINUTE_MS),
                )
                if include_index
                else StreamGapReport("index", "NOT_REQUESTED", ())
            ),
        }

        aggregated_4h = aggregate_klines(trade_1m, interval_ms=FOUR_HOURS_MS)
        aggregated_1d = aggregate_klines(trade_1m, interval_ms=ONE_DAY_MS)
        aggregation[symbol] = {
            "4h": asdict(
                validate_native_bars(
                    aggregated_4h.bars,
                    native_4h,
                    incomplete_intervals=aggregated_4h.incomplete_intervals,
                )
            ),
            "1d": asdict(
                validate_native_bars(
                    aggregated_1d.bars,
                    native_1d,
                    incomplete_intervals=aggregated_1d.incomplete_intervals,
                )
            ),
            "incomplete_4h": [asdict(item) for item in aggregated_4h.incomplete_intervals],
            "incomplete_1d": [asdict(item) for item in aggregated_1d.incomplete_intervals],
        }

    exchange_info, exchange_retry_count = requester.get_json(
        "/fapi/v1/exchangeInfo", {}
    )
    if not isinstance(exchange_info, dict):
        raise TypeError("exchangeInfo must return an object")
    exchange_hash = _sha256_json(exchange_info)
    exchange_identity = {
        "dataset_name": "exchange_info",
        "downloader_schema_version": "BINANCE_PUBLIC_DOWNLOADER_V2",
        "end_time_ms": None,
        "interval": None,
        "limit": None,
        "normalized_params": {},
        "path": "/fapi/v1/exchangeInfo",
        "pair": None,
        "start_time_ms": None,
        "symbol": None,
        "symbol_or_pair": None,
    }
    exchange_identity_hash = _sha256_json(exchange_identity)
    store.write_raw_page(
        "exchange_info",
        0,
        {
            "metadata": {
                "downloaded_at_utc_ms": acquisition_started_at_utc_ms,
                "first_timestamp": None,
                "last_timestamp": None,
                "next_start": None,
                "page_index": 0,
                "path": "/fapi/v1/exchangeInfo",
                "request": {},
                "request_identity": exchange_identity,
                "request_identity_hash": exchange_identity_hash,
                "retry_count": exchange_retry_count,
                "row_count": len(exchange_info.get("symbols", [])),
            },
            "payload": exchange_info,
            "request": {},
        },
    )
    contract_validation = contract_rule_validation_snapshot(
        exchange_info,
        symbols=symbols,
        acquired_at_utc_ms=acquisition_started_at_utc_ms,
        source_hash=exchange_hash,
    )
    try:
        contract_rules = normalize_contract_rules(
            exchange_info,
            symbols=symbols,
            acquired_at_utc_ms=acquisition_started_at_utc_ms,
            source_hash=exchange_hash,
        )
    except ContractRuleValidationFailure as exc:
        store.write_json_atomic("contract_rule_validation_failure.json", asdict(exc.snapshot))
        raise
    contract_validation_dict = asdict(contract_validation)
    for field in ("requested_symbols", "returned_symbols", "missing_symbols"):
        contract_validation_dict[field] = list(contract_validation_dict[field])
    contract_dicts = [asdict(rule) for rule in contract_rules]
    store.write_canonical_jsonl(
        "canonical/contract_rules_current.jsonl",
        contract_dicts,
        key_fields=("symbol",),
    )
    store.write_json_atomic(
        "canonical/contract_rules_current_snapshot.json", contract_validation_dict
    )
    contract_content_records = [
        {
            key: value
            for key, value in record.items()
            if key not in {"acquired_at_utc_ms", "source_hash"}
        }
        for record in contract_dicts
    ]
    contract_content_hash = dataset_content_hash(
        contract_content_records, key_fields=("symbol",)
    )
    contract_rule_dataset_manifest = {
        "acquired_at_utc_ms": acquisition_started_at_utc_ms,
        "canonical_schema_version": CONTRACT_RULE_SCHEMA_VERSION,
        "dataset_content_hash": contract_content_hash,
        "dataset_name": "contract_rules_current",
        "record_count": len(contract_dicts),
        "source_hash": exchange_hash,
    }
    contract_rule_dataset_manifest["acquisition_manifest_hash"] = (
        acquisition_manifest_hash(contract_rule_dataset_manifest)
    )
    store.write_json_atomic(
        "manifests/contract_rules_current.json", contract_rule_dataset_manifest
    )
    acquisition_content_hashes["contract_rules_current"] = contract_content_hash
    acquisition_bundle_content_hash = versioned_content_bundle_hash(
        bundle_version=ACQUISITION_BUNDLE_CONTENT_VERSION,
        dataset_hashes=acquisition_content_hashes,
    )
    strategy_data_content_hash = versioned_content_bundle_hash(
        bundle_version=STRATEGY_DATA_CONTENT_VERSION,
        dataset_hashes=strategy_content_hashes,
    )
    execution_data_content_hash = versioned_content_bundle_hash(
        bundle_version=EXECUTION_DATA_CONTENT_VERSION,
        dataset_hashes=execution_content_hashes,
    )
    audit_data_content_hash = versioned_content_bundle_hash(
        bundle_version=AUDIT_DATA_CONTENT_VERSION,
        dataset_hashes=audit_content_hashes,
    )
    content_hash_versions = {
        "acquisition_bundle": ACQUISITION_BUNDLE_CONTENT_VERSION,
        "audit_data": AUDIT_DATA_CONTENT_VERSION,
        "contract_rule": CONTRACT_RULE_SCHEMA_VERSION,
        "execution_data": EXECUTION_DATA_CONTENT_VERSION,
        "strategy_data": STRATEGY_DATA_CONTENT_VERSION,
    }
    acquisition_manifest = {
        "acquisition_bundle_content_hash": acquisition_bundle_content_hash,
        "audit_data_content_hash": audit_data_content_hash,
        "completed_at_utc_ms": clock_ms(),
        "content_hash_versions": content_hash_versions,
        "contract_rule_content_hash": contract_content_hash,
        "contract_rule_dataset_manifest_hash": contract_rule_dataset_manifest[
            "acquisition_manifest_hash"
        ],
        "dataset_content_hash": acquisition_bundle_content_hash,
        "datasets": {name: manifest["acquisition_manifest_hash"] for name, manifest in manifests.items()},
        "dataset_page_hashes": {
            **{
                name: [page["raw_payload_sha256"] for page in manifest["pages"]]
                for name, manifest in manifests.items()
            },
            "exchange_info": [
                store.read_raw_pages("exchange_info")[0]["metadata"]["raw_payload_sha256"]
            ],
            "source_server_time": [source_server_time_raw_payload_sha256],
        },
        "exchange_info_acquired_at_utc_ms": acquisition_started_at_utc_ms,
        "exchange_info_raw_payload_sha256": store.read_raw_pages("exchange_info")[0][
            "metadata"
        ]["raw_payload_sha256"],
        "exchange_info_request_identity_hash": exchange_identity_hash,
        "execution_data_content_hash": execution_data_content_hash,
        "source_server_time_raw_payload_sha256": source_server_time_raw_payload_sha256,
        "source_server_time_utc_ms": source_server_time_utc_ms,
        "strategy_data_content_hash": strategy_data_content_hash,
        "symbols": list(symbols),
    }
    global_acquisition_hash = acquisition_manifest_hash(acquisition_manifest)
    summary = {
        "acquisition_bundle_content_hash": acquisition_bundle_content_hash,
        "acquisition_manifest_hash": global_acquisition_hash,
        "acquisition_run_id": acquisition_run_id(acquisition_manifest),
        "aggregation": aggregation,
        "audit_data_content_hash": audit_data_content_hash,
        "content_hash_versions": content_hash_versions,
        "contract_rule_content_hash": contract_content_hash,
        "contract_rules": contract_dicts,
        "contract_rule_snapshot": contract_validation_dict,
        "contract_rule_dataset_manifest": contract_rule_dataset_manifest,
        "dataset_content_hash": acquisition_bundle_content_hash,
        "dataset_manifests": manifests,
        "execution_data_content_hash": execution_data_content_hash,
        "gap_reports": gap_reports,
        "source_server_time_raw_payload_sha256": source_server_time_raw_payload_sha256,
        "source_server_time_utc_ms": source_server_time_utc_ms,
        "strategy_data_content_hash": strategy_data_content_hash,
    }
    store.write_json_atomic("acquisition_manifest.json", acquisition_manifest)
    store.write_json_atomic("summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download public Binance research data")
    parser.add_argument("--output", type=Path, default=Path("research_data_output"))
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--start-ms", type=int, required=True)
    parser.add_argument("--end-ms", type=int, required=True)
    parser.add_argument("--page-limit", type=int, default=1_000)
    parser.add_argument("--include-index", action="store_true")
    parser.add_argument(
        "--existing-data-policy", choices=("reject", "reuse"), default="reject"
    )
    args = parser.parse_args(argv)
    run_first_batch(
        client=BinancePublicClient(),
        output_dir=args.output,
        symbols=tuple(args.symbols),
        start_time_ms=args.start_ms,
        end_time_ms=args.end_ms,
        page_limit=args.page_limit,
        include_index=args.include_index,
        clock_ms=lambda: int(time.time() * 1_000),
        existing_data_policy=args.existing_data_policy,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
