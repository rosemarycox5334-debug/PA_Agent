"""Design tokens for the PA Agent dark trading-terminal theme (OKLch-inspired)."""
from __future__ import annotations

# ---------------------------------------------------------------------------
# OKLch-style palette (canonical tokens)
# ---------------------------------------------------------------------------

# Backgrounds
BG = "#0a0e14"
SURFACE_1 = "#161b22"
SURFACE_2 = "#1c2128"
SURFACE_3 = "#21262d"
SURFACE_4 = "#30363d"

# Text
FG = "#e6edf3"
FG_2 = "#8b949e"
FG_3 = "#6e7681"

# Accents
ACCENT = "#2dd4bf"
ACCENT_2 = "#f87171"
ACCENT_3 = "#38bdf8"

# Semantic
SUCCESS = "#22c55e"
DANGER = "#ef4444"
WARNING = "#f59e0b"
INFO = "#38bdf8"

# Trading
CHART_UP = "#22c55e"
CHART_DOWN = "#ef4444"
CHART_GRID = "#1c2128"
CHART_LINE = "#fbbf24"
CHART_LINE_2 = "#7dd3fc"
CHART_LINE_3 = "#fb923c"

# Pills
PILL_GREEN_TEXT = "#86efac"
PILL_GREEN_BORDER = "rgba(34,197,94,0.35)"
PILL_GREEN_BG = "rgba(34,197,94,0.10)"

PILL_AMBER_TEXT = "#fbbf24"
PILL_AMBER_BORDER = "rgba(245,158,11,0.35)"
PILL_AMBER_BG = "rgba(245,158,11,0.10)"

PILL_BLUE_TEXT = "#7dd3fc"
PILL_BLUE_BORDER = "rgba(56,189,248,0.35)"
PILL_BLUE_BG = "rgba(56,189,248,0.10)"

PILL_RED_TEXT = "#fca5a5"
PILL_RED_BORDER = "rgba(239,68,68,0.35)"
PILL_RED_BG = "rgba(239,68,68,0.10)"

PILL_CYAN_TEXT = "#5eead4"
PILL_CYAN_BORDER = "rgba(45,212,191,0.35)"
PILL_CYAN_BG = "rgba(45,212,191,0.10)"

# Typography
FONT_UI = '"Segoe UI", "Microsoft YaHei UI", sans-serif'
FONT_MONO = '"JetBrains Mono", "Cascadia Mono", "Consolas", monospace'

# Layout
RADIUS = 6
SPACING = 8

# ---------------------------------------------------------------------------
# Legacy aliases (backward-compatible names mapped to the new palette)
# ---------------------------------------------------------------------------
BG_BASE = BG
BG_PANEL = SURFACE_1
BG_ELEVATED = SURFACE_2
BG_REASONING = SURFACE_2
BG_INPUT = BG

BORDER = SURFACE_4
BORDER_MUTED = SURFACE_3

TEXT_PRIMARY = FG
TEXT_SECONDARY = FG_2
TEXT_MUTED = FG_3

ACCENT_PRIMARY = ACCENT_3
ACCENT_REASONING = ACCENT
ACCENT_SUCCESS = SUCCESS
ACCENT_WARNING = WARNING
ACCENT_DANGER = DANGER

TRADE_LONG = CHART_UP
TRADE_SHORT = CHART_DOWN
TRADE_NEUTRAL = FG_2

TOKEN_YELLOW = WARNING
TOKEN_RED = DANGER
