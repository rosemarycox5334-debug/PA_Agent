# 服务端二期实施计划：并发分析 + 品种详情页

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 轮巡改为并发池（默认 2，可配 1–8）；Web 端新增品种详情页（K 线图 + 决策价位标注 + 实时推理流）。

**Architecture:** 每个并发槽位独立数据源实例（`DataSourcePool` 借还语义，根治单订阅串号）；`ThreadPoolExecutor` 逐轮并发、轮 barrier 后等间隔；实时推理 token 写入 `ServerState` 限长缓冲，前端 1s 轮询 `/api/live/{symbol}`；K 线图用 vendor 的 lightweight-charts，数据取自分析记录 `kline_data`（前端自算 EMA20）。

**Tech Stack:** Python threading/ThreadPoolExecutor、FastAPI、Vue 3（无构建）、lightweight-charts standalone。

**Spec:** `docs/superpowers/specs/2026-07-22-server-phase2-design.md`

## Global Constraints

- 一期约束继续有效：服务端不得 import Qt；中文文案；测试 `tests/server/`；提交信息中文 + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- `settings.py` 本次允许小改（加 `watch_concurrency` 字段，紧邻 watch_symbols）。
- `state.current` 快照结构改为字典 `{symbol: {...}}`，一期的 `set_current/clear_current` 直接移除并同步更新所有调用点与测试（无外部兼容负担）。
- 已核实 `kline_data` 元素：`{seq(1=最新), ts_open(毫秒), open, high, low, close, volume, ...}`，无 ema 列。lightweight-charts 需 oldest-first、time 秒。

---

### Task 1: `watch_concurrency` 配置字段

**Files:** Modify `pa_agent/config/settings.py`（watch_round_interval_min 之后）、`pa_agent/server/static/index.html`（监控设置区）。
**Produces:** `settings.general.watch_concurrency: int`（默认 2，ge=1 le=8）。

- [ ] settings.py 加字段：

```python
    #: 多品种轮巡监控：同时分析的品种数（并发池大小）
    watch_concurrency: int = Field(default=2, ge=1, le=8)
```

- [ ] index.html 监控设置 form-grid 内、轮巡间隔之后加：

```html
          <label>同时分析品种数（1-8）
            <input v-model.number="cfg.general.watch_concurrency" type="number" min="1" max="8">
          </label>
```

- [ ] 验证：`.venv/bin/python -c "from pa_agent.config.settings import Settings; print(Settings().general.watch_concurrency)"` 输出 2。提交 `feat: 轮巡并发数配置字段`。

### Task 2: `ServerState` 多 current + 实时推理缓冲

**Files:** Modify `pa_agent/server/state.py`、`tests/server/test_state.py`。
**Produces:**
- `set_symbol_phase(symbol, phase, round_num)` / `clear_symbol(symbol)` / `clear_all_current()`；`snapshot()["current"]: dict[str, {"phase", "round", "started_ts"}]`（started_ts 首次进入时记，phase 更新不重置）
- `reset_live(symbol)`、`append_live(symbol, stage, kind, chunk)`（stage ∈ stage1/stage2，kind ∈ reasoning/content，每流限长 16384 字符丢头部）、`get_live(symbol) -> dict | None`（键：`stage, stage1_reasoning, stage1_content, stage2_reasoning, stage2_content, seq, running`；stage=最近 append 的 stage；seq 每次 append +1；running=symbol 在 current 中）
- 移除 `set_current/clear_current/set_round_wait 保持`（set_round_wait 不变；进入新 phase 不再清 round_wait_eta——改由 `clear_all_current` 时机与调度器显式控制：调度器每轮开始调 `state.set_round_wait(None)`；`set_round_wait` 改为可接受 None）

- [ ] **测试**（替换 test_state.py 中 current 相关用例，保留事件/线程用例）：

```python
def test_multi_current_and_live():
    from pa_agent.server.state import ServerState

    st = ServerState()
    st.set_symbol_phase("AAA", "stage1", 1)
    st.set_symbol_phase("BBB", "waiting_data", 1)
    snap = st.snapshot()
    assert set(snap["current"]) == {"AAA", "BBB"}
    assert snap["current"]["AAA"]["phase"] == "stage1"
    started = snap["current"]["AAA"]["started_ts"]
    st.set_symbol_phase("AAA", "stage2", 1)
    assert st.snapshot()["current"]["AAA"]["started_ts"] == started  # 不重置
    st.clear_symbol("AAA")
    assert "AAA" not in st.snapshot()["current"]
    st.clear_all_current()
    assert st.snapshot()["current"] == {}


def test_live_buffer_cap_and_seq():
    from pa_agent.server import state as state_mod
    from pa_agent.server.state import ServerState

    st = ServerState()
    st.reset_live("AAA")
    st.append_live("AAA", "stage1", "reasoning", "abc")
    st.append_live("AAA", "stage2", "content", "x" * 20000)
    live = st.get_live("AAA")
    assert live["stage"] == "stage2"
    assert live["stage1_reasoning"] == "abc"
    assert len(live["stage2_content"]) == state_mod.LIVE_CAP  # 丢头部保尾部
    assert live["stage2_content"].endswith("x")
    assert live["seq"] == 2
    assert st.get_live("NOPE") is None
```

- [ ] 确认失败 → 实现（`LIVE_CAP = 16384`；current 字典；live dict per symbol；running 由 current 派生）→ 测试通过 → 提交 `feat: 状态容器支持并发 current 与实时推理缓冲`。

### Task 3: 数据源池 `server/ds_pool.py`

**Files:** Create `pa_agent/server/ds_pool.py`、`tests/server/test_ds_pool.py`。
**Produces:** `DataSourcePool(settings, size)`：`acquire(timeout=30) -> ds`（空则惰性创建，实例总数 ≤ size，超时抛 TimeoutError）、`release(ds)`、`close_all()`、`rebuild(settings)`（close_all + 重置，下次 acquire 按新配置建）。内部用 `_create_data_source_from_settings`（bootstrap 已有）。

- [ ] **测试**：

```python
from types import SimpleNamespace
from unittest.mock import patch


def _settings():
    return SimpleNamespace(general=SimpleNamespace(
        last_data_source="tradingview", last_tradingview_exchange=""))


class _FakeDS:
    n = 0
    def __init__(self):
        _FakeDS.n += 1
        self.id = _FakeDS.n
        self.closed = False
    def disconnect(self):
        self.closed = True


def test_acquire_distinct_and_reuse():
    from pa_agent.server.ds_pool import DataSourcePool

    with patch("pa_agent.server.ds_pool._create_data_source_from_settings",
               side_effect=lambda s: _FakeDS()):
        pool = DataSourcePool(_settings(), size=2)
        a, b = pool.acquire(), pool.acquire()
        assert a is not b
        pool.release(a)
        assert pool.acquire() is a  # 复用而非新建
        import pytest
        with pytest.raises(TimeoutError):
            pool.acquire(timeout=0.1)  # 池空且已达上限


def test_rebuild_closes_and_recreates():
    from pa_agent.server.ds_pool import DataSourcePool

    with patch("pa_agent.server.ds_pool._create_data_source_from_settings",
               side_effect=lambda s: _FakeDS()):
        pool = DataSourcePool(_settings(), size=1)
        a = pool.acquire(); pool.release(a)
        pool.rebuild(_settings())
        assert a.closed
        assert pool.acquire() is not a
```

- [ ] 确认失败 → 实现（`queue.Queue` 空闲队列 + 计数锁；release 放回队列；close_all 逐个 disconnect 吞异常）→ 通过 → 提交 `feat: 数据源实例池（并发分析实例隔离）`。

### Task 4: service 并发适配

**Files:** Modify `pa_agent/server/service.py`、`tests/server/test_service.py`。
**Produces:** `run_symbol_analysis(..., data_source=None)`；模块级 `_CHART_LOCK`（包住 `save_trade_record`）；`orch.submit` 接入 4 个流式回调写 `state.append_live`；分析开始处 `state.reset_live(symbol)`；`state.set_current` 调用点全部替换为 `set_symbol_phase`，结束（finally）`clear_symbol(symbol)`。

- [ ] **测试**（新增；既有用例中 set_current 断言同步改 current dict）：

```python
def test_injected_data_source_used(monkeypatch):
    from pa_agent.server import service
    from pa_agent.server.state import ServerState

    ctx = _fake_ctx([])          # ctx.data_source 无数据
    inj = _fake_ctx(_mk_bars(60)).data_source   # 注入的有数据
    record = SimpleNamespace(stage2_decision={"decision": {"order_type": "观望"}},
                             exception=None)
    with patch("pa_agent.server.service.build_orchestrator",
               return_value=SimpleNamespace(submit=lambda *a, **k: record)):
        summary = service.run_symbol_analysis(
            ctx, ServerState(), "XAUUSD", "15m", data_source=inj)
    assert summary["ok"] and inj.subscribed == ("XAUUSD", "15m")


def test_live_stream_callbacks(monkeypatch):
    from pa_agent.server import service
    from pa_agent.server.state import ServerState

    ctx = _fake_ctx(_mk_bars(60))
    state = ServerState()

    def fake_submit(frame, token, on_event, **kw):
        kw["on_stage1_reasoning"]("思考中")
        kw["on_stage2_content"]("结论")
        return SimpleNamespace(
            stage2_decision={"decision": {"order_type": "观望"}}, exception=None)

    with patch("pa_agent.server.service.build_orchestrator",
               return_value=SimpleNamespace(submit=fake_submit)):
        service.run_symbol_analysis(ctx, state, "XAUUSD", "15m")
    live = state.get_live("XAUUSD")
    assert live["stage1_reasoning"] == "思考中"
    assert live["stage2_content"] == "结论"
```

- [ ] 确认失败 → 实现 → 全部 service 测试通过 → 提交 `feat: 分析编排支持注入数据源与实时推理流`。

### Task 5: 调度器并发改造

**Files:** Modify `pa_agent/server/scheduler.py`、`tests/server/test_scheduler.py`。
**Produces:** `WatchScheduler(ctx, state, ds_pool)`（新参 ds_pool）；每轮 `ThreadPoolExecutor(max_workers=concurrency)`：任务=借 ds → `run_symbol_analysis(..., data_source=ds, cancel_token=tok)` → finally 还 ds + `state.clear_symbol`；`self._tokens: dict[str, CancelToken]` 锁保护；`stop()` 置事件 + set 全部 token + `pool.shutdown(wait=False, cancel_futures=True)` + join；轮全收齐后 `set_round_wait` 等间隔；每轮开始 `state.set_round_wait(None)`。

- [ ] **测试**（更新既有 + 新增并发峰值/stop 全取消）：

```python
def test_concurrency_cap(monkeypatch):
    import threading, time
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    peak = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def fake_run(ctx, state, symbol, timeframe, **kw):
        with lock:
            peak["cur"] += 1; peak["max"] = max(peak["max"], peak["cur"])
        time.sleep(0.2)
        with lock:
            peak["cur"] -= 1
        return {"ok": True}

    monkeypatch.setattr(sched_mod, "run_symbol_analysis", fake_run)
    ctx = _ctx(symbols="A,B,C,D", interval=60)
    ctx.settings.general.watch_concurrency = 2
    s = sched_mod.WatchScheduler(ctx, ServerState(), _fake_pool())
    s.start()
    time.sleep(1.0)
    s.stop()
    assert peak["max"] == 2  # 并发不超配置、且确实并发


def test_stop_cancels_all_active(monkeypatch):
    import time
    from pa_agent.server import scheduler as sched_mod
    from pa_agent.server.state import ServerState

    cancelled = []

    def fake_run(ctx, state, symbol, timeframe, cancel_token=None, **kw):
        cancel_token.wait(10)
        cancelled.append(symbol)
        return {"ok": False}

    monkeypatch.setattr(sched_mod, "run_symbol_analysis", fake_run)
    ctx = _ctx(symbols="A,B", interval=0)
    ctx.settings.general.watch_concurrency = 2
    s = sched_mod.WatchScheduler(ctx, ServerState(), _fake_pool())
    s.start(); time.sleep(0.3)
    t0 = time.time(); s.stop()
    assert time.time() - t0 < 5 and set(cancelled) == {"A", "B"}
```

（`_fake_pool()`：`SimpleNamespace(acquire=lambda timeout=30: SimpleNamespace(), release=lambda ds: None)`。既有 `_ctx` 加 `watch_concurrency=2` 字段。）
- [ ] 确认失败 → 实现 → 全部通过 → 提交 `feat: 轮巡并发池调度`。

### Task 6: API 适配（live 端点、latest、池接线）

**Files:** Modify `pa_agent/server/api.py`、`pa_agent/server/bootstrap.py`（create_app 组装 DataSourcePool）、`tests/server/test_api.py`。
**Produces:** `create_app` 内建 `ds_pool = DataSourcePool(ctx.settings, size=8)`（上限即字段 le）传给 scheduler；`/api/analyze` 借池（借不到 → 409「数据源繁忙」）；PUT settings 的 ds_changed 分支改调 `ds_pool.rebuild(new_settings)`（守卫条件不变）；新增 `GET /api/live/{symbol}`（`state.get_live` None → 404）；`GET /api/records` 加 `latest: int = 0`（=1 时等价 limit=1 offset=0 且必须带 symbol，缺 symbol → 400）。

- [ ] **测试**（新增；status 相关既有断言按 current dict 调整）：

```python
def test_live_endpoint(client):
    c, _, _ = client
    assert c.get("/api/live/XAUUSD").status_code == 404
    app_state = c.app.state  # noqa: F841  (TestClient 暴露 app)
    from pa_agent.server.state import ServerState  # 类型提示用
    st = c.app.state.server_state
    st.reset_live("XAUUSD")
    st.append_live("XAUUSD", "stage1", "reasoning", "推理片段")
    body = c.get("/api/live/XAUUSD").json()
    assert body["stage1_reasoning"] == "推理片段" and body["seq"] == 1


def test_records_latest_shortcut(client):
    c, pending, _ = client
    _write_record(pending, "20260722_100000_XAUUSD_15m.json")
    _write_record(pending, "20260722_110000_XAUUSD_15m.json")
    assert c.get("/api/records", params={"latest": 1}).status_code == 400
    body = c.get("/api/records", params={"latest": 1, "symbol": "XAUUSD"}).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["name"].startswith("20260722_110000")
```

- [ ] 确认失败 → 实现 → 全部 API 测试通过 → 提交 `feat: 实时推理端点与记录 latest 快捷参数`。

### Task 7: 前端详情页 + 并发卡片

**Files:** Create `pa_agent/server/static/vendor/lightweight-charts.standalone.production.js`（`curl -L -o ... https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js`，确认 >40KB）、`pa_agent/server/static/chart.js`（图表组件模块）。Modify `index.html`、`app.js`、`style.css`。

**契约与要求：**
- `chart.js` 导出 `renderKlineChart(el, klineData, decision)`：接收记录的 `kline_data`（seq=1 最新）与 `decision`；内部 reverse 成 oldest-first、`time = ts_open/1000`；蜡烛 + EMA20（`ema[i] = i ? a*close+(1-a)*ema[i-1] : close`，`a=2/21`）+ `createPriceLine` 画 entry(蓝)/stop(红)/tp1/tp2(绿) 带中文 title；深色主题（背景 `#1b1e27`、网格 `#2e3342`、涨 `#34c98e` 跌 `#f05c5c`）；返回 `{destroy()}`。
- 监控页「当前分析」区：`v-for="(cur, sym) in status.current"` 卡片（品种、`phaseLabel(cur.phase)`、第 `cur.round` 轮、已用时 `relTime(cur.started_ts)`），点击 `openDetail(sym)`；顶栏徽标运行时显示 `轮巡运行中 · 并发 ${Object.keys(status.current).length}`。
- 详情视图 `tab === 'detail'`：数据加载 `openDetail(symbol)` → `GET /api/records?latest=1&symbol=` 取最新记录名 → `GET /api/records/{name}` 全量 → 渲染图 + 决策表；无记录时图区显示「等待首次分析完成」。live 轮询：详情打开期间每 1s `GET /api/live/{symbol}`，`seq` 变化才更新 DOM，`running` 为真时推理区自动滚底；离开视图/页面隐藏时停止轮询。推理区四块折叠（阶段一/二 × 思考/输出），进行中 stage 自动展开。返回按钮回监控页。
- 历史详情抽屉：决策表上方插入 K 线图（同 `renderKlineChart`，用该记录数据）。

- [ ] vendor 下载并确认文件头合法 JS。
- [ ] 实现三处前端改动。
- [ ] 提交 `feat: 品种详情页（K线图+实时推理）与并发监控卡片`。

### Task 8: 浏览器手动验证

- [ ] `preview_start` 起本地服务端；历史页点一条 002241 记录 → 抽屉内 K 线图渲染、价位线可见（该记录为「不下单」则无价位线，属正常——另点或构造有下单记录验证价位线）。
- [ ] 监控卡片/详情页：无真实轮巡时用 `/api/analyze` 触发单品种（本机有 Key 会真实调 LLM——用 002241 East Money 源 + 真实 Key 跑一次完整分析验证实时推理流滚动；若不想耗 token，用假 Key 的隔离配置起服务端，验证「等待数据→阶段一→失败」的 live 流与卡片状态迁移即可）。
- [ ] 控制台无报错。修补样式问题后提交 `fix: 前端细节修补`（如无改动跳过）。

### Task 9: 回归 + 审查

- [ ] `tests/server/` 全绿；GUI 冒烟 `python -c "import pa_agent.main"`。
- [ ] 沿一期模式跑多智能体对抗审查工作流（维度：并发正确性/数据串号回归/前端-API 契约/资源泄漏），确认项修复后提交。

### Task 10: 合并与出包

- [ ] 合并到 main；`docker buildx build --platform linux/amd64 ... --load` 重建 → 容器冒烟（status/首页/py 导入）→ `docker save | gzip > pa-agent-server-amd64.tar.gz`。
- [ ] 更新 `docs/服务端部署指南.md` 功能一览（并发与详情页一句话）并提交。
