# BTC_ETH_PA_STRATEGY_V1 确定性规则规范

版本：1.0.0-draft.2
日期：2026-07-13
状态：未冻结；不得开始策略、仓位或回测代码

## 1. 规则边界

本规范定义 `BTCUSDT`、`ETHUSDT` 永续合约的市场 Candidate、执行计划、退出和失效规则。任何 LLM 输出都不是输入。

交易生命周期严格分为：

1. `StrategyCandidate`：4H 收盘后的市场结论。
2. `ExecutionPlan`：到达下一执行时刻后，基于当时可见成交价计算 stop、TP、quantity、费用和保证金。
3. `FillEvent`：不可变的模拟实际成交。

Candidate 阶段禁止预写依赖下一开盘成交价的 stop、TP、quantity、手续费或保证金。

## 2. 市场、数据与时间

- Binance USDⓈ-M `BTCUSDT`、`ETHUSDT` 永续，允许 LONG/SHORT。
- 4H 成交价生成 Candidate；最近已收盘 1D 成交价做趋势过滤。
- 1m 成交价用于执行和保护性退出；1m 标记价只用于估算爆仓。
- 指数价格仅供审计，不参与 V1 计算，缺失不阻断路径。
- 信号只使用通过 1m 聚合与原生 4H/1D 交叉验证的已收盘数据。
- 所有时间为 UTC。
- 第一批只记录各流缺口区间；第二批仅在缺口影响行情、入场、持仓、估算爆仓或跨资金费结算点时按上下文将路径置为 INVALID。

## 3. 指标的精确定义

### 3.1 Pre-roll

指标从规范数据集中该合约最早的连续有效 K 线开始递推，跨训练、验证和锁定 OOS 边界保持连续，不在区间边界重新播种。研究区间开始前至少有 250 根有效 1D 和 100 根有效 4H；不足时不生成 Candidate，并记录 `PRE_ROLL_INSUFFICIENT` ValidationFailure。

### 3.2 EMA

- `EMA50_D` 与 `EMA200_D` 使用 close。
- `alpha=2/(N+1)`、`adjust=False`、`min_periods=N`。
- 第一根有效 close 是递推内部种子：`ema_1=close_1`；随后 `ema_t=alpha*close_t+(1-alpha)*ema_(t-1)`。
- 前 `N-1` 项即使有内部递推值，也对策略视为无效。

### 3.3 ATR14

True Range：

- 第一根：`TR_1=high_1-low_1`。
- 后续：`max(high-low, abs(high-prev_close), abs(low-prev_close))`。
- 第 14 根的首个有效 ATR 是前 14 个 TR 的算术平均。
- 此后 `ATR_t=(ATR_(t-1)*13+TR_t)/14`。
- `min_periods=14`；ATR 非有限或不大于零时 ValidationFailure。

### 3.4 Donchian 20

- `DONCHIAN_HIGH_20_PREV=max(high[t-20:t])`。
- `DONCHIAN_LOW_20_PREV=min(low[t-20:t])`。
- 切片包含索引 `t-20` 至 `t-1` 共 20 根，不含当前 `t`；至少第 21 根才有效。
- Donchian 源自 Decimal OHLC，不先转 float。

### 3.5 舍入与比较

- 指标内部不舍入，使用锁定 numpy/pandas 版本的 IEEE-754 float64。
- 决策边界把 EMA/ATR 以 round-half-even 规范为 15 位有效数字的 Decimal。
- ATR 形成价格距离前再按上述规则转换；最终交易价格按 tick_size 量化。
- EMA 与 close、EMA 与 EMA 使用规范 Decimal 严格 `>`/`<`；相等为 NEUTRAL。
- 4H close 与 Donchian 使用原始 Decimal 严格 `>`/`<`；相等不突破。
- 指标实现版本、numpy/pandas 版本和转换规则进入配置/代码哈希。

## 4. StrategyCandidate 市场规则

### 4.1 1D 状态

- `BULL`：`close_D > EMA200_D` 且 `EMA50_D > EMA200_D`。
- `BEAR`：`close_D < EMA200_D` 且 `EMA50_D < EMA200_D`。
- 否则 `NEUTRAL`。

在 4H decision time 只能使用 `close_time <= decision_time` 的最近已收盘 1D。

### 4.2 LONG Candidate

有效数据下，1D 为 BULL 且当前 4H `close > DONCHIAN_HIGH_20_PREV`，产生 `market_view=LONG`。

### 4.3 SHORT Candidate

有效数据下，1D 为 BEAR 且当前 4H `close < DONCHIAN_LOW_20_PREV`，产生 `market_view=SHORT`。

### 4.4 NO_TRADE Candidate

有效数据下不满足 LONG/SHORT 时产生 `NO_TRADE`，市场原因仅限 `TREND_NEUTRAL`、`NO_BREAKOUT`、`BREAKOUT_AGAINST_TREND`。

数据/pre-roll/版本错误生成 `ValidationFailure`，不生成 NO_TRADE。已有仓位、HALTED、风险、资金、数量和保证金问题由执行/风险层拒绝，不改变 Candidate 的市场结论。

### 4.5 Candidate 内容

Candidate 保存方向、市场原因、decision time、下一 4H open、冻结 ATR、决策 close、趋势/突破阈值以及数据/配置/代码/指标版本和哈希。Candidate 的数据身份只能绑定 `strategy_data_content_hash` 及其冻结 bundle version；不得绑定可选 Index、`audit_data_content_hash`、当前 `contract_rule_content_hash` 或完整 acquisition bundle。不得保存 stop、TP、quantity、fill-dependent cost、contract rule 或 maintenance margin 版本。

历史 contract rule 缺失不能抹掉 LONG/SHORT Candidate。执行层需要 tick/step/minimum/maintenance margin 时才解析相应版本；缺失则产生 `ExecutionRejection/CONTRACT_RULE_UNAVAILABLE`。

## 5. 版本化执行延迟与 ExecutionPlan

- `EXEC_DELAY_V1` 基准为下一 4H UTC open 后 1 分钟的 1m open。
- 压力实验固定测试 0、1、2 分钟，每个值产生不同执行版本和实验 ID。
- 目标分钟缺失时路径 INVALID，不顺延。
- 到达目标分钟后读取参考成交价 `P0`，先执行 gap 过滤，再计算 fill、stop、TP 和 quantity。

Gap 过滤：LONG 若 `P0 > decision_close+0.5*ATR`，SHORT 若 `P0 < decision_close-0.5*ATR`，ExecutionPlan 拒绝 `GAP_TOO_LARGE`。

## 6. 冻结的 V1 成本模型

- `FEE_V1_ASSUMED_TAKER_5BP`：每次 fill 的 taker fee rate `f=0.0005`。
- `SLIPPAGE_V1_BASE`：BTC 单边 `0.0001`，ETH 单边 `0.0002`。
- `FUNDING_BUFFER_V1_1BP_X6`：仓位 sizing 每次预留 `0.0001`，48 小时最多 6 次。
- 压力实验把 fee/slippage 同时乘 `1.5`、`2.0`；真实账本资金费仍使用历史真实值。

这些是版本化研究假设，不代表所有历史账户费率或真实市场冲击。

令 `q>0`：

- LONG entry fill：`quantize_up(P0*(1+s), tick)`。
- SHORT entry fill：`quantize_down(P0*(1-s), tick)`。
- LONG stop trigger：`quantize_up(entry_fill-2*ATR, tick)`。
- SHORT stop trigger：`quantize_down(entry_fill+2*ATR, tick)`。
- LONG TP trigger：`quantize_down(entry_fill+3*ATR, tick)`。
- SHORT TP trigger：`quantize_up(entry_fill-3*ATR, tick)`。
- LONG stop fill estimate：`quantize_down(stop_trigger*(1-s), tick)`。
- SHORT stop fill estimate：`quantize_up(stop_trigger*(1+s), tick)`。
- fill fee：`abs(q*fill_price)*f`。
- 资金费现金变化：`-side_sign*q*mark_price*funding_rate`，LONG `side_sign=+1`、SHORT `-1`。

entry/exit fill 已包含相应滑点。不得再从 PnL、费用或 unit_risk 重复扣除该滑点；审计滑点金额为 `abs(fill-P0)*q`。

## 7. unit_risk、仓位与保证金

每单位：

```text
price_loss = abs(entry_fill-stop_fill_estimate)
entry_fee = entry_fill*f
exit_fee = stop_fill_estimate*f
funding_buffer = entry_fill*0.0001*6
unit_risk = price_loss+entry_fee+exit_fee+funding_buffer
risk_budget = pre_entry_equity*0.005
raw_quantity = risk_budget/unit_risk
```

数量按有效 contract rule 的 step_size 向下量化，之后重新计算风险、名义价值和 required cash；不得向上取整满足最小数量。

1 倍逐仓：

```text
initial_margin = abs(q*entry_fill)
entry_fee_cash = abs(q*entry_fill)*f
exit_fee_reserve = abs(q*stop_fill_estimate)*f
funding_reserve = q*entry_fill*0.0001*6
required_cash = initial_margin+entry_fee_cash+exit_fee_reserve+funding_reserve
```

单笔风险上限 0.5%，组合开放风险上限 1%。真实资金费逐次记账，剩余 funding reserve 平仓释放。估算维持保证金按有效版本 `notional*mmr-cumulative_deduction`；版本不覆盖当时则路径 INVALID。

同一执行时刻 BTC/ETH 先独立计算，再按：

```text
scale=min(1, remaining_risk/sum_planned_risk,
            available_cash/sum_required_cash)
```

共同等比例缩量，按各自 step 向下量化并复核。输入顺序不得影响结果。

`EXISTING_POSITION`、`HALTED`、风险、资金、最小数量等产生 ExecutionPlan rejection，不产生 NO_TRADE。

## 8. FillEvent 与退出

FillEvent 保存未加滑点参考价、最终 fill、滑点金额、数量、手续费、时间、计划 ID、路径和原因，写入后不可变。

- 固定 stop/TP，全部退出；不分批、不移动保本、不追踪、不加仓、不反手。
- LONG 的 1D 状态不再 BULL、SHORT 不再 BEAR 时，在下一版本化执行时刻退出。
- 最大持有期固定为从入场 FillEvent 时间起精确 48 小时。
- 在首个 `timestamp >= fill_time+48h` 的有效 1m open 全平；不采用“入场后 12 根完整 4H”。
- 持仓期间关键成交/标记 1m 缺失，路径立即 INVALID，不延迟退出。
- 退出后最早从下一根完整 4H 收盘重新评估。

## 9. 第二批事件与终态规则

资金费、撮合、PATH_AMBIGUOUS、估算爆仓、保证金和账本属于第二批；本规范确认不代表授权实现。

- 标记价开盘已越过估算爆仓线时两条路径均先估算爆仓。
- 同分钟 stop 与估算爆仓均可能触发且开盘均未触发：记录 PATH_AMBIGUOUS；baseline stop 优先，conservative 估算爆仓优先。
- 同分钟 stop 与 TP 均触发：两条路径 stop 优先。
- 资金费时刻前已有仓位先结算，同刻退出在后，同刻新入场不参与该次资金费。
- 关键资金费记录或结算标记价缺失：路径 INVALID，不按零处理。
- 回撤达到 10%：触发 HALT，取消 Candidate/Plan，下一有效 1m open 执行 HALT_EXIT；关键数据缺失则 INVALID，否则退出后终态 HALTED。
- HALTED 后不延长现金曲线，指标只计算至 terminal time 并标记删失。

## 10. 合约规则与确定性

- Candidate 不依赖 contract rule。ExecutionPlan 的每个交易边界必须有覆盖当时的 `contract_rule_version`；当前 exchangeInfo 快照不得静默回填历史，缺失时拒绝执行但保留 Candidate。
- 价格/数量/费用/保证金/账本用 Decimal；指标内部可用 float64。
- 相同 dataset content、配置、代码、依赖、执行、成本、contract rule 和模型版本必须产生逐字节一致结果。
- 修改任一规则必须提升版本并创建新实验。
