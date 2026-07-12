# BTC/ETH 永续合约确定性研究系统 V1 设计

日期：2026-07-13
状态：待用户书面确认
上游项目：PA_Agent（AGPL-3.0-or-later）

## 1. 目标与边界

V1 用 Binance USDⓈ-M 的 `BTCUSDT`、`ETHUSDT` 永续合约研究可复现的确定性交易策略。信号主周期为 4H，1D 只用于趋势过滤；允许做多和做空。

V1 不是实盘交易系统。它只包含公开市场数据下载、数据校验、确定性策略、事件驱动回测、模拟账户、风险控制和研究报告。

以下边界不可绕过：

- Python 确定性规则是 `LONG`、`SHORT`、`NO_TRADE`、入场、止损、止盈和仓位的唯一决策源。
- 三个 LLM 永久处于 Shadow Mode。第一批任务不接入 LLM。
- Binance 客户端只允许公开、无鉴权的只读 `GET` 市场数据接口。
- 禁止交易所 API Key、签名请求、账户接口、`create_order` 和任何自动实盘能力。
- V1 固定逐仓、单向持仓模式和 1 倍杠杆。系统绝对上限为 2 倍；只有 1 倍策略通过锁定样本外验证后，才能把 2 倍作为独立实验。
- 单笔计划风险上限 0.5%，组合总开放风险上限 1%，组合回撤达到 10% 后暂停新仓。

## 2. 系统架构

### 2.1 模块

1. `market_data`：下载成交 K 线、标记价格 K 线、指数价格 K 线、真实历史资金费率和公开合约元数据；支持分页、断点续传、校验和内容哈希。
2. `data_validation`：校验时间、闭合状态、重复、断档、价格约束以及原生周期与 1m 聚合周期的一致性。
3. `strategy_schema`：定义版本化、不可变的信号、取消和退出数据结构。
4. `strategy_v1`：实现独立规范 `BTC_ETH_PA_STRATEGY_V1.md`。在该规范书面冻结前，不得编写策略代码。
5. `risk`：仓位量化、风险预算、逐仓保证金占用、组合风险和回撤暂停。
6. `backtest`：以 1m 成交价和 1m 标记价驱动事件，生成 baseline 与 conservative 两条可复现路径。
7. `paper`：复用同一事件、撮合、风险和账本接口，但只维护本地模拟账户。
8. `research`：管理训练、验证、锁定样本外、walk-forward、成本压力、敏感性和基准对比。
9. `shadow`：未来阶段的 LLM 独立进程，只能读取冻结快照并写 Shadow 日志，不能导入或调用组合、风险、订单和撮合写接口。
10. `reporting`：输出数据质量、信号、交易、成本、路径歧义、回撤和实验元数据。

PA_Agent GUI 保留为上游展示与人工研究入口，不进入第一批任务，也不能直接驱动模拟账户。

### 2.2 数据流

```text
Binance public GET
  -> raw immutable responses
  -> normalized UTC 1m/native 4H/native 1D datasets
  -> validation and cross-validation
  -> versioned dataset manifest + hashes
  -> deterministic Python strategy
  -> risk gate
  -> baseline/conservative event replay
  -> immutable ledger and reports

validated closed bars
  -> future Shadow workers
  -> shadow-only log store
```

任何关键数据集不完整、周期交叉验证失败、模型版本过期或哈希不匹配时，系统必须 fail closed：不生成可交易信号，不继续相关回测区间，并记录明确原因。

## 3. 市场数据设计

### 3.1 数据范围

每个品种保存：

- 永续合约成交 K 线：原生 1m、4H、1D。
- 标记价格 K 线：1m；必要时保留原生 4H/1D 作为审计数据。
- 指数价格 K 线：1m；必要时保留原生 4H/1D 作为审计数据。
- 真实历史资金费率及资金费结算时刻的标记价格字段。
- 公开合约交易规则：价格精度、数量精度、`tick_size`、`step_size`、最小数量、最小名义价值和合约状态。
- 版本化费用、滑点和估算维持保证金配置。

时间统一为 UTC。持久化主键使用 `symbol/pair + interval + open_time_utc_ms` 或 `symbol + funding_time_utc_ms`。

### 3.2 K 线字段

通用字段：

- `source`
- `market`
- `symbol` 或 `pair`
- `interval`
- `open_time_utc_ms`
- `close_time_utc_ms`
- `open`、`high`、`low`、`close`
- `is_closed`
- `downloaded_at_utc_ms`
- `raw_payload_hash`
- `schema_version`

成交 K 线增加：

- `base_volume`
- `quote_volume`
- `trade_count`
- `taker_buy_base_volume`
- `taker_buy_quote_volume`

资金费率增加：

- `funding_time_utc_ms`
- `funding_rate`
- `mark_price`
- `source_page_hash`

### 3.3 数值规则

- 价格、数量、费用、资金费、保证金、已实现/未实现盈亏和账户账本使用 `Decimal`。
- 指标和向量化研究计算允许使用 `float64`。
- 从指标层进入交易边界时，价格按 `tick_size`、数量按 `step_size` 进行方向明确的量化；数量一律向下取整，风险保护价按不放大风险的方向量化。
- 禁止把二进制浮点结果直接写入订单、费用、保证金或账本。

### 3.4 原生周期交叉验证

1m 成交 K 线分别聚合为 4H 和 1D，再与 Binance 原生 4H/1D 成交 K 线逐根比较：

- `open` 必须等于第一根 1m 的 `open`。
- `high`/`low` 必须等于区间内极值。
- `close` 必须等于最后一根 1m 的 `close`。
- 成交量与成交额在配置的 Decimal 容差内一致。
- 时间边界必须与 UTC Binance 周期边界一致。

任何 OHLC 不一致、缺少分钟、周期边界错位或成交量超出容差，都标记该聚合周期为无效并 fail closed。不得用原生周期静默覆盖聚合结果，也不得静默填补关键缺口。

### 3.5 下载可靠性

- 每页保存请求区间、响应哈希、首尾时间和下一游标。
- 使用临时分片与原子提交；校验通过后才进入规范数据集。
- 中断后从最后一个已验证游标恢复，恢复时重新请求边界重叠页并去重。
- 对 429/5xx 使用有上限的指数退避；对 schema 变化立即停止。
- 数据集 manifest 保存来源 URL、下载时刻、区间、行数、哈希、缺口和代码版本。

## 4. 信号 Schema

每个信号至少包含：

- 身份：`signal_id`、`strategy_id`、`strategy_version`、`symbol`。
- 决策：`decision_time_utc_ms`、`side`、`order_type`、`trigger_basis`。
- 价格：`entry_rule`、`entry_reference_price`、`stop_price`、`take_profit_price`。
- 生命周期：`eligible_from_utc_ms`、`valid_until_utc_ms`、`max_holding_bars`。
- 风险：`risk_pct`、`planned_quantity`、`leverage`、`margin_mode=ISOLATED`、`position_mode=ONE_WAY`。
- 数据身份：`dataset_manifest_hash`、`trade_data_hash`、`mark_data_hash`、`index_data_hash`、`funding_data_hash`。
- 实验身份：`strategy_config_hash`、`code_commit_hash`、`fee_model_version`、`slippage_model_version`、`liquidation_model_version`、`maintenance_margin_version`。
- 状态：`status`、`cancel_reason`、`path_status`。

枚举要求：

- `side`: `LONG | SHORT | NO_TRADE`
- `order_type`: V1 仅 `MARKET_NEXT_4H_OPEN`
- `trigger_basis`: V1 入场、止损、止盈为 `CONTRACT_TRADE_PRICE`；爆仓为 `MARK_PRICE`
- `status`: `CREATED | ELIGIBLE | FILLED | CANCELLED | EXPIRED | CLOSED`
- `path_status`: `UNAMBIGUOUS | PATH_AMBIGUOUS`

取消必须给出机器可读原因，例如 `DATA_INVALID`、`NATIVE_BAR_MISMATCH`、`RISK_LIMIT`、`DRAWDOWN_PAUSE`、`INSUFFICIENT_MARGIN`、`BELOW_MIN_QTY`、`GAP_TOO_LARGE`、`EXISTING_POSITION`、`EXPIRED`、`MODEL_VERSION_EXPIRED`。

## 5. 估算爆仓与保证金模型

V1 的爆仓模型统一称为“估算爆仓模型”，不得声称精确复刻 Binance 爆仓引擎。原因包括账户级细节、费用、保险基金处理、维持保证金档位和交易所规则可能变化。

每次回测必须保存：

- `liquidation_model_version`
- `maintenance_margin_version`
- `maintenance_margin_source`
- `effective_from_utc_ms`
- `effective_to_utc_ms`
- `retrieved_at_utc_ms`
- `source_hash`
- 公式版本、舍入规则和强平费用假设

若回测时刻不在保证金版本有效期内，或来源/哈希缺失，则该区间 fail closed。维持保证金版本通过人工审核的公开资料快照导入；V1 不调用需要账户鉴权的档位接口。

爆仓仅由标记价格触发。报告必须使用“估算爆仓”“估算强平价格”等措辞，并单独列出模型不确定性。

## 6. 事件驱动回测顺序

信号只使用已收盘的 4H 和 1D 数据。4H 收盘形成的信号当根禁止成交，最早在下一根 4H 的第一个有效 1m 开盘处理。

每个 1m 时间片按以下顺序运行：

1. 验证本时间片成交价、标记价、指数价和必要配置有效。
2. 对时间片开始前已持有的仓位，按真实结算时刻和真实费率结算资金费。
3. 用标记价开盘检查跳空估算爆仓。
4. 用成交价开盘处理已有仓位的跳空止损或时间退出。
5. 处理已到 `eligible_from` 的入场；按成交价加不利滑点成交，收取手续费并占用逐仓保证金。
6. 检查分钟内止损、估算爆仓和止盈。
7. 处理退出费用、释放逐仓保证金、更新已实现盈亏。
8. 以分钟收盘更新权益、可用资金、未实现盈亏、总开放风险和回撤。
9. 4H 收盘后运行确定性策略，使用最近一根已收盘 1D 作为过滤。
10. 写入不可变事件、订单、成交、仓位和账户账本。

资金费边界采用明确顺序：在资金费时刻之前已经持有的仓位先结算资金费，再处理该时刻的退出；恰好在资金费时刻新开仓的仓位在结算之后建立，不参与本次结算。资金费率或结算标记价格缺失时，该品种在受影响区间 fail closed，禁止用零费率代替。

同一 1m 内止损与估算爆仓的真实先后无法从 OHLC 唯一判断时：

- 标记 `PATH_AMBIGUOUS`。
- `baseline` 路径：若标记价开盘未先爆仓，同一分钟内保护性止损优先于估算爆仓；同分钟止损和止盈均触发时止损优先。
- `conservative` 路径：估算爆仓优先于分钟内保护性退出，并使用更不利的允许成交假设。
- 两条路径共享完全相同的数据、信号和参数哈希，分别保存账本和绩效。

若标记价开盘已经越过估算爆仓线，两条路径均先爆仓，不属于路径歧义。

资金不可重复使用：已占用逐仓保证金、预估费用缓冲和开放风险预算都从可用额度中扣除。暂停新仓不能阻止已有仓位止损、止盈、时间退出或估算爆仓。

## 7. 风险与组合规则

- 初始资金由实验配置给定，不在策略代码中硬编码。
- 单笔计划最大亏损含入场/退出费用和基准滑点缓冲，不超过入场前权益的 0.5%。
- BTC 与 ETH 的总开放风险不超过权益的 1%。
- 计划数量先按风险计算，再按 `step_size` 向下量化并重新校验最小数量、最小名义价值、保证金和风险。
- V1 每个品种最多一个逐仓方向仓位，不加仓、不摊平、不对冲。
- 同时信号按 `decision_time`、再按固定品种顺序 `BTCUSDT`、`ETHUSDT` 处理；后处理信号只能使用剩余风险和资金。
- 组合从历史权益高点回撤达到 10% 后进入 `DRAWDOWN_PAUSE`，拒绝新仓。恢复必须开启新的、显式批准的实验；V1 不自动恢复。

## 8. 研究与验证层

### 8.1 样本治理

- 训练区间：用于形成和冻结规则及少量预先声明的参数候选。
- 验证区间：用于选择候选，但不得反复扩展参数空间。
- 锁定样本外区间：在规则、参数、成本模型和代码哈希冻结前，报告层拒绝展示绩效。
- 前向模拟区间：部署后只追加，不回写历史结果。

具体日期和数据截止时间写入实验 manifest，不能隐藏在代码默认值里。任何修改日期、参数、成本或策略代码都会产生新的实验 ID，旧的锁定样本外结果不得覆盖。

### 8.2 Walk-forward

V1 使用时间顺序 walk-forward；每个窗口只允许使用过去数据确定参数，在下一个窗口评价。窗口长度在首次实验 manifest 中冻结。不得随机打乱时间序列。

### 8.3 成本压力与敏感性

- 基准费用/滑点模型。
- 费用与滑点分别为基准的 1.5 倍和 2 倍。
- 资金费率使用真实历史值，并测试资金费边界恰好发生在开仓、平仓和数据缺失时的行为。
- 对 Donchian、EMA、ATR、止损倍数、止盈倍数和最大持有期使用小范围、预声明网格；禁止观察锁定样本外后新增“恰好有效”的参数。
- 输出参数表面，不只输出最佳点；孤立尖峰视为不稳健。

### 8.4 基准

至少对比：

- 全程现金。
- BTC/ETH 1 倍买入持有。
- 仅 1D 趋势过滤的简单基准。
- 不使用 1D 过滤的 4H Donchian 基准。
- `BTC_ETH_PA_STRATEGY_V1` baseline 路径。
- `BTC_ETH_PA_STRATEGY_V1` conservative 路径。

报告同时展示收益、年化波动、最大回撤、Calmar、Sharpe、Sortino、Profit Factor、胜率、平均盈亏、交易数、资金利用率、手续费、滑点、资金费、估算爆仓次数和 `PATH_AMBIGUOUS` 次数。不得只按收益排序。

## 9. 可复现性

一次回测由以下内容唯一标识：

- 数据 manifest 与所有分表哈希
- 策略规范版本与配置哈希
- 代码 commit 哈希
- Python 与依赖锁文件哈希
- 费用、滑点、估算爆仓、维持保证金版本
- 样本区间与 walk-forward 配置
- 随机种子；V1 核心路径应无随机性

使用相同标识重复执行，事件、成交、账本和汇总指标必须逐项一致。若不一致，测试失败。

## 10. 第一批范围

第一批只实现：

1. 公共市场数据下载和断点恢复。
2. UTC 规范数据模型与数据 manifest。
3. 原生 4H/1D 与 1m 聚合交叉验证。
4. 策略、订单、取消、持仓和模型版本 Schema。
5. 独立规则规范 `BTC_ETH_PA_STRATEGY_V1.md`。
6. 确定性指标、信号和仓位计算。
7. 与上述模块对应的单元、属性和集成测试。

第一批明确不实现 GUI、LLM、模拟实时循环、完整回测报告和任何实盘接口。详细清单见 `2026-07-13-v1-first-batch-tasks.md`。

## 11. 第一批验收条件

- 所有 Binance 调用均为公开无鉴权 `GET`。
- BTCUSDT、ETHUSDT 指定小区间数据可重复下载并断点恢复。
- 1m 聚合与原生 4H/1D 交叉验证；人为制造不一致会 fail closed。
- 未收盘、缺失、重复、错位和 schema 变化数据会 fail closed。
- 确定性策略对固定 fixture 产生完全一致的信号和哈希。
- 信号当根无法成交，未来数据变化不影响过去信号。
- 价格、数量、费用和风险边界量化符合 Decimal/tick/step 规则。
- 未配置云雾 API 或任何交易所密钥时，第一批全部测试可通过。
- 静态与运行时守卫证明不存在交易端点、签名请求和 `create_order`。

## 12. 已知限制

- OHLC 无法完整重建分钟内路径，因此保留 baseline/conservative 双路径和 `PATH_AMBIGUOUS`。
- 估算爆仓模型不等同 Binance 实际强平引擎。
- 历史盘口深度不可得时，滑点是版本化假设，需要压力测试。
- 回测通过不代表未来盈利；只有锁定样本外与持续前向模拟能逐步提高可信度。

## 13. 参考资料

- PA_Agent README：<https://github.com/rosemarycox5334-debug/PA_Agent>
- Binance USDⓈ-M 市场数据：<https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data>
- 云雾 OpenAI 兼容 Chat Completions：<https://yunwuapi.apifox.cn/api-390890183>
