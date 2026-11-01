"""End-to-end check: a real PA stage-2 task whose primary 429s must be answered
by the spare model (sensenova-6.8-flash-lite).

It replays the stored stage-2 prompt, forces the primary client to raise a
rate-limit error, and lets the orchestrator's normal fallback path run — so the
new layer is exercised exactly as it runs in production.

Usage (any directory):
    python scripts/verify_lite_spare_channel.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pa_agent.ai.deepseek_client import DeepSeekClient  # noqa: E402
from pa_agent.ai.lite_fallback import LITE_MAX_TOKENS, LITE_MODEL  # noqa: E402
from pa_agent.config.paths import SETTINGS_JSON_PATH  # noqa: E402
from pa_agent.config.settings import load_settings  # noqa: E402
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator  # noqa: E402
from pa_agent.util.threading import CancelToken  # noqa: E402

RECORD = ROOT / "records" / "pending" / "2026-10-30_09-10-01_XAUUSD_5m.json"


def main() -> int:
    settings = load_settings(SETTINGS_JSON_PATH)
    print(f"primary model={settings.provider.model} base_url={settings.provider.base_url}")
    print(f"spare model={LITE_MODEL} max_tokens={LITE_MAX_TOKENS}")

    with open(RECORD, encoding="utf-8") as f:
        data = json.load(f)
    messages = [{"role": m["role"], "content": m["content"]} for m in data["stage2_messages"]]
    chars = sum(len(m["content"]) for m in messages)
    print(f"record={RECORD.name} messages={len(messages)} chars={chars}\n")

    client = DeepSeekClient(settings.provider)

    def _rate_limited(*args, **kwargs):
        raise RuntimeError("429 rate limit (simulated primary) — this is what deepseek-flash does")

    client.stream_chat = _rate_limited  # type: ignore[assignment]

    orch = TwoStageOrchestrator(
        client=client,
        assembler=MagicMock(),
        router=MagicMock(),
        validator=MagicMock(),
        pending_writer=MagicMock(),
        exp_reader=MagicMock(),
        settings=settings,
    )

    chunks = {"reasoning": 0, "content": 0, "reasoning_pieces": 0, "content_pieces": 0}
    first = {"at": 0.0}

    def _mark() -> None:
        if not first["at"]:
            first["at"] = time.monotonic()

    def on_reasoning(chunk: str) -> None:
        _mark()
        chunks["reasoning"] += len(chunk)
        chunks["reasoning_pieces"] += 1

    def on_content(chunk: str) -> None:
        _mark()
        chunks["content"] += len(chunk)
        chunks["content_pieces"] += 1

    progress: list[str] = []

    def on_progress(text: str) -> None:
        progress.append(text)

    t0 = time.monotonic()
    try:
        reply = orch._stream_chat_resilient(
            messages,
            on_reasoning_token=on_reasoning,
            on_content_token=on_content,
            cancel_token=CancelToken(),
            thinking=True,
            reasoning_effort="low",
            stage_label="Stage 2",
            on_progress=on_progress,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 失败（{time.monotonic() - t0:.1f}s）：{type(exc).__name__}: {exc}")
        return 1

    elapsed = time.monotonic() - t0
    usage = reply.usage
    content = reply.content or ""
    print(f"[ok] 用时 {elapsed:.1f}s")
    print(f"[ok] prompt={usage.prompt_tokens} completion={usage.completion_tokens}")
    print(f"[ok] 首片延迟：{(first['at'] - t0):.1f}s" if first["at"] else "[!] 没有收到任何流式分片")
    print(
        f"[ok] 思考 {chunks['reasoning']} 字/{chunks['reasoning_pieces']} 片，"
        f"正文 {chunks['content']} 字/{chunks['content_pieces']} 片"
    )
    print(f"[ok] 正文开头：{content[:160].replace(chr(10), ' ')}")
    uniq = sorted(set(line.strip() for line in progress))
    print(f"[ok] 进度行 {len(progress)} 条，去重后：{uniq}")
    try:
        parsed = json.loads(content)
        print(f"[ok] 是合法 JSON，顶层键={sorted(parsed.keys())}")
    except Exception:  # noqa: BLE001
        print("[!] 正文不是纯 JSON（可能是围栏或说明文字）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
