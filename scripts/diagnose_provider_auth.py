"""Diagnose the primary provider's credentials with one minimal chat request.

Reads config/settings.json, sends a 1-token ping, prints status/body only.
Never prints the API key.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def ping(provider: dict, label: str) -> None:
    key = str(provider.get("api_key") or "")
    base = str(provider.get("base_url") or "").rstrip("/")
    model = str(provider.get("model") or "")
    print(f"[{label}] base={base} model={model} key_len={len(key)}")
    if not key:
        print("  -> 没有配置密钥")
        return
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("  -> HTTP", r.status, r.read()[:200].decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        print("  -> HTTP", exc.code, exc.read()[:400].decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        print("  -> 请求异常", type(exc).__name__, exc)


def main() -> None:
    settings = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8"))
    provider = settings["provider"]
    ping(provider, "主通道")
    for i, fb in enumerate(provider.get("fallbacks") or [], 1):
        ping(fb, f"备用{i}")


if __name__ == "__main__":
    main()
