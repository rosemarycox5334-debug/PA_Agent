"""Verification for SenseNova free-model fallback (Kilo isFree only).

Locate paths via ``__file__``. Writes one JSON record per run under
``research/sensenova_fallback/runs/`` (no API keys).

Modes:
- Offline checks always run (gate / key isolation / candidate rules).
- With ``PA_FALLBACK_LIVE=1`` (default): Kilo connectivity **smoke** only —
  proves listing + strict JSON chat reply. This is **not** a full Stage1/Stage2
  analysis success. Full pipeline acceptance is owned by
  ``D:\\tools\\data\\pa-fallback-review\\full_pipeline_probe.py``.

Usage::

    D:\\cl\\PA_Agent\\.venv\\Scripts\\python.exe ^
      D:\\cl\\PA_Agent\\research\\sensenova_fallback\\verify_free_fallback.py

    set PA_FALLBACK_LIVE=0   # offline only
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUNS = Path(__file__).resolve().parent / "runs"
RUNS.mkdir(parents=True, exist_ok=True)

_SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|api[_-]?key\s*[:=]\s*['\"][^'\"]+['\"])"
)


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _redact(text: str) -> str:
    """Redact secret-shaped substrings without replace('', '***')."""
    if not text:
        return text
    return _SECRET_RE.sub("***", text)


def _save(name: str, payload: dict[str, Any]) -> Path:
    path = RUNS / f"{_now_stamp()}_{name}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    text = _redact(text)
    path.write_text(text, encoding="utf-8")
    return path


def check_gate() -> dict[str, Any]:
    from pa_agent.ai.sensenova_fallback import (
        is_sensenova_fallback_eligible,
        normalize_base_url,
    )

    cases = {
        "exact": is_sensenova_fallback_eligible("https://token.sensenova.cn/v1"),
        "slash": is_sensenova_fallback_eligible("https://token.sensenova.cn/v1/"),
        "case": is_sensenova_fallback_eligible("HTTPS://TOKEN.SENSENOVA.CN/v1"),
        "deepseek": is_sensenova_fallback_eligible("https://api.deepseek.com"),
        "normalized": normalize_base_url("https://token.sensenova.cn/v1/"),
    }
    ok = (
        cases["exact"]
        and cases["slash"]
        and cases["case"]
        and not cases["deepseek"]
        and cases["normalized"] == "https://token.sensenova.cn/v1"
    )
    return {"ok": ok, "cases": cases}


def check_key_isolation() -> dict[str, Any]:
    from pa_agent.ai.sensenova_fallback import FreeCandidate, call_free_candidate

    secret = "sk-VERIFY-MUST-NOT-LEAK"
    leaks: list[str] = []
    captured: dict[str, Any] = {}

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        blob = json.dumps(headers) + json.dumps(body) + url
        if secret in blob:
            leaks.append(url)
        captured["request"] = {
            "url": url,
            "headers": dict(headers),
            "body": body,
            "timeout": timeout,
        }
        return 200, {
            "id": "verify",
            "model": body.get("model"),
            "choices": [
                {
                    "message": {"role": "assistant", "content": '{"status":"ok","sum":7}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0},
        }

    call_free_candidate(
        FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway"),
        [{"role": "user", "content": "synthetic"}],
        primary_api_key=secret,
        http_post=_post,
    )
    ua = (captured.get("request") or {}).get("headers", {}).get("user-agent", "")
    return {
        "ok": (not leaks) and ua.startswith("PA-Agent/"),
        "leaks": leaks,
        "user_agent": ua,
        "request": captured.get("request"),
    }


def check_candidates_offline() -> dict[str, Any]:
    from pa_agent.ai.sensenova_fallback import (
        FreeCandidate,
        build_candidates,
        mark_cooldown,
        reset_fallback_state_for_tests,
    )

    reset_fallback_state_for_tests()

    def _get(url, *, headers, timeout):  # noqa: ANN001
        return {
            "data": [
                {"id": "paid/model", "isFree": False},
                {"id": "cool-a", "isFree": True},
                {"id": "cool-b", "isFree": True},
                {"id": "cool-c", "isFree": True},
                {"id": "cool-d", "isFree": True},
                {"id": "cool-e", "isFree": True},
                {"id": "cool-f", "isFree": True},
                {"id": "inclusionai/ling-3.1-flash", "isFree": True},
                {"id": "nvidia/nemotron-3.5-lightning:free", "isFree": True},
                {"id": "nvidia/nemotron-3.5-content-safety:free", "isFree": True},
                {"id": "kilo-auto/free", "isFree": True},
            ]
        }

    for mid in ("cool-a", "cool-b", "cool-c", "cool-d", "cool-e", "cool-f"):
        mark_cooldown(FreeCandidate("kilo", mid, "https://api.kilo.ai/api/gateway"))
    cands = build_candidates(http_get=_get, max_candidates=6)
    models = [c.model for c in cands]
    # Of this synthetic pool only the preferred lightning route is usable: the
    # safety-only row is filtered, kilo-auto/ is excluded for ignoring the
    # thinking switch, and ling-3.1-flash no longer exists in the free pool.
    ok = (
        models[:1] == ["nvidia/nemotron-3.5-lightning:free"]
        and "nvidia/nemotron-3.5-content-safety:free" not in models
        and "kilo-auto/free" not in models
        and "inclusionai/ling-3.1-flash" not in models
        and "paid/model" not in models
        and all(not m.startswith("cool-") for m in models)
    )
    return {"ok": ok, "models": models}


def _strict_expected_json(content: str) -> tuple[bool, Any, str]:
    """Require exact JSON object {status:ok,sum:7}; never accept bare 'ok' text."""
    try:
        obj = json.loads(content)
    except Exception as exc:  # noqa: BLE001
        return False, None, f"json_decode: {exc}"
    if not isinstance(obj, dict):
        return False, obj, "not_object"
    if obj.get("status") == "ok" and obj.get("sum") == 7:
        return True, obj, ""
    return False, obj, f"unexpected_object: {obj!r}"


def live_kilo_smoke() -> dict[str, Any]:
    """Connectivity smoke against public Kilo isFree pool (not full analysis)."""
    from pa_agent.ai.sensenova_fallback import (
        FreeCandidate,
        call_free_candidate,
        fetch_kilo_free_models,
        reset_fallback_state_for_tests,
    )

    reset_fallback_state_for_tests()
    t0 = time.monotonic()
    record: dict[str, Any] = {
        "mode": "smoke",
        "note": (
            "Connectivity smoke only — not full PromptAssembler/"
            "JsonValidator/TwoStageOrchestrator analysis success"
        ),
    }
    try:
        free_ids = fetch_kilo_free_models()
    except Exception as exc:  # noqa: BLE001
        record.update(
            {
                "ok": False,
                "step": "list_models",
                "error": str(exc),
                "elapsed_s": time.monotonic() - t0,
            }
        )
        return record

    # Follow the shipped whitelist order instead of a private copy of it, so a
    # smoke always exercises the routes production would actually try.
    from pa_agent.ai.sensenova_fallback import _PREFERRED_KILO

    models = [m for m in _PREFERRED_KILO if m in free_ids]
    models.extend(m for m in free_ids if m not in models and "content-safety" not in m)
    record["free_count"] = len(free_ids)
    record["candidate_order"] = models[:8]
    if not models:
        record.update({"ok": False, "step": "list_models", "error": "no isFree models"})
        return record

    messages = [
        {
            "role": "user",
            "content": (
                "Synthetic connectivity smoke only. "
                'Return ONLY JSON: {"status":"ok","sum":7}'
            ),
        }
    ]
    attempts: list[dict[str, Any]] = []
    for model in models[:5]:
        req_body = {
            "model": model,
            "messages": messages,
            "max_tokens": 256,
            "stream": False,
        }
        try:
            reply = call_free_candidate(
                FreeCandidate("kilo", model, "https://api.kilo.ai/api/gateway"),
                messages,
                primary_api_key="sk-SHOULD-NOT-APPEAR",
                timeout_s=90.0,
                max_tokens=256,
            )
        except Exception as exc:  # noqa: BLE001
            attempts.append(
                {
                    "model": model,
                    "ok": False,
                    "error": str(exc),
                    "request": req_body,
                }
            )
            continue

        content = (reply.content or "").strip()
        parsed_ok, obj, err = _strict_expected_json(content)
        attempt = {
            "model": model,
            "ok": parsed_ok,
            "request": req_body,
            "response": {
                "content": content,
                "reasoning_content": getattr(reply, "reasoning_content", None),
                "raw": getattr(reply, "raw", None),
                "latency_ms": reply.latency_ms,
            },
            "parsed": obj,
            "parse_error": err,
        }
        attempts.append(attempt)
        if parsed_ok:
            record.update(
                {
                    "ok": True,
                    "step": "chat_smoke",
                    "provider": "kilo",
                    "model": model,
                    "elapsed_s": time.monotonic() - t0,
                    "attempts": attempts,
                }
            )
            return record

    record.update(
        {
            "ok": False,
            "step": "chat_smoke",
            "error": "all kilo smoke attempts failed strict JSON check",
            "elapsed_s": time.monotonic() - t0,
            "attempts": attempts,
        }
    )
    return record


def main() -> int:
    live = os.environ.get("PA_FALLBACK_LIVE", "1").strip() not in {"0", "false", "no"}
    report: dict[str, Any] = {
        "root": str(ROOT),
        "script": str(Path(__file__).resolve()),
        "live": live,
        "analysis_success": False,
        "smoke_only": bool(live),
        "checks": {},
    }
    report["checks"]["gate"] = check_gate()
    report["checks"]["key_isolation"] = check_key_isolation()
    report["checks"]["candidates_offline"] = check_candidates_offline()
    if live:
        report["checks"]["kilo_live_smoke"] = live_kilo_smoke()

    offline_ok = all(
        report["checks"][k].get("ok")
        for k in ("gate", "key_isolation", "candidates_offline")
    )
    smoke = report["checks"].get("kilo_live_smoke")
    smoke_ok = True if smoke is None else bool(smoke.get("ok"))
    report["ok"] = offline_ok and smoke_ok
    # Explicitly never claim full analysis success from this script.
    report["analysis_success"] = False
    path = _save("verify", report)
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "analysis_success": False,
                "smoke_only": bool(live),
                "saved": str(path),
                "checks": {k: v.get("ok") for k, v in report["checks"].items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
