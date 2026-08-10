# PA Agent NAS 服务端改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 headless 服务端（`pa_agent/server/`）：多品种轮巡两阶段 LLM 分析 + 飞书推送 + Web 管理页（配置/状态/历史），Docker 部署到 NAS。

**Architecture:** FastAPI 单容器一体化。后台线程 `WatchScheduler` 逐品种调用 `AnalysisService`（订阅数据源 → 构建 `KlineFrame` → 复用 `TwoStageOrchestrator.submit()` → 判定下单机会 → 飞书/PushPlus 推送）。前端为无构建 Vue 3 单页，由 FastAPI 托管静态文件。配置/记录目录通过 Docker volume 挂载（`paths.py` 全部锚定 PROJECT_ROOT，路径代码零改动）。

**Tech Stack:** Python 3.11+、FastAPI、uvicorn、Vue 3（vendor ESM，无构建）、Docker。

**Spec:** `docs/superpowers/specs/2026-07-21-server-backend-design.md`

## Global Constraints

- 现有文件只允许两处小改：`pyproject.toml`（加 `server` extra）、`pa_agent/gui/order_opportunity.py`（改为 re-export）。其余全部新增文件。
- 服务端代码（`pa_agent/server/`、`pa_agent/notify/`）**禁止 import 任何 PyQt6 模块**（含间接：不得 import `pa_agent.gui.*`、`pa_agent.util.event_bus`、`pa_agent.ai.session_ledger`、`pa_agent.data.refresh_loop`、`pa_agent.demo.replayer`）。
- 所有用户可见文案为中文。
- 端口默认 `8688`；环境变量 `PA_SERVER_HOST` / `PA_SERVER_PORT` 覆盖。
- 敏感字段（`provider.api_key`、`feishu.secret`、`feishu.app_secret`、`pushplus.token`、`tushare.token`）：GET 脱敏（`mask_secret`），PUT 空字符串 = 保留旧值。
- 测试放 `tests/server/`；运行命令 `.venv/bin/python -m pytest tests/server/ -v`（若 `.venv` 不存在则先 `uv venv && uv pip install -e ".[dev,server]"`）。
- 提交信息用中文、结尾带 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。

## 已核实的关键接口（各任务直接引用）

- `TwoStageOrchestrator(client, assembler, router, validator, pending_writer, exp_reader, settings)`；`submit(frame, cancel_token, on_event, *, on_stage1_reasoning=None, on_stage1_content=None, on_stage2_reasoning=None, on_stage2_content=None, on_stage_prompt=None, on_stage2_files=None, previous_record=None, incremental_new_bar_count=None) -> AnalysisRecord`（阻塞，纯回调，无 Qt）。`record.stage2_decision` 是 stage2 全量 dict，下单判定用 `record.stage2_decision.get("decision")` 内层 dict。
- `CancelToken`（`pa_agent/util/threading.py`）：`.set()` / `.is_set()` / `.wait(timeout)` / `.clear()`。
- `DataSource`（`pa_agent/data/base.py:92`）：`connect() / disconnect() / subscribe(symbol, timeframe) / unsubscribe() / latest_snapshot(n) -> list[KlineBar]`（index 0 = 最新，可能含未收盘 bar）。
- `build_display_frame(bars_raw, n, symbol, timeframe, *, now_ms) -> KlineFrame | None`（`pa_agent/data/snapshot.py:130`）。取数条数应为 `n + INDICATOR_WARMUP_BARS + 1`（warmup=50，+1 容忍 forming bar）。
- `create_data_source(kind)`（`pa_agent/data/factory.py:80`）、`normalize_data_source_kind(kind)`、`create_ai_client(provider_settings, logger_=...)`（`pa_agent/ai/client_factory.py:12`）。
- TradingView 交易所设置：`isinstance(ds, TradingViewSource)` 时 `ds.set_exchange(settings.general.last_tradingview_exchange or "")`。
- 飞书：`send_order_signal(decision_inner=, stage2_full=, symbol=, timeframe=, chart_image_path=, settings=)`（`pa_agent/notify/feishu_notifier.py:293`）。PushPlus：`pushplus_is_active(settings)` + `send_order_signal(decision_inner=, stage2_full=, symbol=, timeframe=, settings=)`。
- 交易记录：`save_trade_record(decision_inner=, stage2_full=, stage1_diagnosis=, frame=, meta_symbol=, meta_timeframe=, decision_stance=, model_name=, structure_flip_cooldown_bars=)`（`pa_agent/records/trade_logger.py`，matplotlib Agg 无头出图到 `_TRADE_RECORDS_DIR`，PNG 命名 `{symbol}_{tf}_{ts}.png`）。
- `mask_secret(s: str) -> str`（`pa_agent/util/mask_secret.py:4`）。
- `load_settings(path) / save_settings(settings, path)`（`pa_agent/config/settings.py`）；`SETTINGS_JSON_PATH / RECORDS_PENDING_DIR / EXPERIENCE_DIR / PROMPT_DIR`（`pa_agent/config/paths.py`）。
- 下单判定纯逻辑现位于 `pa_agent/gui/order_opportunity.py`（Task 1 将其移入 `pa_agent/notify/order_opportunity.py`）：`has_order_opportunity(decision, *, confidence_threshold=None) -> bool`、`format_order_alert_message(decision) -> str`。阈值来源 `settings.general.decision_confidence_threshold`（0-100，默认 40）。
- 轮巡配置：`settings.general.watch_symbols`（逗号分隔）、`watch_round_interval_min`、周期 `last_timeframe`、K 线数 `analysis_bar_count`。品种解析函数 `parse_watch_symbols(raw)`（现在 `gui/watch_rotation.py:40`，纯函数，Task 5 复制到 scheduler 模块）。
- 事件枚举 `OrchestratorEvent`（`util/threading.py`）：Stage1Started/Stage1Done/Stage2Started/Stage2Done/RecordSaved/Cancelled/Stage1Failed/Stage2Failed/Stage1Retry/Stage2Retry/InsufficientData。

---

### Task 1: 抽取下单判定纯函数到 `pa_agent/notify/order_opportunity.py`

**Files:**
- Create: `pa_agent/notify/order_opportunity.py`
- Modify: `pa_agent/gui/order_opportunity.py`（删除纯函数，改为 re-export）
- Test: `tests/server/test_order_opportunity.py`

**Interfaces:**
- Produces: `pa_agent.notify.order_opportunity.has_order_opportunity(decision: dict | None, *, confidence_threshold: int | None = None) -> bool`；`format_order_alert_message(decision: dict) -> str`；`ORDER_OPPORTUNITY_TYPES: frozenset[str]`。GUI 侧旧 import 路径继续可用。

- [ ] **Step 1: 写失败测试**

```python
# tests/server/__init__.py 为空文件；tests/server/test_order_opportunity.py:
"""notify.order_opportunity 纯函数（无 Qt 依赖）测试."""
import sys


def test_import_without_qt():
    """服务端模块 import 后不得引入 PyQt6."""
    for mod in list(sys.modules):
        if mod.startswith("PyQt6"):
            del sys.modules[mod]
    from pa_agent.notify.order_opportunity import has_order_opportunity  # noqa: F401

    assert not any(m.startswith("PyQt6") for m in sys.modules)


def test_has_order_opportunity_basic():
    from pa_agent.notify.order_opportunity import has_order_opportunity

    assert has_order_opportunity({"order_type": "限价单"})
    assert not has_order_opportunity({"order_type": "观望"})
    assert not has_order_opportunity(None)


def test_confidence_threshold_gate():
    from pa_agent.notify.order_opportunity import has_order_opportunity

    d = {"order_type": "市价单", "trade_confidence": 55}
    assert has_order_opportunity(d, confidence_threshold=50)
    assert not has_order_opportunity(d, confidence_threshold=60)
    # 无 confidence 字段且设了阈值 → 拒绝
    assert not has_order_opportunity({"order_type": "市价单"}, confidence_threshold=50)


def test_gui_reexport_compat():
    """GUI 旧路径必须仍然可导入同一函数（需要 PyQt6 环境）."""
    from pa_agent.gui.order_opportunity import has_order_opportunity as gui_fn
    from pa_agent.notify.order_opportunity import has_order_opportunity as pure_fn

    assert gui_fn is pure_fn
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/server/test_order_opportunity.py -v`，预期 `ModuleNotFoundError: pa_agent.notify.order_opportunity`。
- [ ] **Step 3: 实现** — 把 `gui/order_opportunity.py` 里的 `ORDER_OPPORTUNITY_TYPES`、`_parse_trade_confidence`、`has_order_opportunity`、`_fmt_price`、`format_order_alert_message` **原样搬**到新文件 `pa_agent/notify/order_opportunity.py`（模块 docstring：`"""下单机会判定与文案（纯函数，服务端/GUI 共用）."""`，只 import `logging`/`typing`）。`gui/order_opportunity.py` 删除这些定义，在顶部加：

```python
from pa_agent.notify.order_opportunity import (  # noqa: F401
    ORDER_OPPORTUNITY_TYPES,
    format_order_alert_message,
    has_order_opportunity,
)
```

（保留文件中 Qt 相关的 `play_order_alert_sound` / `show_order_opportunity_alert` 等函数；顺带删掉不再使用的顶部 `from PyQt6.QtCore import Qt` 若其仅被已搬走的代码使用——先 grep 确认。）
- [ ] **Step 4: 测试通过** — 同命令，4 个用例 PASS。
- [ ] **Step 5: 提交** — `git add pa_agent/notify/order_opportunity.py pa_agent/gui/order_opportunity.py tests/server/ && git commit -m "重构: 下单机会判定纯函数抽至 notify 供服务端复用"`

### Task 2: `server` 依赖组 + 包骨架

**Files:**
- Modify: `pyproject.toml:34`（optional-dependencies 段）
- Create: `pa_agent/server/__init__.py`

- [ ] **Step 1:** `pyproject.toml` 的 `[project.optional-dependencies]` 中新增：

```toml
server = [
    "fastapi>=0.111",
    "uvicorn>=0.30",
]
```

- [ ] **Step 2:** `pa_agent/server/__init__.py` 内容仅一行 docstring：`"""Headless 服务端（NAS 部署）：FastAPI + 多品种轮巡调度。"""`
- [ ] **Step 3:** 安装并验证：`uv pip install -e ".[dev,server]"`（在 `.venv` 激活状态下；或 `uv pip install --python .venv/bin/python -e ".[dev,server]"`），然后 `.venv/bin/python -c "import fastapi, uvicorn; print('ok')"` 输出 ok。
- [ ] **Step 4: 提交** — `feat: 新增 server 可选依赖组与 server 包骨架`

### Task 3: headless 装配 `server/bootstrap.py`

**Files:**
- Create: `pa_agent/server/bootstrap.py`
- Test: `tests/server/test_bootstrap.py`

**Interfaces:**
- Produces:
  - `ServerContext`（dataclass, slots）：字段 `settings, logger, data_source, client, assembler, router, validator, pending_writer, exp_reader`
  - `bootstrap_headless(settings_path: Path | None = None) -> ServerContext`
  - `build_orchestrator(ctx: ServerContext) -> TwoStageOrchestrator`
  - `rebuild_client(ctx) -> None`（按 `ctx.settings.provider` 重建 `ctx.client`）
  - `rebuild_data_source(ctx) -> None`（disconnect 旧的→按 `ctx.settings.general.last_data_source` 重建，TradingView 时 set_exchange；不 connect，由调用方连）

- [ ] **Step 1: 写失败测试**

```python
# tests/server/test_bootstrap.py
"""headless 装配测试：不依赖 Qt、组件齐全."""
import json
import sys


def _write_settings(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "provider": {"model": "deepseek-chat", "base_url": "https://api.deepseek.com",
                     "api_key": "sk-test"},
        "general": {"last_data_source": "tradingview", "last_symbol": "XAUUSD",
                    "last_timeframe": "15m", "watch_symbols": "XAUUSD, BTCUSD"},
    }, ensure_ascii=False), encoding="utf-8")
    return p


def test_bootstrap_headless_no_qt(tmp_path):
    from pa_agent.server.bootstrap import bootstrap_headless

    ctx = bootstrap_headless(settings_path=_write_settings(tmp_path))
    assert ctx.settings.general.last_symbol == "XAUUSD"
    for name in ("client", "assembler", "router", "validator",
                 "pending_writer", "exp_reader", "data_source"):
        assert getattr(ctx, name) is not None, name
    assert not any(m.startswith("PyQt6") for m in sys.modules)


def test_build_orchestrator(tmp_path):
    from pa_agent.server.bootstrap import bootstrap_headless, build_orchestrator

    ctx = bootstrap_headless(settings_path=_write_settings(tmp_path))
    orch = build_orchestrator(ctx)
    assert orch is not None
```

注意：conftest 需保证测试不触碰真实 `config/settings.json` —— `bootstrap_headless` 必须把 `settings_path` 透传给 `load_settings`，测试里传 tmp_path。若 PyQt6 已被其他测试模块加载，本文件需最先运行或在测试内跳过污染检查（用 `pytest.mark.forked` 不引入新依赖的情况下：改为断言 `pa_agent.server.bootstrap` 模块源码不含 `PyQt6` 字符串 + `pa_agent.gui` 未被该 import 引入）。实现时以稳定为准。

- [ ] **Step 2: 确认失败**（ModuleNotFoundError）。
- [ ] **Step 3: 实现 `bootstrap.py`**——复刻 `AppContext.bootstrap()`（`pa_agent/app_context.py:29-143`）的接线并删去 Qt 组件：

```python
"""Headless 装配：无 EventBus/SessionTokenLedger/Qt，供服务端使用."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ServerContext:
    settings: Any
    logger: logging.Logger
    data_source: Any
    client: Any
    assembler: Any
    router: Any
    validator: Any
    pending_writer: Any
    exp_reader: Any


def bootstrap_headless(settings_path: Path | None = None) -> ServerContext:
    from pa_agent.config.paths import (EXPERIENCE_DIR, PROMPT_DIR,
                                       RECORDS_PENDING_DIR, SETTINGS_JSON_PATH)
    from pa_agent.config.settings import load_settings
    from pa_agent.util.logging import configure_logging

    path = settings_path or SETTINGS_JSON_PATH
    settings = load_settings(path)

    # 网关型 provider 的本地同步（QClaw/WorkBuddy/Cursor）；失败不阻塞启动
    for sync in ("sync_qclaw_agent_provider_on_load",
                 "sync_workbuddy_provider_on_load",
                 "sync_cursor_provider_on_load"):
        try:
            mod = {"sync_qclaw_agent_provider_on_load": "pa_agent.ai.qclaw_connector",
                   "sync_workbuddy_provider_on_load": "pa_agent.ai.workbuddy_connector",
                   "sync_cursor_provider_on_load": "pa_agent.ai.cursor_connector"}[sync]
            import importlib
            getattr(importlib.import_module(mod), sync)(settings, save_path=path)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("pa_agent").warning("provider 同步跳过 %s: %s", sync, exc)

    configure_logging(api_key=settings.provider.api_key)
    app_logger = logging.getLogger("pa_agent.server")

    from pa_agent.data.kline_adjust import apply_kline_adjust_from_settings
    apply_kline_adjust_from_settings(settings)

    data_source = _create_data_source_from_settings(settings)

    from pa_agent.ai.client_factory import create_ai_client
    client = create_ai_client(settings.provider, logger_=app_logger)

    from pa_agent.records.experience_reader import ExperienceReader
    from pa_agent.ai.prompt_assembler import PromptAssembler
    exp_reader = ExperienceReader(experience_dir=EXPERIENCE_DIR, logger=app_logger)
    assembler = PromptAssembler(prompt_dir=PROMPT_DIR, experience_reader=exp_reader,
                                prompt_settings=settings.prompt)

    from pa_agent.ai.json_validator import JsonValidator
    from pa_agent.ai.router import route_strategy_files
    from pa_agent.records.pending_writer import PendingWriter
    pending_writer = PendingWriter(pending_dir=RECORDS_PENDING_DIR, event_bus=None,
                                   api_key=settings.provider.api_key)

    return ServerContext(settings=settings, logger=app_logger, data_source=data_source,
                         client=client, assembler=assembler, router=route_strategy_files,
                         validator=JsonValidator(settings), pending_writer=pending_writer,
                         exp_reader=exp_reader)


def _create_data_source_from_settings(settings: Any) -> Any:
    from pa_agent.data.factory import create_data_source, normalize_data_source_kind

    kind = normalize_data_source_kind(getattr(settings.general, "last_data_source", "tradingview"))
    if kind == "mt5":  # 服务端（Linux 容器）无 MT5，强制回退
        kind = "tradingview"
    ds = create_data_source(kind)
    if kind == "tradingview":
        from pa_agent.data.tradingview import TradingViewSource
        if isinstance(ds, TradingViewSource):
            ds.set_exchange(getattr(settings.general, "last_tradingview_exchange", "") or "")
    return ds


def build_orchestrator(ctx: ServerContext) -> Any:
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    return TwoStageOrchestrator(client=ctx.client, assembler=ctx.assembler, router=ctx.router,
                                validator=ctx.validator, pending_writer=ctx.pending_writer,
                                exp_reader=ctx.exp_reader, settings=ctx.settings)


def rebuild_client(ctx: ServerContext) -> None:
    from pa_agent.ai.client_factory import create_ai_client
    ctx.client = create_ai_client(ctx.settings.provider, logger_=ctx.logger)


def rebuild_data_source(ctx: ServerContext) -> None:
    try:
        if ctx.data_source is not None:
            ctx.data_source.disconnect()
    except Exception as exc:  # noqa: BLE001
        ctx.logger.warning("旧数据源断开失败: %s", exc)
    ctx.data_source = _create_data_source_from_settings(ctx.settings)
```

（若 `create_ai_client` / `JsonValidator` 的真实签名与此不符，以源码为准调整；`PendingWriter(event_bus=None)` 已确认可选。）
- [ ] **Step 4: 测试通过。**
- [ ] **Step 5: 提交** — `feat: 服务端 headless 装配（无 Qt）`

### Task 4: 运行状态 `server/state.py`

**Files:**
- Create: `pa_agent/server/state.py`
- Test: `tests/server/test_state.py`

**Interfaces:**
- Produces: `ServerState` —— 线程安全；方法：
  - `set_scheduler(running: bool, error: str | None = None)`
  - `set_current(symbol: str, phase: str, round_num: int, idx: int, total: int)`（phase ∈ `switching/waiting_data/stage1/stage2/notifying/done`）
  - `set_round_wait(eta_epoch: float)`；`clear_current()`
  - `set_symbol_result(symbol: str, summary: dict)`（summary 键：`ts, ok, direction, order_type, confidence, has_order, error`）
  - `add_event(text: str)`（自动加时间戳，环形 200 条）
  - `snapshot() -> dict`（一次深拷贝返回全部：`{"scheduler": {"running", "error"}, "current": {...} | None, "round_wait_eta": float | None, "results": {symbol: summary}, "events": [{"ts", "text"}, ...]}`）

- [ ] **Step 1: 失败测试**

```python
# tests/server/test_state.py
import threading
import time


def test_snapshot_roundtrip():
    from pa_agent.server.state import ServerState

    st = ServerState()
    st.set_scheduler(True)
    st.set_current("XAUUSD", "stage1", round_num=1, idx=0, total=2)
    st.set_symbol_result("XAUUSD", {"ts": time.time(), "ok": True, "direction": "做多",
                                    "order_type": "限价单", "confidence": 70,
                                    "has_order": True, "error": None})
    st.add_event("测试事件")
    snap = st.snapshot()
    assert snap["scheduler"]["running"] is True
    assert snap["current"]["symbol"] == "XAUUSD"
    assert snap["results"]["XAUUSD"]["has_order"] is True
    assert snap["events"][-1]["text"] == "测试事件"
    # snapshot 是拷贝：改动不回渗
    snap["results"]["XAUUSD"]["ok"] = False
    assert st.snapshot()["results"]["XAUUSD"]["ok"] is True


def test_event_ring_capacity():
    from pa_agent.server.state import ServerState

    st = ServerState()
    for i in range(300):
        st.add_event(f"e{i}")
    events = st.snapshot()["events"]
    assert len(events) == 200 and events[-1]["text"] == "e299"


def test_thread_safety_smoke():
    from pa_agent.server.state import ServerState

    st = ServerState()
    def worker():
        for i in range(200):
            st.add_event(str(i))
            st.snapshot()
    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
```

- [ ] **Step 2: 确认失败。**
- [ ] **Step 3: 实现**——`threading.Lock` + `collections.deque(maxlen=200)` + `copy.deepcopy` 出快照；`add_event` 存 `{"ts": time.time(), "text": text}`。
- [ ] **Step 4: 测试通过。**
- [ ] **Step 5: 提交** — `feat: 服务端运行状态容器`

### Task 5: 单品种分析编排 `server/service.py`

**Files:**
- Create: `pa_agent/server/service.py`
- Test: `tests/server/test_service.py`

**Interfaces:**
- Consumes: Task 3 `ServerContext/build_orchestrator`，Task 4 `ServerState`，Task 1 `has_order_opportunity`。
- Produces:
  - `DATA_READY_TIMEOUT_S = 120`、`ANALYSIS_TIMEOUT_S = 1800`
  - `run_symbol_analysis(ctx, state, symbol: str, timeframe: str, *, cancel_token: CancelToken | None = None) -> dict`——阻塞执行完整单品种分析；返回 summary dict（键同 Task 4 `set_symbol_result`）；内部已调用 `state.set_symbol_result` 与 `state.add_event`；异常不外抛（写入 summary.error）。
  - 供测试注入的模块级函数拆分：`_wait_bars(ctx, symbol, timeframe, need, timeout_s) -> list`、`_notify_order(ctx, inner, stage2_full, symbol, timeframe, frame, state) -> None`。

- [ ] **Step 1: 失败测试**（全部用 fake，不碰网络）：

```python
# tests/server/test_service.py
"""run_symbol_analysis 编排测试：fake 数据源与 orchestrator."""
from types import SimpleNamespace
from unittest.mock import patch


def _fake_ctx(bars):
    class FakeDS:
        def __init__(self): self.subscribed = None; self.connected = False
        def connect(self): self.connected = True
        def subscribe(self, s, tf): self.subscribed = (s, tf)
        def latest_snapshot(self, n): return bars[:n]

    general = SimpleNamespace(analysis_bar_count=5, decision_confidence_threshold=0,
                              decision_stance="", structure_flip_cooldown_bars=3,
                              last_tradingview_exchange="")
    settings = SimpleNamespace(general=general,
                               provider=SimpleNamespace(model="m"),
                               feishu=SimpleNamespace(enabled=False),
                               pushplus=SimpleNamespace(enabled=False))
    return SimpleNamespace(settings=settings, logger=__import__("logging").getLogger("t"),
                           data_source=FakeDS(), client=None, assembler=None, router=None,
                           validator=None, pending_writer=None, exp_reader=None)


def _mk_bars(n):
    from pa_agent.data.base import KlineBar
    return [KlineBar(seq=i, ts_open=1700000000.0 + 900 * (n - i), open=1.0, high=2.0,
                     low=0.5, close=1.5, volume=10.0, closed=True) for i in range(n)]


def test_success_flow_with_order(monkeypatch):
    from pa_agent.server.state import ServerState
    from pa_agent.server import service

    ctx = _fake_ctx(_mk_bars(60))
    state = ServerState()
    record = SimpleNamespace(stage2_decision={"decision": {
        "order_type": "限价单", "order_direction": "做多", "trade_confidence": 66}})
    fake_orch = SimpleNamespace(submit=lambda *a, **k: record)
    notified = {}
    monkeypatch.setattr(service, "_notify_order",
                        lambda *a, **k: notified.setdefault("called", True))
    with patch("pa_agent.server.service.build_orchestrator", return_value=fake_orch):
        summary = service.run_symbol_analysis(ctx, state, "XAUUSD", "15m")
    assert summary["ok"] and summary["has_order"] and summary["direction"] == "做多"
    assert notified.get("called") is True
    assert state.snapshot()["results"]["XAUUSD"]["ok"] is True


def test_data_timeout_returns_error(monkeypatch):
    from pa_agent.server.state import ServerState
    from pa_agent.server import service

    ctx = _fake_ctx([])  # 无数据
    monkeypatch.setattr(service, "DATA_READY_TIMEOUT_S", 0.2)
    summary = service.run_symbol_analysis(ctx, ServerState(), "XAUUSD", "15m")
    assert summary["ok"] is False and summary["error"]


def test_no_order_skips_notify(monkeypatch):
    from pa_agent.server.state import ServerState
    from pa_agent.server import service

    ctx = _fake_ctx(_mk_bars(60))
    record = SimpleNamespace(stage2_decision={"decision": {"order_type": "观望"}})
    fake_orch = SimpleNamespace(submit=lambda *a, **k: record)
    called = {}
    monkeypatch.setattr(service, "_notify_order",
                        lambda *a, **k: called.setdefault("x", True))
    with patch("pa_agent.server.service.build_orchestrator", return_value=fake_orch):
        summary = service.run_symbol_analysis(ctx, ServerState(), "XAUUSD", "15m")
    assert summary["ok"] and not summary["has_order"] and "x" not in called
```

- [ ] **Step 2: 确认失败。**
- [ ] **Step 3: 实现要点**（完整流程）：

```python
"""单品种分析编排：订阅 → 等数据 → 构帧 → submit → 判定 → 推送."""
from __future__ import annotations

import threading
import time
from typing import Any

from pa_agent.server.bootstrap import build_orchestrator  # noqa: F401  (供 patch)
from pa_agent.util.threading import CancelToken, OrchestratorEvent

DATA_READY_TIMEOUT_S = 120
ANALYSIS_TIMEOUT_S = 1800

_EVENT_LABELS = {  # 与 GUI _AnalysisWorker 一致的中文文案
    OrchestratorEvent.Stage1Started: "阶段一分析中",
    OrchestratorEvent.Stage1Done: "阶段一完成",
    OrchestratorEvent.Stage2Started: "阶段二分析中",
    OrchestratorEvent.Stage2Done: "阶段二完成",
    OrchestratorEvent.RecordSaved: "记录已保存",
    OrchestratorEvent.Cancelled: "已取消",
    OrchestratorEvent.Stage1Failed: "阶段一失败",
    OrchestratorEvent.Stage2Failed: "阶段二失败",
}
```

流程（`run_symbol_analysis`）：
1. `state.set_current(symbol, "waiting_data", ...)` 由调度器负责 round/idx；service 里仅 `state.add_event` + phase 更新（给 service 增加可选参数 `round_num=0, idx=0, total=1` 简化：service 自己调 `state.set_current`）。
2. `ctx.data_source.connect()`（幂等，异常捕获重试一次）→ `subscribe(symbol, timeframe)`。
3. `_wait_bars`: 轮询 `latest_snapshot(need)`（`need = analysis_bar_count + 50 + 1`），每 3s 一次，直到返回条数 ≥ `analysis_bar_count + 1` 或超时（`DATA_READY_TIMEOUT_S`）→ 超时 raise `TimeoutError("数据未就绪")`。
4. `build_display_frame(bars, analysis_bar_count, symbol, timeframe, now_ms=now_local_ms())`；None → 报错「数据不足」。
5. `orch = build_orchestrator(ctx)`；`cancel_token = cancel_token or CancelToken()`。在**内层线程**执行 `orch.submit(frame, cancel_token, on_event, ...)`（on_event 把 `_EVENT_LABELS` 写进 `state.add_event(f"{symbol} {label}")`，Stage1Started/Stage2Started 时同步 `set_current` phase=stage1/stage2）；`join(ANALYSIS_TIMEOUT_S)`，超时则 `cancel_token.set()` + join(30) + raise TimeoutError("分析超时")。线程内异常存入 `result_box["exc"]` 重新抛出。
6. `stage2_full = record.stage2_decision or {}`；`inner = stage2_full.get("decision") or {}`。
7. `has_order = has_order_opportunity(inner, confidence_threshold=settings.general.decision_confidence_threshold)`；若 True → `state.set_current(..., "notifying", ...)` → `_notify_order(ctx, inner, stage2_full, symbol, timeframe, frame, state)`。
8. summary 组装并 `state.set_symbol_result(symbol, summary)`；`finally` 里 `state.clear_current()` 不做（由调度器管理），只保证异常路径也写 summary（`ok=False, error=str(exc)`）+ `add_event`。

`_notify_order` 复刻 `main_window._spawn_post_order_followup`（`gui/main_window.py:3984-4057`）但**同步执行**：`save_trade_record(...)`（异常仅告警）→ 用 `_TRADE_RECORDS_DIR.glob(f"{safe_sym}_{safe_tf}_*.png")` mtime 最新图 → `send_feishu_order(...)` → `pushplus_is_active(settings)` 时 `send_pushplus_order(...)`；整体 try/except 写事件日志「推送失败」。
- [ ] **Step 4: 测试通过。**
- [ ] **Step 5: 提交** — `feat: 服务端单品种分析编排（含飞书推送触发下沉）`

### Task 6: 轮巡调度器 `server/scheduler.py`

**Files:**
- Create: `pa_agent/server/scheduler.py`
- Test: `tests/server/test_scheduler.py`

**Interfaces:**
- Consumes: Task 5 `run_symbol_analysis`（通过模块属性引用，便于测试 monkeypatch `scheduler.run_symbol_analysis`）。
- Produces:
  - `parse_watch_symbols(raw: str) -> list[str]`（从 `gui/watch_rotation.py:40` 原样复制，含中文逗号/顿号/空格分隔与去重保序）
  - `WatchScheduler(ctx, state)`：`start() -> str | None`（None=成功；错误文本=失败：列表空/未配 API Key/已在运行）、`stop(timeout: float = 35.0) -> None`、属性 `running: bool`。
  - start 读取 `ctx.settings.general`：`watch_symbols`、`last_timeframe`、`watch_round_interval_min`。
  - 循环：逐品种 `run_symbol_analysis`；单品种异常→事件日志+继续；每品种前检查 stop 标志；一轮结束 `state.set_round_wait(eta)` 且 `interval>0` 时 `stop_event.wait(interval)`；`stop()` 置位 `stop_event` + 当前 `cancel_token.set()`。
  - 线程崩溃兜底：外层 try/except → `state.set_scheduler(False, error=str(exc))`。

- [ ] **Step 1: 失败测试**

```python
# tests/server/test_scheduler.py
"""轮巡调度器状态机测试（fake 分析函数）."""
import time
from types import SimpleNamespace


def _ctx(symbols="AAA, BBB", interval=0):
    general = SimpleNamespace(watch_symbols=symbols, last_timeframe="15m",
                              watch_round_interval_min=interval)
    provider = SimpleNamespace(api_key="sk-x")
    return SimpleNamespace(settings=SimpleNamespace(general=general, provider=provider),
                           logger=__import__("logging").getLogger("t"))


def test_parse_watch_symbols():
    from pa_agent.server.scheduler import parse_watch_symbols

    assert parse_watch_symbols("XAUUSD，BTCUSD、eth  xauusd") == ["XAUUSD", "BTCUSD", "eth"]


def test_rotation_order_and_skip_on_error(monkeypatch):
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    calls = []
    def fake_run(ctx, state, symbol, timeframe, **kw):
        calls.append(symbol)
        if symbol == "AAA" and len(calls) <= 2:
            raise RuntimeError("boom")  # 异常也不能中断轮巡
        return {"ok": True}
    monkeypatch.setattr(sched_mod, "run_symbol_analysis", fake_run)

    state = ServerState()
    s = sched_mod.WatchScheduler(_ctx(), state)
    assert s.start() is None
    deadline = time.time() + 5
    while len(calls) < 4 and time.time() < deadline:
        time.sleep(0.05)
    s.stop()
    assert calls[:4] == ["AAA", "BBB", "AAA", "BBB"]  # interval=0 连续轮
    assert state.snapshot()["scheduler"]["running"] is False


def test_start_validation():
    from pa_agent.server.scheduler import WatchScheduler
    from pa_agent.server.state import ServerState

    assert WatchScheduler(_ctx(symbols="  "), ServerState()).start() is not None  # 空列表
    ctx = _ctx(); ctx.settings.provider.api_key = ""
    assert WatchScheduler(ctx, ServerState()).start() is not None  # 无 API Key


def test_stop_during_round_wait(monkeypatch):
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    monkeypatch.setattr(sched_mod, "run_symbol_analysis",
                        lambda *a, **k: {"ok": True})
    s = sched_mod.WatchScheduler(_ctx(symbols="AAA", interval=60), ServerState())
    s.start()
    time.sleep(0.5)  # 进入 round_wait（60 分钟）
    t0 = time.time(); s.stop()
    assert time.time() - t0 < 5  # stop 立即生效，而非等满一轮间隔
    assert not s.running
```

- [ ] **Step 2: 确认失败。**
- [ ] **Step 3: 实现**——单 `threading.Thread(daemon=True)` + `threading.Event`；`start()` 校验后 `state.set_scheduler(True)` + `add_event("轮巡启动：…")`；主循环：

```python
while not self._stop_evt.is_set():
    for idx, sym in enumerate(symbols):
        if self._stop_evt.is_set(): break
        self._state.set_current(sym, "switching", round_num, idx, len(symbols))
        self._cancel_token = CancelToken()
        try:
            run_symbol_analysis(self._ctx, self._state, sym, timeframe,
                                cancel_token=self._cancel_token,
                                round_num=round_num, idx=idx, total=len(symbols))
        except Exception as exc:  # noqa: BLE001 — 兜底，正常错误已在 service 内消化
            self._state.add_event(f"{sym} 分析异常跳过：{exc}")
    else:
        if self._stop_evt.is_set(): break
        if interval_s > 0:
            self._state.set_round_wait(time.time() + interval_s)
            self._state.add_event(f"第 {round_num} 轮完成，等待 {interval_s//60:.0f} 分钟")
            if self._stop_evt.wait(interval_s): break
        round_num += 1
        continue
    break  # for 被 break（stop）时退出 while
```

结束时 `state.set_scheduler(False)` + `clear_current()` + `add_event("轮巡已停止")`；异常兜底 `set_scheduler(False, error=...)`。`stop()`：置位 event + `self._cancel_token and self._cancel_token.set()` + `thread.join(timeout)`。
- [ ] **Step 4: 测试通过。**
- [ ] **Step 5: 提交** — `feat: 多品种轮巡调度器（服务端版状态机）`

### Task 7: 配置脱敏与 REST API `server/api.py`

**Files:**
- Create: `pa_agent/server/api.py`
- Test: `tests/server/test_api.py`

**Interfaces:**
- Consumes: Task 3/4/5/6 全部；`mask_secret`；`save_settings/load_settings`。
- Produces:
  - `SECRET_FIELDS = (("provider", "api_key"), ("feishu", "secret"), ("feishu", "app_secret"), ("pushplus", "token"), ("tushare", "token"))`
  - `masked_settings_dict(settings) -> dict`（`model_dump()` 后对 SECRET_FIELDS 逐个 `mask_secret`；同时剔除 `provider.api_key_encrypted`）
  - `apply_settings_update(settings, payload: dict) -> Settings`（deep-merge：payload 中秘密字段为空串或等于当前掩码值 → 保留旧值；返回 `Settings.model_validate(merged)`）
  - `create_app(ctx: ServerContext, *, settings_path: Path | None = None) -> FastAPI`——内部创建 `ServerState` + `WatchScheduler`，挂到 `app.state`；路由见 spec；静态目录 `pa_agent/server/static` 挂载在 `/`（`html=True`）。

- [ ] **Step 1: 失败测试**

```python
# tests/server/test_api.py
"""REST API 测试（TestClient + fake ctx）."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from pa_agent.config.settings import Settings, save_settings
    from pa_agent.server import api as api_mod
    from pa_agent.server.bootstrap import ServerContext
    import logging

    settings = Settings()
    settings.provider.api_key = "sk-1234567890abcdef"
    settings.general.watch_symbols = "XAUUSD"
    sp = tmp_path / "settings.json"
    save_settings(settings, sp)
    # records 目录指到 tmp
    pending = tmp_path / "pending"; pending.mkdir()
    monkeypatch.setattr(api_mod, "RECORDS_PENDING_DIR", pending)
    ctx = ServerContext(settings=settings, logger=logging.getLogger("t"),
                        data_source=None, client=None, assembler=None, router=None,
                        validator=None, pending_writer=None, exp_reader=None)
    app = api_mod.create_app(ctx, settings_path=sp)
    return TestClient(app), pending, sp


def test_settings_get_masks_secret(client):
    c, _, _ = client
    data = c.get("/api/settings").json()
    key = data["provider"]["api_key"]
    assert "sk-1234567890abcdef" not in json.dumps(data)
    assert key.endswith("cdef")  # mask 保留尾部特征


def test_settings_put_empty_secret_keeps_old(client):
    c, _, sp = client
    data = c.get("/api/settings").json()
    data["provider"]["api_key"] = ""          # 空 = 不修改
    data["general"]["watch_symbols"] = "BTCUSD"
    assert c.put("/api/settings", json=data).status_code == 200
    saved = json.loads(Path(sp).read_text(encoding="utf-8"))
    assert saved["provider"]["api_key"] == "sk-1234567890abcdef"
    assert saved["general"]["watch_symbols"] == "BTCUSD"


def test_settings_put_new_secret_overwrites(client):
    c, _, sp = client
    data = c.get("/api/settings").json()
    data["provider"]["api_key"] = "sk-NEW"
    c.put("/api/settings", json=data)
    saved = json.loads(Path(sp).read_text(encoding="utf-8"))
    assert saved["provider"]["api_key"] == "sk-NEW"


def test_status_shape(client):
    c, _, _ = client
    s = c.get("/api/status").json()
    assert {"scheduler", "current", "results", "events"} <= set(s)


def test_records_list_and_detail(client):
    c, pending, _ = client
    rec = {"symbol": "XAUUSD", "timeframe": "15m",
           "stage2_decision": {"decision": {"order_type": "限价单",
                                            "order_direction": "做多",
                                            "trade_confidence": 70}}}
    (pending / "20260722_100000_XAUUSD_15m.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    items = c.get("/api/records").json()["items"]
    assert items[0]["symbol"] == "XAUUSD" and items[0]["has_order"] is True
    detail = c.get(f"/api/records/{items[0]['name']}").json()
    assert detail["stage2_decision"]["decision"]["order_direction"] == "做多"


def test_records_path_traversal_rejected(client):
    c, _, _ = client
    assert c.get("/api/records/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404, 422)


def test_watch_start_stop(client, monkeypatch):
    c, _, _ = client
    from pa_agent.server import scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "run_symbol_analysis", lambda *a, **k: {"ok": True})
    r = c.post("/api/watch/start")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.get("/api/status").json()["scheduler"]["running"] is True
    c.post("/api/watch/stop")
    assert c.get("/api/status").json()["scheduler"]["running"] is False
```

- [ ] **Step 2: 确认失败。**
- [ ] **Step 3: 实现要点：**
  - `masked_settings_dict`：`data = settings.model_dump(mode="json")`；对 SECRET_FIELDS：非空则 `mask_secret(v)`；`data["provider"].pop("api_key_encrypted", None)`。
  - `apply_settings_update`：`current = settings.model_dump(mode="json")`；递归 merge payload；秘密字段规则（空串或 == mask_secret(旧值) → 用旧值）；`Settings.model_validate(merged)`。
  - 路由（均带中文错误信息）：
    - `GET /api/status` → `state.snapshot()` + `{"round_wait_eta"}`。
    - `POST /api/watch/start` → `err = scheduler.start()`；err 非 None → 400 `{"ok": False, "error": err}`。
    - `POST /api/watch/stop` → 200。
    - `POST /api/analyze` body `{symbol, timeframe?}`：轮巡运行中 → 409；否则起 daemon 线程跑 `run_symbol_analysis`，立即返回 `{"ok": True}`。
    - `GET /api/settings` / `PUT /api/settings`：PUT 成功后 `save_settings` 至 `settings_path`，更新 `ctx.settings`，调 `rebuild_client(ctx)`；若 `general.last_data_source` 或 `last_tradingview_exchange` 变化 → `rebuild_data_source(ctx)`；422 时返回 pydantic 错误详情。
    - `POST /api/feishu/test`：构造最小测试卡片调 `pa_agent.notify.feishu_notifier` 的发送函数（实现时查看该文件选择合适入口，如仅 webhook 文本卡片），返回 `{"ok": bool, "detail": str}`。
    - `GET /api/records`：`sorted(RECORDS_PENDING_DIR.glob("*.json"), reverse=True)`；query `symbol/limit(默认50)/offset`；先按文件名倒序切片再逐个 `json.load` 提取摘要（`name, symbol, timeframe, direction, order_type, confidence, has_order, ok(无 exception 字段), ts(文件 mtime)`）；解析失败的文件跳过。**注意**：symbol 过滤需在切片前进行（先全量轻扫文件名，文件名含 symbol；文件名不可靠时 fallback 读内容）。
    - `GET /api/records/{name}`：`name` 必须匹配 `^[\w\-.]+\.json$` 且 `(RECORDS_PENDING_DIR / name).resolve()` 在 pending 目录下；返回完整 JSON（同样过 mask：记录本身已脱敏 api_key，直接返回）。
  - 静态：`app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")`——**必须最后 mount**，且 `/api` 路由优先。
- [ ] **Step 4: 测试通过。**
- [ ] **Step 5: 提交** — `feat: 服务端 REST API（配置/状态/历史/轮巡控制）`

### Task 8: 入口 `server/__main__.py` + 本地冒烟

**Files:**
- Create: `pa_agent/server/__main__.py`

**Interfaces:**
- Produces: `python -m pa_agent.server` 启动；env `PA_SERVER_HOST`（默认 `0.0.0.0`）、`PA_SERVER_PORT`（默认 `8688`）。

- [ ] **Step 1: 实现**

```python
"""服务端入口：python -m pa_agent.server"""
from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from pa_agent.server.api import create_app
    from pa_agent.server.bootstrap import bootstrap_headless

    ctx = bootstrap_headless()
    app = create_app(ctx)
    host = os.environ.get("PA_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("PA_SERVER_PORT", "8688"))
    ctx.logger.info("PA Agent 服务端启动：http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 冒烟**——`PA_SERVER_PORT=8688 .venv/bin/python -m pa_agent.server` 后台启动，`curl -s http://127.0.0.1:8688/api/status` 返回 JSON（scheduler.running=false），`curl -s http://127.0.0.1:8688/api/settings | head -c 200` 不含明文 api_key。杀掉进程。
- [ ] **Step 3: 提交** — `feat: 服务端启动入口`

### Task 9: Web 前端（`server/static/`）

**Files:**
- Create: `pa_agent/server/static/index.html`、`static/style.css`、`static/app.js`、`static/vendor/vue.esm-browser.prod.js`

**Interfaces:**
- Consumes: Task 7 的全部 API（路径与 JSON 结构以 `tests/server/test_api.py` 为准）。

- [ ] **Step 1: vendor Vue** — `curl -L -o pa_agent/server/static/vendor/vue.esm-browser.prod.js https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js`，确认文件 > 100KB 且开头含 `/**` 或合法 JS。
- [ ] **Step 2: 实现三页签单页应用。** 结构要求（实现细节可在此框架内自由发挥，但交互必须齐全）：
  - `index.html`：`<title>PA Agent 控制台</title>`；深色主题；顶栏（标题 + 轮巡状态徽标 + 启/停按钮）；三个页签「监控」「配置」「历史」；`<script type="module" src="./app.js">`，`import { createApp, reactive } from "./vendor/vue.esm-browser.prod.js"`。
  - **监控页**：运行状态卡（当前品种/阶段中文名/第几轮/队列进度）；`round_wait_eta` 存在时显示「下一轮倒计时 mm:ss」（前端本地递减）；品种结果卡片网格（方向/下单类型/信心/相对时间，`has_order` 卡片高亮描边）；事件日志列表（倒序、等宽字体）。轮询 `GET /api/status` 每 2s（`setInterval` + 页面隐藏时暂停 `document.visibilitychange`）。
  - **配置页**：读 `GET /api/settings` 填表；分四组：AI 模型（base_url、model、api_key[password 输入，placeholder 显示已存掩码值，留空=不修改]、thinking 开关、reasoning_effort 下拉 low/medium/high/max）、飞书（enabled、webhook_url、secret、app_id、app_secret、notify_on_order_only、「发送测试」按钮→`POST /api/feishu/test` 显示结果）、监控（watch_symbols 文本框+说明「逗号分隔」、last_timeframe 下拉 5m/15m/30m/1h/4h/1d、watch_round_interval_min 数字、analysis_bar_count 数字、decision_confidence_threshold 数字 0-100）、数据源（last_data_source 下拉 tradingview/eastmoney/akshare/tushare/yfinance、last_tradingview_exchange 文本）。「保存配置」→ `PUT /api/settings`，成功 toast「已保存」，422 显示字段错误；秘密字段留空提交（不修改语义在后端）。
  - **历史页**：`GET /api/records?symbol=&limit=50` 列表（时间/品种/周期/方向/类型/信心，has_order 行高亮）；顶部品种筛选输入；点击行 → `GET /api/records/{name}` 抽屉展示：阶段一诊断 JSON（格式化）、阶段二 decision 字段表（中文标签：方向/方式/入场/止损/TP1/TP2/信心/理由）、原始 JSON 折叠块。
  - 所有 fetch 封装 `api(path, opts)`：非 2xx 抛错并 toast 错误文本。
- [ ] **Step 3: 手动验证** — 起服务端（同 Task 8），浏览器打开 `http://127.0.0.1:8688/`：三页签可切换；配置页能读到并保存（改 watch_symbols 后 GET 确认）；监控页显示「未运行」；历史页显示 records/pending 既有样本记录。
- [ ] **Step 4: 提交** — `feat: Web 管理页（监控/配置/历史）`

### Task 10: Docker 部署文件与部署文档

**Files:**
- Create: `Dockerfile.server`、`docker-compose.server.yml`、`docs/服务端部署指南.md`
- Modify: `.gitignore`（追加 `data/` 一行，若尚未忽略）

- [ ] **Step 1: `Dockerfile.server`**

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app

# git: tvdatafeed 为 git 依赖；tzdata: 时区
RUN apt-get update && apt-get install -y --no-install-recommends git tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY pa_agent ./pa_agent
COPY prompt_engineering ./prompt_engineering

RUN pip install --no-cache-dir uv \
    && uv pip install --system ".[server]" \
    && pip uninstall -y PyQt6 PyQt6-Qt6 PyQt6-sip pyqtgraph || true

EXPOSE 8688
CMD ["python", "-m", "pa_agent.server"]
```

（`pip uninstall PyQt6` 是镜像瘦身：服务端代码不 import Qt，卸载安全。若 `uv pip install --system` 因 git 依赖失败，回退 `pip install ".[server]"`。`prompt_engineering/` 若目录名不同以仓库实际为准，另需确认 `experience/`、`config/settings.example.json` 是否要 COPY——`experience/` 由 volume 提供，`config/` 由 volume 提供，无需 COPY。）
- [ ] **Step 2: `docker-compose.server.yml`**

```yaml
services:
  pa-agent-server:
    build:
      context: .
      dockerfile: Dockerfile.server
    container_name: pa-agent-server
    ports:
      - "8688:8688"
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - ./data/config:/app/config
      - ./data/records:/app/records
      - ./data/experience:/app/experience
      - ./data/trade_records:/app/trade_records
      - ./data/logs:/app/logs
    restart: unless-stopped
```

- [ ] **Step 3: 本地构建验证**（若本机有 Docker）：`docker compose -f docker-compose.server.yml up --build -d` → `curl http://127.0.0.1:8688/api/status` → `docker compose -f docker-compose.server.yml down`。无 Docker 则跳过并在文档注明「未在本机验证构建」。
- [ ] **Step 4: `docs/服务端部署指南.md`**——中文分步：① NAS 装 Docker；② `git clone` 或上传仓库；③ `docker compose -f docker-compose.server.yml up -d --build`；④ 浏览器开 `http://NAS_IP:8688` 配置模型与飞书；⑤ 升级（`git pull && docker compose ... up -d --build`）；⑥ 常见问题（端口冲突改 compose 映射、TradingView 连接失败看 logs、MT5 不支持说明、数据在 `./data/` 下备份即可）。
- [ ] **Step 5: 提交** — `feat: Docker 部署文件与 NAS 部署指南`

### Task 11: 回归验证与收尾

- [ ] **Step 1:** 全量服务端测试：`.venv/bin/python -m pytest tests/server/ -v` 全绿。
- [ ] **Step 2:** 既有测试回归：`.venv/bin/python -m pytest tests/ -x -q`（若仓库既有测试因环境缺 MT5 等本来就跳过/失败，记录基线对比，不引入新失败）。
- [ ] **Step 3:** GUI 冒烟：`.venv/bin/python -c "import pa_agent.gui.order_opportunity as m; print(m.has_order_opportunity({'order_type':'限价单'}))"` 输出 True（验证 re-export 未破坏 GUI 导入链）。
- [ ] **Step 4:** 服务端整链冒烟（真实 TradingView 数据源、不配 API Key）：起服务端 → 前端手动触发 `/api/analyze`，观察事件日志走到「阶段一分析中」后因无 Key 失败并被正确记录为 summary.error，进程不崩。
- [ ] **Step 5:** 收尾提交 + 使用 superpowers:verification-before-completion 核对后向用户汇报。
