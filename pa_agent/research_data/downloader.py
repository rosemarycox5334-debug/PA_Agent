from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import sleep as system_sleep
from typing import Any, Protocol

from pa_agent.research_data.binance_public import PublicTransportError
from pa_agent.research_data.canonical import canonical_dumps
from pa_agent.research_data.hashing import acquisition_manifest_hash
from pa_agent.research_data.storage import AtomicDatasetStore


class JsonClient(Protocol):
    def get_json(self, path: str, params: Mapping[str, Any]) -> dict[str, Any] | list[Any]: ...


@dataclass(frozen=True, slots=True)
class DownloadResult:
    dataset_name: str
    records: tuple[Any, ...]
    manifest: dict[str, Any]
    acquisition_manifest_hash: str
    resumed: bool


DOWNLOADER_SCHEMA_VERSION = "BINANCE_PUBLIC_DOWNLOADER_V2"


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


def _request_identity(
    *,
    dataset_name: str,
    path: str,
    params: Mapping[str, Any],
    start_time_ms: int,
    end_time_ms: int,
    limit: int,
) -> dict[str, Any]:
    normalized_params = {str(key): value for key, value in sorted(params.items(), key=lambda x: str(x[0]))}
    return {
        "dataset_name": dataset_name,
        "downloader_schema_version": DOWNLOADER_SCHEMA_VERSION,
        "end_time_ms": end_time_ms,
        "interval": normalized_params.get("interval"),
        "limit": limit,
        "normalized_params": normalized_params,
        "path": path,
        "pair": normalized_params.get("pair"),
        "start_time_ms": start_time_ms,
        "symbol": normalized_params.get("symbol"),
        "symbol_or_pair": normalized_params.get("symbol", normalized_params.get("pair")),
    }


def _deduplicate_records(
    records: list[Any], timestamp_extractor: Callable[[Any], int]
) -> tuple[Any, ...]:
    deduplicated: dict[int, Any] = {}
    canonical_by_key: dict[int, str] = {}
    for record in records:
        key = timestamp_extractor(record)
        serialized = canonical_dumps(record)
        if key in deduplicated and canonical_by_key[key] != serialized:
            raise ValueError(f"ConflictingDuplicateRecord for primary key {key}")
        deduplicated[key] = record
        canonical_by_key[key] = serialized
    return tuple(deduplicated[key] for key in sorted(deduplicated))


def _validate_raw_pages(
    pages: list[dict[str, Any]],
    *,
    request_identity: Mapping[str, Any],
    request_identity_hash: str,
    timestamp_extractor: Callable[[Any], int],
) -> tuple[list[Any], list[dict[str, Any]]]:
    records: list[Any] = []
    metadata_items: list[dict[str, Any]] = []
    previous_last_timestamp: int | None = None
    for expected_index, page in enumerate(pages):
        if not isinstance(page, dict) or not isinstance(page.get("metadata"), dict):
            raise ValueError("Invalid raw page envelope")
        metadata = page["metadata"]
        payload = page.get("payload")
        if not isinstance(payload, list):
            raise ValueError("Invalid raw page payload")
        if metadata.get("page_index") != expected_index:
            raise ValueError("Raw page indexes are not contiguous")
        if metadata.get("request_identity") != request_identity or metadata.get(
            "request_identity_hash"
        ) != request_identity_hash:
            raise ValueError("Raw page request identity mismatch")
        if metadata.get("raw_payload_sha256") != _sha256(payload):
            raise ValueError("raw payload hash mismatch")
        timestamps = [timestamp_extractor(record) for record in payload]
        first_timestamp = min(timestamps) if timestamps else None
        last_timestamp = max(timestamps) if timestamps else None
        next_start = last_timestamp + 1 if last_timestamp is not None else None
        if metadata.get("first_timestamp") != first_timestamp:
            raise ValueError("Raw page first timestamp mismatch")
        if metadata.get("last_timestamp") != last_timestamp:
            raise ValueError("Raw page last timestamp mismatch")
        if metadata.get("next_start") != next_start:
            raise ValueError("Raw page next start mismatch")
        if metadata.get("row_count") != len(payload):
            raise ValueError("Raw page row count mismatch")
        if page.get("request") != metadata.get("request"):
            raise ValueError("Raw page request evidence mismatch")
        request = metadata["request"]
        if metadata.get("path") != request_identity["path"]:
            raise ValueError("Raw page path does not match request identity")
        expected_request = dict(request_identity["normalized_params"])
        expected_request.update(
            {
                "endTime": request_identity["end_time_ms"],
                "limit": request_identity["limit"],
                "startTime": request.get("startTime"),
            }
        )
        if request != expected_request:
            raise ValueError("Raw page request parameters do not match request identity")
        request_start = request.get("startTime")
        if expected_index == 0:
            if request_start != request_identity["start_time_ms"]:
                raise ValueError("Raw page chain has an invalid first request")
        elif previous_last_timestamp is None or request_start not in {
            previous_last_timestamp,
            previous_last_timestamp + 1,
        }:
            raise ValueError("Raw page chain is not contiguous")
        records.extend(payload)
        metadata_items.append(metadata)
        previous_last_timestamp = last_timestamp
    _deduplicate_records(records, timestamp_extractor)
    return records, metadata_items


class DatasetDownloader:
    def __init__(
        self,
        client: JsonClient,
        store: AtomicDatasetStore,
        *,
        clock_ms: Callable[[], int],
        sleep: Callable[[float], None] = system_sleep,
        max_retries: int = 3,
        base_delay_seconds: float = 0.5,
    ) -> None:
        if max_retries < 0 or base_delay_seconds < 0:
            raise ValueError("Retry settings must be non-negative")
        self._client = client
        self._store = store
        self._clock_ms = clock_ms
        self._sleep = sleep
        self._max_retries = max_retries
        self._base_delay_seconds = base_delay_seconds

    def _get_page(self, path: str, params: Mapping[str, Any]) -> tuple[dict[str, Any] | list[Any], int]:
        retry_count = 0
        while True:
            try:
                return self._client.get_json(path, params), retry_count
            except PublicTransportError as exc:
                if not exc.retryable or retry_count >= self._max_retries:
                    raise
                self._sleep(self._base_delay_seconds * (2**retry_count))
                retry_count += 1

    def download_pages(
        self,
        *,
        dataset_name: str,
        path: str,
        params: Mapping[str, Any],
        start_time_ms: int,
        end_time_ms: int,
        limit: int,
        timestamp_extractor: Callable[[Any], int],
        existing_data_policy: str = "reject",
    ) -> DownloadResult:
        if limit < 1 or start_time_ms > end_time_ms:
            raise ValueError("Invalid pagination range or limit")
        if existing_data_policy not in {"reject", "reuse"}:
            raise ValueError("existing_data_policy must be reject or reuse")
        request_identity = _request_identity(
            dataset_name=dataset_name,
            path=path,
            params=params,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            limit=limit,
        )
        request_identity_hash = _sha256(request_identity)
        checkpoint_path = f"state/{dataset_name}.checkpoint.json"
        checkpoint = self._store.read_json_or_none(checkpoint_path)
        existing_pages = self._store.read_raw_pages(dataset_name)
        resumed = checkpoint is not None
        if checkpoint is None:
            if existing_pages and existing_data_policy == "reject":
                raise ValueError(
                    "Existing completed raw directory requires explicit reuse or a new output directory"
                )
            current_start = start_time_ms
            page_index = 0
        else:
            if checkpoint.get("request_identity") != request_identity or checkpoint.get(
                "request_identity_hash"
            ) != request_identity_hash:
                raise ValueError("Checkpoint request identity mismatch")
            current_start = int(checkpoint["overlap_start"])
            page_index = int(checkpoint["next_page_index"])

        all_records, page_meta = _validate_raw_pages(
            existing_pages,
            request_identity=request_identity,
            request_identity_hash=request_identity_hash,
            timestamp_extractor=timestamp_extractor,
        ) if existing_pages else ([], [])
        if checkpoint is not None:
            if page_index != len(existing_pages):
                raise ValueError("Checkpoint page index does not match raw pages")
            if checkpoint.get("raw_page_hashes") != [
                item["raw_payload_sha256"] for item in page_meta
            ]:
                raise ValueError("Checkpoint raw page hashes do not match raw pages")
        elif existing_pages and existing_data_policy == "reuse":
            current_start = end_time_ms + 1

        while current_start <= end_time_ms:
            request_params = dict(params)
            request_params.update(
                {"endTime": end_time_ms, "limit": limit, "startTime": current_start}
            )
            payload, retry_count = self._get_page(path, request_params)
            if not isinstance(payload, list):
                raise TypeError("Paginated endpoint must return a list")
            downloaded_at = self._clock_ms()
            timestamps = [timestamp_extractor(record) for record in payload]
            first_timestamp = min(timestamps) if timestamps else None
            last_timestamp = max(timestamps) if timestamps else None
            next_start = last_timestamp + 1 if last_timestamp is not None else None
            metadata = {
                "downloaded_at_utc_ms": downloaded_at,
                "first_timestamp": first_timestamp,
                "last_timestamp": last_timestamp,
                "next_start": next_start,
                "page_index": page_index,
                "path": path,
                "raw_payload_sha256": _sha256(payload),
                "request": request_params,
                "request_identity": request_identity,
                "request_identity_hash": request_identity_hash,
                "retry_count": retry_count,
                "row_count": len(payload),
            }
            self._store.write_raw_page(
                dataset_name,
                page_index,
                {"metadata": metadata, "payload": payload, "request": request_params},
            )
            page_meta.append(metadata)
            if not payload:
                break
            assert last_timestamp is not None
            if last_timestamp < current_start:
                raise ValueError("Page did not advance pagination")
            all_records.extend(payload)
            self._store.write_json_atomic(
                checkpoint_path,
                {
                    "next_page_index": page_index + 1,
                    "next_start": last_timestamp + 1,
                    "overlap_start": last_timestamp,
                    "raw_page_hashes": [item["raw_payload_sha256"] for item in page_meta],
                    "request_identity": request_identity,
                    "request_identity_hash": request_identity_hash,
                },
            )
            if len(payload) < limit or last_timestamp >= end_time_ms:
                break
            current_start = last_timestamp + 1
            page_index += 1

        persisted_pages = self._store.read_raw_pages(dataset_name)
        all_records, page_meta = _validate_raw_pages(
            persisted_pages,
            request_identity=request_identity,
            request_identity_hash=request_identity_hash,
            timestamp_extractor=timestamp_extractor,
        )
        ordered = _deduplicate_records(all_records, timestamp_extractor)
        self._store.remove(checkpoint_path)
        manifest = {
            "completed_at_utc_ms": self._clock_ms(),
            "dataset_name": dataset_name,
            "end_time_utc_ms": end_time_ms,
            "pages": page_meta,
            "record_count": len(ordered),
            "request_identity": request_identity,
            "request_identity_hash": request_identity_hash,
            "resumed": resumed,
            "reused_existing": bool(existing_pages and checkpoint is None),
            "start_time_utc_ms": start_time_ms,
        }
        return DownloadResult(
            dataset_name=dataset_name,
            records=ordered,
            manifest=manifest,
            acquisition_manifest_hash=acquisition_manifest_hash(manifest),
            resumed=resumed,
        )
