# PA Agent — AI K-Line Analysis Assistant (Desktop)

**QQ Group: 488729337** (Chinese community)

---

> **Price Action** AI-assisted decision-making tool for discretionary traders: reads OHLCV K-line data from **MT5 / TradingView / A-share sources**, feeds **structured K-line data and pre-computed features** into an LLM for **two-stage analysis** (market diagnosis → trading decision). **Not** screenshot-based; **does not** connect to brokers or execute orders.

---

## Table of Contents

- [Project Overview](#project-overview)
- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the Program](#running-the-program)
- [Running Tests](#running-tests)
- [Directory Structure](#directory-structure)
- [Configuration](#configuration)
- [Contributing & Security](#contributing--security)
- [Detailed Usage](#detailed-usage)
- [Chart K-Line & Snapshot Notes](#chart-k-line--snapshot-notes)
- [FAQ](#faq)

---

## Project Overview

PA Agent is a Windows desktop assistant that helps traders interpret charts using the **Al Brooks** Price Action framework. It structures the "chart reading" process into a reproducible decision path with auditable fields.

The program will:

1. Fetch OHLCV K-line data for your selected symbol and timeframe from **MT5 / TradingView** (supports the currently forming candle; chart updates in real time).
2. Compute **EMA20, ATR14, and candlestick geometric features** locally (body ratio, inside/outside bars, ii/iii patterns, breakout follow-through, etc.).
3. Send the **K-line text table + feature table + prompt-engineering modules** to a large language model (supports DeepSeek, PackyAPI, and other OpenAI-compatible endpoints).
4. Perform **Stage 1 (Diagnosis)** and **Stage 2 (Decision)**, output structured JSON, and overlay entry / stop-loss / take-profit reference lines on the chart.

**It does NOT** send chart screenshots to the AI; the model reads a numerically consistent K-line sequence (K1 is the latest closed bar).

### Key Features

- 📈 **MT5 real-time K-lines** + local candlestick chart, EMA, and index labels
- 🧠 **Two-stage AI analysis**: Gate diagnosis → strategy routing → trading decision (limit / breakout / market, or no order)
- 📋 **Bar-by-bar summary** (`bar_by_bar_summary`) and signal-chain validation to reduce contradictions like "verbally bullish but JSON says short"
- 🔄 **Incremental analysis**: analyzes only newly closed candles on top of the previous successful record
- 💬 **Post-analysis Q&A**: after refreshing and freezing the chart, continue asking the AI questions using the **same K-line table shown on screen**
- 📚 **Experience library**: retrieves historical cases by timeframe/position for Stage 2 reference
- 📝 **Full disk persistence**: prompt, raw response, diagnosis/decision JSON, token usage, and Q&A history
- 🔒 **API Key** stored locally with DPAPI encryption

For complete UI instructions, see `[PA_Agent使用文档.md](PA_Agent使用文档.md)` (Chinese).

---

## How It Works

```text
MT5 Terminal ──Fetch K-line──► Local Buffer / Chart Display
                                    │
                                    ▼
                          Submit Analysis (optional: wait for current bar close)
                                    │
             ┌──────────────────────┴──────────────────────┐
             ▼                                               ▼
       Stage 1 · Market Diagnosis                      Strategy File Routing
       (Timeframe / Direction / Gate / Bar summary)     (load prompt by diagnosis)
             │                                               │
             └──────────────────────┬──────────────────────┘
                                    ▼
                          Stage 2 · Trading Decision
                          (§9 Signal Chain / §10 R/R / §11 Order Type)
                                    │
                                    ▼
                    Validate JSON ──► Overlay Lines ──► Save Record ──► Q&A
```

| Stage | Description |
|-------|-------------|
| **Data Source** | **MetaTrader 5** (terminal must be open and logged in); symbol names must match MT5 Market Watch exactly (e.g., `US500m`) |
| **Sent to AI** | K-line table, geometric feature table, Stage 1 diagnosis result, and routed strategy prompt; Stage 2 additionally includes decision-tree rules |
| **Chart Role** | For visual confirmation; auto-pauses during analysis to avoid inconsistency between UI and submitted data |
| **Output** | Stage 1/2 JSON; Stage 2 includes `decision`, `decision_trace`, risk-reward ratio, etc. |
| **Boundary** | **Analysis-only; does NOT connect to brokers or place orders** |

---

## Requirements

| Item | Requirement |
|------|-------------|
| OS | Windows 10 / 11 |
| Python | 3.11+ (official installer recommended; check **Add to PATH**) |
| MetaTrader 5 | **Required**; must be installed and logged in to fetch K-lines |
| GPU | No special requirement |
| Network | Access to your configured AI API (e.g., DeepSeek, PackyAPI, etc.) |

---

## Installation

### 1. Install Python 3.11+

Download from [python.org](https://www.python.org/downloads/), check **Add Python to PATH**.

```cmd
python --version
```

### 2. Install and Log in to MetaTrader 5

Launch MT5, log in to your broker account, and confirm the symbol names in **Market Watch** (note suffixes like `m`).

### 3. Clone the Repository

```cmd
git clone https://github.com/rosemarycox5334-debug/PA_Agent.git
cd PA_Agent
```

### 4. Create a Virtual Environment (Recommended)

```cmd
python -m venv .venv
.venv\Scripts\activate
```

### 5. Install Dependencies

```cmd
pip install -e ".[dev]"
```

If East Money data source is unavailable: use **TradingView** instead. Select TradingView as the data source; supports **A-shares** (6-digit + SSE/SZSE), **HK stocks** (`HKEX` + code), and **stock names** (e.g., `小米集团` with built-in alias table; **keep your input text in the symbol box**, the backend resolves the alias automatically). When exchange is set to **(Auto)**, it will probe the appropriate market. Custom aliases can be edited in `config/tv_symbol_aliases.json` (see `tv_symbol_aliases.example.json`).

> China mirror example:
>
> ```cmd
> pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

### 6. Configure API

Copy the config template (recommended on first clone):

```cmd
copy config\settings.example.json config\settings.json
```

Launch the program, open **Settings**, and fill in **Base URL**, **Model Name**, and **API Key** (supports DeepSeek official or third-party compatible gateways). The key will be encrypted and written to `config/settings.json`; it will not be committed to Git in plaintext.

For field descriptions, see `[config/README.md](config/README.md)`.

---

## Running the Program

```cmd
python -m pa_agent.main
```

Or after installation:

```cmd
pa-agent
```

You can also use `run.py` in the project root if present.

If it says the data source is not connected on first launch, please confirm MT5 is running and logged in.

---

## Running Tests

```cmd
pytest
```

Skip end-to-end / GUI tests:

```cmd
pytest -m "not e2e"
```

Unit tests only:

```cmd
pytest -m unit
```

Property-based tests only:

```cmd
pytest -m property
```

---

## Directory Structure

```
PA_Agent/
├── pa_agent/                  # Main package
│   ├── main.py                # Entry point
│   ├── app_context.py         # Application context
│   ├── ai/                    # Prompt assembly, routing, JSON validation, API client
│   ├── config/                # Config models and loading
│   ├── data/                  # Data sources (MT5, etc.) and K-line refresh loop
│   ├── gui/                   # PyQt6 UI (chart, real-time stream, decision panel)
│   ├── orchestrator/          # Two-stage analysis orchestration and post-analysis Q&A
│   ├── records/               # Analysis record read/write
│   ├── security/              # API Key encryption (Windows DPAPI)
│   └── util/                  # Utilities
├── prompt_engineering/        # Price Action prompts and strategy modules (.txt)
├── tests/                     # Unit / property / integration / e2e tests
├── config/                    # Config templates and docs (settings.json is generated locally, not committed)
│   ├── settings.example.json
│   └── README.md
├── .github/workflows/         # CI (Windows + pytest)
├── experience/                # Experience library cases
├── records/                   # Analysis records (pending / archived)
├── logs/                      # Runtime logs
├── assets/                    # README resources (e.g., donation QR)
├── pyproject.toml
└── README.md
```

---

## Configuration

Config files are in `config/`; they are auto-generated on first run. **Do NOT commit files containing secrets to Git**.

| File | Description |
|------|-------------|
| `config/settings.json` | Main config (API Key stored as `api_key_encrypted`) |
| `config/settings.example.json` | Template without secrets (copy to `settings.json`) |
| `config/exception_state.example.json` | Exception counter state structure reference |
| `config/exception_state.json` | Auto-generated at runtime; do NOT commit |

### Prevent Keys from Being Pushed to GitHub

1. Run once locally (optional):
   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\setup_git_secrets.ps1
   ```
2. Only configure the Key in the GUI **Settings** or local `settings.json`; never write it into README / test cases.
3. By default, `pytest` does not run `live` tests that require real network access.

---

## Contributing & Security

| Document | Description |
|----------|-------------|
| `[CONTRIBUTING.md](CONTRIBUTING.md)` | Dev environment, tests, and PR conventions |
| `[SECURITY.md](SECURITY.md)` | Reporting vulnerabilities and key leaks |
| `[LICENSE](LICENSE)` | MIT License |

---

## Detailed Usage

- Control bar: **Symbol / Timeframe / Bar Count**, **Submit Analysis**, **Incremental Analysis**, **Wait for Bar Close**, **Demo Mode**
- Right tabs: **Real-time** (thinking stream + Q&A), **Decision Tree**, **Decision**, **Raw**, **Debug**, etc.

Full operation guide, trading bias, and strategy routing table: `[PA_Agent使用文档.md](PA_Agent使用文档.md)` (Chinese).

**Why is there 1 fewer K-line after analysis?** See `[docs/图表K线与分析快照说明.md](docs/图表K线与分析快照说明.md)` (Chinese).

---

## FAQ

### Q: `ModuleNotFoundError: No module named 'pa_agent'` on startup

Activate the virtual environment in the project root and install:

```cmd
.venv\Scripts\activate
pip install -e ".[dev]"
```

### Q: MT5 not connected or no K-lines

1. Confirm the MT5 terminal is open and logged in
2. The symbol name must exactly match MT5 **Market Watch** (including suffixes like `m`)
3. The symbol displays K-lines normally in MT5

### Q: Does the program send screenshots to the AI?

**No.** It submits a K-line OHLCV text table, pre-computed features, and prompts; the chart is for local viewing only.

### Q: Why does the chart stop refreshing during analysis?

The chart **auto-pauses** during analysis to prevent inconsistency between the UI and submitted data. Click **Chart Real-time Update** to resume; during Q&A, the chart refreshes once and then freezes, using that snapshot for follow-up questions.

### Q: API call failed

Check network, Base URL, model name, and API Key; if using a proxy, configure it at the system or gateway level.

### Q: `config/settings.json` is corrupted

Delete and restart; the program will rebuild the default config:

```cmd
del config\settings.json
```

### Q: How to update

```cmd
git pull
pip install -e ".[dev]"
```

### Q: Where are the logs?

Under the `logs/` directory.

---

**Disclaimer**: This tool is for educational and research purposes only and does not constitute investment advice. Trading involves risk; you are solely responsible for your decisions.

---

Released under the [MIT License](LICENSE).

---

## Donations

If you find this program helpful, donations are welcome to motivate continued development. Thank you for your support!

(Priority support is given to donors due to the high volume of requests.)

<p align="center">
  <img src="1d935cac3a4a4575bb3e34beda997633.jpeg" alt="Donation QR" width="420" />
</p>
