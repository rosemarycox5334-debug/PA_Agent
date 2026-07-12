from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pa_agent.research_data.canonical import canonical_dumps, canonical_records


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


def dataset_content_hash(
    records: Iterable[Mapping[str, Any]], *, key_fields: Sequence[str]
) -> str:
    ordered = canonical_records(records, key_fields=key_fields)
    return _sha256(ordered)


def acquisition_manifest_hash(manifest: Mapping[str, Any]) -> str:
    return _sha256(manifest)


def acquisition_run_id(manifest: Mapping[str, Any]) -> str:
    return f"acq_{acquisition_manifest_hash(manifest)[:24]}"


def computational_experiment_id(
    *,
    dataset_content_hash_value: str,
    sample_start_utc_ms: int,
    sample_end_utc_ms: int,
    strategy_version: str,
    execution_version: str,
    cost_version: str,
    code_commit: str,
    dependency_lock_version: str,
) -> str:
    payload = {
        "code_commit": code_commit,
        "cost_version": cost_version,
        "dataset_content_hash": dataset_content_hash_value,
        "dependency_lock_version": dependency_lock_version,
        "execution_version": execution_version,
        "sample_end_utc_ms": sample_end_utc_ms,
        "sample_start_utc_ms": sample_start_utc_ms,
        "strategy_version": strategy_version,
    }
    return f"exp_{_sha256(payload)[:24]}"
