"""Console must never surface rate-limit / quota details.

The GUI's black console window is user-facing. Two rules:

1. The terminal "gave up" line (限流重试 ... 放弃本轮) is dropped entirely -
   the analysis just fails silently.
2. Transient retry lines (触发限流 ... 重试) get neutral wording instead.

Either way the file handler keeps the raw text for diagnostics.
"""
from __future__ import annotations

import io
import logging

import pytest

from pa_agent.util.logging import (
    ConsoleFormatter,
    DropRateLimitExhaustedFilter,
    MaskingFormatter,
    _LOG_FORMAT,
)


_BANNED = ("429", "限流", "rate_limit", "insufficient_quota", "tpm/rpm",
           "inference exceeds", "暂未完成")

_GIVE_UP = (
    "DeepSeek 限流重试 0 次仍失败，放弃本轮: Error code: 429 - "
    "{'error': {'message': 'inference exceeds tpm/rpm limit', "
    "'type': 'rate_limit_error', 'code': 'insufficient_quota'}}"
)
_RETRYING = "DeepSeek 触发限流 (429)，5.0 秒后重试 (第 1/5 次)..."


def _record(msg, level=logging.ERROR, name="pa_agent"):
    return logging.LogRecord(name, level, __file__, 1, "%s", (msg,), None)


def _console_handler(stream):
    h = logging.StreamHandler(stream)
    h.setLevel(logging.INFO)
    h.setFormatter(ConsoleFormatter(_LOG_FORMAT))
    h.addFilter(DropRateLimitExhaustedFilter())
    return h


# ── Filter: give-up line vanishes ────────────────────────────────────────────

def test_give_up_line_produces_no_console_output():
    stream = io.StringIO()
    _console_handler(stream).handle(_record(_GIVE_UP))
    assert stream.getvalue() == "", f"console got: {stream.getvalue()!r}"


def test_give_up_line_not_blocked_by_formatter_alone():
    """The filter is what drops it; the formatter itself stays usable."""
    rec = _record(_GIVE_UP)
    assert DropRateLimitExhaustedFilter().filter(rec) is False


# ── Filter: retry lines still pass through (with neutral wording) ────────────

def test_retry_line_passes_filter():
    rec = _record(_RETRYING, level=logging.WARNING)
    assert DropRateLimitExhaustedFilter().filter(rec) is True


def test_retry_line_console_wording_is_neutral():
    stream = io.StringIO()
    _console_handler(stream).handle(_record(_RETRYING, level=logging.WARNING))
    out = stream.getvalue()
    assert out, "retry line should still be shown"
    for bad in _BANNED:
        assert bad not in out, f"console leaked {bad!r}: {out}"


# ── Filter: ordinary errors unaffected ───────────────────────────────────────

def test_ordinary_error_passes_filter_and_keeps_message():
    stream = io.StringIO()
    _console_handler(stream).handle(_record("Connection failed: upstream timeout"))
    out = stream.getvalue()
    assert "Connection failed" in out


def test_ordinary_error_still_masks_api_key():
    rec = _record("auth failed for sk-secret-123")
    out = ConsoleFormatter(_LOG_FORMAT, api_key="sk-secret-123").format(rec)
    assert "sk-secret-123" not in out


# ── File handler keeps everything ────────────────────────────────────────────

def test_file_keeps_give_up_line_verbatim():
    out = MaskingFormatter(_LOG_FORMAT).format(_record(_GIVE_UP))
    assert _GIVE_UP in out


def test_file_keeps_retry_line_verbatim():
    out = MaskingFormatter(_LOG_FORMAT).format(_record(_RETRYING))
    assert _RETRYING in out


@pytest.mark.parametrize("msg", [
    _GIVE_UP,
    "API 限流重试 3 次仍失败，放弃本轮: HTTP 429 insufficient_quota",
])
def test_every_give_up_variant_is_dropped(msg):
    stream = io.StringIO()
    _console_handler(stream).handle(_record(msg))
    assert stream.getvalue() == ""
