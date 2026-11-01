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
    DropSpareChannelDiagnosticsFilter,
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
    h.addFilter(DropSpareChannelDiagnosticsFilter())
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


# ── The SenseNova "tpm exhausted / code 8" flavour ───────────────────────────

_STREAM_ERROR = (
    "DeepSeekClient stream error after 805 ms: Error code: 429 - "
    "{'error': {'message': 'tpm exhausted', 'type': 'quota_exceeded_error', 'code': '8'}}"
)


def test_stream_error_console_wording_is_neutral():
    """The transient stream error must reach the console without any detail."""
    stream = io.StringIO()
    _console_handler(stream).handle(_record(_STREAM_ERROR))
    out = stream.getvalue()
    assert out, "a neutral progress line is expected"
    for bad in ("429", "tpm", "quota", "code", "stream error", "DeepSeekClient"):
        assert bad.lower() not in out.lower(), f"console leaked {bad!r}: {out}"


def test_stream_error_console_level_is_info_not_error():
    """The user asked for no ERROR lines while the fallback still answers."""
    stream = io.StringIO()
    _console_handler(stream).handle(_record(_STREAM_ERROR))
    out = stream.getvalue()
    assert "ERROR" not in out, f"console showed an ERROR level: {out}"
    assert "INFO" in out


def test_stream_error_still_kept_verbatim_in_the_file():
    out = MaskingFormatter(_LOG_FORMAT).format(_record(_STREAM_ERROR))
    assert _STREAM_ERROR in out


# ── Spare-channel internals never reach the console ──────────────────────────

_LITE_ANSWERED = (
    "lite spare channel answered: model=sensenova-6.8-flash-lite "
    "prompt=78636 completion=4837 84668ms"
)


def test_spare_channel_diagnostics_are_dropped_from_console():
    stream = io.StringIO()
    handler = _console_handler(stream)
    handler.handle(
        logging.LogRecord(
            "pa_agent.ai.lite_fallback", logging.WARNING, __file__, 1, "%s", (_LITE_ANSWERED,), None
        )
    )
    assert stream.getvalue() == "", f"console got: {stream.getvalue()!r}"


def test_spare_channel_diagnostics_are_matched_by_message_too():
    stream = io.StringIO()
    handler = _console_handler(stream)
    handler.handle(_record("lite spare channel failed: timeout", level=logging.WARNING, name="pa_agent.other"))
    assert stream.getvalue() == ""


def test_spare_channel_diagnostics_stay_in_the_file():
    out = MaskingFormatter(_LOG_FORMAT).format(
        logging.LogRecord(
            "pa_agent.ai.lite_fallback", logging.WARNING, __file__, 1, "%s", (_LITE_ANSWERED,), None
        )
    )
    assert _LITE_ANSWERED in out


def test_ordinary_warning_still_reaches_the_console():
    stream = io.StringIO()
    _console_handler(stream).handle(_record("disk almost full", level=logging.WARNING, name="pa_agent.io"))
    assert "disk almost full" in stream.getvalue()
