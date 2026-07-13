from __future__ import annotations

import argparse
import hashlib
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pa_agent.research_data.aggregation import ONE_MINUTE_MS, aggregate_klines
from pa_agent.research_data.binance_public import BinancePublicClient
from pa_agent.research_data.canonical import canonical_dumps
from pa_agent.research_data.downloader import DatasetDownloader, JsonClient
from pa_agent.research_data.gaps import (
    FUNDING_SCHEDULE_VERSION,
    detect_funding_gap_intervals,
    detect_gap_intervals,
)
from pa_agent.research_data.hashing import (
    acquisition_manifest_hash,
    acquisition_run_id,
    dataset_content_hash,
)
from pa_agent.research_data.models import Kline, StreamGapReport
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
    now_ms: int,
    existing_data_policy: str,
) -> tuple[tuple[Kline, ...], dict[str, Any]]:
    if stream == "trade":
        path = "/fapi/v1/klines"
        params = {"interval": interval, "symbol": symbol}

        def normalizer(row):
            return normalize_trade_kline(
                row, symbol=symbol, interval=interval, now_ms=now_ms
            )

    elif stream == "mark":
        path = "/fapi/v1/markPriceKlines"
        params = {"interval": interval, "symbol": symbol}

        def normalizer(row):
            return normalize_price_kline(
                row, stream="mark", symbol=symbol, interval=interval, now_ms=now_ms
            )

    elif stream == "index":
        path = "/fapi/v1/indexPriceKlines"
        params = {"interval": interval, "pair": symbol}

        def normalizer(row):
            return normalize_price_kline(
                row, stream="index", symbol=symbol, interval=interval, now_ms=now_ms
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
    manifest = {**result.manifest, "dataset_content_hash": content_hash}
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
    records = tuple(normalize_funding_rate(row) for row in result.records)
    record_dicts = [asdict(record) for record in records]
    content_hash = dataset_content_hash(
        record_dicts, key_fields=("symbol", "funding_time_utc_ms")
    )
    store.write_canonical_jsonl(
        f"canonical/{name}.jsonl",
        record_dicts,
        key_fields=("symbol", "funding_time_utc_ms"),
    )
    manifest = {**result.manifest, "dataset_content_hash": content_hash}
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
) -> dict[str, Any]:
    store = AtomicDatasetStore(output_dir)
    downloader = DatasetDownloader(client, store, clock_ms=clock_ms)
    now_ms = clock_ms()
    manifests: dict[str, dict[str, Any]] = {}
    content_hashes: list[dict[str, str]] = []
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
            now_ms=now_ms,
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
            now_ms=now_ms,
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
            now_ms=now_ms,
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
            now_ms=now_ms,
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
                now_ms=now_ms,
                existing_data_policy=existing_data_policy,
            )
            symbol_manifests[f"{symbol}_index_1m"] = index_manifest
        manifests.update(symbol_manifests)
        content_hashes.extend(
            {"dataset": name, "hash": manifest["dataset_content_hash"]}
            for name, manifest in symbol_manifests.items()
        )

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

    exchange_info = client.get_json("/fapi/v1/exchangeInfo", {})
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
                "downloaded_at_utc_ms": now_ms,
                "first_timestamp": None,
                "last_timestamp": None,
                "next_start": None,
                "page_index": 0,
                "path": "/fapi/v1/exchangeInfo",
                "request": {},
                "request_identity": exchange_identity,
                "request_identity_hash": exchange_identity_hash,
                "retry_count": 0,
                "row_count": len(exchange_info.get("symbols", [])),
            },
            "payload": exchange_info,
            "request": {},
        },
    )
    contract_validation = contract_rule_validation_snapshot(
        exchange_info,
        symbols=symbols,
        acquired_at_utc_ms=now_ms,
        source_hash=exchange_hash,
    )
    try:
        contract_rules = normalize_contract_rules(
            exchange_info,
            symbols=symbols,
            acquired_at_utc_ms=now_ms,
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
    content_hashes.append({"dataset": "contract_rules_current", "hash": contract_content_hash})
    global_content_hash = dataset_content_hash(
        content_hashes, key_fields=("dataset",)
    )
    acquisition_manifest = {
        "completed_at_utc_ms": clock_ms(),
        "dataset_content_hash": global_content_hash,
        "datasets": {name: manifest["acquisition_manifest_hash"] for name, manifest in manifests.items()},
        "dataset_page_hashes": {
            **{
                name: [page["raw_payload_sha256"] for page in manifest["pages"]]
                for name, manifest in manifests.items()
            },
            "exchange_info": [
                store.read_raw_pages("exchange_info")[0]["metadata"]["raw_payload_sha256"]
            ],
        },
        "exchange_info_acquired_at_utc_ms": now_ms,
        "exchange_info_raw_payload_sha256": store.read_raw_pages("exchange_info")[0][
            "metadata"
        ]["raw_payload_sha256"],
        "exchange_info_request_identity_hash": exchange_identity_hash,
        "symbols": list(symbols),
    }
    global_acquisition_hash = acquisition_manifest_hash(acquisition_manifest)
    summary = {
        "acquisition_manifest_hash": global_acquisition_hash,
        "acquisition_run_id": acquisition_run_id(acquisition_manifest),
        "aggregation": aggregation,
        "contract_rules": contract_dicts,
        "contract_rule_snapshot": contract_validation_dict,
        "dataset_content_hash": global_content_hash,
        "dataset_manifests": manifests,
        "gap_reports": gap_reports,
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
