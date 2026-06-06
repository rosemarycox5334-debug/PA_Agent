"""Debug API — expose AI turn data (system prompt, user prompt, raw response, validation)."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/debug")

# In-memory store for the current session's turns
# Each turn: {label, system_prompt, user_prompt, raw_response, validation_info}
_turns: list[dict] = []


def add_turn(turn: dict) -> None:
    """Append a debug turn to the in-memory store."""
    _turns.append(turn)


@router.get("/turns")
def get_turns():
    """Return all debug turns for the current session."""
    return {"turns": _turns}


@router.post("/turns/clear")
def clear_turns():
    """Clear all debug turns."""
    _turns.clear()
    return {"status": "ok"}
