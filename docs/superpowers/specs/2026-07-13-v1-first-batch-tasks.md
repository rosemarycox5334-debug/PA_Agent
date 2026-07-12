# V1 第一批任务清单

日期：2026-07-13
状态：待设计与策略规范书面确认后转为实施计划

## 范围约束

本批只做数据、Schema、验证、确定性规则与测试。不接 GUI，不接 LLM，不做实时模拟循环，不实现交易所鉴权或交易操作。

## A. 项目与安全基线

- [ ] 建立研究包目录和依赖边界。
- [ ] 增加 Binance 只读 HTTP 客户端接口；仅允许 `GET` 和公开市场数据路径。
- [ ] 增加静态测试，禁止交易端点、签名参数、API secret 和 `create_order`。
- [ ] 增加依赖锁定、UTC、Decimal 和日志规范。

## B. Schema 与版本

- [ ] 定义 K 线、资金费率、合约规则和数据 manifest Schema。
- [ ] 定义信号、取消、订单意图、持仓意图和退出 Schema。
- [ ] 定义费用、滑点、估算爆仓和维持保证金版本 Schema。
- [ ] 定义数据/配置/代码哈希及实验 ID 生成规则。
- [ ] 为 Schema 增加序列化、枚举和不可变性测试。

## C. Binance 公共数据

- [ ] 下载 BTCUSDT、ETHUSDT 成交 1m、原生 4H、原生 1D K 线。
- [ ] 下载 1m 标记价格和 1m 指数价格 K 线。
- [ ] 下载真实历史资金费率。
- [ ] 下载公开 exchange information 并规范 tick/step/minimum filters。
- [ ] 实现分页、限速、退避、断点恢复、重叠页去重和原子提交。
- [ ] 保存 raw page hash、规范数据 hash 和完整 manifest。

## D. 数据验证

- [ ] 校验 UTC 周期边界、闭合状态、OHLC 约束、重复和断档。
- [ ] 将 1m 聚合为 4H/1D，与 Binance 原生 4H/1D 交叉验证。
- [ ] 对不一致、缺分钟、边界错位和 schema 变化 fail closed。
- [ ] 校验成交、标记、指数和资金费时间范围可用性。
- [ ] 测试分页中断恢复、边界重叠、重复数据和数据流缺失。

## E. BTC_ETH_PA_STRATEGY_V1

- [ ] 将已批准规范转成冻结的版本化配置。
- [ ] 实现 EMA50/EMA200、Donchian 20 和 Wilder ATR14。
- [ ] 实现 LONG、SHORT、NO_TRADE 及原因枚举。
- [ ] 实现 next-4H-open 入场意图、gap 失效、固定 stop/TP、趋势退出和 12-bar 时间退出。
- [ ] 实现 Decimal 交易边界、tick/step 量化和最小交易规则。
- [ ] 实现 0.5% 单笔、1% 组合、1× 杠杆、逐仓和 10% 回撤暂停规则的纯函数。

## F. 第一批测试

- [ ] 指标 golden fixtures 与预热测试。
- [ ] LONG/SHORT 对称性与 NO_TRADE 原因测试。
- [ ] 信号当根禁止成交、下一 4H 开盘生效测试。
- [ ] 日线只读取最近已收盘数据测试。
- [ ] 修改未来数据不改变历史信号测试。
- [ ] Decimal/tick/step 边界与数量向下取整属性测试。
- [ ] 资金费率结算时刻边界：开仓前、同刻、平仓同刻和缺失。
- [ ] 保证金模型版本有效期、来源和哈希测试。
- [ ] `PATH_AMBIGUOUS` baseline/conservative 数据结构测试。
- [ ] 相同 manifest/config/code hash 重跑结果逐字节一致。
- [ ] 人为制造原生周期不一致、数据流缺失和过期模型时 fail closed。

## 第一批完成定义

- 所有任务对应测试通过。
- 可在不配置云雾 Key、Binance Key 或任何私钥的环境运行。
- 小型固定数据集能从 raw 数据稳定生成相同 manifest、验证结果和策略信号。
- 安全守卫确认不存在实盘能力。
- 第一批不以 GUI 启动或 LLM 调用作为验收条件。
