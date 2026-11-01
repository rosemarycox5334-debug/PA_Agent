"""Compare deepseek-flash vs sensenova-6.8-flash-lite on a REAL PA task.

Replays the exact stage-2 production prompt from a saved record (about 200k
chars / ~107k prompt tokens) so the "429 or not" question is answered by
measurement rather than by the docs.

Two message shapes are tested, because they behave differently:
  as-is      — what PA actually sends (… user, assistant): assistant-tailed
  user-tailed— same content, last turn kept as user

Usage (any directory):
    python scripts/probe_lite_vs_deepseek_429.py --runs 1
    python scripts/probe_lite_vs_deepseek_429.py --max-tokens 16384 --runs 2
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pa_agent.config.paths import SETTINGS_JSON_PATH  # noqa: E402

URL = "https://token.sensenova.cn/v1/chat/completions"
DEEPSEEK = "deepseek-flash"
LITE = "sensenova-6.8-flash-lite"
DEFAULT_RECORD = ROOT / "records" / "pending" / "2026-10-30_09-10-01_XAUUSD_5m.json"


def load_key() -> str:
    with open(SETTINGS_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return str((data.get("provider") or {}).get("api_key") or "").strip()


def load_shapes(record: Path) -> dict[str, list[dict]]:
    with open(record, encoding="utf-8") as f:
        data = json.load(f)
    msgs = [{"role": m["role"], "content": m["content"]} for m in data["stage2_messages"]]
    as_is = msgs
    user_tailed = msgs[:-1] if msgs and msgs[-1]["role"] == "assistant" else msgs
    return {"as-is": as_is, "user-tailed": user_tailed}


def call(key: str, model: str, messages: list[dict], *, max_tokens: int) -> dict:
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False}
    if model == LITE:
        payload["reasoning_effort"] = "low"  # string `thinking` is rejected by this gateway
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return {"status": r.status, "elapsed": time.time() - t0, "body": json.loads(r.read().decode())}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:  # noqa: BLE001
            body = {"raw": raw[:300]}
        return {"status": e.code, "elapsed": time.time() - t0, "body": body}
    except Exception as exc:  # noqa: BLE001
        return {"status": -1, "elapsed": time.time() - t0, "body": {"error": f"{type(exc).__name__}: {exc}"}}


def report(label: str, res: dict) -> None:
    body = res["body"]
    if res["status"] != 200:
        err = body.get("error") if isinstance(body, dict) else body
        code = err.get("code") if isinstance(err, dict) else ""
        msg = err.get("message") if isinstance(err, dict) else str(err)
        print(f"  {label:<34} {res['status']} {res['elapsed']:6.1f}s code={code} {str(msg)[:90]}")
        return
    usage = body.get("usage") or {}
    choice = (body.get("choices") or [{}])[0]
    msg = (choice.get("message") or {})
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning") or ""
    print(
        f"  {label:<34} 200 {res['elapsed']:6.1f}s prompt={usage.get('prompt_tokens')} "
        f"completion={usage.get('completion_tokens')} finish={choice.get('finish_reason')} "
        f"out={len(content)}ch reason={len(reasoning)}ch"
    )
    if content:
        print(f"      {content[:100].replace(chr(10), ' ')}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--record", default=str(DEFAULT_RECORD))
    args = parser.parse_args()

    key = load_key()
    shapes = load_shapes(Path(args.record))
    for name, msgs in shapes.items():
        chars = sum(len(m["content"]) for m in msgs)
        print(f"[{name}] {len(msgs)} 条消息, {chars} 字符")
    print(f"max_tokens={args.max_tokens}  key={'ok' if key else 'MISSING'}\n")

    for run in range(1, args.runs + 1):
        print(f"--- run {run}/{args.runs} ---")
        for name, msgs in shapes.items():
            for model in (DEEPSEEK, LITE):
                report(f"{model} / {name}", call(key, model, msgs, max_tokens=args.max_tokens))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
