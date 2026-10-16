"""Atomic text writes (temp file in the target directory + ``os.replace``).

Every persistence path in this project used a bare ``Path.write_text``, so a
crash or power loss mid-write left a truncated ``settings.json`` / analysis
record / trade CSV behind (see the 2026-10 code review). ``os.replace`` is
atomic on Windows and POSIX for same-volume renames, so readers see either the
old file or the complete new one — never a half-written one.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> None:
    """Write *text* to *path* atomically.

    The temporary file is created in the destination directory so the final
    ``os.replace`` is a same-volume rename. On failure the temp file is removed
    and the original file is left untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Byte-oriented counterpart of :func:`atomic_write_text`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
