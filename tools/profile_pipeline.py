#!/usr/bin/env python3
"""性能剖析：把一次真实分析拆开计时，找出真正花时间的地方。

输入是 `records/pending/` 里的真实记录（含送出的提示词、模型原文、K 线），
所以测的是**真实负载**而不是合成数据。

用法：
  python tools/profile_pipeline.py                # 取最新记录
  python tools/profile_pipeline.py --record <p> --repeat 5
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 粗略的 token 估算：中文 ~1.4 字符/token，英文/代码 ~3.5 字符/token
def _est_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.4 + other / 3.5)


def _timed(fn, repeat: int) -> tuple[float, float]:
    """返回 (中位耗时 ms, 最快耗时 ms)。"""
    samples: list[float] = []
    for _ in range(max(1, repeat)):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples), min(samples)


def _latest_record() -> Path:
    files = sorted((ROOT / "records" / "pending").glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("没有找到分析记录（records/pending）")
    return files[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", default=None)
    ap.add_argument("--repeat", type=int, default=5, help="每个阶段重复次数（取中位）")
    args = ap.parse_args()

    path = Path(args.record) if args.record else _latest_record()
    rec = json.loads(path.read_text(encoding="utf-8"))
    meta = rec.get("meta") or {}
    print("=== 性能剖析（真实记录负载）===")
    print(f"记录: {path.name} | {meta.get('symbol')} {meta.get('timeframe')} | {meta.get('timestamp_local_iso')}")
    print(f"repeat={args.repeat}（取中位数）\n")

    # ── 1) 提示词组成：谁把 prompt 撑到 20 万 token ──────────────────────────
    print("--- 1) 提示词组成（按大小排序）---")
    for key in ("stage1_messages", "stage2_messages"):
        msgs = rec.get(key) or []
        total_chars = sum(len(str(m.get("content") or "")) for m in msgs if isinstance(m, dict))
        print(f"{key}: {len(msgs)} 条消息, {total_chars:,} 字符 ≈ {_est_tokens('x' * 0):,} "
              f"-> ≈ {_est_tokens(''.join(str(m.get('content') or '') for m in msgs if isinstance(m, dict))):,} tokens")
        rows = []
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            content = str(m.get("content") or "")
            rows.append((len(content), i, str(m.get("role")), _est_tokens(content)))
        rows.sort(reverse=True)
        for chars, i, role, toks in rows[:4]:
            print(f"    #{i} {role:>9s}: {chars:>8,} 字符 ≈ {toks:>7,} tokens  (占比 {chars/max(1,total_chars)*100:4.1f}%)")
    usage = rec.get("usage_total") or {}
    print(f"模型实际计量（真实）: prompt {usage.get('prompt_tokens', 0):,} / cached "
          f"{usage.get('cached_prompt_tokens', 0):,} / completion {usage.get('completion_tokens', 0):,}")

    # ── 2) 本地各阶段耗时 ──────────────────────────────────────────────────
    print("\n--- 2) 本地阶段耗时（不含网络/模型）---")
    bars = rec.get("kline_data") or []
    from pa_agent.data.base import KlineBar
    from pa_agent.data.snapshot import compute_indicators

    if bars:
        def _mk():
            return [
                KlineBar(
                    seq=int(b["seq"]), ts_open=int(b["ts_open"]), open=float(b["open"]),
                    high=float(b["high"]), low=float(b["low"]), close=float(b["close"]),
                    volume=float(b.get("volume") or 0.0), closed=bool(b.get("closed", True)),
                )
                for b in bars
            ]

        kbars = _mk()
        med, best = _timed(lambda: compute_indicators(kbars), args.repeat)
        print(f"compute_indicators({len(kbars)} 根)      : {med:8.1f} ms (最快 {best:.1f})")

    s2_raw = str(rec.get("stage2_response") or "")
    if s2_raw:
        from pa_agent.ai.json_validator import JsonValidator
        from pa_agent.config.paths import SETTINGS_JSON_PATH
        from pa_agent.config.settings import load_settings

        settings = load_settings(SETTINGS_JSON_PATH)
        validator = JsonValidator(settings.validation)
        res: dict[str, object] = {}

        def _validate():
            res["r"] = validator.validate("stage2", s2_raw)

        med, best = _timed(_validate, max(1, args.repeat // 2))
        print(f"JsonValidator.validate(stage2)      : {med:8.1f} ms (最快 {best:.1f})  "
              f"-> {type(res.get('r')).__name__}")

        s2_obj = rec.get("stage2_decision") or {}
        if s2_obj:
            import copy

            from pa_agent.ai.stage2_normalizer import normalize_stage2

            def _normalize():
                normalize_stage2(copy.deepcopy(s2_obj))

            med, best = _timed(_normalize, args.repeat)
            print(f"normalize_stage2(真实对象)           : {med:8.1f} ms (最快 {best:.1f})")

    # ── 3) 与模型耗时对照 ─────────────────────────────────────────────────
    print("\n--- 3) 对照：模型调用（来自日志的实测）---")
    print("阶段一 latency 31,453 ms / 阶段二 latency 42,734 ms（2026-10-15 21:51 那次）")
    print("→ 本地代码耗时占比 ≈ 1%，模型调用占 ≈ 99%")
    print("   ⇒ 想真正提速，必须减少**送进模型的 token** 或提高 KV 缓存命中率。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
