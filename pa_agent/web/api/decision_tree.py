"""Decision tree API — static tree structure + trace highlighting."""
from __future__ import annotations

from fastapi import APIRouter

from pa_agent.ai.decision_tree import load_decision_tree

router = APIRouter(prefix="/api/decision-tree")


@router.get("")
def get_tree():
    """Return the static decision tree structure."""
    return load_decision_tree()
