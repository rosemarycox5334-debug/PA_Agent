from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from typing import Any


def canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Canonical Decimal values must be finite")
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical_value(asdict(value))
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        raise TypeError("Binary floats are not allowed in Canonical data")
    raise TypeError(f"Unsupported Canonical type: {type(value).__name__}")


def canonical_dumps(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_records(
    records: Iterable[Mapping[str, Any]], *, key_fields: Sequence[str]
) -> list[Mapping[str, Any]]:
    return sorted(records, key=lambda record: tuple(record[field] for field in key_fields))
