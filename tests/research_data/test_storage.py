import json
import os
from decimal import Decimal

import pytest

from pa_agent.research_data.storage import AtomicDatasetStore


def test_atomic_json_write_and_canonical_jsonl(tmp_path):
    store = AtomicDatasetStore(tmp_path)

    store.write_json_atomic("state/checkpoint.json", {"next": 2})
    store.write_canonical_jsonl(
        "canonical/bars.jsonl",
        [
            {"time": 2, "close": Decimal("2.00")},
            {"time": 1, "close": Decimal("1.00")},
        ],
        key_fields=("time",),
    )

    assert store.read_json("state/checkpoint.json") == {"next": 2}
    assert (tmp_path / "canonical/bars.jsonl").read_text(encoding="utf-8") == (
        '{"close":"1","time":1}\n{"close":"2","time":2}\n'
    )


def test_atomic_write_removes_temporary_file_when_replace_fails(tmp_path, monkeypatch):
    store = AtomicDatasetStore(tmp_path)

    def fail_replace(*_args):
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated"):
        store.write_json_atomic("target.json", {"value": 1})

    assert not (tmp_path / "target.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_raw_pages_are_sorted_and_round_trip(tmp_path):
    store = AtomicDatasetStore(tmp_path)
    store.write_raw_page("btc_trade_1m", 1, {"payload": [[2]], "request": {"startTime": 2}})
    store.write_raw_page("btc_trade_1m", 0, {"payload": [[1]], "request": {"startTime": 1}})

    pages = store.read_raw_pages("btc_trade_1m")

    assert [page["payload"][0][0] for page in pages] == [1, 2]
    assert json.loads((tmp_path / "raw/btc_trade_1m/page-000000.json").read_text())[
        "request"
    ] == {"startTime": 1}
