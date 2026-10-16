"""Manual smoke check for the Binance / OKX data sources (network required).

Usage::

    python tools/smoke_crypto_sources.py                      # both venues, BTCUSDT 15m
    python tools/smoke_crypto_sources.py --venue binance --symbol BTCUSDT.perp --bars 300
    python tools/smoke_crypto_sources.py --venue okx --timeframe 1h

Exit code 0 = every requested venue returned sane bars. Behind a firewall set
``HTTPS_PROXY`` (httpx honours it).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pa_agent.data.base import DataSourceError, DataSourceTransientError
from pa_agent.data.binance_source import BinanceSource
from pa_agent.data.okx_source import OkxSource

DEFAULTS = {"binance": "BTCUSDT", "okx": "BTC-USDT"}


def _make_source(venue: str):
    return BinanceSource() if venue == "binance" else OkxSource()


def _check(venue: str, symbol: str, timeframe: str, bars_wanted: int) -> bool:
    source = _make_source(venue)
    print(
        f"[{venue}] connecting … (HTTPS_PROXY={'set' if os.environ.get('HTTPS_PROXY') else 'unset'})"
    )
    try:
        source.connect()
    except DataSourceTransientError as exc:
        print(f"[{venue}] FAIL connect: {exc}")
        return False
    if hasattr(source, "transport_name"):
        print(f"[{venue}] transport={source.transport_name} host={source._client.preferred_host}")

    source.subscribe(symbol, timeframe)
    try:
        bars = source.latest_snapshot(bars_wanted)
    except (DataSourceTransientError, DataSourceError) as exc:
        print(f"[{venue}] FAIL fetch: {type(exc).__name__}: {exc}")
        return False

    stamps = [b.ts_open for b in bars]
    ok_ordered = stamps == sorted(stamps, reverse=True)
    ok_ohlc = all(b.high >= b.low and b.low <= b.close <= b.high for b in bars)
    ok_positive = all(b.close > 0 for b in bars)
    forming = [b for b in bars if not b.closed]

    print(
        f"[{venue}] {symbol} {timeframe}: {len(bars)} bars "
        f"| newest={bars[0].ts_open} oldest={bars[-1].ts_open} "
        f"| head_close={bars[0].close} head_seq={bars[0].seq} "
        f"| forming={len(forming)} | volume={bars[0].volume} amount={bars[0].amount}"
    )
    print(f"[{venue}] ordered={ok_ordered} ohlc_ok={ok_ohlc} prices_positive={ok_positive}")
    sanity = ok_ordered and ok_ohlc and ok_positive and len(bars) >= min(10, bars_wanted)
    print(f"[{venue}] {'OK' if sanity else 'SUSPECT'}")
    return sanity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test crypto data sources")
    parser.add_argument("--venue", choices=["binance", "okx", "both"], default="both")
    parser.add_argument("--symbol", default="", help="defaults per venue (BTCUSDT / BTC-USDT)")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--bars", type=int, default=120)
    args = parser.parse_args(argv)

    venues = ["binance", "okx"] if args.venue == "both" else [args.venue]
    results = {}
    for venue in venues:
        symbol = args.symbol or DEFAULTS[venue]
        results[venue] = _check(venue, symbol, args.timeframe, args.bars)

    print("\n=== summary ===")
    for venue, ok in results.items():
        print(f"  {venue:8s} {'OK' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
