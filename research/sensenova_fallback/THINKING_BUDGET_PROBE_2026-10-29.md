# 免费备用通道的思考策略：实测与控制手段（2026-10-29）

## 需求变更

1. 先要求：备用/免费通道**统一不开思考**，关不掉就开最低。
2. 随后改为：**优先用有思考功能的模型**，但把思考强度调到最低。

第 2 条不能靠调参口号实现——先把「思考强度」在这条网关上的实际行为测清楚。

## 关键发现：`effort` 档位基本无效，`max_tokens` 才真的能压住

同一份 lite Stage-2 提示词（XAUUSD 5m / 100 根 K 线，`build_messages(frame, "stage2")`），
`max_tokens=8192, stream=False`，只改思考控制形状：

| 路由 | 关思考 | `effort=minimal` | `reasoning={"max_tokens":512}` |
|---|---|---|---|
| nvidia/nemotron-3.5-lightning | 4.2s / 0 字 / 合规 | **65.4s / 17 165 字** / 合规 | **9.2s / 1 821 字** / 合规 |
| openrouter/free | 10.6s / 0 字 / 合规 | **94.3s / 10 369 字** / 合规 | **13.5s / 1 875 字** / 合规 |
| reflection/beam-free | 4.0s / 0 字 / 合规 | **7.7s / 254 字** / 合规 | 36.6s / 13 134 字 / **契约失败** |

结论有三点：
1. `reasoning={"effort":"minimal"}` 会被网关**接受但普遍忽略**，隐藏推理照样上万字、耗时 65–94 秒。
2. `reasoning={"max_tokens": N}` 是**真正生效**的预算控制，能把同样路由压到 8–15 秒。
3. 哪种形状有效是**逐路由**的事实，且方向相反：`lightning`/`openrouter` 要预算，`beam-free` 要 effort。
4. `reasoning.effort` 与 `reasoning.max_tokens` **互斥**，同一条请求只能给一个
   （上游原文：`Only one of "reasoning.effort" and "reasoning.max_tokens" ...`）。

## 全池实测（思考开启、各路由最小预算）

排序策略按用户要求改为**推理字数降序**：越会思考的路由越先尝试（见文末「排序策略」）。

| 路由 | 延迟 | 隐藏推理 | 契约 | 是否真思考 |
|---|---|---|---|---|
| nvidia/nemotron-3.5-super-vl | 7.8s | 849 字 | 合规 | 是 |
| reflection/beam-free | 7.7s | 254 字 | 合规 | 是（用 effort=minimal） |
| nvidia/nemotron-3.5-lightning | 9.2s | 1 821 字 | 合规 | 是 |
| recursive/flint-v1.0 | 13.4s | 5 102 字 | 合规 | 是 |
| openrouter/free | 13.5s | 1 875 字 | 合规 | 是 |
| dots-studio/dots-3-note-preview | 46.6s | 8 903 字 | 合规 | 是 |
| cohere/north-mini-code | 59.7s | 12 378 字 | 合规 | 是 |
| poolside/laguna-s-2.1 | 8.7s | 0 字 | 合规 | 否 |
| stealth/glyph-cluster | 36.5s | 0 字 | 合规 | 否 |
| kilo-auto/free | 89.3s | 15 265 字 | 合规 | 是，但控制不住 → 剔除 |
| nvidia/nemotron-3-nano-omni | 17.4s | 837 字 | **契约失败** | → 剔除 |
| poolside/laguna-xs-2.1 | — | — | — | HTTP 429（限流）→ 降为末位 |
| nvidia/nemotron-3-ultra-550b | — | — | — | HTTP 502 上游错误、Nvidia 内部错误 → 降为末位 |

`super-vl` 与 `dots-3-note` 在关思考时都填不满契约（前者把方向输出成字符串 `"null"`），
但在最小思考预算下**恢复合规**——这正好支持「优先用有思考功能的模型」这一方向。

## 实现

- 思考控制按顺序尝试，每条请求只带一种形状：
  `{"reasoning":{"max_tokens":512}}` → `{"reasoning":{"effort":"minimal"}}`
  → `{"reasoning":{"effort":"low"}}` → 不带字段。
  只有 400/422（形状被拒）才降级；429/5xx 视为限流或上游故障，不降级。
- 实测结论写进 `_REASONING_VARIANT_PRESET` 播种缓存（`beam-free` 预置 `minimal`，
  其余走预算），首个真实请求就用对形状，不必试探。
- 白名单按「真思考优先」重排，`_THINKING_CAPABLE_KILO` 记录哪些路由实际产生过隐藏推理，
  并有用例强制「思考型必须全部排在非思考型之前」的顺序不变量。

## 代价（必须知情）

一旦要求「有思考」，这批路由的最快档位从 4 秒级变成 **7.7–59.7 秒**：
关思考时 `lightning` 只要 4.2 秒，开启最小预算后是 9.2 秒；`north-mini-code` 从 5.9 秒变成 59.7 秒。
生产每次最多轮换 6 个候选，若前几个都慢，单次兜底的总耗时会明显上升。

## 排序策略（2026-10-29 用户指定，两次调整）

最终规则：**思考过程是中文的优先**，同一语言档内推理字数多的优先。

### 为什么按语言排，而不是改提示词

用户怀疑"英文思考主要跟模型有关"。实测证实了这个判断：在系统提示里显式加上
「思考过程必须全程使用简体中文，禁止用英文思考」后，同一提示词下各路由的隐藏推理语言是：

| 路由 | 中文占比 | 推理字数 | 延迟 |
|---|---|---|---|
| reflection/beam-free | **96.7%** | 269 | 4.0s |
| cohere/north-mini-code | **93.3%** | 1843 | 14.6s |
| recursive/flint-v1.0 | 46.0% | 9131 | 20.9s |
| openrouter/free | 28.7% | 1803 | 18.8s |
| nvidia/nemotron-3.5-lightning | 14.6% | 1732 | 11.3s |
| nvidia/nemotron-3.5-super-vl | **0%** | 1528 | 6.5s |

即：语言是**模型自带属性**，提示词改不动（super-vl 加了硬约束仍是 0% 中文）。
而且这句硬约束有副作用——`dots-3-note` 在该提示词下把 8192 输出预算烧穿、直接截断失败。
因此**提示词里的中文思考要求已回滚**，改为按模型挑（`_REASONING_CJK_SHARE_KILO` 排序键，
用例强制 `_PREFERRED_KILO` 必须按中文占比降序）。

### 生产轮换顺序（每次最多 6 个）

| # | 路由 | 中文占比 | 推理字数 |
|---|---|---|---|
| 1 | reflection/beam-free | 97% | 269 |
| 2 | cohere/north-mini-code | 93% | 1843 |
| 3 | recursive/flint-v1.0 | 46% | 9131 |
| 4 | openrouter/free | 29% | 1803 |
| 5 | nvidia/nemotron-3.5-lightning | 15% | 1732 |
| 6 | nvidia/nemotron-3.5-super-vl | 0% | 1528 |

垫底：`laguna-s`、`glyph-cluster`（不产生隐藏推理，没有中文可言）。
降级到不稳定档：`dots-3-note`（本轮截断）、`laguna-xs`（429）、`nemotron-3-ultra`（502）。

**注意**：推理字数的单次采样波动很大（`north-mini-code` 曾测到 12 378 字，复测只有 1 843 字），
所以字数只作同档内的次键，不作主键。

## 复现

从 `records/pending/` 取一条 stage-2 记录重建 `KlineFrame` → `build_messages(frame, "stage2")`
→ 直接 `_default_http_post`（绕过截断守卫以便看原始响应），逐形状记录
`finish_reason` / `reasoning` 字符数 / `usage.completion_tokens` / 延迟，再跑同一套枚举检查。

## 后续：思考过程在 UI 里逐字显示（同日追加）

原先只有「成功候选」在调用结束后整块推给界面（免费通道是 `stream=False`），
在那 8–60 秒里窗格是空的，观感等同卡死。现已改为流式 + 心跳：

1. **流式**：`_default_http_post` 在 `body["stream"]` 为真时委派给 `_default_http_stream`
   （SSE 逐行解析 → `_sse_line_to_event`），增量通过 `on_reasoning_token` /
   `on_content_token` 实时推给界面；`run_free_stage_fallback(stream=True)` 由编排器开启，
   单元测试默认仍走非流式，避免既有假传输被改写。
2. **不重复显示**：每个通道单独判重——已经流过的内容不再回放整块；
   若某路由无视 `stream=True` 只返回整包 JSON，则该通道退回"结束后整块显示"，
   两种行为都有用例锁定。
3. **心跳**：调用进行中每 5 秒输出一行「备用通道仍在分析中（已等待 N 秒）」，
   调用返回即停（守护线程 + `Event`），用例断言返回后不再新增行。
4. **失败保留可解释性**：流式下部分内容已经进入窗格、随后该候选失败时，
   追加一行「该次返回未完整，已切换到下一个通道继续…」，避免上文凭空出现半截内容。

实测（真实网关，`nvidia/nemotron-3.5-lightning:free`，思考预算 512，lite Stage-2 提示词）：

```
total_sec=12.3
reasoning_chunks=474 chars=1722
content_chunks=350  chars=612
first_chunk_at=1.43s  last_chunk_at=11.34s  span=9.91s  progressive=True
completion_tokens=879   content_head={"decision": {"order_type": "不下单", ...
```

首片 1.43 秒到达、跨 9.91 秒持续输出，证明是逐字流而非末尾整块。
