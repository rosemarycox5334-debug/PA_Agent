# PA Agent 服务端改造设计（NAS 部署 + Web 管理页）

日期：2026-07-21
状态：已确认（方案 A）

## 目标

把现有本地 PyQt6 股票分析智能体扩展出一个可部署在 NAS 上的 headless 后端：

- 后台多品种轮巡监控：按配置的品种列表逐个执行两阶段 LLM 分析，命中下单机会时推送飞书
- Web 管理页面（内网、免登录）：配置管理、监控状态面板、历史记录浏览
- 桌面 GUI 保持不变、继续可用；改动以新增模块为主，便于持续合并上游更新

非目标（YAGNI）：

- Web 端 K 线图表展示（后续迭代再加）
- 多用户 / 登录鉴权
- MT5 数据源的服务端支持（仅 Windows，容器内不可用）

## 方案选型

采用 **方案 A：单容器一体化**。FastAPI 同时提供 REST API 与静态前端；前端为无构建
Vue 3 单页（vendor 进仓库）。备选的 Vite 双工程 / noVNC 方案因过度设计或不满足
服务化需求被否决。

## 架构

```
NAS Docker 容器（python:3.12-slim, TZ=Asia/Shanghai）
┌───────────────────────────────────────────────┐
│ uvicorn + FastAPI  :8688                      │
│  ├─ /              静态前端（Vue 3 单页）      │
│  ├─ /api/*         REST API                   │
│  └─ WatchScheduler 后台线程（轮巡状态机）      │
│        └─ AnalysisService（单次分析编排）      │
│             ├─ DataSource（TradingView/东财…） │
│             ├─ TwoStageOrchestrator（复用）    │
│             ├─ PendingWriter 落盘（复用）      │
│             └─ 飞书/PushPlus 推送（复用）      │
│ volumes: config/ records/ experience/         │
│          trade_records/ logs/                 │
└───────────────────────────────────────────────┘
```

核心引擎全部复用：`orchestrator/two_stage.py`（纯回调、无 Qt）、`ai/`（LLM 客户端与
三级网络 fallback）、`data/` 各数据源、`records/`（JSON/CSV/PNG 落盘，matplotlib Agg
无头渲染）、`notify/`（纯 requests）。

## 新增模块（`pa_agent/server/`）

| 文件 | 职责 |
|---|---|
| `bootstrap.py` | headless 装配：复刻 `AppContext.bootstrap()` 的接线，但不创建 `EventBus`、`SessionTokenLedger`（仅有的两个 Qt 组件），`PendingWriter(event_bus=None)`；不做启动时预订阅（由调度器按品种订阅） |
| `service.py` | `AnalysisService`：订阅品种 → 等数据就绪 → 构建 `KlineFrame` → `orchestrator.submit()` → 用 `has_order_opportunity()`（含置信度阈值）判定 → 飞书/PushPlus 推送（带最新图表 PNG） |
| `scheduler.py` | `WatchScheduler`：单后台线程状态机 `idle → switching → waiting_data → analyzing → round_wait`；逐品种执行、失败跳过、超时保护（借鉴 `gui/watch_rotation.py` 的 60s 切换 / 120s 数据 / 1800s 分析超时）；一轮结束后等待 `watch_round_interval_min` 分钟；支持 start/stop |
| `state.py` | 运行状态快照（线程安全）：调度器状态、当前品种与阶段、每品种最近结论摘要、下一轮倒计时、事件日志环形缓冲（约 200 条） |
| `api.py` | FastAPI 路由（见下） |
| `__main__.py` | `python -m pa_agent.server` 入口，uvicorn 启动 |
| `static/` | 前端页面 |

## 现有文件的两处小改

1. `pyproject.toml`：新增 `[project.optional-dependencies] server = ["fastapi>=0.111", "uvicorn>=0.30"]`
2. 抽取纯函数 `has_order_opportunity` / `format_order_alert_message` 至新模块
   `pa_agent/notify/order_opportunity.py`；`gui/order_opportunity.py` 改为从该模块
   re-export（保持 GUI 兼容，改动约两行）。原 GUI 文件顶部 `from PyQt6...` 导致服务端
   无法直接 import，故必须抽取。

## REST API

| 方法与路径 | 说明 |
|---|---|
| `GET /api/status` | 调度器状态、当前品种/阶段、本轮队列、每品种最近结论、下一轮倒计时、事件日志（前端 2s 轮询此接口） |
| `POST /api/watch/start` / `POST /api/watch/stop` | 启停轮巡 |
| `POST /api/analyze` | 手动触发单品种分析 `{symbol, timeframe}`（轮巡运行中返回 409） |
| `GET /api/settings` | 全量配置；`api_key`、`app_secret`、`secret` 等敏感字段脱敏（`****` + 尾 4 位） |
| `PUT /api/settings` | 保存配置；敏感字段若仍为掩码值则保留旧值不覆盖；保存后热更新（重建 client/data source，调度器下一品种生效） |
| `POST /api/feishu/test` | 发送飞书测试消息，返回成功/失败详情 |
| `GET /api/records?symbol=&limit=&offset=` | 历史记录摘要列表（扫描 `records/pending/*.json`，按文件名时间倒序） |
| `GET /api/records/{name}` | 单条完整记录（校验 name 合法性，防路径穿越） |

## 前端（`server/static/`，中文，移动端可用）

无构建：`index.html` + ES module `app.js` + `vendor/vue.esm-browser.prod.js`（vendor
进仓库，不依赖外网 CDN）+ 手写 CSS（深色主题，与桌面版观感一致）。三个页签：

1. **监控面板**：启停开关、运行状态卡（当前品种、阶段、进度）、品种卡片网格
   （最近方向/信心/下单机会标记/时间）、下一轮倒计时、事件日志流
2. **配置**：分区表单——模型（base_url、model、api_key、thinking、reasoning_effort）、
   飞书（enabled、webhook_url、secret、app_id、app_secret、notify_on_order_only、
   测试按钮）、监控（watch_symbols、K 线周期、watch_round_interval_min、数据源
   下拉[tradingview/eastmoney/akshare/tushare/yfinance]、TradingView 交易所、
   置信度阈值 `general.decision_confidence_threshold`）
3. **历史**：记录列表（时间/品种/周期/方向/下单机会），按品种筛选，点击展开详情
   （阶段一诊断、阶段二决策字段表、推理文本）

## 部署（Docker）

- `Dockerfile.server`：`python:3.12-slim` + uv → `uv pip install .[server]`；
  含 PyQt6 依赖但服务端代码永不 import（可接受的镜像体积代价，换取与上游依赖
  声明一致）；`CMD python -m pa_agent.server`
- `docker-compose.server.yml`：端口 `8688`，`TZ=Asia/Shanghai`，volumes：
  `./data/config:/app/config`、`./data/records:/app/records`、
  `./data/experience:/app/experience`、`./data/trade_records:/app/trade_records`、
  `./data/logs:/app/logs`（利用 `paths.py` 全部锚定 PROJECT_ROOT 的特性，
  路径代码零改动）
- 文件名带 `.server` 后缀，避免与上游未来可能新增的 Dockerfile 冲突
- 服务端 settings.json 与桌面 GUI 的完全独立（各自的 config 目录），无并发写冲突

## 错误处理

- 单品种失败（数据源超时 / LLM 异常 / 校验失败）：记入事件日志，跳过，继续下一品种
- LLM 网络错误：沿用 `two_stage.py` 现有三级 fallback
- 调度线程未捕获异常：置 `error` 状态并展示于面板，可手动重新 start
- 配置保存校验：Pydantic 校验失败返回 422 与字段错误信息

## 测试

- `tests/server/test_scheduler.py`：状态机单测（fake service，验证轮巡顺序、失败
  跳过、轮间隔、stop 响应）
- `tests/server/test_api.py`：FastAPI TestClient——settings 脱敏/掩码回写、records
  列表与详情、路径穿越防护、status 结构
- `tests/server/test_order_opportunity_move.py`：抽取后 GUI re-export 兼容性
- 本地冒烟：无 Docker 直接 `python -m pa_agent.server`，浏览器验证三页面
