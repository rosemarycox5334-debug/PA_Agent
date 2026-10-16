"""Unit tests for the atomic text-write helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from pa_agent.util.atomic_write import atomic_write_bytes, atomic_write_text


@pytest.mark.unit
def test_atomic_write_creates_file_and_parents(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "file.json"
    atomic_write_text(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'


@pytest.mark.unit
def test_atomic_write_overwrites_and_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    atomic_write_text(target, "first")
    atomic_write_text(target, "second")

    assert target.read_text(encoding="utf-8") == "second"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "settings.json"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


@pytest.mark.unit
def test_atomic_write_keeps_original_when_serialisation_fails(tmp_path: Path) -> None:
    """A failure before ``os.replace`` must not damage the existing file."""
    target = tmp_path / "records.json"
    atomic_write_text(target, "original")

    class Boom(RuntimeError):
        pass

    def _explode(_: str) -> str:
        raise Boom("bad payload")

    with pytest.raises(Boom):
        atomic_write_text(target, _explode("x"))

    assert target.read_text(encoding="utf-8") == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["records.json"]


@pytest.mark.unit
def test_atomic_write_bytes_and_bom_encoding(tmp_path: Path) -> None:
    target = tmp_path / "trade.csv"
    atomic_write_bytes(target, b"\x00\x01\x02")
    assert target.read_bytes() == b"\x00\x01\x02"

    # Trade CSV uses utf-8-sig (BOM) — the helper must honour the encoding.
    csv_path = tmp_path / "trades.csv"
    atomic_write_text(csv_path, "a,b\n1,2\n", encoding="utf-8-sig")
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
