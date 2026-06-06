"""Records API for demo mode — list and load pending analysis records."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from pa_agent.config.paths import RECORDS_PENDING_DIR

router = APIRouter(prefix="/api/records")


@router.get("")
def list_records():
    """Return list of pending record files with metadata."""
    files = sorted(RECORDS_PENDING_DIR.glob("*.json"), reverse=True)
    result = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            meta = data.get("meta", {})
            result.append(
                {
                    "filename": f.name,
                    "symbol": meta.get("symbol", ""),
                    "timeframe": meta.get("timeframe", ""),
                    "timestamp": meta.get("timestamp_local_iso", ""),
                    "bar_count": meta.get("bar_count", 0),
                }
            )
        except Exception:
            continue
    return result


@router.get("/{filename}")
def load_record(filename: str):
    """Load a single pending record file."""
    path = RECORDS_PENDING_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Record not found")
    return json.loads(path.read_text(encoding="utf-8"))
