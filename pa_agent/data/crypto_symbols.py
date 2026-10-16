"""Symbol / instrument normalisation shared by the Binance and OKX sources.

Both venues speak a *pair* language (``BASE/QUOTE``) but spell it differently:

===========  ======================  ===============================
venue        spot                    perpetual swap
===========  ======================  ===============================
Binance      ``BTCUSDT`` (concatenated, ``.perp`` suffix for USDⓈ-M)
OKX          ``BTC-USDT``            ``BTC-USDT-SWAP`` (``-SWAP`` suffix)
===========  ======================  ===============================

Users type all of the above forms (``BTC/USDT``, ``btcusdt``, ``BTC-USDT``,
``ETHUSDT.perp`` …), so the sources normalise through this module instead of
each inventing its own rules. Pure functions, no I/O — easy to unit test.
"""

from __future__ import annotations

import re

# Quote assets we are willing to split on, longest first so that ``USDT`` wins
# over ``USD`` for inputs like ``BTCUSDT``.
QUOTE_ASSETS: tuple[str, ...] = (
    "USDT",
    "USDC",
    "TUSD",
    "BUSD",
    "FDUSD",
    "USD",
    "BTC",
    "ETH",
    "BNB",
    "EUR",
    "TRY",
)

_PAIR_RE = re.compile(r"^[A-Z0-9]{2,20}$")
_OKX_SWAP_SUFFIX = "-SWAP"
_BINANCE_PERP_SUFFIX = ".PERP"


def split_pair(symbol: str) -> tuple[str, str] | None:
    """Split ``BTCUSDT`` / ``BTC-USDT`` / ``BTC/USDT`` into ``("BTC", "USDT")``.

    Returns ``None`` when no known quote asset can be identified.
    """
    raw = (symbol or "").strip().upper()
    if not raw:
        return None
    for sep in ("/", "-", "_", ":"):
        if sep in raw:
            base, _, quote = raw.partition(sep)
            if base and quote:
                return base, quote
            return None
    for quote in QUOTE_ASSETS:
        if raw.endswith(quote) and len(raw) > len(quote):
            return raw[: -len(quote)], quote
    return None


# Bases that are metals / FX / index tickers rather than crypto assets. Without
# this guard ``XAUUSDm`` (MT5 gold) would look like a crypto pair because it ends
# with ``USD``, so switching MT5 → Binance would keep a gold ticker.
_NON_CRYPTO_BASES: frozenset[str] = frozenset(
    {
        "XAU",
        "XAG",
        "XPT",
        "XPD",
        "WTI",
        "BRENT",
        "USOIL",
        "UKOIL",
        "NG",
        "DXY",
        "EUR",
        "GBP",
        "JPY",
        "AUD",
        "NZD",
        "CAD",
        "CHF",
        "CNH",
        "GC",
        "SI",
        "HG",
        "CL",
        "ES",
        "NQ",
        "YM",
        "RTY",
    }
)


def looks_like_crypto_pair(symbol: str) -> bool:
    """True when *symbol* plausibly names a crypto pair (any venue spelling)."""
    raw = (symbol or "").strip().upper()
    if not raw:
        return False
    body = raw.removesuffix(_BINANCE_PERP_SUFFIX).removesuffix(_OKX_SWAP_SUFFIX)
    if not body:
        return False
    pair = split_pair(body)
    if pair is None:
        return False
    if pair[0] in _NON_CRYPTO_BASES:
        return False
    return bool(_PAIR_RE.match(body.replace("/", "").replace("-", "").replace("_", "")))


# ── Binance ───────────────────────────────────────────────────────────────────


def normalize_binance_symbol(symbol: str) -> str:
    """Return the Binance API symbol, e.g. ``BTCUSDT`` or ``BTCUSDT.PERP``.

    Accepts ``btc/usdt``, ``BTC-USDT``, ``BTCUSDT.perp`` … and preserves the
    ``.perp`` marker that selects the USDⓈ-M futures endpoint.
    """
    raw = (symbol or "").strip().upper()
    if not raw:
        return ""
    perp = raw.endswith((_BINANCE_PERP_SUFFIX, "-PERP"))
    body = raw.removesuffix(_BINANCE_PERP_SUFFIX).removesuffix("-PERP")
    pair = split_pair(body)
    if pair is None:
        return ""
    base, quote = pair
    return f"{base}{quote}{_BINANCE_PERP_SUFFIX if perp else ''}"


def binance_symbol_is_perp(symbol: str) -> bool:
    return (symbol or "").strip().upper().endswith(_BINANCE_PERP_SUFFIX)


def strip_binance_perp_suffix(symbol: str) -> str:
    return (symbol or "").strip().upper().removesuffix(_BINANCE_PERP_SUFFIX)


# ── OKX ───────────────────────────────────────────────────────────────────────


def normalize_okx_inst_id(symbol: str) -> str:
    """Return an OKX ``instId``: ``BTC-USDT`` (spot) or ``BTC-USDT-SWAP``.

    Accepts concatenated (``BTCUSDT``), slashed (``BTC/USDT``) and already
    canonical (``BTC-USDT-SWAP``) input.
    """
    raw = (symbol or "").strip().upper()
    if not raw:
        return ""
    swap = raw.endswith((_OKX_SWAP_SUFFIX, _BINANCE_PERP_SUFFIX, "-PERP"))
    body = (
        raw.removesuffix(_OKX_SWAP_SUFFIX).removesuffix(_BINANCE_PERP_SUFFIX).removesuffix("-PERP")
    )
    pair = split_pair(body)
    if pair is None:
        return ""
    base, quote = pair
    inst = f"{base}-{quote}"
    return f"{inst}{_OKX_SWAP_SUFFIX}" if swap else inst


def okx_inst_is_swap(inst_id: str) -> bool:
    return (inst_id or "").strip().upper().endswith(_OKX_SWAP_SUFFIX)


def okx_inst_to_display(inst_id: str) -> str:
    """``BTC-USDT-SWAP`` → ``BTC/USDT 永续`` for the UI status bar."""
    raw = (inst_id or "").strip().upper()
    if not raw:
        return ""
    if raw.endswith(_OKX_SWAP_SUFFIX):
        return raw.removesuffix(_OKX_SWAP_SUFFIX).replace("-", "/") + " 永续"
    return raw.replace("-", "/")
