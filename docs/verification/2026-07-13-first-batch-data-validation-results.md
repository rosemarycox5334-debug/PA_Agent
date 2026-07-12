# 第一批数据与验证实施结果

日期：2026-07-13
分支：`feature/first-batch-data-validation`

## 1. 三份设计文档修改摘要

- Donchian 20 修正为 `high[t-20:t]` / `low[t-20:t]`，明确包含 `t-20` 至 `t-1` 共 20 根。
- StrategyCandidate 不再绑定 contract rule 或 maintenance margin；缺失规则只在未来 ExecutionPlan 产生 `CONTRACT_RULE_UNAVAILABLE`。
- `computational_experiment_id` 与 `acquisition_run_id` 分离；采集哈希不进入策略、成交或账本的确定性 ID。
- 第一批只输出 trade/mark/funding/index 四类独立缺口区间，不在数据层解释持仓上下文或全局 INVALID。
- 冻结 `AGG_VALIDATION_V1`：base volume 绝对容差 `1E-8`、quote volume `1E-6`、四字段相对容差 `1E-12`，采用绝对或相对容差通过；OHLC、UTC 和 trade_count 精确一致。

## 2. 新增和修改的目录树

```text
pa_agent/research_data/
├── __init__.py
├── aggregation.py
├── binance_public.py
├── canonical.py
├── cli.py
├── downloader.py
├── gaps.py
├── hashing.py
├── models.py
├── normalize.py
├── storage.py
└── validation.py

tests/research_data/
├── __init__.py
├── test_aggregation.py
├── test_aggregation_validation.py
├── test_binance_public_security.py
├── test_canonical.py
├── test_cli.py
├── test_downloader_resume.py
├── test_gaps.py
├── test_hashing.py
├── test_models.py
├── test_normalize.py
├── test_scope_guard.py
└── test_storage.py

scripts/
└── download_binance_research.py
```

另修改 `.gitignore`，忽略运行时 `research_data_output/`。

## 3. 第一批实际完成项

- Binance USDⓈ-M 固定主机、固定路径、无鉴权公共 GET 客户端。
- 成交 1m/4H/1D、标记价 1m、可选指数价 1m、真实历史资金费率和当前 exchangeInfo 下载。
- Raw page、Canonical JSONL、checkpoint、dataset manifest 和 summary 的原子保存。
- 分页、边界重拉、主键去重、断点恢复、429/5xx/网络错误有界指数退避。
- UTC 毫秒、Decimal 规范化和严格 schema 拒绝。
- Canonical JSON、稳定 dataset content hash、独立 acquisition manifest hash/run ID、独立 computational experiment ID 纯函数。
- 当前 contract rule 保存为 `CURRENT_SNAPSHOT_ONLY`，不推断历史有效期。
- 四类独立缺口区间和状态。
- 1m 聚合 4H/1D，按 `AGG_VALIDATION_V1` 与 Binance 原生周期交叉验证。
- 公共接口安全守卫、范围守卫、原子性、恢复、哈希、容差和 CLI 测试。

## 4. 测试命令与完整结果

### 第一批专属测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/research_data -v
```

结果：

```text
collected 65 items
test_aggregation.py                    3 passed
test_aggregation_validation.py       13 passed
test_binance_public_security.py      16 passed
test_canonical.py                     3 passed
test_cli.py                           3 passed
test_downloader_resume.py             4 passed
test_gaps.py                          3 passed
test_hashing.py                       3 passed
test_models.py                        3 passed
test_normalize.py                     7 passed
test_scope_guard.py                   4 passed
test_storage.py                       3 passed
65 passed in 2.21s
```

### 安全守卫单独验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/research_data/test_binance_public_security.py tests/research_data/test_scope_guard.py -v
```

结果：

```text
collected 20 items
test_binance_public_security.py      16 passed
test_scope_guard.py                   4 passed
20 passed in 0.33s
```

### 静态检查

```powershell
.\.venv\Scripts\python.exe -m ruff check pa_agent/research_data tests/research_data scripts/download_binance_research.py
```

结果：

```text
All checks passed!
```

## 5. 安全守卫结果

- 客户端公开方法只有 `get_json`。
- 基础地址只接受 `https://fapi.binance.com`。
- allowlist 只有 klines、mark price klines、index price klines、funding rate、exchange info 和 server time。
- HTTP、替代域名、userinfo、路径穿越、账户/订单路径、signature、API key、secret 和 recvWindow 均在请求前拒绝。
- 包内没有 GUI、AI、indicator、orchestrator、records 等第二批/上游应用导入。
- 包内没有 `create_order(`，也没有 strategy、position、matching、backtest、ledger 或 LLM 模块。

## 6. BTCUSDT/ETHUSDT 小区间真实示例

区间：`2026-07-01 00:00:00.000 UTC` 至 `2026-07-01 23:59:59.999 UTC`。

步骤：

1. BTCUSDT trade 1m 第一页落盘后故意中断。
2. 确认生成 `BTCUSDT_trade_1m.checkpoint.json`。
3. 在同目录恢复，边界重拉并去重。
4. 在独立目录完成一次干净下载。
5. 比较双哈希、缺口状态和原生周期验证。

结果：

```text
RESUMED_FLAG=True
RESUMED_CONTENT_HASH=c36acaae2fae71c837b50fad49a61c9973264f48c2ff1f1f01c6d88b0bae591f
CLEAN_CONTENT_HASH=c36acaae2fae71c837b50fad49a61c9973264f48c2ff1f1f01c6d88b0bae591f
CONTENT_HASH_EQUAL=True
RESUMED_ACQUISITION_HASH=ff14d1378b573010b0651470af660e12eb4b61f06f3eee58498750e0ee1766df
CLEAN_ACQUISITION_HASH=b98376e66d48ac0ab8a3714a6d68518fa29a737fd93290113edf00bf448c8a8d
ACQUISITION_HASH_DIFFERENT=True
BTCUSDT 4H_VALID=True 1D_VALID=True trade/mark/funding/index=COMPLETE
ETHUSDT 4H_VALID=True 1D_VALID=True trade/mark/funding/index=COMPLETE
```

运行时输出：

- `research_data_output/live_resume_example/summary.json`
- `research_data_output/live_clean_example/summary.json`

## 7. 未完成项与已知限制

- 未实现任何第二批内容：指标、Candidate、ExecutionPlan 业务、仓位、撮合、资金费结算、估算爆仓、账本、绩效、GUI 或 LLM。
- 当前 contract rule 只是当前快照，不能用于历史执行计划；历史规则归档仍未建设。
- Funding gap 的第一批事实检测默认比较 8 小时间距；资金费间隔发生历史变更时需引入版本化 schedule 后再解释。
- Index price 仅审计，不参与有效性判断。
- 滑点、手续费、保证金和事件路径尚未实现。
- 上游 PA_Agent 全量单元测试未在该最小环境运行：初始收集因未安装 PyQt6、pydantic、pandas、openai、tzdata 等全应用依赖出现 47 个 collection errors。第一批包只依赖标准库，其 65 项专属测试独立通过。
- PA_Agent GUI 全量依赖尚未部署；本次完成的是获批的第一批数据与验证环境。
