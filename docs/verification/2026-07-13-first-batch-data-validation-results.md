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
├── test_multi_page_resume.py
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
- 每个分页任务使用不可变 request identity；checkpoint 与 raw page 同时保存 identity 和 SHA-256。每页保存 payload SHA-256、首末时间、next start、请求、重试、行数、页号与下载时间；恢复和最终提交前重新计算并验证全部 raw page。
- 分页、边界重拉、仅完全相同记录可去重、冲突主键 fail closed、显式完成目录 reuse/reject、断点恢复、429/5xx/网络错误有界指数退避。
- UTC 毫秒、Decimal 规范化和版本化严格 Kline V1 schema：恰好 12 字段，校验 OHLC、非负 volume/count、整数 trade_count、open/close 时间和周期边界。
- Canonical JSON、稳定 dataset content hash、独立 acquisition manifest hash/run ID、独立 computational experiment ID 纯函数。
- 当前 contract rule 保存为 `CURRENT_SNAPSHOT_ONLY`，同时保存 requested/returned/missing symbols、source hash、采集时间和 review status；任一请求币种缺失产生 `ContractRuleValidationFailure`。
- 四类独立缺口区间和状态；Funding 独立保存 schedule status、coverage status、gap intervals 和 observed steps。
- 1m 聚合 4H/1D，按 `AGG_VALIDATION_V1` 与 Binance 原生周期做双向一对一交叉验证；空集合、额外/重复 native 和 partial edge bucket 均不能返回 valid。
- 公共接口安全守卫、范围守卫、原子性、恢复、哈希、容差和 CLI 测试。

## 4. 测试命令与完整结果

### 第一批专属测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/research_data -v
```

结果：

```text
collected 116 items
test_aggregation.py                    3 passed
test_aggregation_validation.py       18 passed
test_binance_public_security.py      16 passed
test_canonical.py                     3 passed
test_cli.py                           8 passed
test_downloader_resume.py            18 passed
test_gaps.py                          7 passed
test_hashing.py                       4 passed
test_models.py                        3 passed
test_multi_page_resume.py             1 passed
test_normalize.py                    27 passed
test_scope_guard.py                   4 passed
test_storage.py                       4 passed
116 passed in 17.32s
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
20 passed in 0.36s
```

### 静态检查

```powershell
.\.venv\Scripts\python.exe -m ruff check pa_agent/research_data tests/research_data scripts/download_binance_research.py
```

结果：

```text
All checks passed!
```

### Diff 与编译检查

```powershell
git diff --check
.\.venv\Scripts\python.exe -m compileall pa_agent/research_data
```

结果：两条命令退出码均为 `0`；`compileall` 完成 `pa_agent/research_data` 全包扫描。

### 上游 main / feature 同口径回归对照

已安装 PA_Agent 的完整运行与测试依赖。由于 `pip install -e .[dev]` 在上游 `tvdatafeed` Git 依赖下载阶段发生连接重置，改用其相同源码归档安装 `tvdatafeed`，随后执行 `pip install -e . --no-deps` 完成项目可编辑安装。

固定 Hypothesis seed 后，在独立 `main` worktree 与 feature 分支分别运行：

```powershell
python -m pytest tests/unit tests/property --tb=no --hypothesis-seed=20260713
```

结果：

```text
main:    43 failed, 659 passed in 28.69s
feature: 43 failed, 659 passed in 24.15s
失败测试集合一致；feature 未新增上游离线测试失败。
```

两分支均另行运行完整 `python -m pytest -q`。完整集合包含 live integration/e2e，两分支都在相同 300 秒测试上限内无最终结果并于约 304 秒被终止。因此不把该运行声明为通过；可重复的离线回归对照与第一批专属测试结果如上。

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

### 完整性修复后的 3 天、多页、第 3 次请求中断真实示例

区间：`2026-07-01 00:00:00.000 UTC` 至 `2026-07-03 23:59:59.999 UTC`，BTCUSDT 与 ETHUSDT 各 4,320 根 1m 成交 K 线，分页上限 1,000。第 3 次真实公共请求前主动抛出中断，随后从 checkpoint 恢复；另在独立目录完成干净下载。

```text
INTERRUPTED intentional live page-three interruption final
RESUMED_CONTENT_HASH 158959dcce3f0ef08271eeea7a6104a71f85d73bd10c24507ca7490df4ba145a
CLEAN_CONTENT_HASH 158959dcce3f0ef08271eeea7a6104a71f85d73bd10c24507ca7490df4ba145a
CONTENT_HASH_EQUAL True
RESUMED_ACQUISITION_HASH 19738b1e329fb09e75dd4f4c1eb1177cc1354b3de7482d43bea492ef09c390a6
CLEAN_ACQUISITION_HASH f8a2d02133f149b1fe671a47d461a4324218a5ce8dbd37d715596593b4885d9b
ACQUISITION_HASH_DIFFERENT True
BTCUSDT ROWS=4320 PAGES=5 PAGE_EVIDENCE=True 4H_VALID=True 1D_VALID=True
ETHUSDT ROWS=4320 PAGES=5 PAGE_EVIDENCE=True 4H_VALID=True 1D_VALID=True
BTCUSDT/ETHUSDT FUNDING=COMPLETE SCHEDULE=VERIFIED COVERAGE=COMPLETE
```

Funding 实际结算时间存在毫秒级抖动，`FUNDING_SCHEDULE_ASSUMED_8H_V1` 现使用版本化 `±1000 ms` 结算容差；本区间 schedule 与 coverage 分别验证为 `VERIFIED` 和 `COMPLETE`。真实非 8 小时排程仍输出 `FUNDING_SCHEDULE_UNVERIFIED`，缺失结算槽位单独进入 coverage gaps，不再混为一类。每个 1m 数据集的 5 个 page 均通过 payload hash、request identity 与页链复核。

运行时输出：

- `research_data_output/live_3day_integrity_resume_final/summary.json`
- `research_data_output/live_3day_integrity_clean_final/summary.json`

### PR #15 原报告纠正

PR 初版报告曾把“保存 raw page”与“已保存并复核 raw page payload hash”混写；初版实际没有逐页 `raw_payload_sha256`、request identity 和最终提交前复核。本轮已实现这些字段和 fail-closed 校验，并新增文件篡改、错误 checkpoint、范围/参数变化、完成目录重跑、等价复用和页链回归测试。旧运行目录属于 V1 证据格式，不能由 V2 静默恢复或追加。

## 7. 未完成项与已知限制

- 未实现任何第二批内容：指标、Candidate、ExecutionPlan 业务、仓位、撮合、资金费结算、估算爆仓、账本、绩效、GUI 或 LLM。
- 当前 contract rule 只是当前快照，不能用于历史执行计划；历史规则归档仍未建设。
- Funding gap 使用 `FUNDING_SCHEDULE_ASSUMED_8H_V1` 与版本化毫秒容差；schedule verification 与 requested-range coverage 分开记录。该版本仍是研究假设，不代表交易所未来排程保证。
- Index price 仅审计，不参与有效性判断。
- 滑点、手续费、保证金和事件路径尚未实现。
- 上游 PA_Agent 已安装完整依赖并完成 main/feature 离线同口径回归；两者存在相同的 43 个上游既有失败。完整集合中的 live integration/e2e 在两分支均触发 300 秒上限，因此没有完整集合通过结论。
- 本次只完成获批的第一批数据与验证环境；不修复上游 43 个既有失败，也不扩展到第二批。

## 8. PR #15 最后一轮四项完整性修复

### 8.1 未收盘 K 线与确定性时间源

- `run_first_batch` 的第一个公共请求为 `GET /fapi/v1/time`，仅使用响应中的 `serverTime` 判断 K 线是否闭合；本地 `clock_ms` 只记录采集时间。
- 原始时间响应保存到 `raw/source_server_time/page-000000.json`；`source_server_time_utc_ms` 与 `source_server_time_raw_payload_sha256` 同时进入 acquisition manifest 和 summary。
- trade、mark、index、原生 4H/1D 任一 K 线若 `close_time_utc_ms >= source_server_time_utc_ms`，立即产生 `UNCLOSED_BAR`，该数据集不会写入 Canonical 文件或 content hash。
- 真实三天验证的恢复下载与干净下载取得不同 server time，但相同规范数据的 content hash 保持一致。

### 8.2 Canonical Schema 身份

以下固定版本字段已进入 dataclass、Canonical JSONL、dataset content hash 和 dataset manifest：

```text
Kline.schema_version=BINANCE_KLINE_V1_EXACT_12
FundingRate.schema_version=BINANCE_FUNDING_V1
ContractRuleSnapshot.schema_version=CONTRACT_RULE_SNAPSHOT_V1
```

回归测试确认：仅修改 Canonical `schema_version` 即会改变 dataset content hash。

### 8.3 Funding 身份与数值校验

`normalize_funding_rate` 现在必须接收 `expected_symbol`，并 fail closed 校验：

- `returned symbol == expected symbol`；
- `funding_time` 为非负整数；
- `mark_price > 0`；
- `funding_rate` 为有限 Decimal。

错误币种或非法数值只能保留在 Raw 审计证据中，不能进入 Canonical funding 数据或 content hash。

### 8.4 Raw 响应范围校验

每页新响应在写入 Raw 前、恢复时读取既有 Raw 页后，均校验每条记录满足：

```text
original_start_time <= timestamp <= original_end_time
timestamp >= page_request.startTime
```

违反时产生 `RAW_RECORD_OUT_OF_REQUEST_RANGE`。测试覆盖原始范围越界、后续分页起点越界，以及重算页哈希和 checkpoint 哈希后的恢复页越界，防止范围校验被完整性元数据替代。

### 8.5 最终测试结果

```powershell
.\.venv\Scripts\python.exe -m pytest tests/research_data -v
.\.venv\Scripts\python.exe -m pytest tests/research_data/test_binance_public_security.py tests/research_data/test_scope_guard.py -v
.\.venv\Scripts\ruff.exe check pa_agent/research_data tests/research_data scripts/download_binance_research.py
git diff --check origin/main...HEAD
.\.venv\Scripts\python.exe -m compileall pa_agent/research_data
```

```text
首批数据测试：116 passed in 17.32s
安全与范围守卫：20 passed in 0.36s
ruff：All checks passed!
diff-check：exit 0
compileall：exit 0
```

### 8.6 BTCUSDT/ETHUSDT 三天真实最终验证

窗口：`2026-07-01T00:00:00Z` 至 `2026-07-03T23:59:59.999Z`，包含 trade、mark、index、funding、原生 4H/1D 和当前 contract rules。第 3 个分页请求故意中断，随后从 checkpoint 恢复，并与独立干净下载对照。

```text
BTCUSDT trade 1m pages=5
ETHUSDT trade 1m pages=5
RESUMED=True
RESUMED_SOURCE_SERVER_TIME_UTC_MS=1783938166908
CLEAN_SOURCE_SERVER_TIME_UTC_MS=1783938197290
SOURCE_SERVER_TIME_RAW_HASH_PRESENT=True
ALL_CANONICAL_KLINES_CLOSED=True
SCHEMA_VERSIONS=BINANCE_KLINE_V1_EXACT_12,BINANCE_FUNDING_V1,CONTRACT_RULE_SNAPSHOT_V1
FUNDING_SYMBOLS_MATCH=True
ALL_RAW_RECORDS_IN_ORIGINAL_AND_PAGE_RANGES=True
BTCUSDT/ETHUSDT 4H_VALID=True 1D_VALID=True
BTCUSDT/ETHUSDT FUNDING=COMPLETE SCHEDULE=VERIFIED COVERAGE=COMPLETE
DATASET_CONTENT_HASH_EQUAL=True
DATASET_CONTENT_HASH=36a66639422fcc3bc739574c6c7c83227ae9b0c822db0d227b782fadb1f33286
RESUMED_ACQUISITION_HASH=2991648944fe281a6041bc192e94d7999cfbeb5c7c0cc866c8c6e519bbdd4d7f
CLEAN_ACQUISITION_HASH=591197da532c7f97a8a24351db4f6253ca83ea2e2089c8d9528acfd644929c9e
ACQUISITION_HASH_DIFFERENT=True
```

运行时输出：

- `research_data_output/live_3day_pr15_final_v2_resume/summary.json`
- `research_data_output/live_3day_pr15_final_v2_clean/summary.json`

本轮仍未实现任何指标、Candidate、ExecutionPlan、仓位、撮合、资金费结算、爆仓、账本、GUI、LLM、鉴权或交易接口；第二批仍未开始。

## 9. PR #15 最后四项合并阻塞复验（2026-07-13）

### 9.1 编排预检与统一公共 GET 重试

- `run_first_batch` 在创建存储对象、读取时钟和发起网络请求前执行只读 preflight；默认 `reject` 遇到已完成目录时立即拒绝。
- 测试对完成目录中每个文件计算 SHA-256，重入前后文件集合与哈希完全一致，并确认客户端调用数和时钟调用数均为 0。
- 分页、`/fapi/v1/time` 与 `/fapi/v1/exchangeInfo` 共用 `PublicGetRetrier`；默认最多重试 3 次、指数退避基数 0.5 秒。
- Raw metadata 保存每次请求的 `retry_count`；测试覆盖 429、5xx、网络异常和不可重试异常。

### 9.2 版本化数据依赖哈希

冻结以下互相独立的内容身份：

```text
acquisition_bundle_content_hash@ACQUISITION_BUNDLE_CONTENT_V1
strategy_data_content_hash@STRATEGY_DATA_CONTENT_V1
execution_data_content_hash@EXECUTION_DATA_CONTENT_V1
audit_data_content_hash@AUDIT_DATA_CONTENT_V1
contract_rule_content_hash@CONTRACT_RULE_SNAPSHOT_V1
```

旧 `dataset_content_hash` 是完整 acquisition bundle 的兼容别名。`computational_experiment_id` 只接收调用方明确给出的版本化依赖；`candidate` scope 只允许 `strategy_data@<version>`，因此可选 Index 和当前 contract rule 不会改变 Candidate 实验身份。

### 9.3 Index 请求区间边界

Index gap 检测现在与 trade/mark 一样接收请求的 `expected_start_ms` 与按分钟对齐的 `expected_end_ms`。测试覆盖首分钟和末分钟同时缺失，并输出两个机器可读边界缺口；第一批仍只记录事实，不解释交易影响。

### 9.4 最终本地验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/research_data -v
.\.venv\Scripts\python.exe -m pytest tests/research_data/test_binance_public_security.py tests/research_data/test_scope_guard.py -v
.\.venv\Scripts\ruff.exe check pa_agent/research_data tests/research_data scripts/download_binance_research.py
git diff --check
.\.venv\Scripts\python.exe -m compileall pa_agent/research_data
```

```text
首批数据测试：125 passed in 22.98s
安全与范围守卫：20 passed in 0.35s
ruff：All checks passed!
diff-check：exit 0（仅 Git 的 LF/CRLF 提示）
compileall：exit 0
```

使用固定 Hypothesis seed `20260713` 分别在 `origin/main@33170ab` 和当前 feature worktree 执行 `tests/unit tests/property`。两边均为 43 个失败，失败节点集合逐项完全相同，因此没有引入新的上游离线失败；这不是把该组测试描述为通过。

### 9.5 BTCUSDT/ETHUSDT 三日真实下载、恢复与哈希隔离

窗口仍为 `2026-07-01T00:00:00Z` 至 `2026-07-03T23:59:59.999Z`。在第 3 个分页调用前故意中断（已持久化 2 页），随后恢复同一目录，并与独立干净目录对照：

```text
RESUMED=True
PREFLIGHT_ZERO_CLIENT_CALLS=True
PREFLIGHT_ZERO_CLOCK_CALLS=True
PREFLIGHT_DIRECTORY_HASHES_UNCHANGED=True
ALL_CANONICAL_KLINES_CLOSED=True
FUNDING_SYMBOLS_MATCH=True
ALL_RAW_RECORDS_IN_ORIGINAL_AND_PAGE_RANGES=True
BTCUSDT/ETHUSDT 4H_VALID=True 1D_VALID=True
TRADE/MARK/INDEX/FUNDING_GAP_STATUS=COMPLETE
FUNDING_SCHEDULE=VERIFIED COVERAGE=COMPLETE

acquisition_bundle_content_hash=8ecac760f7d37ef5d6a6e9f621b072f3a1ea13b5eca659625298be2348b52265
strategy_data_content_hash=5132ce4a5d1340d219a2b8da3ac5047afce46f8fe00e43ef1edaa2d2b0a8c608
execution_data_content_hash=d672bb8714d725fbabd91152de1d456cd32fd13114d110c1093e34c5c0eecc20
audit_data_content_hash=e05fe4743587fc63e38f8f79d390c030f79d2fcec8baf595826eafc414681b13
contract_rule_content_hash=304e132a1ab2d93af64ca61b55b18b7ac76a4257ce26fede2d053f2aa2adfa45
ALL_FIVE_CONTENT_HASHES_EQUAL=True

RESUMED_ACQUISITION_MANIFEST_HASH=fe2ecaeb12beeb5dc15d52afff75291b22eb72df32730c83dc7470dda85f01b9
CLEAN_ACQUISITION_MANIFEST_HASH=b4c9da52cf237ce49826aa4550e0cf69034eb3ecdd856fe4801e73305eb2caae
ACQUISITION_MANIFEST_HASH_DIFFERENT=True
CANDIDATE_COMPUTATIONAL_EXPERIMENT_ID_EQUAL=True
```

运行时输出：

- `research_data_output/live_3day_pr15_hashsplit_resume/summary.json`
- `research_data_output/live_3day_pr15_hashsplit_clean/summary.json`

范围守卫复核：没有新增指标、Candidate 生成、ExecutionPlan 业务逻辑、仓位、撮合、资金费结算、爆仓、账本、GUI、LLM、API Key、鉴权或交易接口。第二批未开始。
