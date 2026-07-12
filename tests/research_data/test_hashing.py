from decimal import Decimal

import pytest

from pa_agent.research_data.hashing import (
    acquisition_manifest_hash,
    acquisition_run_id,
    computational_experiment_id,
    dataset_content_hash,
)


def test_content_hash_ignores_record_order_but_not_content():
    a = [
        {"symbol": "BTCUSDT", "time": 2, "close": Decimal("2.0")},
        {"symbol": "BTCUSDT", "time": 1, "close": Decimal("1.0")},
    ]
    b = list(reversed(a))

    assert dataset_content_hash(a, key_fields=("symbol", "time")) == dataset_content_hash(
        b, key_fields=("symbol", "time")
    )
    b[0] = {**b[0], "close": Decimal("9")}
    assert dataset_content_hash(a, key_fields=("symbol", "time")) != dataset_content_hash(
        b, key_fields=("symbol", "time")
    )


def test_acquisition_hash_changes_when_download_process_changes():
    first = {"dataset_content_hash": "same", "downloaded_at_utc_ms": 1, "pages": ["a"]}
    second = {"dataset_content_hash": "same", "downloaded_at_utc_ms": 2, "pages": ["a", "b"]}

    assert acquisition_manifest_hash(first) != acquisition_manifest_hash(second)
    assert acquisition_run_id(first) != acquisition_run_id(second)


def test_computational_id_is_independent_of_acquisition_metadata():
    kwargs = {
        "dataset_content_hash_value": "content",
        "sample_start_utc_ms": 1,
        "sample_end_utc_ms": 2,
        "strategy_version": "strategy-v1",
        "execution_version": "execution-v1",
        "cost_version": "cost-v1",
        "code_commit": "abc",
        "dependency_lock_version": "lock",
    }

    result = computational_experiment_id(**kwargs)

    assert result.startswith("exp_")
    with pytest.raises(TypeError):
        computational_experiment_id(**kwargs, acquisition_manifest_hash="forbidden")
