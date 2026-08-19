"""Captured East Money HTTP/SSE quote APIs (盘口 / 逐笔 / 分时).

Reverse-engineered from ``quote.eastmoney.com/newstatic/build/vendor.js`` (2026-06).
"""
from __future__ import annotations

from pa_agent.data.eastmoney_field_enums import FIELDS_TEN_DEPTH

# ── Hosts (CDN mirrors, same JSON schema) ─────────────────────────────────────
QUOTE_HOSTS = (
    "push2delay.eastmoney.com",
    "push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "91.push2.eastmoney.com",
)

UT_WEB = "fa5fd1943c7b386f172d6893dbfba10b"
WBP2U = "|0|0|0|web"

# ── 1) 五档盘口 + 现价 ────────────────────────────────────────────────────────
# GET https://{host}/api/qt/stock/get
# Params:
#   secid  = {market}.{code}   # SH 1.xxxxxx  SZ 0.xxxxxx
#   fltt=2 invt=2 ut={UT_WEB}
#   **Do not pass a narrow ``fields`` list** — f11–f40 become null.
#   If you must pass fields, use ORDER_BOOK_FIELDS below (akshare-compatible).
#
# Field map (same as akshare.stock_bid_ask_em):
#   卖5..卖1  f31/f32 .. f39/f40  (卖1 最接近现价)
#   买1..买5  f19/f20 .. f11/f12  (买1 最接近现价)
#   现价/量   f43 / f47(手)
#   涨跌幅    f170
#
# **十档 (L2) 协议**（vendor.js 逆向，需 ``fltt=1`` + ``FIELDS_TEN_DEPTH``）:
#   卖1–5  f39/f40 .. f31/f32  （同五档，整数价格按 f59 小数位缩放）
#   买1–5  f19/f20 .. f11/f12
#   卖6–10 f29/f30, f27/f28, f25/f26, f23/f24, f21/f22
#   买6–10 f9/f10,  f7/f8,   f5/f6,   f3/f4,   f1/f2
# 未开通超级 Level-2 时 f21–f30、f1–f10 返回 ``"-"``（服务端鉴权，非缺字段）。
#
# **SSE 推送**（网页实时行情，同字段）:
#   GET {push_host}/api/qt/stock/sse?fields=...&fltt=1&mpi=1000&invt=2&secid=...
#   GET .../api/qt/stock/details/sse  逐笔
#   GET .../api/qt/stock/trends2/sse  分时
STOCK_GET_PATH = "/api/qt/stock/get"
STOCK_SSE_PATH = "/api/qt/stock/sse"
DETAILS_SSE_PATH = "/api/qt/stock/details/sse"
TRENDS2_SSE_PATH = "/api/qt/stock/trends2/sse"

ORDER_BOOK_FIELDS = (
    "f120,f121,f122,f174,f175,f59,f163,f43,f57,f58,f169,f170,f46,f44,f51,"
    "f168,f47,f164,f116,f60,f45,f52,f50,f48,f167,f117,f71,f161,f49,f530,"
    "f135,f136,f137,f138,f139,f141,f142,f144,f145,f147,f148,f140,f143,f146,"
    "f149,f55,f62,f162,f92,f173,f104,f105,f84,f85,f183,f184,f185,f186,f187,"
    "f188,f189,f190,f191,f192,f107,f111,f86,f177,f78,f110,f262,f263,f264,f267,"
    "f268,f255,f256,f257,f258,f127,f199,f128,f198,f259,f260,f261,f171,f277,f278,"
    "f279,f288,f152,f250,f251,f252,f253,f254,f269,f270,f271,f272,f273,f274,f275,"
    "f276,f265,f266,f289,f290,f286,f285,f292,f293,f294,f295,"
    "f11,f12,f13,f14,f15,f16,f17,f18,f19,f20,"
    "f31,f32,f33,f34,f35,f36,f37,f38,f39,f40"
)

# Sell price/volume field pairs: (卖1)..(卖5)
ASK_FIELD_PAIRS: tuple[tuple[str, str], ...] = (
    ("f39", "f40"),
    ("f37", "f38"),
    ("f35", "f36"),
    ("f33", "f34"),
    ("f31", "f32"),
)

# Buy price/volume field pairs: (买1)..(买5)
BID_FIELD_PAIRS: tuple[tuple[str, str], ...] = (
    ("f19", "f20"),
    ("f17", "f18"),
    ("f15", "f16"),
    ("f13", "f14"),
    ("f11", "f12"),
)

FREE_DEPTH_LEVELS = 5
L2_DEPTH_LEVELS = 10

# L2 扩展档位（fltt=1，卖六~卖十 / 买六~买十）
L2_ASK_EXTENDED: tuple[tuple[str, str], ...] = (
    ("f29", "f30"),
    ("f27", "f28"),
    ("f25", "f26"),
    ("f23", "f24"),
    ("f21", "f22"),
)
L2_BID_EXTENDED: tuple[tuple[str, str], ...] = (
    ("f9", "f10"),
    ("f7", "f8"),
    ("f5", "f6"),
    ("f3", "f4"),
    ("f1", "f2"),
)

# vendor.js ``_(enums)`` 构建的完整十档 fields（勿缩成 f1–f40 列表）
TEN_DEPTH_FIELDS = FIELDS_TEN_DEPTH

# ── 2) 当日逐笔成交 ───────────────────────────────────────────────────────────
# GET https://{host}/api/qt/stock/details/get
# Params (required — omitting fields2 → rc=102):
#   secid, ut, fields1=f1, fields2=f51,f52,f53,f54,f55, pos=0, lmt=N
# Each line: HH:MM:SS,price,volume,?,side  side: 1=买 2=卖 0=中性 4=竞价
DETAILS_GET_PATH = "/api/qt/stock/details/get"
DETAILS_FIELDS1 = "f1"
DETAILS_FIELDS2 = "f51,f52,f53,f54,f55"

# ── 3) 分时 ───────────────────────────────────────────────────────────────────
# GET https://{host}/api/qt/stock/trends2/get
TRENDS2_PATH = "/api/qt/stock/trends2/get"
