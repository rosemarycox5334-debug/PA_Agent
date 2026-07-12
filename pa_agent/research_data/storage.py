from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pa_agent.research_data.canonical import canonical_dumps, canonical_records


class AtomicDatasetStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, relative: str | Path) -> Path:
        path = (self.root / relative).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise ValueError("Storage path escapes dataset root")
        return path

    def _write_text_atomic(self, relative: str | Path, text: str) -> None:
        target = self._path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def write_json_atomic(self, relative: str | Path, value: Any) -> None:
        self._write_text_atomic(relative, f"{canonical_dumps(value)}\n")

    def read_json(self, relative: str | Path) -> Any:
        return json.loads(self._path(relative).read_text(encoding="utf-8"))

    def read_json_or_none(self, relative: str | Path) -> Any | None:
        path = self._path(relative)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def remove(self, relative: str | Path) -> None:
        self._path(relative).unlink(missing_ok=True)

    def write_canonical_jsonl(
        self,
        relative: str | Path,
        records: Iterable[Mapping[str, Any]],
        *,
        key_fields: Sequence[str],
    ) -> None:
        ordered = canonical_records(records, key_fields=key_fields)
        text = "".join(f"{canonical_dumps(record)}\n" for record in ordered)
        self._write_text_atomic(relative, text)

    def write_raw_page(self, dataset_name: str, page_index: int, value: Any) -> None:
        self.write_json_atomic(
            f"raw/{dataset_name}/page-{page_index:06d}.json",
            value,
        )

    def read_raw_pages(self, dataset_name: str) -> list[dict[str, Any]]:
        folder = self._path(f"raw/{dataset_name}")
        if not folder.exists():
            return []
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("page-*.json"))]

