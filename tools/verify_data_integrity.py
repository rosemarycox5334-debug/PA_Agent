#!/usr/bin/env python3
"""行情对账：把「最近一次分析记录」里的 K 线与交易所原始数据逐根比对，并检查新鲜度。

回答两个问题：
  1. 喂给模型的行情是否**准确**（逐根 O/H/L/C/V 与交易所原始数据一致）
  2. 是否**实时**（最新一根是否已收盘、快照距最新收盘多久、跨交易所是否一致）

用法：
  python tools/verify_data_integrity.py                     # 自动取 records/pending 最新记录
  python tools/verify_data_integrity.py --record <path>
  python tools/verify_data_integrity.py --venue binance
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pa_agent.data.base import KlineBar
from pa_agent.data.factory import create_data_source

DEFAULT_RECORD_DIR = ROOT / "records" / "pending"
_PRICE_TOL = 1e-6  # 相对容差：交易所数据应完全一致，这里只用于浮点比较


def _latest_record() -> Path:
    files = sorted(DEFAULT_RECORD_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"没有找到分析记录：{DEFAULT_RECORD_DIR}")
    return files[-1]


def _fetch(kind: str, symbol: str, timeframe: str, count: int, end_ms: int | None) -> list[KlineBar]:
    src = create_data_source(kind)
    src.connect()
    try:
        src.subscribe(symbol, timeframe)
        bars = (
            list(src.historical_snapshot(count, end_ms))
            if end_ms is not None
            else list(src.latest_snapshot(count))
        )
        src.unsubscribe()
    finally:
        src.disconnect()
    return bars


def _http_json(url: str) -> dict | list:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "PA-Agent/verify"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def cross_venue_check(symbol: str = "BTCUSDT") -> tuple[float, float, float]:
    """币安 vs 火币最新成交价（%偏离）。返回 (币安价, 火币价, 偏离%)。"""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    bn = float(
        _http_json(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}")["price"]
    )
    hb = float(
        _http_json(f"https://api.huobi.pro/market/detail/merged?symbol={base.lower()}usdt")["tick"][
            "close"
        ]
    )
    dev = abs(bn - hb) / ((bn + hb) / 2) * 100
    return bn, hb, dev


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", default=None, help="分析记录 JSON 路径（默认取最新）")
    ap.add_argument("--venue", default="binance", help="数据源（默认 binance）")
    args = ap.parse_args()

    path = Path(args.record) if args.record else _latest_record()
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.get("meta") or {}
    bars = data.get("kline_data") or []
    symbol = str(meta.get("symbol") or "")
    timeframe = str(meta.get("timeframe") or "")
    captured_ms = int(meta.get("timestamp_local_ms") or 0)

    print("=== 行情对账 ===")
    print(f"记录文件   : {path.name}")
    print(f"标的/周期  : {symbol} {timeframe} | 记录里的 K 线 {len(bars)} 根 | 抓取时刻 {meta.get('timestamp_local_iso')}")

    # ── 1) 逐根比对 ────────────────────────────────────────────────────────
    end_ms = max(int(b["ts_open"]) for b in bars) + 1
    fetched = _fetch(args.venue, symbol, timeframe, len(bars) + 5, end_ms)
    by_ts = {int(b.ts_open): b for b in fetched}
    mismatched: list[tuple[int, str, float, float]] = []
    missing = 0
    for b in bars:
        ts = int(b["ts_open"])
        ex = by_ts.get(ts)
        if ex is None:
            missing += 1
            continue
        for field in ("open", "high", "low", "close", "volume"):
            got, want = float(b[field]), float(getattr(ex, field))
            if abs(got - want) > max(_PRICE_TOL, abs(want) * _PRICE_TOL):
                mismatched.append((ts, field, got, want))

    print("\n--- 1) 与交易所原始数据逐根比对 ---")
    print(f"比对 {len(bars)} 根 | 交易所侧缺失 {missing} 根 | 字段不一致 {len(mismatched)} 处")
    if mismatched:
        for ts, field, got, want in mismatched[:5]:
            print(f"   ✗ ts={ts} {field}: 记录 {got} vs 交易所 {want}")
    verdict_bars = "PASS" if (missing == 0 and not mismatched) else "FAIL"
    print(f"   结论: {verdict_bars}")

    # ── 2) 新鲜度 / 是否只用已收盘 bar ─────────────────────────────────────
    print("\n--- 2) 新鲜度 ---")
    closed = [b for b in bars if b.get("closed")]
    newest_ts = max(int(b["ts_open"]) for b in bars)
    tf_ms = _tf_ms(timeframe)
    newest_age_s = (captured_ms - newest_ts) / 1000 if captured_ms else float("nan")
    all_closed = len(closed) == len(bars)
    # 「最新一根已收盘 bar」按定义比抓取时刻早 1~2 个周期：抓取时正在走的 bar
    # 尚未收盘，所以它的前一根才是最新已收盘。用这个口径判定新鲜度，
    # 而不是要求 age < 1 个周期（那样会必然误报）。
    max_expected_age_s = 2 * tf_ms / 1000
    print(f"全部已收盘   : {all_closed}（{len(closed)}/{len(bars)}）")
    print(f"最新一根开盘 : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(newest_ts/1000))}"
          f"（距抓取 {newest_age_s/60:.1f} 分钟；周期 {tf_ms/60000:.0f} 分钟 → 允许 ≤ {max_expected_age_s/60:.0f} 分钟）")

    # 精确口径：记录里最新已收盘 bar 应当正好是「抓取时刻正在走的 bar」的前一根
    forming_open = (captured_ms // tf_ms) * tf_ms if captured_ms else None
    exact_ok = forming_open is not None and newest_ts == forming_open - tf_ms
    if forming_open is not None:
        print(f"抓取时正在走的 bar 开盘 : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(forming_open/1000))}"
              f" → 期望最新已收盘 = 其前一根: {exact_ok}")
    now_newest = max(int(b.ts_open) for b in _fetch(args.venue, symbol, timeframe, 3, None))
    print(f"交易所现在最新一根开盘 : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now_newest/1000))}")
    stale_s = (int(time.time() * 1000) - now_newest) / 1000
    print(f"交易所侧当前 bar 已走 : {stale_s:.0f}s（< 一个周期 = 正常）")
    verdict_fresh = (
        "PASS"
        if all_closed and not newest_age_s > max_expected_age_s and (exact_ok or captured_ms == 0)
        else "FAIL"
    )
    print(f"   结论: {verdict_fresh}")

    # ── 3) 跨交易所一致性（当下）────────────────────────────────────────────
    print("\n--- 3) 跨交易所一致性（当下快照）---")
    try:
        bn, hb, dev = cross_venue_check(symbol)
        print(f"币安 {bn:,.2f} / 火币 {hb:,.2f} -> 偏离 {dev:.4f}%")
        verdict_dev = "PASS" if dev < 0.1 else "FAIL"
        print(f"   结论: {verdict_dev}")
    except Exception as exc:  # noqa: BLE001
        print(f"   跨所检查失败: {type(exc).__name__}: {exc}")
        verdict_dev = "SKIP"

    print("\n=== 汇总 ===")
    print(f"行情准确性 {verdict_bars} | 新鲜度 {verdict_fresh} | 跨所一致 {verdict_dev}")
    return 0 if "FAIL" not in (verdict_bars, verdict_fresh) else 1


def _tf_ms(tf: str) -> int:
    unit = tf[-1]
    n = int(tf[:-1] or 1)
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}[unit]


if __name__ == "__main__":
    raise SystemExit(main())
