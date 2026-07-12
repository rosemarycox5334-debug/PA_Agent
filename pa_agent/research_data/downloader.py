from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

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


class DatasetDownloader:
    def __init__(
        self,
        client: JsonClient,
        store: AtomicDatasetStore,
        *,
        clock_ms: Callable[[], int],
    ) -> None:
        self._client = client
        self._store = store
        self._clock_ms = clock_ms

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
    ) -> DownloadResult:
        if limit < 1 or start_time_ms > end_time_ms:
            raise ValueError("Invalid pagination range or limit")
        checkpoint_path = f"state/{dataset_name}.checkpoint.json"
        checkpoint = self._store.read_json_or_none(checkpoint_path)
        existing_pages = self._store.read_raw_pages(dataset_name)
        resumed = checkpoint is not None
        if checkpoint is None:
            current_start = start_time_ms
            page_index = len(existing_pages)
        else:
            current_start = int(checkpoint["overlap_start"])
            page_index = int(checkpoint["next_page_index"])

        all_records = [record for page in existing_pages for record in page["payload"]]
        page_meta = [page["metadata"] for page in existing_pages]

        while current_start <= end_time_ms:
            request_params = dict(params)
            request_params.update(
                {"endTime": end_time_ms, "limit": limit, "startTime": current_start}
            )
            payload = self._client.get_json(path, request_params)
            if not isinstance(payload, list):
                raise TypeError("Paginated endpoint must return a list")
            downloaded_at = self._clock_ms()
            metadata = {
                "downloaded_at_utc_ms": downloaded_at,
                "page_index": page_index,
                "path": path,
                "request": request_params,
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
            timestamps = [timestamp_extractor(record) for record in payload]
            last_timestamp = max(timestamps)
            if last_timestamp < current_start:
                raise ValueError("Page did not advance pagination")
            all_records.extend(payload)
            self._store.write_json_atomic(
                checkpoint_path,
                {
                    "next_page_index": page_index + 1,
                    "next_start": last_timestamp + 1,
                    "overlap_start": last_timestamp,
                },
            )
            if len(payload) < limit or last_timestamp >= end_time_ms:
                break
            current_start = last_timestamp + 1
            page_index += 1

        self._store.remove(checkpoint_path)
        deduplicated: dict[int, Any] = {}
        for record in all_records:
            deduplicated[timestamp_extractor(record)] = record
        ordered = tuple(deduplicated[key] for key in sorted(deduplicated))
        manifest = {
            "completed_at_utc_ms": self._clock_ms(),
            "dataset_name": dataset_name,
            "end_time_utc_ms": end_time_ms,
            "pages": page_meta,
            "record_count": len(ordered),
            "resumed": resumed,
            "start_time_utc_ms": start_time_ms,
        }
        return DownloadResult(
            dataset_name=dataset_name,
            records=ordered,
            manifest=manifest,
            acquisition_manifest_hash=acquisition_manifest_hash(manifest),
            resumed=resumed,
        )
