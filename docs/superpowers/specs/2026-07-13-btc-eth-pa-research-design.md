# BTC/ETH 永续合约确定性研究系统 V1 设计

日期：2026-07-13
状态：第二轮修订，待用户书面确认
上游项目：PA_Agent（AGPL-3.0-or-later）

## 1. 目标与不可绕过边界

V1 研究 Binance USDⓈ-M `BTCUSDT`、`ETHUSDT` 永续合约。市场策略使用成交价 4H 主周期和已收盘 1D 趋势过滤，允许做多和做空；事件回放使用 1m 成交价，估算爆仓使用 1m 标记价。

- Python 确定性规则是市场结论和交易计划的唯一来源。
- 三个 LLM 永久处于 Shadow Mode；第一、第二批均不接入 LLM。
- Binance 客户端只允许公开、无鉴权的只读 `GET` 市场数据接口。
- 禁止 Binance API Key、签名请求、账户接口、交易端点、`create_order` 和自动实盘。
- V1 固定逐仓、单向持仓、1 倍杠杆；2 倍仅可在 1 倍通过锁定样本外后作为新实验测试。
- 单笔计划风险不超过 0.5%，组合开放风险不超过 1%。回撤达到 10% 时实验进入终态 `HALTED`。
- 第一批只实现数据下载、规范化、版本和数据验证；策略、仓位、撮合、回测、GUI 与 LLM 代码在书面设计重新确认前不得实施。

## 2. 分层架构

1. `market_data`：公共市场数据下载、分页、断点恢复、原始响应保存。
2. `data_model`：UTC 规范表、Canonical 序列化、内容哈希与采集清单哈希。
3. `data_validation`：闭合、重复、断档、时间边界、OHLCV、原生周期交叉验证和版本覆盖。
4. `strategy_schema`：只定义 `StrategyCandidate`、`ExecutionPlan`、`FillEvent` 及验证/拒绝事件的结构。
5. `strategy_v1`：未来按独立规范生成市场 Candidate；不得访问组合状态来改变市场结论。
6. `execution`：未来把有效 Candidate 与执行时市场状态转换为 ExecutionPlan。
7. `risk`：未来验证风险、资金、逐仓和组合限制，只能批准、缩量或拒绝计划。
8. `event_engine`：第二批实现资金费、撮合、路径歧义、估算爆仓、保证金和账本。
9. `paper`：后续复用同一执行和账本接口，只维护本地模拟账户。
10. `shadow`：后续独立进程，只读冻结快照、只写 Shadow 日志，不能导入执行、风险或账本写接口。
11. `reporting`：区分市场策略、执行拒绝、风险拒绝、路径有效性和实验终态。

PA_Agent GUI 保留为上游展示入口，不参与第一批。

## 3. 三阶段交易生命周期

### 3.1 StrategyCandidate

每根有效 4H 收盘后产生市场研究结果：

- `candidate_id`、`strategy_id`、`strategy_version`、`symbol`
- `decision_time_utc_ms`
- `market_view = LONG | SHORT | NO_TRADE`
- `market_reason`
- `entry_intent = NEXT_4H_OPEN_MARKET`（仅 LONG/SHORT）
- 冻结的 `atr_value`、趋势状态、突破阈值和决策 4H close
- `eligible_4h_open_utc_ms`、`execution_delay_version`
- 数据内容、配置、代码和指标版本/哈希

Candidate 不得包含依赖下一开盘成交价的 `stop_price`、`take_profit_price`、`quantity`、保证金或费用。数据校验失败时不生成 `NO_TRADE`，而生成独立 `ValidationFailure`。

`NO_TRADE` 只表示有效市场数据下没有市场入场条件，例如 `TREND_NEUTRAL` 或 `NO_BREAKOUT`。`EXISTING_POSITION`、`DRAWDOWN_PAUSE/HALTED`、资金不足和风险上限不是市场结论。

### 3.2 ExecutionPlan

到达版本化执行时刻后，执行层读取当时可见的成交价，依次：

1. 根据参考开盘价和滑点版本计算预计/确定性成交价。
2. 基于该成交价和 Candidate 冻结 ATR 计算止损、止盈。
3. 基于完整 `unit_risk` 公式计算数量、费用缓冲、资金费缓冲和逐仓保证金。
4. 同刻多品种计划先独立计算，再按组合风险与资金等比例缩量。
5. 风险层产生 `APPROVED | SCALED | REJECTED`，并记录独立 `execution_reject_reason`。

ExecutionPlan 保存 `order_type`、`trigger_basis`、`margin_mode=ISOLATED`、`position_mode=ONE_WAY`、stop/TP、数量、`contract_rule_version`、`contract_rule_hash`、`maintenance_margin_version`、费用/滑点/资金费/保证金/估算爆仓版本、数据/配置/代码哈希及有效期。

Candidate 不绑定 tick size、step size、最小数量、最小名义价值、合约规则或维持保证金。历史合约规则缺失时保留原始 LONG/SHORT Candidate，由执行层产生 `ExecutionRejection/CONTRACT_RULE_UNAVAILABLE`。

### 3.3 FillEvent

FillEvent 是不可变的实际模拟成交记录，包含计划 ID、时间、方向、数量、未加滑点参考价、滑点金额、最终成交价、手续费、成交原因和路径 ID。最终成交价已经含滑点；后续任何公式不得重复加入同一侧滑点。

取消与拒绝记录在 Candidate 之外，例如：`DATA_INVALID`、`CONTRACT_RULE_EXPIRED`、`EXISTING_POSITION`、`HALTED`、`RISK_LIMIT`、`INSUFFICIENT_MARGIN`、`BELOW_MIN_QTY`、`GAP_TOO_LARGE`、`EXECUTION_DATA_MISSING`。

## 4. 市场数据与版本

### 4.1 必需数据

- 永续合约成交 K 线：原生 1m、4H、1D。
- 标记价格 K 线：1m。
- 真实历史资金费率及返回的结算标记价格。
- 公开合约规则及其采集证据。

指数价格为审计数据，不参与 V1 策略、撮合、风险、资金费或估算爆仓计算。指数数据缺失只降低审计完整度，不阻断回测。若未来模型使用指数价，必须提升依赖版本并把它改为必需数据。

### 4.2 通用字段

- `source`、`market`、`symbol/pair`、`interval`
- `open_time_utc_ms`、`close_time_utc_ms`
- `open`、`high`、`low`、`close`、`is_closed`
- 成交数据的 volume、quote volume、trade count、taker volumes
- `downloaded_at_utc_ms`、`source_page_hash`、`schema_version`

所有内部时间使用 UTC 毫秒。价格、数量、费用、保证金和账本使用 Decimal；指标允许 `float64`，进入交易边界时按明确规则转换和量化。

### 4.3 dataset_content_hash 与 acquisition_manifest_hash

- `dataset_content_hash`：只代表规范化数据内容，与下载重试次数、下载时间和页边界无关。
- `acquisition_manifest_hash`：代表采集过程，包含端点、请求参数、页哈希、请求时间、重试、断点、采集器版本和最终 content hash。
- `acquisition_run_id`：由 acquisition manifest 的 Canonical 内容派生，只用于追踪一次采集运行。
- `computational_experiment_id`：只由 dataset content hash、样本区间、策略版本、执行版本、成本版本、代码 commit 和依赖锁版本派生。它不得包含 acquisition manifest hash、acquisition run ID、下载时间、分页或重试信息。

重新下载得到相同 Canonical 数据时，`dataset_content_hash` 和 `computational_experiment_id` 必须相同；采集过程不同可产生不同 `acquisition_manifest_hash` 与 `acquisition_run_id`。采集哈希和采集 ID 不得进入 Candidate、FillEvent、账本或策略计算的确定性 ID。

Canonical 序列化规则：

1. UTF-8、JSON、对象键按 Unicode code point 升序、无无意义空白。
2. 时间为 UTC `int64` 毫秒；布尔值和 null 使用标准 JSON。
3. Decimal 一律序列化为无指数、无多余前导/尾随零的字符串；负零规范为 `"0"`。
4. 表记录按声明的复合主键升序；数组保持声明顺序。
5. 哈希算法固定 `SHA-256`，输入为 Canonical UTF-8 字节。
6. schema、Canonical 规则或排序键变化必须提升版本，禁止复用旧哈希。

### 4.4 原生周期交叉验证

1m 成交 K 线分别聚合为 4H、1D，并与 Binance 原生 4H/1D 比较：open 为首分钟 open，high/low 为区间极值，close 为末分钟 close，UTC 边界完全一致。

`aggregation_validation_version=AGG_VALIDATION_V1` 固定以下 Decimal 容差：

| 字段 | 绝对容差 | 相对容差 |
|---|---:|---:|
| `base_volume` | `0.00000001` | `0.000000000001` |
| `quote_volume` | `0.000001` | `0.000000000001` |
| `taker_buy_base_volume` | `0.00000001` | `0.000000000001` |
| `taker_buy_quote_volume` | `0.000001` | `0.000000000001` |

计算规则：先用 Decimal 对 1m 字段精确求和；`abs_diff=abs(aggregated-native)`；若两者均为零则通过，否则 `relative_diff=abs_diff/max(abs(aggregated),abs(native))`。字段在 `abs_diff <= abs_tolerance OR relative_diff <= relative_tolerance` 时通过。`trade_count` 必须以整数精确求和并与原生周期完全相等。OHLC 与 UTC 边界不设容差，必须精确一致。

任何 OHLC 不一致、分钟缺失、边界错位或成交量超出容差均 fail closed。不得用原生周期静默覆盖聚合数据。

### 4.5 合约规则版本

`contract_rule_version` 必须包含 symbol、tick/step、最小数量、最小名义价值、合约状态、来源、采集时间、source hash、`effective_from`、`effective_to` 和审核状态。

当前 `exchangeInfo` 只能证明采集时附近的当前规则，不能静默用于全部历史。历史区间必须由有证据的归档规则版本覆盖；缺少覆盖时，该区间不得进入可交易回测。第一批可以下载并保存当前规则，但必须标记 `CURRENT_SNAPSHOT_ONLY`，不能伪造历史有效期。

估算维持保证金同样具有独立版本、来源和有效期；爆仓输出只能称“估算”。

### 4.6 下载可靠性

- 每页保存请求区间、响应哈希、首尾时间和下一游标。
- 使用临时分片与原子提交，校验后才进入规范数据集。
- 恢复时重新请求边界重叠页并去重。
- 429/5xx 有上限指数退避；schema 变化立即停止。
- 第一批数据层只输出独立、机器可读的 `trade_gap_intervals`、`mark_gap_intervals`、`funding_gap_intervals`、`index_gap_intervals` 和每条数据流状态，不把任何缺口直接解释为整个数据集或实验失败。

## 5. 指标数值规范

完整规则同时写入 `BTC_ETH_PA_STRATEGY_V1.md`：

- EMA(N)：`alpha=2/(N+1)`、`adjust=False`、`min_periods=N`；首个输入 close 是递推种子，但前 N-1 项无有效输出。
- ATR14：首个 TR 为 `high-low`；第 14 个 TR 的 ATR 以首 14 个 TR 算术平均为种子，之后用 `(prior_atr*13+tr)/14`。
- Donchian 20：取当前索引 `t` 之前首尾包含的 20 根，即索引 `t-20` 至 `t-1`；Python 等价切片为 `high[t-20:t]`、`low[t-20:t]`。至少第 21 根才有效。
- pre-roll 从规范数据集中该合约最早连续有效 K 线开始，跨训练/验证/OOS 边界连续计算，不在区间边界重置；进入研究区间前至少 250 根 1D 与 100 根 4H。
- 指标内部不舍入。决策边界将 float64 指标用 round-half-even 规范为 15 位有效数字的 Decimal；价格型阈值随后按 tick 量化。比较使用严格 `>`/`<`，相等为不触发。
- numpy/pandas 与实现版本进入代码和实验哈希。

## 6. 执行、成本、资金和保证金公式

### 6.1 执行延迟

`execution_delay_version=EXEC_DELAY_V1`：Candidate 在 4H 收盘形成，基准在下一根 4H 开盘后 1 分钟的 1m open 执行。压力测试冻结为 0、1、2 分钟。延迟从下一 4H UTC open 计算；目标分钟缺失则拒绝/使路径 INVALID，不顺延。

### 6.2 V1 成本常量

- taker fee rate：`f=0.0005`，版本 `FEE_V1_ASSUMED_TAKER_5BP`。
- baseline 单边滑点：BTC `s=0.0001`、ETH `s=0.0002`，版本 `SLIPPAGE_V1_BASE`。
- 成本压力：相应滑点和手续费乘 1.5、2.0；每个组合是新实验。
- 资金费使用历史真实费率；仓位 sizing 的不利资金费缓冲固定为每次 `0.0001`，48 小时最多 6 次，版本 `FUNDING_BUFFER_V1_1BP_X6`。

这些是研究假设，不声称等于所有历史账户费率或真实市场冲击。

### 6.3 价格与费用

令执行参考 1m open 为 `P0`、数量为正数 `q`：

- LONG 入场 fill：`quantize_up(P0*(1+s_entry), tick_size)`。
- SHORT 入场 fill：`quantize_down(P0*(1-s_entry), tick_size)`。
- LONG 止损预计 fill：`quantize_down(stop_trigger*(1-s_exit), tick_size)`。
- SHORT 止损预计 fill：`quantize_up(stop_trigger*(1+s_exit), tick_size)`。
- 每次成交手续费：`abs(q*fill_price)*f`。
- 资金费现金变化：`-side_sign*q*mark_price_at_funding*funding_rate`，LONG 的 `side_sign=+1`，SHORT 为 `-1`。

fill price 已含该次滑点，不再额外从 PnL 或 unit risk 扣同一笔滑点金额。滑点金额仅作为审计字段 `abs(fill_price-P0)*q`。

### 6.4 unit_risk、数量和保证金

止损触发价由 fill price 与冻结 ATR 计算；止损预计 fill 含退出滑点。每单位：

```text
price_loss_per_unit = abs(entry_fill - stop_fill_estimate)
entry_fee_per_unit = entry_fill * f
exit_fee_per_unit = stop_fill_estimate * f
funding_buffer_per_unit = entry_fill * 0.0001 * 6
unit_risk = price_loss_per_unit + entry_fee_per_unit
            + exit_fee_per_unit + funding_buffer_per_unit
risk_budget = pre_entry_equity * 0.005
raw_quantity = risk_budget / unit_risk
```

数量按 step 向下量化后重新计算全部值。1 倍逐仓：

```text
initial_margin = abs(quantity * entry_fill) / 1
entry_fee = abs(quantity * entry_fill) * f
exit_fee_reserve = abs(quantity * stop_fill_estimate) * f
funding_reserve = quantity * entry_fill * 0.0001 * 6
required_cash = initial_margin + entry_fee + exit_fee_reserve + funding_reserve
```

真实资金费逐次进入账本，未使用的 funding reserve 在平仓时释放。估算维持保证金按有效版本的 tier 公式 `notional*mmr-cumulative_deduction` 计算；版本不覆盖回放时刻则路径 INVALID。

## 7. 第二批最小事件引擎

资金费、撮合、保证金、估算爆仓、PATH_AMBIGUOUS 和账户账本全部移至第二批。第二批开始前仍需单独批准。

每个 1m 事件顺序：验证关键数据；结算此前持仓的真实资金费；检查标记价开盘估算爆仓；处理成交价开盘保护性退出；创建/执行到期 ExecutionPlan；检查分钟内保护性退出和估算爆仓；更新费用、逐仓、PnL、权益和回撤；写不可变事件。

第二批事件引擎按上下文解释第一批输出的缺口事实：成交价缺失且影响行情、入场或持仓时路径 INVALID；标记价缺失且当时有持仓时路径 INVALID；标记价缺失但空仓且没有相关事件时只记录缺口，不阻断 Candidate；资金费结算点缺失且有仓位跨越时路径 INVALID；指数价始终只作审计告警。

同分钟止损与估算爆仓无法排序时记录 `PATH_AMBIGUOUS`：baseline 为开盘未爆仓时止损优先；conservative 为估算爆仓优先。同分钟止损与 TP 均触发时均按止损优先。两条路径共享数据与配置哈希，独立保存账本。

资金费时刻前已有仓位先结算，再处理同刻退出；同刻新仓不参与本次结算。资金费记录或结算标记价缺失使路径 INVALID，不能回填零。

## 8. 组合、持有期和终态

- 同一执行时刻的 BTC/ETH ExecutionPlan 先按各自 0.5% 风险独立计算。
- 若总风险超过 1% 或 required cash 超过可用资金，所有同刻计划按共同系数 `min(1, risk_capacity/sum_risk, cash_capacity/sum_required_cash)` 等比例缩量；再按 step 向下量化和重新验证。
- 不使用 BTC_FIRST/ETH_FIRST；测试应证明输入顺序不改变结果。
- V1 每品种最多一个仓位，不加仓、不摊平、不对冲。
- 持有期固定为从 FillEvent 时间起精确 48 小时，不使用“入场后 12 根完整 4H”的定义。首个 `timestamp >= fill_time+48h` 的有效 1m open 执行时间退出；关键数据缺失则路径 INVALID。
- 组合回撤在分钟账本达到 10% 时生成 `HALT_TRIGGERED`，取消所有 Candidate/Plan，并在下一有效 1m open 对所有仓位执行 `HALT_EXIT`。若所需关键数据缺失则终态为 INVALID；否则最后一笔退出后终态为 `HALTED`。
- HALTED 后不延长现金曲线到原计划结束。年化指标只计算到 terminal time，报告必须显示 `HALTED`、实际存续天数和删失状态，不得与完整区间结果混排。

## 9. 研究、样本和前向模拟硬门槛

### 9.1 样本治理

训练、验证、锁定样本外、walk-forward 和前向区间写入实验 manifest。指标 pre-roll 可以读取区间前数据，但交易和绩效不能越界。修改日期、规则、参数、成本或代码必须创建新实验；锁定 OOS 不得覆盖。

### 9.2 基准与压力

比较现金、BTC/ETH 1 倍买入持有、简单 1D 趋势、无 1D 过滤的 Donchian、V1 baseline 与 conservative。执行延迟测试 0/1/2 分钟；成本测试 1.0/1.5/2.0 倍；参数只用预声明小网格并输出完整参数表面。

### 9.3 进入前向模拟的全部硬门槛

只有以下条件全部满足，才允许创建新的前向模拟实验：

1. 锁定 OOS 至少覆盖 18 个月；合计至少 60 笔闭合交易，BTC、ETH 各至少 20 笔。
2. baseline 与 conservative 均为 `COMPLETED`，无 INVALID、无 HALTED，净收益为正，最大回撤严格低于 10%。
3. conservative 路径在 2 倍费用与滑点压力下净收益不低于 0。
4. 时间顺序 walk-forward 至少三窗，至少三分之二窗口净收益为正，任何窗口均未 HALTED。
5. 以 OOS 每日净收益做 7 日 moving-block bootstrap，固定 10,000 次、固定种子；95% 双侧置信区间的平均日收益下界必须大于 0。
6. 以 OOS 交易序列做 10,000 次 block bootstrap；95% 置信区间的 Profit Factor 下界必须大于 1.0。
7. 预声明参数邻域（每个可调参数上下约 20% 的合法网格）中，至少 70% 组合在 conservative、基准成本下净收益为正；最佳点不能是孤立尖峰。
8. 数据验证 100% 通过；所有可交易时段有有效 contract rule、费用、资金费、保证金和估算爆仓版本。
9. 相同数据、配置、依赖和代码哈希重复运行，Candidate、Plan、Fill、账本与指标逐字节一致。

置信区间方法、block 长度、种子和样本选择必须在解锁 OOS 前冻结。任何门槛失败都返回研究阶段，不得以主观判断豁免。

## 10. 可复现性

`computational_experiment_id` 仅由 dataset content hash、样本区间、策略版本、执行版本、成本版本、代码 commit 和依赖锁版本生成。`acquisition_manifest_hash`、`acquisition_run_id`、下载时间、分页和重试不参与。相同 computational experiment ID 的结果必须逐字节一致。

## 11. 批次边界

### 第一批：数据与验证

只实现公共下载、UTC 规范化、Canonical 序列化、双哈希、断点恢复、原生周期交叉验证、contract rule 当前快照和数据测试。允许定义生命周期 Schema，但不得实现 Candidate 生成、ExecutionPlan、仓位、资金费结算、撮合、回测或 GUI/LLM。

### 第二批：待单独批准

实现冻结后的指标与 Candidate、ExecutionPlan、等比例缩量、最小事件引擎、资金费、保证金、baseline/conservative、估算爆仓、HALTED/INVALID 和账本测试。

详细第一批范围见 `2026-07-13-v1-first-batch-tasks.md`。

## 12. 已知限制

- 估算爆仓不等同 Binance 实际强平。
- OHLC 无法重建分钟内路径，因此保留双路径。
- 当前 exchangeInfo 不能证明历史规则。
- 历史盘口缺失使滑点只能作为版本化假设和压力测试。
- 通过回测硬门槛也不保证未来盈利。

## 13. 参考

- PA_Agent：<https://github.com/rosemarycox5334-debug/PA_Agent>
- Binance USDⓈ-M 市场数据：<https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data>
- 云雾 Chat Completions：<https://yunwuapi.apifox.cn/api-390890183>
