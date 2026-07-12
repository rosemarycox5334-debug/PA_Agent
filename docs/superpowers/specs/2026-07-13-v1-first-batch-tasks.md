# V1 第一批任务清单：数据与验证

日期：2026-07-13
状态：待修订设计书面确认后实施

## 范围硬边界

第一批只实现公共数据下载、规范化、版本、Canonical 序列化、双哈希与数据验证。

可以定义纯数据 Schema，但不得实现：

- StrategyCandidate 生成规则或指标计算
- ExecutionPlan、仓位 sizing 或风险拒绝
- FillEvent 产生、撮合、资金费结算、保证金、估算爆仓或 PATH_AMBIGUOUS
- 账户账本、组合回撤或回测绩效
- GUI、LLM、交易所鉴权和任何交易接口

资金费边界、路径歧义、估算爆仓和撮合测试全部移动到待单独批准的第二批。

## A. 安全与项目基线

- [ ] 建立独立研究数据包及模块边界。
- [ ] Binance HTTP 客户端只允许公开市场数据域名、`GET` 和 allowlist 路径。
- [ ] 静态和运行时守卫禁止签名参数、API secret、账户/交易路径、POST/PUT/DELETE 和 `create_order`。
- [ ] 锁定 UTC、Decimal、Canonical JSON、哈希和依赖版本。

## B. 数据 Schema

- [ ] 定义成交 K 线、标记价格 K 线、审计指数 K 线、资金费率和 raw page Schema。
- [ ] 定义 `contract_rule_version`，含来源、哈希、有效期、审核状态和 `CURRENT_SNAPSHOT_ONLY`。
- [ ] 定义未来使用的 lifecycle 结构外壳：StrategyCandidate、ExecutionPlan、FillEvent、ValidationFailure、ExecutionRejection；不实现业务逻辑。
- [ ] Candidate Schema 禁止 stop、TP、quantity 和 fill-dependent cost 字段。
- [ ] 区分 market reason、validation failure 和 execution reject reason。
- [ ] 为 Schema 增加不可变性、枚举、序列化和非法字段测试。

## C. Canonical 序列化与双哈希

- [ ] 实现 UTF-8、键排序、无空白、UTC int64、Decimal 字符串、负零规范和主键排序。
- [ ] 实现独立 `dataset_content_hash`。
- [ ] 实现独立 `acquisition_manifest_hash`，包含 content hash、请求页、时间、重试、断点和采集器版本。
- [ ] 测试同内容不同分页/下载时间产生相同 content hash、不同 acquisition hash。
- [ ] 测试字段顺序、Decimal 表示、CRLF/LF 和运行平台不改变 Canonical content hash。

## D. Binance 公共数据下载

- [ ] 下载 BTCUSDT、ETHUSDT 永续成交 1m、原生 4H、原生 1D。
- [ ] 下载 1m 标记价格。
- [ ] 下载真实历史资金费率及返回的结算标记价格。
- [ ] 可选下载 1m 指数价格作为审计数据；失败只记录审计告警。
- [ ] 下载当前 exchangeInfo 并保存为 `CURRENT_SNAPSHOT_ONLY` contract rule，不推断历史有效期。
- [ ] 实现分页、限速、429/5xx 有界退避、断点恢复、重叠页去重和原子提交。
- [ ] 保存 raw response、page hash、规范表、content hash 和 acquisition manifest。

## E. 数据验证

- [ ] 校验 UTC、K 线闭合、时间单调、主键唯一、OHLC 约束、重复和断档。
- [ ] 将成交 1m 聚合为 4H/1D，与原生 4H/1D 逐根交叉验证。
- [ ] 校验 OHLC 精确一致、volume/quote volume Decimal 容差和 UTC 周期边界。
- [ ] 对成交或标记关键流缺失输出机器可读 fail-closed 结果。
- [ ] 对指数价缺失只输出审计告警，不使必需数据集失败。
- [ ] 校验 contract rule 的有效期覆盖语义；当前快照不能通过历史覆盖测试。
- [ ] 对 schema 变化、未知字段语义和无法解析的 Decimal fail closed。

## F. 第一批测试

- [ ] 分页在任意页中断后恢复，结果无重复、无缺口且 content hash 不变。
- [ ] 边界重叠、空页、乱序页、重复页和响应重试测试。
- [ ] 未收盘 K 线、分钟缺口、标记价缺口、原生周期不一致和成交量超容差测试。
- [ ] 资金费下载的起止边界、重复时刻、缺失区间和结算标记价缺失测试；本批只验证数据，不做结算。
- [ ] 数据流缺失测试：成交/标记为阻断，指数为审计告警。
- [ ] contract_rule_version 来源、哈希、有效期和 CURRENT_SNAPSHOT_ONLY 测试。
- [ ] 两次独立下载产生相同 Canonical content 与 content hash。
- [ ] acquisition manifest 能区分不同下载过程。
- [ ] 固定 fixture 在不同运行顺序下逐字节一致。
- [ ] 安全测试证明无鉴权、无私钥、无交易能力。

## 第一批完成定义

- 上述任务及测试全部通过。
- 指定小区间可以中断恢复并生成稳定双哈希。
- 1m 聚合与原生 4H/1D 交叉验证，任何关键不一致 fail closed。
- 当前 contract rule 明确只代表当前快照，不被用于历史回测。
- 未配置云雾 Key、Binance Key 或任何私钥时可完整运行。
- 没有策略、仓位、事件引擎、回测、GUI 或 LLM 代码。

## 第二批候选范围（未授权）

第二批才考虑冻结后的指标/Candidate、ExecutionPlan、0/1/2 分钟延迟、成本与 unit_risk、同时等比例缩量、FillEvent、真实资金费结算、最小事件引擎、估算爆仓、PATH_AMBIGUOUS、INVALID/HALTED 和可复现账本。
