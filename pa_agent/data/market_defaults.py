"""Default gold (XAU) and A-share identifiers across data sources."""
from __future__ import annotations

import re

# MT5 broker spot gold (suffix varies by broker; m = common micro/mini suffix)
GOLD_MT5_SYMBOL = "XAUUSDm"

# TradingView spot gold — verified with tvdatafeed (anonymous):
#   OANDA:XAUUSD, PEPPERSTONE:XAUUSD, FOREXCOM:XAUUSD  OK
#   TVC:GOLD, CAPITALCOM:GOLD  OK
#   TVC:XAUUSD  INVALID (0 bars)
GOLD_TV_SYMBOL = "XAUUSD"
GOLD_TV_EXCHANGE = "OANDA"

# AkShare A-share defaults (平安银行 / 日线与 1h 分析常用)
A_SHARE_DEFAULT_SYMBOL = "000001"
A_SHARE_DEFAULT_TIMEFRAME = "1h"

# Exchange → correct gold symbol on TradingView (do not use XAUUSD on TVC)
TV_GOLD_SYMBOL_BY_EXCHANGE: dict[str, str] = {
    "OANDA": "XAUUSD",
    "PEPPERSTONE": "XAUUSD",
    "FOREXCOM": "XAUUSD",
    "FX": "XAUUSD",
    "FXCM": "XAUUSD",
    "TVC": "GOLD",
    "CAPITALCOM": "GOLD",
}

_CRYPTO_HINTS = ("BTC", "ETH", "USDT", "SOL", "DOGE", "BNB", "XRP", "CRYPTO")

# Crypto exchanges on TradingView (tvDatafeed exchange ids)
TV_CRYPTO_EXCHANGES: tuple[str, ...] = (
    "BINANCE",
    "BITSTAMP",
    "COINBASE",
    "BYBIT",
    "OKX",
    "BITFINEX",
    "HUOBI",
)

# Common crypto pairs and their likely exchange on TradingView
_CRYPTO_SYMBOL_EXCHANGE_HINTS: dict[str, str] = {
    "BTCUSDT": "BINANCE",
    "ETHUSDT": "BINANCE",
    "BTCUSD": "BITSTAMP",
    "ETHUSD": "BITSTAMP",
    "SOLUSDT": "BINANCE",
    "BNBUSDT": "BINANCE",
    "XRPUSDT": "BINANCE",
    "DOGEUSDT": "BINANCE",
}

# Major index tickers on TradingView — (exchange, probe_order)
# These need specific exchanges; forex/gold auto-probe will never find them.
_KNOWN_INDEX_TICKERS: dict[str, list[tuple[str, str]]] = {
    # S&P 500
    "SPX": [("SP", "SPX"), ("NYSE", "SPX"), ("CBOT", "SPX"), ("TVC", "SPX")],
    # Nasdaq 100
    "NDX": [("NASDAQ", "NDX"), ("TVC", "NDX")],
    # Dow Jones Industrial Average
    "DJI": [("DJ", "DJI"), ("NYSE", "DJI"), ("TVC", "DJI")],
    # CBOE Volatility Index
    "VIX": [("CBOT", "VIX"), ("CBOE", "VIX"), ("TVC", "VIX")],
    # S&P 500 futures (mini)
    "ES1!": [("CME_MINI", "ES1!"), ("CME", "ES1!")],
    # Nasdaq 100 futures (mini)
    "NQ1!": [("CME_MINI", "NQ1!"), ("CME", "NQ1!")],
    # Dow futures (mini)
    "YM1!": [("CBOT", "YM1!"), ("CME", "YM1!")],
    # Russell 2000
    "RUT": [("NYSE", "RUT"), ("TVC", "RUT")],
}

# tvDatafeed exchange ids for China A-shares / Hong Kong (TradingView)
TV_ASHARE_EXCHANGES: frozenset[str] = frozenset({"SSE", "SZSE"})
TV_HK_EXCHANGE = "HKEX"
TV_HK_EXCHANGES: frozenset[str] = frozenset({TV_HK_EXCHANGE, "HK", "HKG", "HONGKONG"})
TV_EQUITY_EXCHANGES: frozenset[str] = TV_ASHARE_EXCHANGES | TV_HK_EXCHANGES
TV_SSE_INDEX_CODES: frozenset[str] = frozenset(
    {"000016", "000300", "000905", "000852"}
)


def _looks_like_ashare_code(code: str) -> bool:
    c = (code or "").strip().lower()
    if len(c) == 6 and c.isdigit():
        return True
    return c.startswith(("sh", "sz")) and len(c) >= 8 and c[2:].isdigit()


def is_likely_crypto_symbol(symbol: str) -> bool:
    s = (symbol or "").upper().replace("/", "").replace("-", "")
    return any(h in s for h in _CRYPTO_HINTS)


def normalize_gold_symbol_for_kind(kind: str, symbol: str) -> str:
    """Map crypto / MT5-style names to gold defaults for *kind*."""
    from pa_agent.data.ashare_common import normalize_ashare_symbol

    sym = (symbol or "").strip()
    if kind in ("akshare", "eastmoney", "tushare"):
        code = normalize_ashare_symbol(sym)
        if not code or not _looks_like_ashare_code(code):
            return A_SHARE_DEFAULT_SYMBOL
        return code
    if kind == "tradingview":
        code = normalize_ashare_tv_code(sym)
        if _is_ashare_tv_code(code):
            return code
        hk = normalize_hk_tv_code(sym)
        if _is_hk_tv_code(hk):
            return hk
    if not sym or is_likely_crypto_symbol(sym):
        return GOLD_TV_SYMBOL if kind == "tradingview" else GOLD_MT5_SYMBOL
    if kind == "tradingview" and sym.lower().endswith("m") and len(sym) > 2:
        return GOLD_TV_SYMBOL
    return sym


def normalize_gold_tv_exchange(exchange: str) -> str:
    """Persisted TV exchange id; empty / AUTO = probe all presets in UI list."""
    ex = (exchange or "").strip().upper()
    if is_tv_exchange_auto(ex):
        return ""
    if ex in ("BINANCE", "COINBASE", "BITSTAMP", "BYBIT", "OKX", "KRAKEN"):
        return ""
    return ex


def normalize_ashare_tv_code(symbol: str) -> str:
    """Normalize user input to 6-digit A-share code for TradingView."""
    from pa_agent.data.akshare_source import normalize_ashare_symbol

    raw = normalize_ashare_symbol(symbol)
    if raw.startswith(("sh", "sz")) and len(raw) >= 8:
        return raw[2:8]
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 6:
        return digits[-6:]
    return digits


def _is_ashare_tv_code(code: str) -> bool:
    return len(code) == 6 and code.isdigit()


def normalize_hk_tv_code(symbol: str) -> str:
    """Extract digit ticker from user input without changing leading zeros."""
    return re.sub(r"\D", "", (symbol or "").strip())


def _is_hk_tv_code(code: str) -> bool:
    return bool(code) and code.isdigit() and 1 <= len(code) <= 5


def is_partial_tv_symbol_input(symbol: str) -> bool:
    """True while input is incomplete (typing codes or short names)."""
    from pa_agent.data.tv_symbol_lookup import is_tv_name_input

    s = (symbol or "").strip()
    if not s:
        return True
    if is_tv_name_input(s):
        key = re.sub(r"\s+", "", s)
        return len(key) < 2
    if s.isdigit():
        if len(s) < 3:
            return True
        if len(s) == 6:
            return False
        if 3 <= len(s) <= 5:
            return False
    return False


def is_numeric_tv_equity_symbol(symbol: str) -> bool:
    """Digit-only symbols are stocks/indices, not spot gold."""
    s = (symbol or "").strip()
    return bool(s) and s.isdigit()


def infer_ashare_tv_exchange(code: str) -> str:
    """Infer SSE vs SZSE from a 6-digit stock/index code."""
    # STAR 科创板 (688/689) — TradingView 仅 SSE，填 SZSE 会 0 根 K 线
    if code.startswith(("688", "689")):
        return "SSE"
    if code in TV_SSE_INDEX_CODES or code.startswith(
        ("5", "600", "601", "603", "605", "900")
    ):
        return "SSE"
    if code.startswith(("399", "300", "301", "002", "003", "001")):
        return "SZSE"
    if code.startswith("000"):
        return "SZSE"
    return "SSE"


def is_tv_exchange_auto(exchange: str) -> bool:
    """True when UI/source exchange is «auto» (probe venues for equity)."""
    ex = (exchange or "").strip().upper()
    return ex in ("", "AUTO")


_GOLD_TV_SYMBOLS = frozenset({"XAUUSD", "GOLD", "XAU"})


def tv_forex_auto_probe_plan(symbol: str) -> list[tuple[str, str]]:
    """Ordered (exchange, symbol) for auto gold/forex using UI preset venues."""
    from pa_agent.data.tradingview import TV_EXCHANGE_PRESETS

    sym = (symbol or "").strip().upper() or GOLD_TV_SYMBOL
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ex in TV_EXCHANGE_PRESETS:
        if not ex or ex in TV_EQUITY_EXCHANGES or ex in TV_CRYPTO_EXCHANGES:
            continue
        if sym in _GOLD_TV_SYMBOLS:
            feed = TV_GOLD_SYMBOL_BY_EXCHANGE.get(ex)
            if feed is None:
                continue
            pair = (ex, feed)
        else:
            pair = (ex, sym)
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def tv_auto_probe_plan(symbol: str) -> list[tuple[str, str]]:
    """Full auto-probe plan: equity venues first, else all forex presets in the UI list."""
    equity = equity_tv_auto_probe_plan(symbol)
    if equity:
        return equity
    return tv_forex_auto_probe_plan(symbol)


def equity_tv_auto_probe_plan(symbol: str) -> list[tuple[str, str]]:
    """Ordered (exchange, symbol) attempts for auto-detect equity feeds."""
    from pa_agent.data.tv_symbol_lookup import lookup_tv_symbol_by_name, is_tv_name_input

    # 1. Known index tickers (SPX, NDX, VIX, futures, etc.)
    upper = (symbol or "").strip().upper()
    if upper in _KNOWN_INDEX_TICKERS:
        return _KNOWN_INDEX_TICKERS[upper]

    # 2. Company name lookup (Chinese or lowercase English)
    if is_tv_name_input(symbol):
        hit = lookup_tv_symbol_by_name(symbol)
        if hit is not None:
            return [hit]
        return []

    # 3. A-share numeric codes
    code_a = normalize_ashare_tv_code(symbol)
    if _is_ashare_tv_code(code_a):
        first = infer_ashare_tv_exchange(code_a)
        second = "SZSE" if first == "SSE" else "SSE"
        return [(first, code_a), (second, code_a)]

    # 4. HK stock codes
    code_h = normalize_hk_tv_code(symbol)
    if _is_hk_tv_code(code_h):
        return [(TV_HK_EXCHANGE, code_h)]

    # 5. Crypto pairs
    if is_likely_crypto_symbol(upper):
        hint_ex = _CRYPTO_SYMBOL_EXCHANGE_HINTS.get(upper)
        pairs: list[tuple[str, str]] = []
        if hint_ex:
            pairs.append((hint_ex, upper))
        for ex in TV_CRYPTO_EXCHANGES:
            if ex != hint_ex:
                pairs.append((ex, upper))
        return pairs

    return []


def ashare_tv_probe_order(code: str) -> tuple[str, str]:
    """Order of exchanges to try when auto-detecting A-shares on TradingView."""
    first = infer_ashare_tv_exchange(code)
    second = "SZSE" if first == "SSE" else "SSE"
    return first, second


def is_hk_tv_request(exchange: str, symbol: str) -> bool:
    ex = (exchange or "").strip().upper()
    if ex in TV_HK_EXCHANGES:
        return True
    return _is_hk_tv_code(normalize_hk_tv_code(symbol))


def is_ashare_tv_request(exchange: str, symbol: str) -> bool:
    """True when the user intends an A-share feed on TradingView."""
    ex = (exchange or "").strip().upper()
    if ex in TV_ASHARE_EXCHANGES or ex in {"SH", "SZ", "SHSE", "XSHE", "SHANGHAI", "SHENZHEN"}:
        return True
    return _is_ashare_tv_code(normalize_ashare_tv_code(symbol))


def is_equity_tv_request(exchange: str, symbol: str) -> bool:
    from pa_agent.data.tv_symbol_lookup import is_tv_name_input

    if is_tv_name_input(symbol):
        return True
    return is_ashare_tv_request(exchange, symbol) or is_hk_tv_request(exchange, symbol)


def resolve_tv_ashare_pair(
    exchange: str,
    symbol: str,
) -> tuple[str, str, bool] | None:
    """Return (exchange, symbol, adjusted) for A-shares, or None if not A-share."""
    code = normalize_ashare_tv_code(symbol)
    if not _is_ashare_tv_code(code):
        return None

    ex_in = (exchange or "").strip().upper()
    adjusted = False
    if ex_in in ("SH", "SSE", "SHSE", "SHANGHAI"):
        return "SSE", code, ex_in != "SSE"
    if ex_in in ("SZ", "SZSE", "XSHE", "SHENZHEN"):
        required = infer_ashare_tv_exchange(code)
        if required == "SSE":
            return "SSE", code, True
        return "SZSE", code, ex_in != "SZSE"
    if ex_in in TV_ASHARE_EXCHANGES:
        required = infer_ashare_tv_exchange(code)
        if ex_in != required:
            return required, code, True
        return ex_in, code, False
    if is_tv_exchange_auto(ex_in):
        return "", code, False
    inferred = infer_ashare_tv_exchange(code)
    return inferred, code, True


def resolve_tv_hk_pair(
    exchange: str,
    symbol: str,
) -> tuple[str, str, bool] | None:
    """Return (exchange, symbol, adjusted) for HKEX, or None if not HK-style code."""
    code = normalize_hk_tv_code(symbol)
    if not _is_hk_tv_code(code):
        return None

    ex_in = (exchange or "").strip().upper()
    if ex_in in TV_HK_EXCHANGES:
        return TV_HK_EXCHANGE, code, ex_in != TV_HK_EXCHANGE
    if ex_in in TV_ASHARE_EXCHANGES:
        return TV_HK_EXCHANGE, code, True
    if is_tv_exchange_auto(ex_in):
        return "", code, False
    if ex_in in TV_GOLD_SYMBOL_BY_EXCHANGE:
        return TV_HK_EXCHANGE, code, True
    return TV_HK_EXCHANGE, code, True


def resolve_tv_fetch_pair(exchange: str, symbol: str) -> tuple[str, str]:
    """Map user subscription to tvDatafeed ``get_hist`` args only.

    Does not rewrite the user's symbol/exchange in the UI. Name lookup is used
    only for the API call when the user typed a company name.
    """
    from pa_agent.data.tv_symbol_lookup import (
        is_tv_name_input,
        lookup_tv_symbol_by_name,
    )

    ex = (exchange or "").strip().upper()
    sym = (symbol or "").strip()
    if is_tv_exchange_auto(ex):
        return "", sym
    if is_tv_name_input(sym):
        hit = lookup_tv_symbol_by_name(sym)
        if hit is not None:
            return hit
        # Name lookup failed — fall through to ticker path
    return ex, sym


def resolve_tv_pair(
    exchange: str,
    symbol: str,
) -> tuple[str, str, bool]:
    """Resolve TradingView exchange/symbol for names, A/HK shares, indices, or gold/forex."""
    from pa_agent.data.tv_symbol_lookup import (
        is_tv_name_input,
        lookup_tv_symbol_by_name,
    )

    sym = (symbol or "").strip()
    ex_in = (exchange or "").strip().upper()

    # 1. Company name lookup (Chinese or lowercase English)
    if is_tv_name_input(sym):
        hit = lookup_tv_symbol_by_name(sym)
        if hit is not None:
            ex_res, code = hit
            return ex_res, code, True
        # Name lookup failed — fall through

    # 2. Known index tickers (SPX, NDX, VIX, futures, etc.)
    upper = sym.upper()
    if upper in _KNOWN_INDEX_TICKERS:
        plan = _KNOWN_INDEX_TICKERS[upper]
        if is_tv_exchange_auto(ex_in):
            # Auto: use the first (best) exchange from the plan
            return plan[0][0], plan[0][1], True
        # User specified an exchange — find it in the plan or use first
        for ex_try, sym_try in plan:
            if ex_try == ex_in:
                return ex_try, sym_try, False
        # User's exchange not in plan; trust them and pass through
        return ex_in, sym, False

    # 3. A-share numeric codes
    ashare = resolve_tv_ashare_pair(exchange, symbol)
    if ashare is not None:
        return ashare

    # 4. HK stock codes
    hk = resolve_tv_hk_pair(exchange, symbol)
    if hk is not None:
        return hk

    # 5. Crypto pairs — avoid gold/forex fallback
    if is_likely_crypto_symbol(upper):
        if is_tv_exchange_auto(ex_in):
            hint_ex = _CRYPTO_SYMBOL_EXCHANGE_HINTS.get(upper, TV_CRYPTO_EXCHANGES[0])
            return hint_ex, upper, True
        return ex_in, upper, False

    # 6. Gold / forex fallback
    return resolve_tv_gold_pair(exchange, symbol)


def resolve_tv_gold_pair(
    exchange: str,
    symbol: str,
) -> tuple[str, str, bool]:
    """Return ``(exchange, symbol, adjusted)`` for a valid TV gold feed.

    Fixes common mistake ``TVC`` + ``XAUUSD`` → ``TVC`` + ``GOLD``.
    """
    ex_in = (exchange or "").strip().upper()
    sym = (symbol or "").strip().upper()
    if is_numeric_tv_equity_symbol(sym):
        hk = normalize_hk_tv_code(sym)
        if _is_hk_tv_code(hk):
            sym = hk
        if is_tv_exchange_auto(ex_in):
            return "", sym, False
        if ex_in in TV_EQUITY_EXCHANGES:
            return ex_in, sym, False
        return ex_in or "", sym, False
    if is_tv_exchange_auto(ex_in):
        return "", sym or GOLD_TV_SYMBOL, False
    ex = normalize_gold_tv_exchange(exchange)
    sym = sym or GOLD_TV_SYMBOL
    expected = TV_GOLD_SYMBOL_BY_EXCHANGE.get(ex)
    if expected is not None:
        if sym != expected:
            return ex, expected, True
        return ex, expected, False
    if sym == "GOLD":
        return "TVC", "GOLD", ex != "TVC"
    return GOLD_TV_EXCHANGE, GOLD_TV_SYMBOL, ex != GOLD_TV_EXCHANGE or sym != GOLD_TV_SYMBOL


def migrate_general_gold_defaults(general: dict) -> None:
    """In-place migration: gold symbol + valid TV exchange/symbol pair."""
    kind = str(general.get("last_data_source", "mt5"))
    sym = str(general.get("last_symbol", ""))
    general["last_symbol"] = normalize_gold_symbol_for_kind(kind, sym)
    if kind == "tradingview":
        ex, sym, _ = resolve_tv_pair(
            str(general.get("last_tradingview_exchange", "")),
            general["last_symbol"],
        )
        general["last_tradingview_exchange"] = ex
        general["last_symbol"] = sym
    else:
        general["last_tradingview_exchange"] = normalize_gold_tv_exchange(
            str(general.get("last_tradingview_exchange", ""))
        )
