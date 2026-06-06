"""Cancel registry — track in-flight CancelTokens by run_id.

Mirrors the PyQt ``on_stop_clicked`` flow: the GUI keeps a reference to the
active ``CancelToken`` on ``self._cancel_token`` and calls ``.set()`` when the
user clicks Stop. In the Web UI we cannot rely on instance state because the
HTTP request that submits an analysis is separate from the request that
cancels it, so we keep a small thread-safe registry keyed by a server-issued
``run_id``.

The registry is intentionally storage-agnostic and process-local — analyses
run inside the same Uvicorn worker that owns the FastAPI app, so a single
in-process dict is sufficient. If the worker exits the run dies with it.
"""
from __future__ import annotations

import secrets
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pa_agent.util.threading import CancelToken


class CancelRegistry:
    """Thread-safe map of ``run_id`` -> :class:`CancelToken`.

    The registry is shared between the analysis service (which inserts a
    token at the start of a submission and removes it when the run ends)
    and the cancel router (which looks the token up and calls ``set()``).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, "CancelToken"] = {}

    @staticmethod
    def new_run_id() -> str:
        """Return a short, URL-safe identifier for a fresh run."""
        return secrets.token_urlsafe(12)

    def register(self, run_id: str, token: "CancelToken") -> None:
        """Associate ``token`` with ``run_id``. Overwrites any prior token."""
        with self._lock:
            self._tokens[run_id] = token

    def unregister(self, run_id: str) -> None:
        """Forget ``run_id``; safe to call even if it was never registered."""
        with self._lock:
            self._tokens.pop(run_id, None)

    def cancel(self, run_id: str) -> bool:
        """Signal cancellation. Returns True if a matching token was found."""
        with self._lock:
            token = self._tokens.get(run_id)
        if token is None:
            return False
        token.set()
        return True

    def has(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._tokens

    def active_run_ids(self) -> list[str]:
        with self._lock:
            return list(self._tokens.keys())


# Module-level singleton — used when the FastAPI app doesn't supply its own
# registry on ``app.state``. Tests and ad-hoc embeddings can override by
# attaching a fresh ``CancelRegistry`` to the app state.
_DEFAULT_REGISTRY = CancelRegistry()


def default_registry() -> CancelRegistry:
    """Return the module-level singleton registry."""
    return _DEFAULT_REGISTRY
