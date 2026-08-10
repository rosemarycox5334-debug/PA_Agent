# PA Agent 服务端二期设计：并发分析 + 品种详情页

日期：2026-07-22
状态：已确认
前置：一期设计 `2026-07-21-server-backend-design.md`（已落地并合并 main）

## 目标

1. **并发分析**：轮巡由逐品种串行改为并发池（默认 2 路，可配 1–8），一轮内
   多品种同时分析，全部完成后统一等轮间隔。
2. **品种详情页**：Web 端新增分析详情视图——K 线图（蜡烛 + EMA20 + 决策价位
   标注）与实时推理过程（分析进行中流式文本），从监控卡片与历史记录进入。
3. 一次性交付：完成后合并 main 并重出 amd64 镜像包。

非目标：追问对话、决策树可视化（三期候选）；WebSocket/SSE 推流（内网 1s
轮询足够）；独立行情通道（图表数据全部取自分析记录快照）。

## 先决：本地未提交工作正式入库

服务端已依赖 `GeneralSettings.watch_symbols / watch_round_interval_min`，这两个
字段目前只存在于用户工作区未提交的 `settings.py` 修改中——从 git 内容构建的
部署（源码 tar 包）会因 `extra="ignore"` 丢弃已保存的监控列表。二期第一步把
以下本地改动正式提交（用户已确认）：

- `pa_agent/config/settings.py`（watch 字段、TradingView 默认源）
- `pa_agent/data/factory.py`（数据源顺序与默认值）
- `pa_agent/gui/main_window.py`（轮巡菜单、屏幕自适应）
- 新文件 `pa_agent/gui/watch_rotation.py`、`watch_rotation_dialog.py`、
  `运行智能体.command`

## 子项目 A：并发分析

### 配置

`GeneralSettings` 新增 `watch_concurrency: int = Field(default=2, ge=1, le=8)`。
配置页「监控设置」加数字输入「同时分析品种数」。

### 数据源池（`server/ds_pool.py`）

硬约束：`TradingViewSource`/东财源均为单订阅设计（subscribe 覆盖当前品种，
`latest_snapshot` 返回最后订阅品种），共享实例并发必然数据串号。方案：

- `DataSourcePool(settings, size)`：持有 `size` 个独立数据源实例（惰性创建、
  常驻复用连接）；`acquire() -> DataSource` / `release(ds)`（借还语义，
  `queue.Queue` 实现）；`close_all()`；`rebuild(settings)`（配置变更时整池
  重建，沿用一期「分析进行中禁止热切数据源（409）」的守卫）。
- 手动分析（/api/analyze）也从池里借，废弃直接用 `ctx.data_source` 的路径；
  `ctx.data_source` 保留用于启动时连通性自检，不再参与分析。

### 调度器改造（`server/scheduler.py`）

- 每轮：`ThreadPoolExecutor(max_workers=concurrency)` 提交全部品种；
  每个任务：从池借数据源 → `run_symbol_analysis(..., data_source=ds)` →
  归还。轮内品种全部结束（`as_completed`）后统一等 `watch_round_interval_min`。
- stop：置 stop 事件 + set **所有**活动任务的 cancel_token +
  `pool.shutdown(cancel_futures=True)`；未开始的任务直接跳过。
- token 管理：`{symbol: CancelToken}` 字典，锁保护（沿一期修复的模式）。

### 服务层（`server/service.py`）

- `run_symbol_analysis(..., data_source=None)`：传入则用之（并发路径），
  None 回落 `ctx.data_source`（兼容既有测试）。
- `_notify_order` 的 `save_trade_record`（matplotlib pyplot 非线程安全）外加
  模块级 `_CHART_LOCK` 串行化。
- 推理流回调接入：`orch.submit` 传 `on_stage1_reasoning/on_stage1_content/
  on_stage2_reasoning/on_stage2_content`，写入 `state` 的实时缓冲（见 B）。

### 状态（`server/state.py`）

- `current` 由单对象改为**字典** `{symbol: {phase, round, started_ts}}`：
  `set_symbol_phase(symbol, phase, round_num)` / `clear_symbol(symbol)` /
  `clear_all_current()`；`snapshot()["current"]` 返回该字典（空字典 = 空闲）。
  一期的 `set_current/clear_current` 移除（服务端内部 API，无外部兼容负担；
  前端与测试同步更新）。
- 实时推理缓冲：`append_live(symbol, stage, kind, chunk)`（kind ∈
  reasoning/content；每 symbol 每流限长 16KB，超出丢头部）；
  `get_live(symbol) -> {stage, stage1_reasoning, stage1_content,
  stage2_reasoning, stage2_content, seq}`（seq 单调递增供前端判断有无更新）；
  `reset_live(symbol)` 在每次分析开始时调用，结束后缓冲保留（可回看最后一次）。

### 前端监控页

- 「当前分析」区改为**卡片列表**：每个进行中品种一张卡（品种、阶段中文名、
  第几轮），点击卡片进入详情页。
- 状态徽标显示「并发 x/N」。

## 子项目 A2：仅交易时段轮巡（节省 token）

- `GeneralSettings` 新增：`watch_trading_hours_only: bool = False`（开关）、
  `watch_trading_hours: str = "09:30-12:00, 13:00-16:00"`（逗号分隔的
  HH:MM-HH:MM 时段串，北京时间，周一至周五生效；默认为 A 股+港股并集）。
- `scheduler.py` 纯函数：`parse_trading_hours(raw) -> list[(start_min, end_min)]`
  （非法段忽略）；`in_trading_hours(windows, now=None) -> bool`（周六日 False；
  windows 空 → True）；`next_trading_open(windows, now=None) -> float`（下一个
  开盘时刻 epoch，供前端倒计时）。
- 调度循环：开关开启且当前不在时段 → `state.set_market_closed(next_open)` +
  事件日志（每次进入休市只记一条），以 ≤60s 粒度 `stop_evt.wait` 等待，恢复
  后 `set_market_closed(None)` 继续轮巡；stop 随时可打断。
- `state` 新增 `market_closed_until: float | None`（snapshot 输出同名键）。
- 前端：监控页休市卡片（「休市中，HH:MM 恢复轮巡」倒计时）；配置页开关 +
  时段输入框。手动分析（/api/analyze）不受时段限制。
- 节假日不做日历判定（YAGNI）：节假日跑一轮只会重复分析旧 K 线，配合本
  功能的周末+时段过滤已消除绝大部分空耗。

## 子项目 B：品种详情页

### 数据来源

- **K 线**：分析记录 `kline_data: list[dict]`（发给 AI 的已收盘 K 线表）。
  实现时核对字段名（seq/ts_open/open/high/low/close/volume/ema20…）；若记录
  内无 EMA 列，则前端用 close 序列自算 EMA20（20 根指数均线，一行 reduce）。
- **决策价位**：`stage2_decision.decision` 的 entry_price / stop_loss_price /
  take_profit_price / take_profit_price_2。
- **实时推理**：新端点 `GET /api/live/{symbol}` 返回 `state.get_live()`；
  404 当品种从未分析。前端在详情页打开且该品种分析进行中时 1s 轮询，
  空闲时停止轮询。

### 图表库

vendor `lightweight-charts`（standalone 生产版，Apache-2.0，约 50KB）至
`server/static/vendor/lightweight-charts.standalone.production.js`。
蜡烛图 + EMA20 线 + 价位横线（入场=蓝、止损=红、TP1/TP2=绿，带中文标签）。
深色主题与现有 UI 一致。

### 页面结构

- 新增「详情」视图（非新标签页，SPA 内切换）：顶部品种/周期/最近分析时间与
  结论摘要；中部 K 线图；下部两个折叠区——「实时推理」（进行中自动展开、
  自动滚动到底）与「最近一次决策」（复用现有决策字段表）。
- 入口：监控页进行中卡片、品种结果卡片、历史记录行（历史入口用该条记录的
  K 线与决策数据；监控入口用该品种最新记录 + live 流）。
- 历史详情抽屉内同样渲染 K 线图（同一组件复用）。

### 新/改 API

| 接口 | 变化 |
|---|---|
| `GET /api/live/{symbol}` | 新增：实时推理缓冲 |
| `GET /api/status` | `current` 结构改为字典（见状态节） |
| `GET /api/records/{name}` | 不变（详情页直接用其中 kline_data） |
| `GET /api/records?symbol=&latest=1` | 新增 `latest=1` 快捷参数：只返回该品种最新一条摘要（详情页定位记录用） |

## 错误处理

- 池内某数据源实例连接坏死：借出前 `_connected` 检查 + 使用中异常由
  service 现有重试消化；归还时不做健康检查（下次借出时自愈重连）。
- 并发下单品种失败照旧跳过；轮完成以 `as_completed` 全收齐为准。
- 详情页无记录（新品种首轮进行中）：K 线区显示「等待首次分析完成」，
  实时推理区正常工作。

## 测试

- `test_ds_pool.py`：借还语义、并发借用互不相同实例、rebuild。
- `test_scheduler.py` 更新：并发轮次（fake 分析记录并发峰值 ≤ 配置值）、
  stop 取消全部活动任务、轮 barrier（全完成才进入 round_wait）。
- `test_state.py` 更新：多 current 字典、live 缓冲限长与 seq 递增。
- `test_api.py` 更新：status 新结构、/api/live 404 与正常、latest=1。
- 前端手动验证：并发卡片、详情页 K 线渲染与价位线、实时推理滚动。

## 交付

实现 → 全量服务端测试 → 多智能体对抗审查（沿一期模式）→ 修复确认问题 →
合并 main → 重建导出 amd64 镜像包。
