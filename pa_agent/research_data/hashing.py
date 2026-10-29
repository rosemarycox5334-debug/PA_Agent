from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pa_agent.research_data.canonical import canonical_dumps, canonical_records

ACQUISITION_BUNDLE_CONTENT_VERSION = "ACQUISITION_BUNDLE_CONTENT_V1"
STRATEGY_DATA_CONTENT_VERSION = "STRATEGY_DATA_CONTENT_V1"
EXECUTION_DATA_CONTENT_VERSION = "EXECUTION_DATA_CONTENT_V1"
AUDIT_DATA_CONTENT_VERSION = "AUDIT_DATA_CONTENT_V1"


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


def dataset_content_hash(
    records: Iterable[Mapping[str, Any]], *, key_fields: Sequence[str]
) -> str:
    ordered = canonical_records(records, key_fields=key_fields)
    return _sha256(ordered)


def versioned_content_bundle_hash(
    *, bundle_version: str, dataset_hashes: Mapping[str, str]
) -> str:
    if not bundle_version:
        raise ValueError("bundle_version must be non-empty")
    normalized = [
        {"dataset": str(name), "hash": str(content_hash)}
        for name, content_hash in sorted(dataset_hashes.items())
    ]
    if any(not item["dataset"] or not item["hash"] for item in normalized):
        raise ValueError("dataset names and hashes must be non-empty")
    return _sha256(
        {"bundle_version": bundle_version, "datasets": normalized}
    )


def acquisition_manifest_hash(manifest: Mapping[str, Any]) -> str:
    return _sha256(manifest)


def acquisition_run_id(manifest: Mapping[str, Any]) -> str:
    return f"acq_{acquisition_manifest_hash(manifest)[:24]}"


def computational_experiment_id(
    *,
    content_dependency_hashes: Mapping[str, str],
    experiment_scope: str,
    sample_start_utc_ms: int,
    sample_end_utc_ms: int,
    strategy_version: str,
    execution_version: str,
    cost_version: str,
    code_commit: str,
    dependency_lock_version: str,
) -> str:
    if not experiment_scope:
        raise ValueError("experiment_scope must be non-empty")
    if not content_dependency_hashes:
        raise ValueError("content_dependency_hashes must be non-empty")
    normalized_dependencies: dict[str, str] = {}
    for dependency, content_hash in sorted(content_dependency_hashes.items()):
        name, separator, version = str(dependency).partition("@")
        if not separator or not name or not version:
            raise ValueError(
                "content dependency names must be explicitly versioned with '@'"
            )
        if not content_hash:
            raise ValueError("content dependency hashes must be non-empty")
        normalized_dependencies[str(dependency)] = str(content_hash)
    if experiment_scope == "candidate" and any(
        not dependency.startswith("strategy_data@")
        for dependency in normalized_dependencies
    ):
        raise ValueError(
            "Candidate experiments may depend only on versioned strategy_data content"
        )
    payload = {
        "code_commit": code_commit,
        "content_dependency_hashes": normalized_dependencies,
        "cost_version": cost_version,
        "dependency_lock_version": dependency_lock_version,
        "execution_version": execution_version,
        "experiment_scope": experiment_scope,
        "sample_end_utc_ms": sample_end_utc_ms,
        "sample_start_utc_ms": sample_start_utc_ms,
        "strategy_version": strategy_version,
    }
    return f"exp_{_sha256(payload)[:24]}"
