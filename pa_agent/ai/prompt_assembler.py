"""Prompt assembler for Stage 1 (diagnosis) and Stage 2 (decision)."""
from __future__ import annotations

import datetime
import functools
import json
import logging
import math
from pathlib import Path
from typing import Any

from pa_agent.ai.decision_stance import build_decision_stance_guidance, normalize_stance
from pa_agent.ai.pattern_routing import (
    STAGE1_DETECTED_PATTERNS_GUIDE,
    STAGE1_PATTERN_BRIEFS_BLOCK,
)
from pa_agent.ai.kline_features import bar_candle_direction_label, compute_kline_geometry_features
from pa_agent.ai.market_features import (
    compute_simple_market_features,
    inject_market_features_section,
    render_simple_market_features,
)
from pa_agent.data.base import KlineFrame
from pa_agent.data.datetime_ts import format_epoch_for_display
from pa_agent.records.schema import AnalysisRecord

logger = logging.getLogger(__name__)

_KLINE_INDICATOR_NOTE = (
    "说明：下表仅含最近 N 根已收盘 K 线；几何特征亦基于此 N 根。"
    "EMA20/ATR14 由程序在更老缓冲 K 线上预热后重算，与外盘图表「全历史延续」"
    "指标可能略有差异，勿逐点对比。"
)

# ── Language (both stages, thinking + final output) ───────────────────────────

_LANGUAGE_ZH_RULE = """
## 语言要求（阶段一、阶段二均必须遵守）

- **通俗易懂（最重要）**：`reasoning`、`diagnosis_summary`、`next_bar_prediction.reasoning`、`next_cycle_prediction.reasoning`、`key_factors`、`risk_assessment`、`watch_points`、`decision_trace` 的 `question` 与 `reason` 等**面向用户的解释文本，必须通俗易懂**。像给刚入门的新手讲解一样，用日常语言描述市场发生了什么、为什么这样判断、接下来可能怎样。避免堆砌专业术语；用到专业概念时用一句话解释清楚。例如不要写"H2 顺势回撤信号确认"，而写"价格第二次回调后继续上涨，说明多头仍然主导"。
- **思考过程**：扩展思考、内部推理、以及写入 JSON 的 `reason`、`diagnosis_confidence_reasoning`、`trade_confidence_reasoning`、`estimated_win_rate_reasoning` 等说明，**全程使用简体中文**。禁止用英文写推理段落或中英混杂的长句。
- **最终输出**：阶段一诊断 JSON、阶段二决策 JSON 中所有面向用户的字符串**一律使用简体中文**，不得出现英文单词。
- **仅允许英文**：JSON 字段名（schema 键名）、规定的枚举取值（如 `proceed`、`wait`、`bullish`、`bearish`）、K 线序号格式（如 `K1`、`K42-K1`）。**除此之外不得使用任何英文**。
- **价格行为术语**：优先使用下列简体中文 PA 术语；英文缩写须附带中文解释（如"H2（第二次顺势回调）"），**不得单独使用英文缩写**。
""".strip()

_PA_TERMINOLOGY_ZH = """
## 价格行为常用术语（简体中文，思考与 JSON 说明中优先使用）

| 术语 | 含义 / 用法提示 |
|------|----------------|
| 信号棒 | 触发入场计划的 K 线；极点外 1 跳动设止损/突破单 |
| 入场棒 | 实际触发入场的 K 线；须在信号棒之后 |
| 确认棒 / 跟随 | 信号或入场后 1–2 根同向延续；无跟随则信号易失败 |
| 突破 | 价格越过结构位、通道线、区间边界或信号棒极点 |
| 假突破 | 突破后快速回到原结构内；区间中常见 |
| 突破回踩 / 回测 | 突破后回撤测试被突破位再延续（勿与「历史回测」混淆） |
| 外包棒 | 高低点完全包含前一根；方向未定时勿追两端 |
| 内包棒 | 完全在前一根范围内；ii/iii 为连续内包 |
| 流星线 | 长上影、小实体，常作顶部拒绝 |
| 锤子线 | 长下影、小实体，常作底部拒绝 |
| 十字星 | 开收接近、多空犹豫 |
| 趋势棒 | 实体大、收盘近极点、影线短 |
| 铁丝网 | 极窄重叠区间，默认少交易 |
| 被套 | 突破方向上的交易者被迫止损离场 |
| 磁力位 | 失败信号棒/入场棒极点吸引价格回测 |

英文缩写须加中文解释（不可单独使用英文）：SB/EB→信号棒/入场棒、OB/IB→外包棒/内包棒、H1/H2→第一次/第二次顺势回调、L1/L2→第一次/第二次逆势回调、MTR→主要趋势反转、AIL/AIS→持续看多/持续看空、20GB→约20根K线未触及均线、TR→交易区间、MM→测量移动（等距目标位）、SPS→尖峰顺势突破、SCS→尖峰连续尖峰。
""".strip()

_STAGE2_API_TASK_RULE = """
## 阶段二 API 任务模式（硬约束，非聊天）

本次调用是 PA Agent **阶段二的一次独立 API 请求**。提示中虽含阶段一诊断 JSON，**不代表**阶段二已完成或可以收尾对话。

**禁止**输出：
- 「阶段一和阶段二都已输出完毕」「分析已完成」等会话总结
- 「告诉我你想怎么处理」「请选择 1/2/3/4」等菜单式追问
- Markdown 摘要、复盘建议、保存文件提示（除非写在 JSON 字段内）

**必须**：在 assistant 正文 `content` 输出**完整阶段二裸 JSON**（仅此一种交付物）。
""".strip()

_OPENCLAW_AGENT_NO_TOOLS_RULE = """
## PA Agent × QClaw 任务模式（硬约束）

你正在接收 **PA Agent 程序化 K 线分析**请求，不是通用编程/运维助手会话。

**禁止调用任何工具**，包括但不限于：`exec`、运行 Python/shell、读/写/编辑文件、浏览器、联网搜索、在 `~/.qclaw/workspace` 写中间 `.md`/`.json` 等。

- K 线表、EMA/ATR、几何特征、阶段一诊断（若有）**已全部在用户消息中给出**；禁止再拉数据或读盘。
- 风险点数、盈亏比、交易者方程、胜率估算等**一律在思考过程或 JSON 字段内心算**；禁止为 `risk=stop-entry` 之类简单算术启动解释器。
- **唯一交付物**：assistant 正文 `content` 中的裸 JSON（阶段一或阶段二 schema）。不得在磁盘上留档后再回复。

违反会导致分析极慢、工具刷屏，且程序无法解析你的输出。
""".strip()

_THINKING_CONTENT_OUTPUT_RULE = """
## 思考与正式输出分离（硬约束，违反则程序判定失败）

启用扩展思考时，**思考区仅用于推演草稿**；**程序只读取 assistant 消息的 `content`（正文）** 做 JSON 校验，**不会**把 `reasoning_content` / 思考流当作阶段结果。

**你必须做到：**
1. 思考可以较长，但思考结束后**必须在 `content` 正文里输出完整、可 `json.loads` 的裸 JSON 对象**（阶段一诊断 JSON 或阶段二决策 JSON）。界面会把思考流与 `content` 正文（撰写回答）都显示在「思考过程」窗口；**正文 JSON 仍必须写在 `content`，不能只在思考里写完。**
2. **禁止**把完整 JSON **只**写在思考里而让 `content` 为空、空白或纯叙述文字。
3. **禁止**在 `content` 里输出 markdown 说明、英文长文分析、或「详见上文思考」——`content` 里**只能**是裸 JSON。
4. 若思考预算较大，请**预留足够 token** 给最终 JSON；宁可压缩思考篇幅，也**不得**省略正文 JSON。

阶段一：`content` = 阶段一诊断 JSON（含 `gate_trace`、`gate_result` 等必填字段）。
阶段二：`content` = 阶段二决策 JSON（含 `decision`、`decision_trace`、`terminal` 等必填字段）。
""".strip()

_STAGE1_TAIL_REMINDER = (
    "【最后一步·必做】思考结束后，立即在 assistant 正文 `content` 输出完整阶段一裸 JSON。"
    "思考请用简体中文并尽量简洁；`content` 不得为空。"
    "禁止调用 exec/Python/写文件等工具。\n"
    "若 token 紧张：可缩短思考、将 bar_by_bar_summary 保持 5 根，"
    "但 gate_trace 与 gate_result 必须写在 JSON 末尾且不可省略。"
).strip()

_INCREMENTAL_OUTPUT_HARD_RULES = """
## 增量输出格式（硬约束，违反则程序自动重试）

本次是**程序自动分析**，不是人机聊天。assistant 正文 `content` **只能**是完整阶段一裸 JSON（以 `{` 开头、以 `}` 结尾）。

**禁止**在 `content` 里输出：
- Markdown 标题（`##`）、表格、项目符号摘要、emoji
- 「诊断已更新完毕」「如需进入阶段二」「随时告诉我」等对话用语
- 「诊断更新摘要」「主要更新字段」类 executive summary（变化说明应写在 JSON 的 `incremental_delta.summary`、`risk_warning`、`gate_trace` 等字段内）
- ` ```json ` 代码围栏或任何 markdown 围栏

**必须**：输出与全量阶段一相同 schema 的**完整** JSON（含 `incremental_delta`），不是差异补丁或文字版变更说明。
""".strip()

_MARKET_FEATURES_AUTHORITY_NOTE = (
    "⚠️ **程序结构辅助特征以本消息为准**：下方重算块覆盖上方阶段一 user 消息中"
    "任何旧版/缺失的结构摘要；若数值冲突，以本消息重算结果为准。\n\n"
)

_STAGE2_TAIL_REMINDER = (
    "【最后一步·必做】思考结束后，立即在 assistant 正文 `content` 输出完整阶段二裸 JSON"
    "（含 decision、decision_trace、terminal）。思考用简体中文并尽量简洁；`content` 不得为空。"
    "禁止调用 exec/Python/写文件等工具；算术在 JSON 推理字段内完成。\n"
    "若 token 紧张，优先保证 `content` 有 JSON，可缩短思考。\n"
    "⚠️ 禁止在 content 中只写思考过程或分隔符（如 ---输出JSON---）而不附 JSON——"
    "这会导致校验直接失败。哪怕只输出最小骨架 {\"decision\":{\"order_type\":\"不下单\",...}} 也比没有强。\n\n"
    "【⚠️ 输出前自检 — terminal.outcome 语义规则（在输出 JSON 前逐项确认）：】\n"
    "1. §9.0=否 时，**必须先写 §9.0P** 评估背景限价；仅当 §9.0P 也=否 且无三价方案时，"
    "terminal.outcome=wait（node_id=9.0P 或 9.0）。\n"
    "   **禁止** §9.0=否 后直接 wait 而跳过 §9.0P/§10。\n"
    "   §9.0=是（有合格信号棒）或 §9.0P=是（计划型限价）且有三价 → 继续 §10，不得因缺信号棒直接 wait。\n"
    "   禁止写 reject — 你没有东西可以拒绝（除非 §10.3 已有三价方案）。\n"
    "2. 你有入场方案（entry/stop/target 三价齐全），但 10.3 交易者方程不通过？\n"
    "   → 这才可以写 terminal.outcome=reject，node_id=\"10.3\"。\n"
    "3. 你有入场方案且 10.3 通过？→ terminal.outcome=trade，node_id 为最终节点。\n"
    "   **禁止**写 action/execute/entry 等自创词，只能是 wait|reject|trade|proceed。\n"
    "4. 限价/突破尚未触发？→ entry_bar.freshness=pending（禁止 limit_order_pending 等自创词）。\n"
    "5. `terminal` 与 `decision` **同级**（顶层字段），禁止把 terminal 嵌套在 decision 内。\n"
    "6. `next_cycle_prediction` 在 `unpredictable=false` 时 **必须** 含 `probabilities` 对象（各 cycle 概率，和≈100）。\n"
    "禁止在 JSON 前写「好的」「修改完成」等对话前缀；禁止 ```json 围栏。\n"
    "常见错误速查：§9.0=否 + §10.1=否 → outcome=wait（不是 reject！）"
).strip()

# ── Hardcoded output format reminders ─────────────────────────────────────────

_STAGE1_OUTPUT_REMINDER = """
请严格按照以下 JSON 格式输出诊断结果,不要输出任何其他内容。
**硬约束：思考结束后，必须在 assistant 正文 `content` 输出下方完整阶段一 JSON；不得仅在思考区分析而让 `content` 为空。**
**思考过程与 JSON 内所有说明性文字必须使用简体中文**（仅 JSON 键名与规定枚举除外）。
禁止用 markdown 代码围栏（不要写 ```json 或结尾的 ```），只输出裸 JSON 对象。
JSON 字符串内不要用英文双引号强调，改用「」或不用引号。

```json
{
  "cycle_position": "spike|micro_channel|tight_channel|normal_channel|broad_channel|trending_tr|trading_range|extreme_tr|unknown",
  "alternative_cycle_position": null,
  "direction": "bullish|bearish|neutral",
  "diagnosis_confidence": 75,
  "spike_stage": null,
  "climax_risk": "none|warning|triggered",
  "market_phase": "stable|transitioning",
  "transition_risk": null,
  "detected_patterns": [],
  "key_signals": [],
  "htf_context": "",
  "entry_setup": "",
  "support_levels": ["5402", "5119"],
  "resistance_levels": ["6147", "6300"],
  "strategy_files_needed": ["下跌通道分析识别.txt", "下跌通道交易策略.txt"],
  "risk_warning": "",
  "bar_analysis": {
    "always_in": "long|short|neutral",
    "last_closed_bar": "K1",
    "bar_type": "trend_bull|trend_bear|doji|inside|outside_bull|outside_bear|flat|other",
    "signal_bar": {
      "bar": "K2 或 null（无独立信号棒时填 null，禁止整个 signal_bar 为 null）",
      "quality": "strong|medium|weak|invalid（bar=null 时必须填 invalid，禁止填 null）",
      "reason": "信号棒质量判断"
    },
    "entry_setup_type": "H1|H2|L1|L2|MTR|wedge|tr_boundary|breakout_pullback|none",
    "follow_through": "yes|no|pending|failed"
  },
  "bar_by_bar_summary": [
    {
      "bar": "K1",
      "role": "structure|signal|entry|confirmation|noise|trap|climax|test",
      "bar_type": "trend_bull|trend_bear|doji|inside|outside_bull|outside_bear|flat|other",
      "context_effect": "strengthens_bull|weakens_bull|strengthens_bear|weakens_bear|weakened_bull|weakened_bear|neutral|transition",
      "follow_through": "yes|no|pending|failed",
      "trapped_side": "bulls|bears|both|none|unknown",
      "reason": "一句话说明该K线对当前市场状态的增量影响"
    }
  ],
  "gate_trace": [
    {
      "node_id": "1.2",
      "question": "是否能识别出当前市场周期？",
      "answer": "是",
      "reason": "K线结构特征清晰，可识别为正常通道",
      "branch": "normal_channel",
      "section": "K线识别",
      "bar_range": "K12-K1"
    },
    {
      "node_id": "2.1",
      "question": "近期结构是否呈现明确惯性方向？",
      "answer": "是",
      "reason": "LH+LL 结构清晰",
      "branch": "bearish",
      "section": "方向判断",
      "bar_range": "K8-K1"
    }
  ],
  "gate_result": "proceed"
}
```

## 阶段一闸门（二元决策树 §1–§2，必须执行）

在输出诊断 JSON 前，按《二元决策.txt》与内置提示文本**依次**评估以下节点，并写入 gate_trace：
**当 gate_result=proceed 时，必须包含节点 1.2、1.3、2.1、2.2、2.5 共 5 条（§1.1/§2.3/§2.4 由程序判定，AI 不输出）**（每条须填 bar_range；中间节点 reason 可留空，见下）：
§1：**§1.1 由程序判定**（数据量已通过前置闸门确认）→ 1.2 识别周期 → 1.3 极端混乱
- **节点 1.2**：answer 用 是/否；识别出的周期类型写在 **branch**（如 `broad_channel`、`trading_range`），**禁止** branch 写 `yes`/`no`。
§2：2.1 惯性方向 → 2.2 大时间框架 → **§2.3/§2.4 由程序判定，AI 不输出** → 2.5 惯性强度（**answer 只能用 是/否/中性**；方向或 AIL/AIS 写在 branch，勿写「多头」「空头」作 answer）

**§2.5 重要说明：§2.5 answer=否/中性 ≠ gate_result=wait。**
- §2.5 answer=否 或 answer=中性：均只代表惯性不足、不做激进趋势跟踪，**阶段二分析必须继续进入**，gate_result 必须为 **proceed**。
- **禁止**因 §2.5 判断惯性不足（否/中性）而将 gate_result 设为 wait——这是最常见的错误。
- gate_result=wait 只在以下情况下才成立：§1.2 无法识别周期（unknown）、§1.3 极端混乱（extreme_tr）。
- **校验硬规则**：当 gate_result=wait 时，gate_trace 最后一个节点的 answer 必须是"否"或"等待"，不得为"中性"。
- §2.5 答"否"或"中性"时，gate_result 仍为 proceed，阶段二切换为"等待反弹/回撤到位后的顺势信号"策略，并在 watch_points 中明确触发条件。

**禁止在阶段一评估：**
- **0.3**（交易者方程仅为原则；数值检验在阶段二 **10.3**）
- **§9–§11**（入场、风险、下单均属阶段二）

**逐K摘要硬规则：**
- 必须输出 `bar_by_bar_summary`，**恰好 5 条**（分析窗口≥5根时），覆盖最近 **K5–K1** 每一根已收盘 K 线各 1 条。数据不足 5 根则覆盖全部已有 K 线（每条 1 根）。
- 每条只写该 K 线对当前结构的增量作用，不写下单价格、不写止损止盈。
- `role` 只能使用示例中的英文枚举（structure/signal/entry/confirmation/noise/trap/climax/test 等）；延续/跟随棒统一写 `confirmation`，不要写 `continuation`。
- **`reversal_attempt`、`mtr`、`h2`、`l2` 等形态标签只能写在顶层 `detected_patterns` 数组中；禁止用作 `bar_by_bar_summary[].role`。**
- K线序号方向：K1 是最新已收盘，K2 是它前一根；判断 K2 的后续跟随时看 K1，判断 K3 的后续跟随时看 K2/K1；K1 的跟随通常为 pending。
- `bar_type` **必须与程序 K线几何特征表「类型」列完全一致，禁止自行推断或覆盖**。程序对每根 K 线只输出**一个** `bar_type`，按下列优先级判定（命中即停止，与 `kline_features._classify_bar` 一致）：
  1. **与前棒关系优先**：`inside`（高低点均在上一根范围内）→ `outside_bull` / `outside_bear`（外包上一根，按收阳/收阴）
  2. **无关系类时再判单棒几何**：`flat`（无有效振幅）→ `doji`（实体≤25%）→ `trend_bull`（阳线且收盘位置≥65%）→ `trend_bear`（阴线且收盘位置≤35%）→ `other`
- 外包棒会标为 `outside_*` 而不会再标 `trend_bull`/`trend_bear`，这是正常结果。若与肉眼感受不同，在 `reason` 补充说明，但 `bar_type` 字段必须照抄程序值，否则校验失败。
- `context_effect` 必须使用 **strengthens_bull / strengthens_bear**（带 s），禁止写 strengthen_bull、strengthen_bear。

**node_overrides（可选，默认不输出）：**
程序已为 §1.1/§2.3/§2.4 填充权威判定，**默认不要输出这些节点**。
仅当你识别到程序规则**未捕捉到**的明确结构性依据时，在顶层 `node_overrides` 数组中提交覆盖：
```json
"node_overrides": [
  {"node_id": "2.3", "answer": "是", "branch": "bearish", "override_reason": "近3根出现强势看跌反转，斜率窗口未捕捉到该结构突变"}
]
```
约束：§1.1/§9.1 为锁定节点不可覆盖；安全闸门（§10.3/§14）只能朝更保守方向；§2.3 answer/branch 须自洽（bullish/bearish↔是，neutral↔中性）；`answer` **禁止**写多头/空头/bullish/bearish，方向只写在 `branch`；不输出时请勿包含该字段。

**§2.3 覆盖门槛（三项全部满足才允许提交）：**
1. 指明具体是哪根 K 线（如 K2、K1）、哪个结构特征（如强势空头趋势棒跌破颈线、MTR 四组件齐全）导致方向突变；
2. 该特征明确超出程序三信号（EMA斜率/收盘重心位移/波段结构枢轴）的计算范围——例如出现程序窗口未捕捉到的突破/假突破/多空角力转换；
3. override_reason 须用具体 K 线序号和价格结构描述，不接受"整体看跌""趋势感觉已变"等模糊表述。

**§2.4 覆盖门槛（三项全部满足才允许提交）：**
1. 程序判定的 §2.4 reason 字段中**已出现** "⚠️ 近N根K线…与全窗口…结论存在背离"预警，或近期 K 线有明确的 EMA 跌破/站上事件（收盘价穿越 EMA20 且后续 K 线未立即修复）；
2. 指明具体是哪几根 K 线导致短期背离（如"K1-K5 有4根收于 EMA 下方"）；
3. override_reason 须同时说明：①短窗口背离的具体数据（几根中几根）；②为何认为该背离足以推翻全窗口 AIL/AIS 判定而非只是正常回撤。

规则：
- answer 只能是：是 / 否 / 中性 / 等待 / 不适用（**禁止**写「部分」「待确认」「待定」等——部分一致用 **中性**，尚需下一根K线确认用 **等待**）
- **gate_result=wait/unknown 的合法触发条件只有两个：§1.2 answer≠是（无法识别周期）或 §1.3 answer=否（极端混乱 extreme_tr）。** §2.1/§2.5 答「否」或「中性」、§2.3 中性、方向不明等**均不得**设 gate_result=wait——须 proceed 并在阶段二提高门槛或 wait 下单。
- §6/§9/§10 等阶段二节点的"否"答案不代表阶段一闸门阻断。
- gate_result=proceed 表示可通过闸门进入阶段二；wait/unknown 表示不应进入策略与下单评估（仅 §1.2/§1.3 可触发）
- gate_trace 与 cycle_position、direction 不得矛盾
- **中间节点 reason 可省略**：gate_trace / decision_trace 中除下列情况外，`reason` 可留空 `""` 或填 `"—"`，勿在每条分支重复长段说明：
  - 阶段一 **gate_trace 最后一条**（gate_result=proceed 时须含「闸门通过」或「进入阶段二」；wait/unknown 时写明等待原因）
  - 阶段二 **§10.3** 交易者方程（须写方程用到的入场/止损/目标数字或胜率假设）
  - node_overrides 的说明写在 **override_reason**，不要求 trace.reason
- bar_range、question、answer 仍必填；程序填充节点（§1.1/§2.3/§2.4/§9.1–§9.5/§11）由程序写入 reason，AI 不输出
- **禁止在 gate_trace 中输出 node_id 为 "14.1" 的节点**：14.1（禁止行为扫描）由程序自动注入，AI 输出会导致重复节点和校验失败
- **禁止在 gate_trace 中输出 `gate_end` / `gate_summary` / `summary` 等汇总节点**：`gate_result` 字段已表达 proceed/wait/unknown；闸门通过说明写在 **最后一条真实节点**（通常是 §2.5）的 reason 中，answer 只能用 是/否/中性/等待/不适用
- 节点 2.4 / 2.5 的 question 须与决策树原文一致（含 Always In 空格、用「支持」而非仅改措辞）

**每条 gate_trace / decision_trace 必须包含 bar_range（K线依据，由你自行判断）：**
- **程序不会替你填写**；你必须根据「本节点实际引用了哪些 K 线」写出序号范围
- 格式：`K{较老序号}-K{较新序号}` 或单根 `K1`（**序号1=最新已收盘**，序号越大越早）
- **⚠️ bar_range 禁止出现 K0**：K0 是当前未收盘棒，不在 frame 中。如需讨论"下一根K线"请写在 reason 中，bar_range 只能引用 K1~K{max}
- **每个节点的 bar_range 应不同**（除非该节点确实与上一节点使用完全相同窗口）；禁止所有节点照抄同一个范围
- 区间格式必须为 **K{较老}-K{较新}**（如 K4-K1），**禁止** K1-K4；单根写 K1；全图分析可写「全局」（程序会展开）
- **reason 里写到的每一根 K 线**（如「K4 之后」「对比 K2」）都必须落在该条 **bar_range** 内；勿在 bar_range=K2 的 reason 里单独提 K4——应写 **K4-K2** 或 **K4-K1**，或 reason 只谈 K2
- 方向/分类类节点（如 4.2 上涨还是下跌）：**answer 只用 是/否/中性**，方向写在 **branch**（bullish/bearish），勿写「上涨」「下跌」作 answer
- **6.2**（区间类型）：answer=是/否，branch=trending_tr 或 trading_range；勿把「趋势型交易区间」写在 answer
- **6.3**（是否在边界）：answer=是/否，branch=lower/upper/middle；勿写「是，在下边界」——应写 answer=是、branch=lower
- **阶段二 §14（禁止行为扫描，仅 decision_trace）**：`answer=是` = **触犯**禁止项；`answer=否` = **未触犯**。禁止用 `是` 表示「扫描完成/通过」。阶段一 gate_trace **不得**输出 §14/14.1。
- **禁止照抄**本提示 JSON 示例里的占位文字或说明中的举例数字；必须对应当前 K 线表与你在 reason 中的分析
- 跳过节点（skipped:true）：answer=不适用，bar_range 填字符串 `不适用`（**禁止填 null**）
- question 只写问题本身，不要把 bar_range 写进 question

diagnosis_confidence 必须为 0-100 的整数(满分100),表示对 cycle_position 等诊断结论的综合置信评分。
禁止使用 high、medium、low 等字符串;分数越高表示对当前市场状态判断越有把握。

diagnosis_confidence 分档说明（全系统统一阈值 **50**）:
- 90-100:周期位置非常典型,K线特征完全匹配频谱定义,长程背景与近期结构同向共振,信号充分无矛盾
- 70-89:周期位置较明确,主要特征吻合频谱定义,可能有个别模糊信号但不影响核心判断
- 50-69:周期位置存在歧义(如 trending_tr vs normal_channel),或长程背景与近期方向冲突(冲突不否决、不自动wait,仅降置信);需更多K线确认
- 30-49:信号严重矛盾,周期位置难以判定,K线特征与多种状态都有部分重叠;阶段二应显著降低 trade_confidence
- 0-29:数据不足以支撑任何诊断,或市场状态极度混乱(如极端交易区间)
- **<50 且 key_signals 为空**：阶段二强烈倾向 `order_type=不下单`（仍须经 §9–§10 完整评估，不得跳过）

**support_levels / resistance_levels 填写规则：**
- `support_levels`：从近期 K 线结构中识别出的**当前价格下方**支撑价位，按由近到远排列，最多 3 个。每项填价格字符串（如 `"5402"` 或 `"5380-5400"` 表示区间），不识别时填空数组 `[]`。
- `resistance_levels`：从近期 K 线结构中识别出的**当前价格上方**阻力价位，按由近到远排列，最多 3 个。格式同上，不识别时填空数组 `[]`。
- **突破后必须更新**：若 K1 收盘已跌破原支撑（支撑价位 ≥ 收盘价），该支撑不得继续保留，应改填**当前收盘价下方**最近的有效摆动低点/结构位；若 K1 收盘已突破原阻力（阻力价位 ≤ 收盘价），该阻力不得继续保留，应改填**当前收盘价上方**最近的有效摆动高点/结构位。增量分析时禁止照抄上一轮 support/resistance。
- 填写依据：近期摆动高低点、通道边界、EMA、前期整数关口、突破/失败突破位。**禁止**填写远离当前价格超过长程结构窗口波动幅度的历史高低点。
- 若市场处于 `extreme_tr` 或无法识别周期，允许填 `[]`。""".strip()

_STAGE2_OUTPUT_CONTRACT = """
请严格按照以下 JSON 格式输出决策结果，不要输出任何其他内容。
**硬约束：思考结束后，必须在 assistant 正文 `content` 输出下方完整阶段二 JSON；不得仅在思考区分析而让 `content` 为空。**
**思考过程与 JSON 内所有说明性文字必须使用简体中文**（仅 JSON 键名与规定枚举除外）。
禁止用 markdown 代码围栏（不要写 ```json 或结尾的 ```），只输出裸 JSON 对象。
JSON 字符串内不要用英文双引号强调，改用「」或不用引号。
重要规则：当 order_type 为“不下单”时，entry_price、take_profit_price、take_profit_price_2、stop_loss_price、order_direction 必须全部为 null。

```json
{
  "decision": {
    "order_direction": "做多|做空|null（禁止写 bearish/bullish/short/long）",
    "order_type": "限价单|突破单|市价单|不下单",
    "entry_price": null,
    "entry_basis_bar": null,
    "entry_basis_extreme": null,
    "entry_rule": null,
    "take_profit_price": null,
    "take_profit_price_2": null,
    "stop_loss_price": null,
    "reasoning": "",
    "diagnosis_confidence": 75,
    "diagnosis_confidence_reasoning": "",
    "trade_confidence": 70,
    "trade_confidence_reasoning": "",
    "estimated_win_rate": 50,
    "estimated_win_rate_reasoning": "",
    "key_factors": [],
    "watch_points": [],
    "risk_assessment": "",
    "invalidation_condition": ""
  },
  "diagnosis_summary": {
    "cycle_position": "",
    "direction": "",
    "key_signals": []
  },
  "bar_analysis": {
    "always_in": "long|short|neutral",
    "last_closed_bar": "K1",
    "bar_type": "【必须与阶段一 bar_analysis.bar_type 完全一致，不得重新推断】trend_bull|trend_bear|doji|inside|outside_bull|outside_bear|flat|other",
    "signal_bar": {
      "bar": "K2 或 null（计划型挂单尚无已收盘信号棒时为 null）",
      "quality": "strong|medium|weak|invalid",
      "pattern": "H1|H2|L1|L2|MTR|wedge|tr_boundary|breakout_pullback|none",
      "reason": "信号棒质量判断"
    },
    "entry_bar": {
      "strength": "strong|weak|not_triggered",
      "follow_through": true,
      "still_valid": true,
      "freshness": "fresh|pending|stale|invalid"
    },
    "second_entry": {
      "is_second_entry": true,
      "type": "H2|L2|MTR|wedge|tr_boundary|trendline|none（is_second_entry=false 时必须填 none，禁止 null）"
    }
  },
  "decision_trace": [
    {
      "node_id": "4.1",
      "section": "通道",
      "question": "是否出现有序波段结构？",
      "answer": "是",
      "reason": "HH+HL",
      "skipped": false,
      "bar_range": "由你填写"
    }
  ],
  "terminal": {
    "node_id": "11.2",
    "outcome": "trade",
    "label": "..."
  }
}
```

说明：decision_trace 需输出完整决策路径（通常多条）；每条 trace 的 **bar_range 必须由你根据该节点实际使用的 K 线填写**，不得照抄示例。
**⚠️ bar_range 禁止出现 K0**（K0 是未收盘棒，不在 frame 中；如需讨论下一根 K 线写在 reason 里）。
**每条 trace 的 answer 只能是以下五选一**：`是`、`否`、`中性`、`等待`、`不适用`。
禁止写「部分符合」「部分是」「上涨通道」等；模糊或分类细节写在 **reason**（方向类节点可另填 **branch**）。

**⚠️ diagnosis_summary.direction 与阶段一 direction 不一致时的强制规则：**

**⚠️ bar_analysis.bar_type 强制规则：必须直接沿用阶段一 `bar_analysis.bar_type` 的值，禁止在阶段二重新推断或修改。** 该值来自程序对 K1 的 `_classify_bar` 结果；若与肉眼感受不同，可在 reasoning 说明，但字段不得改。

**⚠️ bar_type 单字段优先级（全系统唯一标准，与程序 `_classify_bar` 一致）：**
- 每根 K 线只有**一个** `bar_type`；几何特征表「类型」列、`bar_by_bar_summary[].bar_type`、`bar_analysis.bar_type`（K1）**均须与程序预计算值完全一致**。
- 判定顺序（前一项命中则不再往下）：
  1. `inside` — 高低点均在上一根 K 线范围内
  2. `outside_bull` / `outside_bear` — 高低点均超出上一根（外包），按收盘≥开盘或反之
  3. `flat` — 无有效振幅
  4. `doji` — 实体占振幅 ≤25%
  5. `trend_bull` — 阳线且收盘位置 ≥65%
  6. `trend_bear` — 阴线且收盘位置 ≤35%
  7. `other` — 其余
- **禁止**臆造「几何 trend_bear + 关系 outside_bear」两套并行标签；外包棒程序只会输出 `outside_bear` 或 `outside_bull`。补充体感请写 `reason`，不要改 `bar_type`。
- `diagnosis_summary.direction` 必须与 `stage1.direction` **保持一致**，除非你在阶段二的 decision_trace 中以 **node_id="2.3"** 明确记录方向变更及原因。
- **例外（无需 2.3 节点）**：
  - 阶段一 direction=**neutral** → 阶段二 direction=bullish/bearish：程序判不了方向时 AI 阶段二识别出方向属于正常补充，校验器已豁免。不强制补写 2.3，但建议补（给本人看更清晰）。
  - 阶段二 将 direction 覆盖为 neutral 且周期属于震荡类（trading_range / extreme_tr / trending_tr）时。
- 若阶段一 direction=bullish/bearish，而阶段二判断方向反转，**必须**在 decision_trace 中加入：
  ```json
  {"node_id": "2.3", "section": "方向重判", "question": "阶段二是否重新判定市场方向？", "answer": "是", "branch": "bullish", "reason": "说明为何方向改变的具体依据", "skipped": false, "bar_range": "由你填写"}
  ```
  做空方向则 `"branch": "bearish"`。**`branch` 字段必须填写且值必须与 `diagnosis_summary.direction` 完全一致**（`bullish` 或 `bearish`）。
- 其他情况若未加 2.3 节点而 direction 不同，校验器**必定报错**。最稳妥的做法：**让 diagnosis_summary.direction 直接沿用阶段一的 direction 值**，只在有充分依据时才覆盖。

## 阶段二决策路径（二元决策树 §3–§11、§14）

阶段一 gate_result=proceed 时，decision_trace 必须遵守**执行顺序**（可跳过不适用分支，但不可乱序）：

1. **§3–§8** 按 cycle_position 走对应结构分支（尖峰/通道/区间/反转/楔形等）
2. **§9 执行顺序（硬规则）**：§9.0（信号棒）→ 若否则 **§9.0P（背景限价，必填）** → §9.4/§9.6/§9.7（AI）→ §9.1–§9.5（程序填充）。
   - **§9.0=否 不是终局**；必须评估 §9.0P 后才能决定 wait。
   - **§9.0P=是** → 继续 §10/§11 尝试限价单；三价写入 decision，**禁止**只在 watch_points 写触发价。
3. **§9.0、§9.0P、§9.4、§9.6、§9.7 由 AI 判定**，须写入 decision_trace
   - **§9.1/§9.2/§9.3/§9.5 由程序填充，AI 不输出**（程序依据几何特征确定性判断）
4. **§10** 风险收益（必须按序）：**10.1 止损明确 → 10.2 止损不过大 → 10.3 交易者方程**（勿编造具体手数、合约数或资金规模）
5. **§11 下单方式由程序填充，AI 不输出**（程序依据 cycle_position 路由，仅当 10.3=是 且下单时填充）
6. **§14** 禁止行为清单：下单前快速扫描，触犯任一条 → order_type=不下单
   - **⚠️ §14 answer 语义硬规则（违反会被程序强制改为不下单）：**
     - `answer=是` = **触犯了禁止行为**（程序据此强制 order_type=不下单）
     - `answer=否` = **未触犯任何禁止项**（可以继续下单）
   - **未触犯时必须写 `answer=否`**，不能写 `是`。许多 AI 误用 `是` 表示"已完成扫描"，这是错误的。
   - 例：扫描完成、无触犯 → `{"node_id":"14","answer":"否","reason":"扫描§14：未触犯任何禁止项。①...②..."}`
   - 例：触犯了宽通道追突破 → `{"node_id":"14","answer":"是","reason":"触犯：宽通道中追突破，放弃入场，order_type=不下单"}`

**node_overrides（可选，默认不输出）：**
仅当你识别到程序规则未捕捉到的明确结构性依据时，在顶层 `node_overrides` 数组中提交覆盖（如改变 §9.2/§9.3/§11 路由）：
```json
"node_overrides": [
  {"node_id": "9.3", "answer": "否", "override_reason": "信号棒虽ATR比值略超2，但止损结构合理，程序未考虑此场景"}
]
```
约束：§9.1 为锁定节点不可覆盖；§11 可横向切换（限价/突破/市价），但「不下单」不能改为下单；不输出时请勿包含该字段。

**交易者方程（10.3）规则：**
- 必须使用 **decision 中已填写的 entry_price / stop_loss_price / take_profit_price** 做数值计算（**take_profit_price_2 不参与 §10.3**），**禁止**用 K 线收盘、信号棒极点间距或「计划中的 1.8 点/3 点」代替三价
- **突破单须先定 entry 再定 stop/target**：按下方「极值±1跳动」公式写入 `entry_price` 后，再用这三价做 10.3；程序校验前会把错误的突破 entry **校正**为极值±跳动。校正后若盈亏比/方程仍不达标，**10.3 必须判否**且 `order_type=不下单`
- `decision_trace[10.3].reason` 中的入场/止损/目标数字必须与 `decision` 三价一致（勿用未写入 decision 的中间价）
- 做多：风险点数 = entry − stop，回报点数 = take_profit_price − entry；做空：风险 = stop − entry，回报 = entry − take_profit_price
- 盈亏比 = 回报 ÷ 风险（程序与界面只认此公式；reasoning 中写的 RR 必须与三价一致，否则校验失败）
- **无盈亏比上限（模型侧）**：按结构自由定 entry / TP1 / TP2 / stop；**禁止**为凑 RR 而缩小 TP1 或贴噪音止损。程序会在 RR>1.0 时自动向外扩 stop（保持 TP1/TP2 不变）。
- **定价顺序（推荐）**：
  1. 定 **entry**（结构位/边界/回撤位或突破极值±跳动）
  2. 定 **take_profit_price（TP1）** 于最近有效结构目标（通道对边、区间对侧、前 swing 等）
  3. 定 **take_profit_price_2（TP2）** 于更远结构目标（Measured Move、通道对边远端、区间翻测等）
  4. 定 **stop_loss_price** 于结构失效位（信号棒/波段极点外 1 跳等）
  5. 若按结构 stop 算得 RR = 回报÷风险 **> 1.0**：**保持 TP1/TP2 不变**；程序校验时会自动向外扩 stop（模型也可先自行扩 stop）
  6. 若结构 stop 已是最宽合理位且 RR 仍 > 1.0：程序会自动扩 stop；只要 §10.3 交易者方程通过即可
  7. 若结构 stop 导致 RR < 1.0：优先**收紧** stop 至更近的结构失效位，或调整 entry；**禁止**向外扩 stop；仍无法 ≥1.0 → reject
- **TP1 / TP2 硬规则**：
  - 有下单时 `take_profit_price` 与 `take_profit_price_2` **均必填**；不下单时均为 null
  - 做多：stop < entry < take_profit_price < take_profit_price_2
  - 做空：take_profit_price_2 < take_profit_price < entry < stop
  - §10.3 交易者方程与 RR 校验**仅使用 take_profit_price（TP1）**；TP2 不得用于方程计算
- 有下单时：盈亏比须 **≥ 1.0**（回报÷风险），且须满足 **胜率%×回报 > (100−胜率)%×风险**
- 不满足上述任一条 → **10.3 必须判「否」**，order_type=**不下单**，不得输出限价/突破/市价单
- **10.3 通过之前**不得输出具体下单类型；**10.3 之后**才写 §11
- 因方程不通过而放弃（已有三价方案）：terminal.node_id 应为 **10.3**，outcome=**reject**（有方案可拒，不用 wait）
- 完成 10.3 后，必须把你在方程中使用的**胜率主观估计**写入 decision.estimated_win_rate（0–100 整数），并在 estimated_win_rate_reasoning 简要说明依据；**禁止**留空或仅从 trace 文字里暗示

**结构型止损 / 止盈质量规则（防止噪音内小单）：**
- `stop_loss_price` 必须放在「本笔交易假设真正失效」的结构位之外，而不是为了通过 10.3 方程而贴近 EMA、K1 low/high、整数位或单根 K 线内部噪音。
- 若止损只是在 EMA / 支撑 / 阻力外侧很近的位置，且没有越过明确 swing low/high、信号棒极点、通道边界失效位或区间边界失效位，则视为「噪音内止损」；§10.1 或 §10.2 应判「否」。
- `take_profit_price`（TP1）应放在有结构依据的最近有效目标位，不要为了通过方程而选 K1 内部噪音位
- `take_profit_price_2`（TP2）应为更远但有结构依据的目标（MM 投影、通道对边远端、区间高度翻测等）；必须满足做多 tp2>tp1、做空 tp2<tp1
- 若结构止损合理但 RR < 1.0：收紧 stop 或调整 entry；**禁止**向外扩 stop；若仍 < 1.0 或方程不通过 → `order_type=不下单`
- 计划型限价单只有在「结构失效位」和「目标结构位」都清晰时才可执行；宽通道 / 区间边界 setup 只是允许进入评估，不代表必须下单。

**计划型限价优先级（背景与周期 > 独立信号棒）：**
- 阶段一 `gate_result=proceed` 且 `cycle_position` 为 broad_channel / trading_range / normal_channel / trending_tr 时，**默认先评估计划型限价（§9.0P）**。
- **无独立信号棒（signal_bar.bar=null）** → §9.0=否，**必须**继续 §9.0P；若 §9.0P=是 则给出限价三价。
- `direction=neutral`、K1 为 doji/inside/弱棒、或 `transition_risk=medium` **单独出现**时，仍应尝试 §9.0P 边界/回撤限价；仅当 **§10.1–10.3 无法通过** 或 **§14 触犯** 时才 `不下单`。
- 禁止以「等下一根 K 确认信号棒」为由跳过 §9.0P——计划型限价本来就是等价格到位。
- 仅当同时满足：**区间/通道中部（6.3=middle）**、**无结构锚点定 stop**、**K1 已穿过计划 entry/stop**、或 **barbwire 且无边界锚点** 时，才可在 §9.0P 判否。

**低质量计划型限价降级规则（已弱化 — 仅作风控提醒，非默认不下单）：**
- 以下情形**倾向降低 trade_confidence**，但**不自动**改为 `不下单`；若 10.3 通过且结构清晰，仍应输出限价单并在 reasoning 说明接受的瑕疵：neutral 方向、transition_risk 偏高、diagnosis_confidence<50、K1 弱棒无跟随、方程边际通过。
- 只有 **10.3 不通过** 或 **§14 触犯** 时，才必须 `order_type=不下单`。

**突破单不可用时的限价单备选路径（重要）：**
- 当通道/趋势结构默认倾向突破单，但**当前没有合格突破入场**（信号棒失效、无跟随、极点不清晰、无法填写 entry_basis_bar/extreme、突破已错过等）时，**不要直接输出「不下单」**。
- 若结构方向仍清晰，且能在**支撑/阻力/通道边界/EMA/前棒极点**附近设定限价 entry，并能给出清晰的**结构失效止损**与**有效结构目标**，且 **10.3 交易者方程可通过（数学期望为正）** → **应尝试 `order_type=限价单`**。
- 限价备选典型场景：顺势回撤到结构位做多/做空、区间边界反弹/回落、宽通道靠边界挂单、突破测试失败后的反向结构位。
- 限价单 `entry_basis_*` 可填 null；`signal_bar.bar` 可为 null（quality=invalid），须在 **§9.0P** 说明「计划型限价，等待回撤/反弹到位」；`entry_bar` 设 not_triggered/pending。
- 仅当**突破与限价两种路径均无法**给出满足 §10.1–10.3 的三价方案时，才 `order_type=不下单`。

**§9.0P 计划型限价（宽通道/区间/通道边界 — 无合格信号棒时的正式路径）：**
- **§9.0** 只评「是否已有合格收盘信号棒」；无信号棒 → §9.0=否，**必须**写 **§9.0P**。
- 当 cycle_position 为 **broad_channel / trading_range / normal_channel / trending_tr**，且价格靠近 **支撑/阻力/通道边界**（阶段一 support_levels/resistance_levels），或顺势 **回撤/反弹到结构位** 可挂限价时：
  - **§9.0P 应判「是」**，reason 写明「计划型限价，等待回撤/反弹到位」；
  - §9.0 同时写「否」（无合格信号棒）；
  - `signal_bar.bar` 为 **null**，`quality=invalid` 或 **weak**；
  - `entry_bar` 设 `strength=not_triggered`、`freshness=pending`；
  - 继续 §10 定三价 → 10.3 通过 → `order_type=限价单`。
- **禁止**因 K1 为 doji/弱棒/无跟随，就 §9.0P=否 后直接 `不下单`——应先尝试结构位限价。
- **§9.0P=否/等待**：区间/通道 **中部**、`barbwire`/重叠区无边界锚点、无结构锚点定 stop、K1 已穿过计划 entry/stop、或 §14 触犯。
- **direction=neutral 时**：§9.0P 默认 **wait**（禁止双边边界挂单）；仅 §9.0=是 且有合格顺向信号棒时可下单。

**宽通道 vs Always In（硬规则）：**
- **禁止一切逆势**；宽通道仅顺 direction / Always In 一侧。
- AIS 下宽通道上边界**禁止做多**；AIL 下宽通道下边界**禁止做空**。

**楔形 / 三推（硬规则）：**
- **末端楔形 / 楔形反转 / 三推递减**（与主趋势同向）→ 意味反转*可能*发生：**禁止追顺势**、**禁止逆势三价**；`order_type=不下单`，仅 `watch_points`。
- **楔形回撤**（与主趋势相反的楔形）→ 突破确认后可评估**顺主趋势**；若同时 `climax_risk` 或末端三推特征，**禁止追单**。

**限价单 K1 新鲜度（计划型 pending 与已触发区分）**：
- **计划型限价**（entry_bar.freshness=pending / not_triggered）：只检查 entry 相对 **K1.close** 的方向是否正确（做多 entry < close；做空 entry > close）。
  - **禁止**因 K1 影线曾触及 entry 就判失效 — 限价等的是**未来**回撤/反弹。
  - K1 已触及 **stop** 仍必须 `不下单`。
- **非计划型 / 已触发限价**：仍用完整 K1 high/low 对照（K1 已走过 entry 则 stale）。
- 若 K1.close 已在 entry 错误一侧（买单 close 低于 entry、卖单 close 高于 entry）→ reprice 或 `不下单`。

**突破单 entry_price 硬规则（程序会按 K 线表小数位推断最小跳动并校验）：**
- order_type="突破单" 时，必须填写 decision.entry_basis_bar、decision.entry_basis_extreme、decision.entry_rule。
- 做多突破单：entry_basis_extreme 必须为 "high"。从 K 线表读出 entry_basis_bar 的 **high**，设 `entry_price = high + 1×最小跳动`（**必须严格大于 high，禁止等于 high**）。示例：K1 high=4556.595、跳动=0.001 → entry_price=4556.596。
- 做空突破单：entry_basis_extreme 必须为 "low"。从 K 线表读出 entry_basis_bar 的 **low**，设 `entry_price = low − 1×最小跳动`（**必须严格低于 low**；禁止用 K 线中部、收盘价或高于 low 的价位）。示例：K2 low=10.67、跳动=0.01 → entry_price=10.66。
- **做空突破单 basis 必须是 low**：即使叙事是「反弹至高点做空」，突破单仍挂在依据 K 的 **低点下方** 突破位；禁止写 `entry_basis_extreme="high"`（与「限价在高点附近做空」不同）。
- entry_rule 只写挂单位置规则（如「K1 高点上方 1 跳动」），**禁止**在 entry_rule 里重复 order_type/方向或写 `entry_price=` 公式串。
- 突破单禁止使用 K 线实体中部、收盘价、EMA 或「约等于高点」作为 entry_price。
- 若无法从 K 线表确定依据 K 的 high/low 或最小跳动，应 order_type="不下单"，勿编造中间价。
- 限价单/市价单不使用 entry_basis_* 字段，可填 null。

**§9 逐K信号链与新鲜度硬规则：**
- §9.0–§9.7 必须引用 `bar_analysis.signal_bar.bar` 与阶段一 `bar_by_bar_summary` 中的对应 K 线；计划型限价时 `signal_bar.bar` 为 null，须在 **§9.0P**（非 §9.0）写明依据；`quality="invalid"`、`pattern="none"`。若限价单/突破单尚未触发，`bar_analysis.entry_bar.bar` 可为 null，但必须设 `strength="not_triggered"`、`freshness="pending"`，并在 9.7 写明“等待触发，尚无入场棒”。
- **⚠️ 市价单 entry_bar 硬规则**：`order_type="市价单"` 代表基于当前已收盘棒立即入场，**不存在「等待触发」状态**。`entry_bar.bar` 必须填写信号棒（通常为 K1），`strength` 设为 `strong` 或 `weak`，`freshness` 设为 `fresh`，`follow_through` 设为 `true`。**禁止**市价单将 `entry_bar.bar` 填为 null 或将 `freshness` 填为 `pending`——这会导致校验失败。
- **K 线序号约定**：K 数字越大表示越早的已收盘棒（K8 早于 K1）。信号棒通常比入场棒更早，故 signal_bar 的 K 序号 **大于** entry_bar 的 K 序号（例：K3 信号 → K1 入场）。
- 如果信号棒之后已经出现 2–3 根无跟随、反向强 K、或 `entry_bar.freshness=stale|invalid`，不得继续把旧信号当作新的突破单依据。
- 如果最新 K1 是 doji、弱入场棒、无跟随或反向确认，应降低 trade_confidence；但若 **计划型限价边界 setup（§9.0P=是）** 且周期/方向/结构位一致，**仍应继续 §10 并尝试限价单**，不要仅因 K1 不完美就 `不下单`。
- 当 `bar_analysis.signal_bar.quality=weak|invalid`，或已触发入场棒但 `entry_bar.follow_through=false` 时，若仍下单，必须在 §9 和 reasoning 中明确说明为何该弱点未使信号失效；否则应等待。挂单未触发时不得把 `follow_through=false` 当作失败跟随，应写 `pending`。
- **计划型限价单**：quality=weak|invalid 且 entry_bar 为 pending 时，**不视为**必须观望；须在 **§9.0P** 判「是」并说明结构位/setup 依据。

**⚠️ watch_points 与 stage1 risk_warning 一致性规则（必须遵守）：**
- 阶段一 `risk_warning` 是风险警示，**watch_points 中的触发条件不得与其直接矛盾**。
- 典型违反：risk_warning 说"在 4435–4440 底轨区域不宜追空"，watch_points 却建议"下破 4438 追空"——这是在 risk_warning 明确警示的区域做 risk_warning 禁止的操作。
- **写 watch_points 前必须回顾 stage1 risk_warning**：如果你的触发条件恰好落在 risk_warning 描述的风险区域，必须在 watch_points 里注明该风险或修改触发条件以避开冲突区域。
- 如果有充分依据认为 risk_warning 的风险在阶段二已经消除，必须在 reasoning 里明确说明原因。

**⚠️ detected_patterns 必须引用规则：**
- 阶段一 `detected_patterns` 中识别出的每一个形态（如 `failed_signal`、`magnet`、`breakout_test`、`breakout_failure`）都与当前交易风险直接相关。
- **阶段二 reasoning 和 watch_points 中必须明确引用 detected_patterns 中的形态**，说明它们对本次交易决策的影响（支持还是否定入场，或设为 watch 条件）。
- 不得从头重新推理而完全忽略 detected_patterns 中已识别的形态。

**跳过规则：**
- 无持仓：跳过 §12、§13（不写 trace）
- 不适用分支：skipped:true，answer=不适用

terminal 必须与 order_type 一致（**decision 与 decision_trace 同步**）：
- 有下单 → outcome=trade，10.3 必须为「是」，decision 含有效三价
- 不下单 → outcome=wait 或 reject，order_type=不下单，三价与 order_direction 均为 null
- **禁止** decision 写突破单/限价单/市价单，同时 decision_trace 里 10.3=否 或 terminal=reject
- **§14 是禁止行为扫描，不是成交终局节点**：若 §14 answer=否（未触犯）且有下单，terminal.node_id 应填最终 §11 下单节点（如 `11.2`/`11.3`）或 `10.3`，**禁止**填 `14`/`14.1`；只有 §14 answer=是（触犯）时才可作为拒绝/等待的终止原因，且必须 `order_type=不下单`。

**⚠️ terminal.node_id 和 outcome 的语义规则（必须区分以下两种情形）：**

情形 A：**有入场计划，但交易者方程不通过**（有具体止损、止盈数字，但盈亏比不达标）
→ `terminal.node_id = "10.3"`，`outcome = "reject"`
→ 典型表现：10.3 trace 里有具体数值计算，方程结果为负

情形 B：**§9.0=否 且 §9.0P=否**（或 §10.1=否 因无止损锚点）
→ `terminal.node_id = "9.0P"`（或 9.0），`outcome = "wait"`
→ **不能** terminal 在 10.3，因为从未有过可评估的交易方案
→ **不能** 写 outcome="reject"——拒绝一个不存在的方案在语义上是无意义的

常见错误：§9.0=否 → **跳过 §9.0P** → §10.1=否 → terminal=10.3/reject
正确做法：§9.0=否 时**必须先写 §9.0P**；仅当 §9.0P 也=否（或 §10.1 因无锚点=否）→ terminal.node_id=**9.0P**（或 9.0），outcome=**wait**；10.3 不应出现在 trace 里（或标 skipped=true）。**禁止**在未评估 §9.0P 时因 §9.0=否 直接 terminal=9.0。

阶段一 gate_result 为 wait/unknown 时：系统会短路，不应调用本阶段。

**decision.reasoning 字数硬限制：≤280 字（中文）**。只写结论 + 1–2 个关键依据（周期/信号/为何不下单或为何入场），细节放在 decision_trace 的 §10.3 或 key_factors，勿重复推演整棵决策树。

置信度分为两部分，各自独立打分（均为 0–100 整数，必须填写）：

一、diagnosis_confidence —— 对市场趋势与市场周期判断的把握
分档说明：
- 90-100：周期位置非常典型，趋势方向明确，多时间框架一致，K线特征完全匹配频谱定义
- 70-89：周期位置较明确，趋势方向可判定，主要特征吻合，可能有个别模糊信号
- 50-69：周期位置存在歧义（如 trending_tr vs normal_channel），趋势方向不够清晰，信号部分矛盾
- 30-49：信号严重矛盾，周期位置难以判定，趋势方向不确定
- 0-29：市场极度混乱或数据不足，无法做出有效诊断
diagnosis_confidence_reasoning：必须简要说明打分依据（如“trending_tr 与 normal_channel 特征重叠，HTF 方向与小框架不一致”）

二、trade_confidence —— 对交易决策本身的把握
分档说明：
- 90-100：极高把握，入场方案结构清晰、理由充分，风险回报比优异
- 70-89：较高把握，主要逻辑明确，入场方案可行
- 50-69：中等把握，存在不确定性但仍可执行当前决策（含观望）
- 30-49：较低把握，建议继续等待更清晰信号
- 0-29：极低把握；若同时判断不应交易，可配合 order_type="不下单"
trade_confidence_reasoning：必须简要说明打分依据（如“入场信号明确但止损空间偏大，risk:reward 仅 1.5:1”）

三、estimated_win_rate —— 对**本笔交易方案**成交后获利概率的主观估计（0–100 整数）
- 与 trade_confidence **不是同一概念**：trade_confidence 是对「是否该做这笔决策」的把握；estimated_win_rate 是「若按该 entry/stop/target 成交，你认为获胜的概率」
- **必须在 §10.3 交易者方程评估完成后**由你自行判断并填写；须与 10.3 节点 reason 中的胜率假设一致
- order_type=「不下单」时：estimated_win_rate 填 **null**，estimated_win_rate_reasoning 填 **null**（无交易方案，无胜率可估）
- 有下单时：estimated_win_rate 为 **必填整数**（不要填区间字符串，取你判断的最可能值，如 47）
estimated_win_rate_reasoning：必须简要说明依据（如“宽通道顺势 Low1，结构支持约 45–50%，取 47% 用于方程”）
""".strip()

# ── Analysis-mode–aware Stage 1 output rule ───────────────────────────────────


def _stage1_output_reminder_for_mode(analysis_mode: str = "original") -> str:
    """Return Stage 1 output rules (same for original and optimized).

    Program prefill handles §1.1/§2.3/§2.4; AI outputs the five gate nodes only.
    """
    _ = analysis_mode  # reserved for future mode-specific tweaks
    return _STAGE1_OUTPUT_REMINDER


_NEXT_BAR_PREDICTION_INSTRUCTION = """\
## 下一根K线预测任务（阶段二附加输出，不影响下单决策）

完成 decision / decision_trace / terminal 后，必须在阶段二 JSON 顶层追加键 `next_bar_prediction`，
表达对下一根（尚未开始或正在形成）K线收盘后的方向预测：

```json
"next_bar_prediction": {
  "direction": "bullish|bearish|neutral",
  "probabilities": {"bullish": 45, "bearish": 35, "neutral": 20},
  "reasoning": "简体中文理由，30–1500 字。须明确引用阶段一诊断、最近 K 线几何特征、以及（若提供）上一轮预测摘要。",
  "unpredictable": false,
  "features_used": ["stage1_diagnosis", "kline_features"]
}
```

硬约束（违反则整体阶段二 JSON 校验失败）：

1. probabilities 三个值均为 0–100 整数，三者之和必须落在 [99, 101]（容差 ±1，源于取整）。
2. direction 必须等于 probabilities 中数值最大的键；并列最大时取 JSON 出现顺序中靠前的键
   （即按 bullish → bearish → neutral 的字面顺序）。
3. reasoning 长度 30–1500 字，简体中文，不写下单价格、不写止损止盈，仅讨论方向与概率依据。
4. features_used 合法取值封闭列表（只能从下方选对应值，禁止自造字符串）：
   "stage1_diagnosis"、"kline_features"、"analysis_history"、"experience_library"、"stage2_decision"、"previous_prediction_summary"。
   至少包含 "stage1_diagnosis"；若提示词中提供了对应来源，应同步包含
   "kline_features" / "analysis_history" / "experience_library" / "previous_prediction_summary"。
5. 数据不足（K 线数 < 8）、或阶段一诊断为 extreme_tr / unknown、或市场极端混乱时：
   设 unpredictable=true，direction=null，probabilities=null，reasoning 写明原因。
6. 此预测**不**进入交易者方程、**不**改变 decision 中任意字段，仅作辅助参考。
""".strip()

_NEXT_BAR_DISABLED_NOTE = """\
## 下根K线预测（用户已关闭，程序处理）

用户已关闭「下根K线预期」功能以节省 token：**你无需在 JSON 中输出 `next_bar_prediction`**。
程序校验时会自动补全占位字段；请把篇幅集中在 decision / decision_trace / terminal /
`next_cycle_prediction` 上，勿因缺少 `next_bar_prediction` 反复重试。
""".strip()

def _build_next_cycle_prediction_instruction(*, enable_next_bar: bool) -> str:
    """Return next-cycle instruction; avoid referencing next_bar when that feature is off."""
    if enable_next_bar:
        return _NEXT_CYCLE_PREDICTION_INSTRUCTION
    return _NEXT_CYCLE_PREDICTION_INSTRUCTION.replace(
        "完成 next_bar_prediction 后，必须在阶段二 JSON 顶层追加键 `next_cycle_prediction`，",
        "必须在阶段二 JSON 顶层追加键 `next_cycle_prediction`，",
    )


_NEXT_CYCLE_PREDICTION_INSTRUCTION = """\
## 下一个市场周期预测任务（阶段二附加输出，不影响下单决策）

完成 next_bar_prediction 后，必须在阶段二 JSON 顶层追加键 `next_cycle_prediction`，
表达对当前市场周期结束后、下一个市场周期的预测：

```json
"next_cycle_prediction": {
  "cycle": "broad_channel",
  "direction": "bullish",
  "probabilities": {
    "spike": 3,
    "micro_channel": 5,
    "tight_channel": 8,
    "normal_channel": 20,
    "broad_channel": 35,
    "trending_tr": 15,
    "trading_range": 10,
    "extreme_tr": 4
  },
  "reasoning": "简体中文理由，1–1500 字。须引用阶段一周期诊断、K 线结构演变特征，说明各周期概率依据。",
  "unpredictable": false,
  "features_used": ["stage1_diagnosis", "kline_features"]
}
```

市场周期枚举（cycle 字段的合法取值，共 8 个，不含 unknown）：
spike | micro_channel | tight_channel | normal_channel | broad_channel | trending_tr | trading_range | extreme_tr

硬约束（违反则整体阶段二 JSON 校验失败）：

1. probabilities 八个值均为 0–100 整数，八者之和必须落在 [99, 101]（容差 ±1，源于取整）。
2. cycle 必须等于 probabilities 中数值最大的键；并列最大时按上方枚举的字面顺序取靠前者
   （即 spike → micro_channel → tight_channel → normal_channel → broad_channel → trending_tr → trading_range → extreme_tr）。
3. direction 为独立的方向预测（bullish / bearish / neutral），不由 cycle argmax 强制推导；
   表达的是预测下一个周期时市场整体偏向的方向。
4. reasoning 长度 1–1500 字，简体中文，仅讨论周期演变依据，不写下单价格、不写止损止盈。
5. features_used 合法取值封闭列表（只能从下方选对应值，禁止自造字符串）：
   "stage1_diagnosis"、"kline_features"、"analysis_history"、"experience_library"、"stage2_decision"、"previous_prediction_summary"。
   至少包含 "stage1_diagnosis"；若提示词中提供了对应来源，应同步包含
   "kline_features" / "analysis_history" / "experience_library" / "previous_prediction_summary"。
6. 数据不足（K 线数 < 8）、或阶段一诊断为 extreme_tr / unknown、或市场极端混乱时：
   设 unpredictable=true，cycle=null，direction=null，probabilities=null，reasoning 写明原因。
7. 此预测**不**进入交易者方程、**不**改变 decision 中任意字段，仅作辅助参考。
""".strip()

# txt files merged into each stage prompt (order preserved)
COMMON_SYSTEM_STAGE1_TXT_FILES: tuple[str, ...] = (
    "提示词大纲_人设与思维方式.txt",
    "二元决策.txt",           # unified with Stage 2 for prefix caching; §0–§2 gate subset is included
)
COMMON_SYSTEM_STAGE2_TXT_FILES: tuple[str, ...] = (
    "提示词大纲_人设与思维方式.txt",
    "二元决策.txt",
)
# Back-compat alias for UI helpers that list “common” files (Stage 2 full tree).
COMMON_SYSTEM_PROMPT_TXT_FILES: tuple[str, ...] = COMMON_SYSTEM_STAGE2_TXT_FILES

# Process-wide system prompt cache: DeepSeek KV hits need byte-identical prefixes
# across PromptAssembler instances that share the same prompt directory.
_SYSTEM_PROMPT_CACHE: dict[str, str] = {}

STAGE1_TASK_PROMPT_TXT_FILES: tuple[str, ...] = (
    "市场诊断框架.txt",
    "文件16-K线信号识别.txt",
)

EASTMONEY_ORDER_BOOK_PROMPT_FILE = "东方财富盘口分析规则.txt"

_CHANNEL_FILE_GROUPS: dict[str, tuple[str, ...]] = {
    "bullish": (
        "上涨通道分析识别.txt",
        "上涨通道交易策略.txt",
    ),
    "bearish": (
        "下跌通道分析识别.txt",
        "下跌通道交易策略.txt",
    ),
}
_SPIKE_FILE_GROUPS: dict[str, tuple[str, ...]] = {
    "bullish": (
        "极速上涨分析识别.txt",
        "极速上涨交易策略.txt",
    ),
    "bearish": (
        "极速下跌分析识别.txt",
        "极速下跌交易策略.txt",
    ),
}

STAGE2_BASE_PROMPT_TXT_FILES: tuple[str, ...] = (
    "逐棒分析检查单.txt",
    "文件16-K线信号识别.txt",
    "文件17-止损和止盈与仓位管理.txt",
    "文件23-MeasuredMove与结构目标.txt",
)

STAGE2_FULL_STRATEGY_PROMPT_TXT_FILES: tuple[str, ...] = (
    "上涨通道分析识别.txt",
    "上涨通道交易策略.txt",
    "下跌通道分析识别.txt",
    "下跌通道交易策略.txt",
    "极速上涨分析识别.txt",
    "极速上涨交易策略.txt",
    "极速下跌分析识别.txt",
    "极速下跌交易策略.txt",
    "震荡区间分析识别.txt",
    "震荡区间交易策略.txt",
    "文件13-窄通道与宽通道策略.txt",
    "文件14-楔形形态分析交易.txt",
    "文件15-二次入场机会.txt",
    "文件18-突破失败与突破测试.txt",
    "文件19-H1H2-L1L2计数.txt",
    "文件20-AlwaysIn与20GB.txt",
    "文件21-铁丝网与无交易环境.txt",
    "文件22-信号失败后的磁力位.txt",
    "文件24-最终旗形与趋势末端.txt",
    "文件25-主要趋势反转MTR.txt",
    "文件27-三角形与收敛形态.txt",
    "文件28-双重顶底与微型结构.txt",
)


def _fmt_feature(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def stage1_prompt_txt_files() -> list[str]:
    """Return ordered .txt filenames injected in the Stage 1 prompt."""
    return [*COMMON_SYSTEM_STAGE1_TXT_FILES, *STAGE1_TASK_PROMPT_TXT_FILES]


def _directional_channel_files(direction: str) -> list[str]:
    key = str(direction or "").strip().lower()
    if key in _CHANNEL_FILE_GROUPS:
        return list(_CHANNEL_FILE_GROUPS[key])
    return []


def stage2_user_task_txt_files(
    strategy_files: list[str] | None = None,
    *,
    direction: str = "",
    load_full_strategy_library: bool = False,
) -> list[str]:
    """Return .txt filenames loaded into the Stage 2 user turn only."""
    routed = [f for f in (strategy_files or []) if f]
    if load_full_strategy_library:
        core = [*STAGE2_FULL_STRATEGY_PROMPT_TXT_FILES, *STAGE2_BASE_PROMPT_TXT_FILES]
    else:
        dir_key = str(direction or "").strip().lower()
        opposite = (
            _CHANNEL_FILE_GROUPS.get("bearish", ())
            if dir_key == "bullish"
            else _CHANNEL_FILE_GROUPS.get("bullish", ())
            if dir_key == "bearish"
            else ()
        )
        opposite_spike = (
            _SPIKE_FILE_GROUPS.get("bearish", ())
            if dir_key == "bullish"
            else _SPIKE_FILE_GROUPS.get("bullish", ())
            if dir_key == "bearish"
            else ()
        )
        skip = frozenset((*opposite, *opposite_spike))
        core = [
            f
            for f in routed
            if f not in skip
        ]
        core.extend(STAGE2_BASE_PROMPT_TXT_FILES)
    return list(dict.fromkeys([*core]))


def stage2_prompt_txt_files(
    strategy_files: list[str] | None = None,
    *,
    direction: str = "",
    load_full_strategy_library: bool = False,
) -> list[str]:
    """Return all .txt files relevant to Stage 2 (system common + user task), for UI/debug."""
    return [
        *COMMON_SYSTEM_STAGE2_TXT_FILES,
        *stage2_user_task_txt_files(
            strategy_files,
            direction=direction,
            load_full_strategy_library=load_full_strategy_library,
        ),
    ]


# ── PromptAssembler ────────────────────────────────────────────────────────────

class PromptAssembler:
    """Builds message lists for Stage 1 and Stage 2 API calls."""

    def __init__(
        self,
        prompt_dir: Path,
        experience_reader: Any = None,
        *,
        prompt_settings: Any = None,
    ) -> None:
        self._prompt_dir = prompt_dir
        self._experience_reader = experience_reader
        self._prompt_settings = prompt_settings
        self._txt_cache: dict[str, str] = {}

    def _load_full_strategy_library(self) -> bool:
        cfg = self._prompt_settings
        if cfg is None:
            return False
        return bool(getattr(cfg, "stage2_load_full_strategy_library", False))

    # ── Process-level system-prompt cache ────────────────────────────────────
    # DeepSeek KV Cache hits require the *prefix* of consecutive requests to
    # be byte-identical.  System prompts are fully static (persona + txt files)
    # and never change during a session, so we cache them at the process level.
    # Stage 1 and Stage 2 share one system blob so S1→S2 prefix matches.

    @functools.cached_property
    def _shared_system_prompt(self) -> str:
        """Shared Stage 1/2 system prompt (cached for this instance)."""
        return self._get_shared_system_prompt()

    def _get_shared_system_prompt(self) -> str:
        key = str(self._prompt_dir.resolve())
        cached = _SYSTEM_PROMPT_CACHE.get(key)
        if cached is not None:
            return cached
        built = self._build_shared_system_prompt_inner()
        _SYSTEM_PROMPT_CACHE[key] = built
        return built

    def _build_stage1_system_prompt(self) -> str:
        """Return cached shared system prompt."""
        return self._shared_system_prompt

    def _build_stage2_system_prompt(self) -> str:
        """Return cached shared system prompt (byte-identical to Stage 1)."""
        return self._shared_system_prompt

    def _build_shared_system_prompt_inner(self) -> str:
        """Persona + full binary decision tree (both stages)."""
        system_parts = [
            _LANGUAGE_ZH_RULE,
            _PA_TERMINOLOGY_ZH,
            _OPENCLAW_AGENT_NO_TOOLS_RULE,
            _THINKING_CONTENT_OUTPUT_RULE,
        ]
        system_parts.extend(self._load(name) for name in COMMON_SYSTEM_PROMPT_TXT_FILES)
        return "\n\n---\n\n".join(p for p in system_parts if p)

    # ── File loading ──────────────────────────────────────────────────────────

    def _load(self, filename: str) -> str:
        """Load a prompt file by name. Returns empty string on error."""
        if filename in self._txt_cache:
            return self._txt_cache[filename]
        path = self._prompt_dir / filename
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to load prompt file %s: %s", filename, exc)
            content = f"[ERROR: could not load {filename}]"
        self._txt_cache[filename] = content
        return content

    # ── K-line table rendering ────────────────────────────────────────────────

    @staticmethod
    def _render_kline_table(frame: KlineFrame, limit: int | None = None) -> str:
        """Render the K-line data as a text table (newest bar first)."""
        lines = [
            "序号 | 时间                | 开盘价    | 最高价    | 最低价    | 收盘价    | 阳阴 | 成交量    | EMA20     | ATR14",
            "-----+--------------------+----------+----------+----------+----------+------+----------+-----------+----------",
        ]
        bars = frame.bars[:limit] if limit is not None else frame.bars
        for i, bar in enumerate(bars):
            ema = frame.indicators.ema20[i]
            atr = frame.indicators.atr14[i]
            ema_str = f"{ema:.4f}" if not math.isnan(ema) else "N/A"
            atr_str = f"{atr:.4f}" if not math.isnan(atr) else "N/A"
            yang_yin = bar_candle_direction_label(bar)
            dt = format_epoch_for_display(bar.ts_open, short=True)
            lines.append(
                f"{bar.seq:<4} | {dt:<19} | {bar.open:<9.4f} | {bar.high:<9.4f} | "
                f"{bar.low:<9.4f} | {bar.close:<9.4f} | {yang_yin:<4} | {bar.volume:<9.0f} | "
                f"{ema_str:<10} | {atr_str}"
            )
        lines.append(_KLINE_INDICATOR_NOTE)
        return "\n".join(lines)

    @staticmethod
    def _render_market_context_block(frame: KlineFrame) -> str:
        """Render optional source-specific market data without analysis rules."""
        context = getattr(frame, "market_context", None)
        if not isinstance(context, dict) or context.get("provider") != "eastmoney":
            return ""
        bids = context.get("bids")
        asks = context.get("asks")
        trades = context.get("recent_trades")
        bid_items = bids if isinstance(bids, list) else []
        ask_items = asks if isinstance(asks, list) else []
        trade_items = trades if isinstance(trades, list) else []
        if not bid_items and not ask_items and not trade_items:
            return ""

        def _item(items: list, index: int) -> tuple[str, str]:
            if index >= len(items) or not isinstance(items[index], dict):
                return "—", "—"
            level = items[index]
            try:
                price = f"{float(level.get('price')):.4f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                price = "—"
            try:
                volume = str(max(0, int(level.get("volume_lots"))))
            except (TypeError, ValueError):
                volume = "—"
            return price, volume

        lines: list[str] = [
            "## 东方财富实时盘口（分析提交时快照）",
            "",
            (
                f"股票：{context.get('name') or '—'} "
                f"({context.get('code') or frame.symbol})；"
                f"最新价：{context.get('last_price', '—')}；"
                f"涨跌幅：{context.get('pct_chg', '—')}%；"
                f"快照时间戳：{context.get('snapshot_ts_ms') or '—'}"
            ),
        ]
        if bid_items or ask_items:
            lines.extend(
                [
                    (
                        f"买盘合计：{context.get('bid_total_lots', 0)}手；"
                        f"卖盘合计：{context.get('ask_total_lots', 0)}手；"
                        f"委比：{context.get('order_imbalance_pct', 0)}%"
                    ),
                    "",
                    "档位 | 买价 | 买量(手) | 卖价 | 卖量(手)",
                    "-----+------+----------+------+----------",
                ]
            )
            depth = min(10, max(len(bid_items), len(ask_items)))
            for index in range(depth):
                bid_price, bid_volume = _item(bid_items, index)
                ask_price, ask_volume = _item(ask_items, index)
                lines.append(
                    f"{index + 1:<4} | {bid_price:<4} | {bid_volume:<8} | "
                    f"{ask_price:<4} | {ask_volume}"
                )

        if trade_items:
            shown_trades = trade_items[-20:]
            lines.extend(
                [
                    "",
                    "### 最近成交明细（按时间升序）",
                    (
                        f"最近{len(shown_trades)}笔："
                        f"主买{context.get('active_buy_lots', 0)}手；"
                        f"主卖{context.get('active_sell_lots', 0)}手；"
                        f"中性/竞价{context.get('neutral_trade_lots', 0)}手；"
                        f"主买净额{context.get('active_net_lots', 0):+}手"
                    ),
                    "",
                    "时间 | 成交价 | 成交量(手) | 方向",
                    "-----+--------+------------+------",
                ]
            )
            for trade in shown_trades:
                if not isinstance(trade, dict):
                    continue
                try:
                    price = (
                        f"{float(trade.get('price')):.4f}"
                        .rstrip("0")
                        .rstrip(".")
                    )
                except (TypeError, ValueError):
                    price = "—"
                try:
                    volume = str(max(0, int(trade.get("volume_lots"))))
                except (TypeError, ValueError):
                    volume = "—"
                lines.append(
                    f"{trade.get('time') or '—'} | {price} | "
                    f"{volume} | {trade.get('side') or '—'}"
                )
        return "\n".join(lines)

    def _render_market_context_prompt(self, frame: KlineFrame) -> str:
        """Conditionally combine prompt-engineering rules with live market data."""
        data_block = self._render_market_context_block(frame)
        if not data_block:
            return ""
        rules = self._load(EASTMONEY_ORDER_BOOK_PROMPT_FILE)
        return f"{rules}\n\n---\n\n{data_block}"

    @staticmethod
    def _render_kline_feature_table(frame: KlineFrame, limit: int | None = None) -> str:
        """Render方案 A single-bar geometry features for prompt grounding."""
        shown = limit if limit is not None else len(frame.bars)
        lines = [
            f"（几何特征：最近 {shown} 根已收盘 K 线；「类型」= 单字段 bar_type，优先级 inside/outside > doji/trend/flat/other；多棒形态已用完整窗口计算）",
            "序号 | 类型          | 实体比 | 上影比 | 下影比 | 收盘位置 | Range/ATR | EMA关系 | 与前棒重叠 | ii/iii | ioi | 微双 | 缺口 | EMA缺口数 | 近5突破 | 后续",
            "-----+---------------+--------+--------+--------+----------+-----------+---------+------------+--------+-----+------+-------+-----------+---------+------",
        ]
        for feat in compute_kline_geometry_features(frame, limit=limit):
            lines.append(
                f"{feat.seq:<4} | {feat.bar_type:<13} | "
                f"{_fmt_feature(feat.body_ratio):<6} | "
                f"{_fmt_feature(feat.upper_wick_ratio):<6} | "
                f"{_fmt_feature(feat.lower_wick_ratio):<6} | "
                f"{_fmt_feature(feat.close_position):<8} | "
                f"{_fmt_feature(feat.range_atr_ratio):<9} | "
                f"{feat.ema_relation:<7} | "
                f"{_fmt_feature(feat.overlap_prev_ratio):<10} | "
                f"{feat.inside_sequence:<6} | "
                f"{str(feat.ioi_pattern):<3} | "
                f"{feat.micro_double:<4} | "
                f"{feat.gap_bar:<5} | "
                f"{feat.ema_gap_count:<9} | "
                f"{feat.breakout_prev:<7} | "
                f"{feat.follow_through_1_2}"
            )
        lines.append(_KLINE_INDICATOR_NOTE)
        return "\n".join(lines)

    @staticmethod
    def _render_simple_market_features_block(frame: KlineFrame) -> str:
        """Render simple structure pre-computations (range, swings, HL count, MM)."""
        try:
            features = compute_simple_market_features(frame)
            return render_simple_market_features(features)
        except Exception as exc:  # noqa: BLE001
            logger.warning("_render_simple_market_features_block failed: %s", exc)
            return ""

    @staticmethod
    def _inject_market_features_block(prompt: str, frame: KlineFrame) -> str:
        """Refresh or insert program market-features into a Stage 1 user prompt."""
        block = PromptAssembler._render_simple_market_features_block(frame)
        if not block:
            return prompt
        return inject_market_features_section(prompt, block)

    # ── Stage 1 ───────────────────────────────────────────────────────────────

    def build_stage1(self, frame: KlineFrame, *, analysis_mode: str = "original") -> list[dict]:
        """Build the message list for Stage 1 (market diagnosis)."""
        system_content = self._build_stage1_system_prompt()
        user_content = self._build_stage1_user_prompt(frame, analysis_mode=analysis_mode)

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    @staticmethod
    def _normalize_prev_stage1_assistant_for_incremental(
        previous_record: AnalysisRecord,
        raw_content: str,
    ) -> str:
        """Use validated diagnosis JSON in incremental context, not prose/markdown replies."""
        from pa_agent.ai.json_validator import format_model_json_for_context

        diag = getattr(previous_record, "stage1_diagnosis", None) or {}
        if isinstance(diag, dict) and diag:
            return json.dumps(diag, ensure_ascii=False, indent=2)

        formatted = format_model_json_for_context(raw_content)
        if formatted:
            return formatted

        logger.warning(
            "incremental stage1: could not normalize previous assistant to JSON; "
            "using raw stage1_response content (%d chars)",
            len(raw_content or ""),
        )
        return raw_content

    def build_incremental_stage1(
        self,
        frame: KlineFrame,
        previous_record: AnalysisRecord,
        new_bar_count: int,
        *,
        analysis_mode: str = "original",
        provider_settings: Any | None = None,
    ) -> list[dict]:
        """Build Stage 1 as a continuation-based incremental update.

        Structure:
          [0] system    — Stage 1 system prompt (same as full Stage 1)
          [1] user      — Previous full Stage 1 user prompt (with K-line table)
          [2] assistant — Previous Stage 1 reply
          [3] user      — Incremental task (new K-lines only, no full table)

        Benefits vs old 2-message incremental:
        - [system, user(S1)] prefix is IDENTICAL to full Stage 1 → prefix cache hit
        - Full K-line table is in [1], not re-sent in [3] → saves ~14.5K tokens
        - Stage 2 continuation can also cache-hit this prefix chain
        """
        prev_s1_messages = getattr(previous_record, "stage1_messages", None) or []
        prev_s1_response = getattr(previous_record, "stage1_response", None) or {}

        # Extract previous Stage 1 user message
        prev_user_content = ""
        for msg in prev_s1_messages:
            if msg.get("role") == "user":
                prev_user_content = msg["content"]
                break

        # Extract previous Stage 1 assistant reply content
        prev_assistant_content = ""
        if isinstance(prev_s1_response, dict):
            prev_assistant_content = prev_s1_response.get("content", "") or ""

        if not prev_user_content:
            raise ValueError(
                f"build_incremental_stage1: previous_record.stage1_messages "
                f"contains no user message. "
                f"stage1_messages has {len(prev_s1_messages)} items, "
                f"roles={[m.get('role') for m in prev_s1_messages]}. "
                f"record.meta: {getattr(previous_record, 'meta', '<missing>')!r}"
            )
        prev_diag = getattr(previous_record, "stage1_diagnosis", None) or {}
        if not prev_assistant_content and not (
            isinstance(prev_diag, dict) and prev_diag
        ):
            raise ValueError(
                f"build_incremental_stage1: previous_record.stage1_response "
                f"has no 'content' field. "
                f"stage1_response type={type(prev_s1_response).__name__}, "
                f"keys={list(prev_s1_response.keys()) if isinstance(prev_s1_response, dict) else 'N/A'}. "
                f"record.meta: {getattr(previous_record, 'meta', '<missing>')!r}"
            )

        prev_assistant_content = self._normalize_prev_stage1_assistant_for_incremental(
            previous_record,
            prev_assistant_content,
        )

        prev_user_content = self._inject_market_features_block(prev_user_content, frame)

        prev_reasoning = ""
        if isinstance(prev_s1_response, dict):
            prev_reasoning = str(prev_s1_response.get("reasoning_content") or "")
        preserve_mimo = False
        if provider_settings is not None:
            from pa_agent.ai.mimo_compat import (
                build_assistant_api_message,
                is_mimo_provider,
            )

            preserve_mimo = is_mimo_provider(
                getattr(provider_settings, "base_url", ""),
                getattr(provider_settings, "model", ""),
            )
        if preserve_mimo:
            assistant_turn = build_assistant_api_message(
                prev_assistant_content,
                reasoning_content=prev_reasoning,
            )
        else:
            assistant_turn = {"role": "assistant", "content": prev_assistant_content}

        system_content = self._build_stage1_system_prompt()
        incremental_user_content = self._build_incremental_stage1_continuation_user_prompt(
            frame,
            previous_record,
            new_bar_count,
            analysis_mode=analysis_mode,
        )

        return [
            {"role": "system",    "content": system_content},
            {"role": "user",      "content": prev_user_content},
            assistant_turn,
            {"role": "user",      "content": incremental_user_content},
        ]

    def _stage1_pattern_supplement(self) -> str:
        """Pattern tag table + briefs for Stage 1 (optional via settings)."""
        if self._prompt_settings is not None and not getattr(
            self._prompt_settings, "stage1_inject_pattern_briefs", True
        ):
            return ""
        return f"{STAGE1_DETECTED_PATTERNS_GUIDE}\n\n---\n\n{STAGE1_PATTERN_BRIEFS_BLOCK}"

    @staticmethod
    def _render_program_prefill_hint(frame: KlineFrame) -> str:
        """Render a compact block showing program pre-computed node verdicts.

        This is injected into the Stage 1 user prompt so the AI can see
        exactly what the deterministic engine computed for §1.1 / §2.3 / §2.4
        *before* making its own judgement.  The AI can still override via
        node_overrides when it sees structural evidence the program missed.

        Why this matters (from prompt_engineering 二元决策.txt §2.3/§2.4):
        - §2.3 direction is now a 5-signal vote; each signal value is exposed
          so the AI knows which signals contributed and why.
        - §2.4 Always In now has 3 gates (ratio, slope, swing+pullback); the
          AI can see whether Gate 3 confirmed or was weak.
        """
        try:
            from pa_agent.ai.decision_nodes import (
                judge_data_sufficiency,
                judge_direction,
                judge_always_in,
            )
            from pa_agent.ai.trend_context import (
                build_trend_context,
                render_three_window_summary,
            )

            hint_lines: list[str] = [
                "## 程序预填充节点判断依据（§1.1 / §2.3 / §2.4，供 AI 参考）",
                "",
                "程序已确定性计算以下节点，结果将写入 gate_trace。"
                "你可以在理解以下依据后，于 node_overrides 中提交有充分理由的覆盖。",
                "",
            ]

            # §1.1
            fill_11 = judge_data_sufficiency(frame)
            hint_lines.append(f"**§1.1 数据是否足够** → {fill_11.answer}")
            hint_lines.append(f"  依据：{fill_11.reason}")
            hint_lines.append("")

            # §2.3
            direction, fill_23 = judge_direction(frame)
            trend_ctx = build_trend_context(frame, direction)
            n_bars_hint = len(frame.bars)
            hint_lines.append(render_three_window_summary(frame, trend_ctx))
            hint_lines.append("")
            hint_lines.append(
                "**§2.2 长程背景 vs 近期方向（程序摘要，供 gate_trace 2.2 引用）**"
            )
            hint_lines.append(
                f"  背景方向（K{n_bars_hint}-K41）≈ {trend_ctx['background_direction']}；"
                f"交易主方向（近期）≈ {trend_ctx['trading_direction']}；"
                f"关系={trend_ctx['relationship']}"
                + ("；**冲突时不否决近期、不自动减半仓位**" if trend_ctx.get("conflict") else "")
            )
            hint_lines.append("")

            hint_lines.append(
                f"**§2.3 当前方向（多/空/中性）** → {fill_23.answer}"
                + (f"（branch={fill_23.branch}）" if fill_23.branch else "")
            )
            hint_lines.append(f"  依据：{fill_23.reason}")
            hint_lines.append("")

            # §2.4
            fill_24 = judge_always_in(frame)
            hint_lines.append(
                f"**§2.4 是否 Always In** → {fill_24.answer}"
                + (f"（branch={fill_24.branch}）" if fill_24.branch else "")
            )
            hint_lines.append(f"  依据：{fill_24.reason}")
            hint_lines.append("")

            hint_lines.append(
                "⚠️ §1.1 为锁定节点不可覆盖。§2.3/§2.4 可通过 node_overrides 覆盖，"
                "但门槛较高：\n"
                "  • §2.3 覆盖须指明具体 K 线序号+结构特征，且该特征超出五信号投票的计算范围；\n"
                "  • §2.4 近端K8-K1为主判、背景K20-K1仅参考；覆盖须基于近端结构突变证据；\n"
                "  • override_reason 必须具体，不接受「整体看跌」「感觉已变」等模糊描述。"
            )
            return "\n".join(hint_lines)
        except Exception as exc:  # noqa: BLE001
            logger.warning("_render_program_prefill_hint failed: %s", exc)
            return ""

    def _build_stage1_user_prompt(self, frame: KlineFrame, *, analysis_mode: str = "original") -> str:
        """Build the Stage 1 task turn; stage-specific rules stay out of system."""
        pattern_block = self._stage1_pattern_supplement()
        prefill_hint = self._render_program_prefill_hint(frame)
        stage1_parts = [
            *(self._load(name) for name in STAGE1_TASK_PROMPT_TXT_FILES),
            *([pattern_block] if pattern_block else []),
            _stage1_output_reminder_for_mode(analysis_mode),
        ]
        stage1_context = "\n\n---\n\n".join(p for p in stage1_parts if p)
        kline_table = self._render_kline_table(frame)
        feature_table = self._render_kline_feature_table(frame)
        simple_features_block = self._render_simple_market_features_block(frame)
        market_context_block = self._render_market_context_prompt(frame)
        n_bars = len(frame.bars)
        if n_bars > 40:
            bg_window = f"**长程背景 K{n_bars}–K41**（较老部分）：\n"
        else:
            bg_window = (
                f"**长程背景**（当前仅 {n_bars} 根，不足 41 根，与近期窗口重叠；"
                f"以程序预填 §2.2 为准）：\n"
            )
        return (
            "## 阶段一任务\n\n"
            "你现在只执行阶段一：市场诊断与闸门判断。不要评估具体下单、止损、止盈或仓位。\n\n"
            f"{stage1_context}\n\n"
            "---\n\n"
            f"## 当前分析目标\n\n"
            f"品种:{frame.symbol} 周期:{frame.timeframe} K线数量:{n_bars}\n"
            f"（K线序号：1=最新已收盘，最大 K{n_bars}；"
            f"每个决策节点的 bar_range 由你自行选择子区间，勿超出 K{n_bars}-K1）\n\n"
            f"## ⚠️ 分析窗口分层规则（与程序 §2.2/§2.3/§2.4 预填一致，必须遵守）\n\n"
            f"你收到全部 {n_bars} 根 K 线数据；下列分层与 `市场诊断框架.txt`、程序三窗口摘要**同一标准**：\n\n"
            f"{bg_window}"
            f"- swing 高低点、磁力位参考 → 写入 `htf_context`；§2.2 背景方向\n"
            f"- **禁止**用背景方向否决近期 `direction`；冲突时近期为主、背景作风险参考\n\n"
            f"**近期结构 K{min(40, n_bars)}–K1：**\n"
            f"- `cycle_position`、`direction`、通道/区间/波段主结构\n"
            f"- 程序 §2.3 方向投票与多数闸门 bar_range 优先此窗口\n\n"
            f"**即时惯性 K{min(8, n_bars)}–K1：**\n"
            f"- §2.4 Always In、§2.5 惯性强度、近端 spike_stage / 尖峰识别\n\n"
            f"**即时信号 K{min(10, n_bars)}–K1：**\n"
            f"- 信号棒/入场棒/二次入场/突破失败（阶段二 §9 裁定窗口）\n\n"
            f"**逐棒摘要 K5–K1：**\n"
            f"- `bar_by_bar_summary` **必须**恰好 5 条（窗口≥5 根时），每条 1 句 reason\n\n"
            f"## K线数据(序号1=最新已收盘K线,序号越大越早;不含当前未收盘K线;"
            f"阳阴列由程序按收盘价与开盘价计算:收盘>开盘=阳线,收盘<开盘=阴线,相等=平)\n\n"
            f"{kline_table}\n\n"
            "## K线几何特征(程序预计算；「类型」列为单字段 bar_type，判定优先级：inside/outside > doji/trend/flat/other；"
            "不替代周期判断；基于当前 N 根已收盘 K 线，指标非全历史延续)\n\n"
            f"{feature_table}\n\n"
            + (f"{simple_features_block}\n\n" if simple_features_block else "")
            + (f"{market_context_block}\n\n" if market_context_block else "")
            + (f"{prefill_hint}\n\n" if prefill_hint else "")
            + f"请根据以上数据，严格输出阶段一 JSON 诊断结果。\n\n"
            f"{_STAGE1_TAIL_REMINDER}"
        )

    def _build_incremental_stage1_user_prompt(
        self,
        frame: KlineFrame,
        previous_record: AnalysisRecord,
        new_bar_count: int,
        *,
        analysis_mode: str = "original",
    ) -> str:
        """Build a Stage 1 update turn using the last completed analysis."""
        pattern_block = self._stage1_pattern_supplement()
        prefill_hint = self._render_program_prefill_hint(frame)
        stage1_parts = [
            *(self._load(name) for name in STAGE1_TASK_PROMPT_TXT_FILES),
            *([pattern_block] if pattern_block else []),
            _stage1_output_reminder_for_mode(analysis_mode),
        ]
        stage1_context = "\n\n---\n\n".join(p for p in stage1_parts if p)
        n_bars = len(frame.bars)
        new_count = max(0, min(new_bar_count, n_bars))
        new_kline_table = self._render_kline_table(frame, limit=new_count)
        new_feature_table = self._render_kline_feature_table(frame, limit=new_count)
        full_kline_table = self._render_kline_table(frame)
        full_feature_table = self._render_kline_feature_table(frame)
        simple_features_block = self._render_simple_market_features_block(frame)
        previous_summary = {
            "meta": previous_record.meta.model_dump(),
            "stage1_diagnosis": previous_record.stage1_diagnosis or {},
            "stage2_decision": previous_record.stage2_decision or {},
            "strategy_files_used": previous_record.strategy_files_used or [],
        }
        return (
            "## 阶段一增量任务\n\n"
            "你现在只执行阶段一：基于上一轮已完成分析和新增 K 线，更新市场诊断与闸门判断。\n"
            "不要评估具体下单、止损、止盈或仓位；这些留到阶段二。\n\n"
            "增量分析规则：\n"
            "- 先检查上一轮诊断在新增 K 线后是否仍成立。\n"
            "- 如果市场结构未被破坏，可以延续上一轮 cycle_position/direction，但必须用新增 K 线重新说明依据。\n"
            "- 如果新增 K 线出现突破、反转、极端波动或让原结论失效，必须更新诊断。\n"
            "- 必须输出顶层字段 **incremental_delta**（不可省略），结构示例：\n"
            '  "incremental_delta": {"new_closed_bars":["K1"],'
            '"changed_fields":["direction","cycle_position"],'
            '"summary":"相对上一轮：新增K1突破区间上沿，方向由中性转偏多"}\n'
            "- new_closed_bars 长度必须等于「新增已收盘K线」数量（1根则只写 [\"K1\"]）。\n"
            "- 并在 summary / risk_warning / gate_trace 中说明相对上一轮变化。\n"
            "- gate_result=proceed 时 gate_trace 仍须覆盖 §1.2、§1.3、§2.1、§2.2、§2.5（§1.1/§2.3/§2.4 由程序填充）。\n"
            "- 输出仍必须是完整阶段一 JSON，而不是差异补丁。\n\n"
            f"{_INCREMENTAL_OUTPUT_HARD_RULES}\n\n"
            f"{stage1_context}\n\n"
            "---\n\n"
            f"## 当前分析目标\n\n"
            f"品种:{frame.symbol} 周期:{frame.timeframe} K线数量:{n_bars} 新增已收盘K线:{new_count}\n"
            f"（K线序号：1=最新已收盘，最大 K{n_bars}；"
            f"每个决策节点的 bar_range 由你自行选择子区间，勿超出 K{n_bars}-K1）\n\n"
            "## 上一轮已完成分析（仅作为延续上下文）\n\n"
            f"```json\n{json.dumps(previous_summary, ensure_ascii=False, indent=2)}\n```\n\n"
            f"## 新增 K线数据(共{new_count}根，序号1=最新已收盘；含阳阴列)\n\n"
            f"{new_kline_table}\n\n"
            f"## 新增 K线几何特征(共{new_count}根；多棒形态按完整{n_bars}根窗口计算，"
            f"与前棒重叠/内包/ioi 以完整表为准)\n\n"
            f"{new_feature_table}\n\n"
            f"## 当前完整 K线数据(共{n_bars}根，用于必要时复核整体结构；含阳阴列)\n\n"
            f"{full_kline_table}\n\n"
            f"## 当前完整 K线几何特征(用于逐棒辅助，不替代周期判断；"
            f"基于当前 N 根已收盘 K 线，指标非全历史延续)\n\n"
            f"{full_feature_table}\n\n"
            + (f"{simple_features_block}\n\n" if simple_features_block else "")
            + (f"{prefill_hint}\n\n" if prefill_hint else "")
            + "请基于上一轮结论、新增K线和当前完整K线，严格输出更新后的阶段一 JSON 诊断结果。\n\n"
            f"{_STAGE1_TAIL_REMINDER}"
        )

    def _build_incremental_stage1_continuation_user_prompt(
        self,
        frame: KlineFrame,
        previous_record: AnalysisRecord,
        new_bar_count: int,
        *,
        analysis_mode: str = "original",
    ) -> str:
        """Build the incremental continuation user turn (message [3] in 4-message mode).

        Only sends NEW K-line data; the model can reference the full K-line table
        from the previous Stage 1 user message ([1]) above.
        Injects program prefill hint so the AI knows updated §2.3/§2.4 verdicts
        even when the full K-line table is not re-sent.
        """
        prefill_hint = self._render_program_prefill_hint(frame)
        simple_features_block = self._render_simple_market_features_block(frame)
        market_context_block = self._render_market_context_prompt(frame)
        if simple_features_block:
            simple_features_block = _MARKET_FEATURES_AUTHORITY_NOTE + simple_features_block
        n_bars = len(frame.bars)
        new_count = max(0, min(new_bar_count, n_bars))
        new_kline_table = self._render_kline_table(frame, limit=new_count)
        new_feature_table = self._render_kline_feature_table(frame, limit=new_count)
        previous_summary = {
            "meta": previous_record.meta.model_dump(),
            "stage1_diagnosis": previous_record.stage1_diagnosis or {},
            "stage2_decision": previous_record.stage2_decision or {},
            "strategy_files_used": previous_record.strategy_files_used or [],
        }
        return (
            "## 阶段一增量更新任务\n\n"
            "上方是你上一轮完成的阶段一诊断。现在基于新增 K 线，更新诊断与闸门判断。\n"
            "完整 K 线数据已包含在上方阶段一用户消息中（K线序号已重新编号，"
            "K1=当前最新已收盘K线），你可以回溯查看任何历史 K 线。\n\n"
            "⚠ 反锚定要求——这是增量分析最重要的原则：\n"
            "- 不要因为上一轮已得出结论就倾向于延续它；上一轮结论只是参考起点，不是约束。\n"
            "- 如果新增 K 线改变了市场结构（突破、反转、趋势加速/衰竭），必须果断推翻上一轮结论，而非在旧结论上微调。\n"
            "- 判断标准：如果你是第一次看到这组完整 K 线（包括上方历史K线+新增K线），你会得出什么结论？那才是正确结论。\n"
            "- 每次增量更新都应视为一次重新诊断——只是你不必重复描述未变的部分。\n\n"
            "增量分析规则：\n"
            "- 先独立审视完整 K 线数据，形成自己的判断，再与上一轮结论对照。\n"
            "- 如果市场结构确实未被破坏，可以延续上一轮 cycle_position/direction，但必须用新增 K 线重新说明依据。\n"
            "- 如果新增 K 线出现突破、反转、极端波动或让原结论失效，必须更新诊断——宁可过度更新，不可锚定延续。\n"
            "- 若 K1 收盘已突破上一轮 resistance_levels 或跌破 support_levels，必须重算支撑/阻力，"
            "不得原样延续已失效价位（程序也会按收盘价剔除失效档位）。\n"
            "- 必须输出顶层字段 **incremental_delta**（不可省略），结构示例：\n"
            '  "incremental_delta": {"new_closed_bars":["K1"],'
            '"changed_fields":["direction","cycle_position"],'
            '"summary":"相对上一轮：新增K1突破区间上沿，方向由中性转偏多"}\n'
            "- new_closed_bars 长度必须等于「新增已收盘K线」数量（1根则只写 [\"K1\"]）。\n"
            "- 并在 summary / risk_warning / gate_trace 中说明相对上一轮变化。\n"
            "- gate_result=proceed 时 gate_trace 仍须覆盖 §1.2、§1.3、§2.1、§2.2、§2.5（§1.1/§2.3/§2.4 由程序填充）。\n"
            "- 输出仍必须是完整阶段一 JSON，而不是差异补丁。\n\n"
            f"{_INCREMENTAL_OUTPUT_HARD_RULES}\n\n"
            f"## 当前分析目标更新\n\n"
            f"品种:{frame.symbol} 周期:{frame.timeframe} K线数量:{n_bars} 新增已收盘K线:{new_count}\n"
            f"（K线序号已重新编号：1=最新已收盘，最大 K{n_bars}；"
            f"每个决策节点的 bar_range 由你自行选择子区间，勿超出 K{n_bars}-K1）\n\n"
            "## 上一轮已完成分析（仅作为延续上下文）\n\n"
            f"```json\n{json.dumps(previous_summary, ensure_ascii=False, indent=2)}\n```\n\n"
            f"## 新增 K线数据(共{new_count}根，序号1=最新已收盘；含阳阴列)\n\n"
            f"{new_kline_table}\n\n"
            f"## 新增 K线几何特征(共{new_count}根；多棒形态按完整{n_bars}根窗口计算，"
            f"与前棒重叠/内包/ioi 以完整表为准)\n\n"
            f"{new_feature_table}\n\n"
            + (f"{simple_features_block}\n\n" if simple_features_block else "")
            + (f"{market_context_block}\n\n" if market_context_block else "")
            + (f"{prefill_hint}\n\n" if prefill_hint else "")
            + "请基于上方完整K线数据、上一轮结论和新增K线，严格输出更新后的阶段一 JSON 诊断结果。\n\n"
            f"{_STAGE1_TAIL_REMINDER}"
        )

    # ── Stage 2 ───────────────────────────────────────────────────────────────

    def build_stage2(
        self,
        frame: KlineFrame,
        stage1_json: dict,
        strategy_files: list[str],
        experience_entries: list[Any],
        *,
        decision_stance: str = "conservative",
    ) -> list[dict]:
        """Build a standalone Stage 2 request (kept for tests/tools)."""
        system_content = self._build_stage2_system_prompt()
        user_content = self._build_stage2_user_prompt(
            frame=frame,
            stage1_json=stage1_json,
            strategy_files=strategy_files,
            experience_entries=experience_entries,
            decision_stance=decision_stance,
            enable_next_bar_prediction=False,
        )
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    @staticmethod
    def _render_previous_prediction(previous_record: Any) -> str:
        """Render previous-bar prediction summary for incremental context (R5.2)."""
        if previous_record is None:
            return ""
        # previous_record may be AnalysisRecord or dict-like
        s2 = getattr(previous_record, "stage2_decision", None)
        if s2 is None and isinstance(previous_record, dict):
            s2 = previous_record.get("stage2_decision")
        if not isinstance(s2, dict):
            return ""
        pred = s2.get("next_bar_prediction")
        if not isinstance(pred, dict):
            return ""

        unpredictable = bool(pred.get("unpredictable", False))
        if unpredictable:
            return (
                "## 上一轮下一根K线预测\n\n"
                "上一轮标记为不可预测；本轮请独立判断。\n"
            )

        direction = pred.get("direction") or "—"
        probs = pred.get("probabilities") or {}
        bull = probs.get("bullish", "?")
        bear = probs.get("bearish", "?")
        neut = probs.get("neutral", "?")
        dir_zh = {"bullish": "阳线", "bearish": "阴线", "neutral": "中性"}.get(direction, direction)
        return (
            "## 上一轮下一根K线预测\n\n"
            f"方向：{dir_zh}（阳 {bull}% / 阴 {bear}% / 中性 {neut}%）。"
            "本轮请基于最新数据独立重新预测，不必延续上轮结论。\n"
        )

    @staticmethod
    def _normalize_stage1_assistant_for_chain(
        stage1_json: dict,
        stage1_reply_content: str,
    ) -> str:
        """Compact validated Stage 1 JSON for assistant turn in prefix-chain mode."""
        from pa_agent.ai.json_validator import format_model_json_for_context

        if isinstance(stage1_json, dict) and stage1_json:
            return json.dumps(stage1_json, ensure_ascii=False, indent=2)
        formatted = format_model_json_for_context(stage1_reply_content)
        if formatted:
            return formatted
        return stage1_reply_content or ""

    def build_stage2_continuation(
        self,
        *,
        frame: KlineFrame,
        stage1_messages: list[dict],
        stage1_reply_content: str,
        stage1_json: dict,
        strategy_files: list[str],
        experience_entries: list[Any],
        decision_stance: str = "conservative",
        previous_record: Any | None = None,
        enable_next_bar_prediction: bool = False,
        provider_settings: Any | None = None,
        use_prefix_chain: bool | None = None,
        structure_flip_cooldown_bars: int = 3,
    ) -> list[dict]:
        """Build Stage 2 messages, optionally chaining after Stage 1 for KV cache.

        Prefix-chain mode (DeepSeek native, default when safe):
          [system, user(S1…), assistant(S1 JSON), user(S2 task only)]

        Standalone mode (OpenClaw Agent and similar):
          [system, user(S2 task + full K-line tables)]
        """
        from pa_agent.ai.deepseek_client import supports_kv_prefix_chain

        if use_prefix_chain is None:
            use_prefix_chain = supports_kv_prefix_chain(provider_settings)

        chain_after_s1 = bool(use_prefix_chain and stage1_messages)
        stage2_user_content = self._build_stage2_user_prompt(
            frame=frame,
            stage1_json=stage1_json,
            strategy_files=strategy_files,
            experience_entries=experience_entries,
            decision_stance=decision_stance,
            previous_record=previous_record,
            enable_next_bar_prediction=enable_next_bar_prediction,
            omit_kline_block=chain_after_s1,
            structure_flip_cooldown_bars=structure_flip_cooldown_bars,
        )

        if chain_after_s1:
            assistant_content = self._normalize_stage1_assistant_for_chain(
                stage1_json,
                stage1_reply_content,
            )
            chain = [dict(m) for m in stage1_messages]
            chain.append({"role": "assistant", "content": assistant_content})
            chain.append({"role": "user", "content": stage2_user_content})
            return chain

        system_content = self._build_stage2_system_prompt()
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": stage2_user_content},
        ]

    def _build_stage2_user_prompt(
        self,
        *,
        frame: KlineFrame,
        stage1_json: dict,
        strategy_files: list[str],
        experience_entries: list[Any],
        decision_stance: str = "conservative",
        previous_record: Any | None = None,
        enable_next_bar_prediction: bool = False,
        omit_kline_block: bool = False,
        structure_flip_cooldown_bars: int = 3,
    ) -> str:
        """Build the Stage 2 task turn for standalone or prefix-chain mode."""
        from pa_agent.ai.decision_continuity import (
            build_continuity_context,
            render_continuity_prompt_block,
        )

        stance_block = build_decision_stance_guidance(normalize_stance(decision_stance))
        continuity_ctx = build_continuity_context(
            frame=frame,
            stage1_json=stage1_json,
            previous_record=previous_record,
            cooldown_bars=structure_flip_cooldown_bars,
        )
        continuity_block = render_continuity_prompt_block(continuity_ctx)
        conflict_block = self._render_trend_conflict_guidance(stage1_json)
        transition_block = self._render_transition_guidance(stage1_json)
        planned_limit_block = self._render_planned_limit_hint(stage1_json, frame)
        stage2_parts = [
            stance_block,
            continuity_block,
            conflict_block,
            transition_block,
            planned_limit_block,
            *(
                self._load(name)
                for name in stage2_user_task_txt_files(
                    strategy_files,
                    direction=str(stage1_json.get("direction", "") or ""),
                    load_full_strategy_library=self._load_full_strategy_library(),
                )
            ),
        ]
        if experience_entries:
            max_chars = 400
            if self._prompt_settings is not None:
                max_chars = int(
                    getattr(
                        self._prompt_settings,
                        "experience_max_chars_per_entry",
                        400,
                    )
                )
            stage2_parts.append(
                self._render_experience(
                    experience_entries,
                    max_chars_per_entry=max_chars,
                )
            )
        stage2_parts.append(_STAGE2_OUTPUT_CONTRACT)
        if enable_next_bar_prediction:
            stage2_parts.append(_NEXT_BAR_PREDICTION_INSTRUCTION)
        else:
            stage2_parts.append(_NEXT_BAR_DISABLED_NOTE)
        stage2_parts.append(
            _build_next_cycle_prediction_instruction(enable_next_bar=enable_next_bar_prediction)
        )
        # Static strategy / contract blocks first → better KV prefix reuse across runs.
        stage2_context = "\n\n---\n\n".join(p for p in stage2_parts if p)

        from pa_agent.util.price_tick import format_breakout_tick_hint

        n_bars = len(frame.bars)
        breakout_tick_hint = format_breakout_tick_hint(frame)
        market_context_block = self._render_market_context_prompt(frame)
        prev_pred_block = self._render_previous_prediction(previous_record)
        compact_s1 = json.dumps(
            self._compact_stage1_for_stage2(stage1_json),
            ensure_ascii=False,
            indent=2,
        )

        if omit_kline_block:
            kline_block = (
                "## K线数据\n\n"
                "完整 K 线表与几何特征已包含在上方阶段一用户消息中"
                "（序号按当前分析窗口编号，K1=最新已收盘）。"
                "阶段二须结合该表与下方阶段一诊断 JSON 做交易者方程与定价。\n\n"
            )
            simple_features_block = self._render_simple_market_features_block(frame)
            if simple_features_block:
                kline_block += (
                    _MARKET_FEATURES_AUTHORITY_NOTE
                    + simple_features_block
                    + "\n\n"
                )
            if breakout_tick_hint:
                kline_block += f"{breakout_tick_hint}\n\n"
        else:
            kline_table = self._render_kline_table(frame)
            feature_table = self._render_kline_feature_table(frame)
            simple_features_block = self._render_simple_market_features_block(frame)
            kline_block = (
                f"## K线数据(共{n_bars}根，含阳阴列；各节点 bar_range 由你据实填写)\n\n"
                f"{kline_table}\n\n"
                "## K线几何特征(程序预计算，仅作逐棒客观辅助；不得替代交易者方程；"
                "基于当前 N 根已收盘 K 线，指标非全历史延续)\n\n"
                f"{feature_table}\n\n"
            )
            if simple_features_block:
                kline_block += f"{simple_features_block}\n\n"
            if breakout_tick_hint:
                kline_block += f"{breakout_tick_hint}\n\n"
        if market_context_block:
            kline_block += f"{market_context_block}\n\n"

        kline_intro = (
            "完整 K 线表见上方阶段一用户消息。\n\n"
            if omit_kline_block
            else "本消息下方附有完整 K 线表与几何特征。\n\n"
        )
        return (
            f"{_STAGE2_API_TASK_RULE}\n\n"
            "## 阶段二任务\n\n"
            "你现在独立执行阶段二：交易决策、风险收益和下单方式评估（基于阶段一诊断结果）。\n"
            "以下 JSON 是程序校验通过后的阶段一诊断结果，请以此为权威依据；"
            f"{kline_intro}"
            f"{stage2_context}\n\n"
            "---\n\n"
            f"## 阶段一诊断结果\n\n```json\n"
            f"{compact_s1}"
            f"\n```\n\n"
            f"{kline_block}"
            f"{prev_pred_block + chr(10) if prev_pred_block else ''}"
            f"请根据以上诊断和K线数据,按《二元决策.txt》§3–§11、§14 输出 JSON 决策结果"
            f"(含 decision_trace 与 terminal)。\n"
            f"注意:如果判断不下单,entry_price、take_profit_price、take_profit_price_2、stop_loss_price、order_direction 必须全部为 null。\n\n"
            f"{_STAGE2_TAIL_REMINDER}"
        )

    def stage2_system_prompt_only(
        self,
        strategy_files: list[str],
        experience_entries: list[Any],
    ) -> str:
        """Return the shared system prompt used by Stage 2 requests."""
        return self._build_stage2_system_prompt()

    @staticmethod
    def _compact_stage1_for_stage2(stage1_json: dict) -> dict:
        """Subset of Stage 1 fields needed for Stage 2 (reduces prompt noise)."""
        keys = (
            "cycle_position",
            "alternative_cycle_position",
            "direction",
            "diagnosis_confidence",
            "spike_stage",
            "market_phase",
            "transition_risk",
            "detected_patterns",
            "key_signals",
            "htf_context",
            "trend_context",
            "entry_setup",
            "support_levels",
            "resistance_levels",
            "strategy_files_needed",
            "risk_warning",
            "bar_analysis",
            "bar_by_bar_summary",
            "gate_trace",
            "gate_result",
        )
        return {k: stage1_json[k] for k in keys if k in stage1_json}

    @staticmethod
    def _render_trend_conflict_guidance(stage1_json: dict) -> str:
        """Stage-2 guidance when long-range background conflicts with recent direction."""
        tc = stage1_json.get("trend_context")
        if not isinstance(tc, dict) or not tc.get("conflict"):
            return ""
        bg = tc.get("background_direction", "neutral")
        td = tc.get("trading_direction", "neutral")
        spike = tc.get("recent_spike")
        lines = [
            "## 新旧趋势冲突指导（Brooks 并列原则）",
            "",
            f"长程背景方向：**{bg}**；交易主方向（近期）：**{td}**。",
            f"- {tc.get('with_trend_rule', '')}",
            "- **禁止**产出逆近期主方向的三价；顺近期即顺势。",
            "- 禁止追高潮/SCS；climax_risk 预警或触发后禁追原方向。",
            "- 在 risk_assessment / watch_points 写明长程背景磁力位。",
        ]
        if spike:
            lines.append(f"- 程序检测到近端 **{spike}** 尖峰：优先按尖峰/回撤逻辑，不追突破。")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_transition_guidance(stage1_json: dict) -> str:
        """Render dynamic risk guidance from Stage 1 market_phase fields."""
        if stage1_json.get("market_phase") != "transitioning":
            return ""
        risk = stage1_json.get("transition_risk") or "medium"
        if risk == "high":
            size = "trade_confidence 倾向 30–45，只接受二次入场/突破回踩/边界强信号"
            selectivity = "只接受最清晰的二次入场、突破回踩或边界信号"
        elif risk == "medium":
            size = "trade_confidence 倾向 45–60，放弃弱信号与中部位置"
            selectivity = "选择性入场，放弃弱信号和中间位置"
        else:
            size = "trade_confidence 略降（约 55–65）"
            selectivity = "保持正常流程，但在 reason 中说明状态转换风险"
        return (
            "## 状态转换期风险指导\n\n"
            f"阶段一判断 market_phase=transitioning，transition_risk={risk}。\n"
            f"- 信号把握：{size}（**禁止**在 JSON 写仓位比例/手数）。\n"
            f"- 入场选择：{selectivity}。\n"
            "- 不因为状态转换而跳过 §9、§10、§14；只是提高信号质量门槛并降低交易频率。"
        )

    @staticmethod
    def _parse_level_midpoint(raw: object) -> float | None:
        """Parse support/resistance level string to a numeric midpoint."""
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        if "-" in text:
            parts = [p.strip() for p in text.split("-", 1)]
            try:
                lo = float(parts[0])
                hi = float(parts[1])
                return (lo + hi) / 2.0
            except (TypeError, ValueError, IndexError):
                return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _render_planned_limit_hint(stage1_json: dict, frame: KlineFrame) -> str:
        """Contextual hint when channel/range structure favors planned limit orders."""
        cycle = str(stage1_json.get("cycle_position", "") or "").strip().lower()
        if cycle not in (
            "broad_channel",
            "trading_range",
            "normal_channel",
            "trending_tr",
        ):
            return ""

        bars = getattr(frame, "bars", None) or ()
        if not bars:
            return ""

        try:
            close = float(getattr(bars[0], "close", 0))
        except (TypeError, ValueError):
            return ""

        indicators = getattr(frame, "indicators", None)
        atr = None
        try:
            atr_vals = getattr(indicators, "atr14", ()) or ()
            if atr_vals and not math.isnan(float(atr_vals[0])):
                atr = float(atr_vals[0])
        except (TypeError, ValueError, IndexError):
            atr = None

        proximity = max(atr * 0.35, abs(close) * 0.0008) if atr and atr > 0 else abs(close) * 0.002

        supports = stage1_json.get("support_levels") or []
        resistances = stage1_json.get("resistance_levels") or []
        if not isinstance(supports, list):
            supports = []
        if not isinstance(resistances, list):
            resistances = []

        near_support: float | None = None
        near_resist: float | None = None
        support_label = ""
        resist_label = ""
        for lv in supports:
            mid = PromptAssembler._parse_level_midpoint(lv)
            if mid is not None and mid <= close and abs(close - mid) <= proximity:
                if near_support is None or mid > near_support:
                    near_support = mid
                    support_label = str(lv)
        for lv in resistances:
            mid = PromptAssembler._parse_level_midpoint(lv)
            if mid is not None and mid >= close and abs(close - mid) <= proximity:
                if near_resist is None or mid < near_resist:
                    near_resist = mid
                    resist_label = str(lv)

        direction = str(stage1_json.get("direction", "neutral") or "neutral").strip().lower()
        lines = [
            "## §9.0 / §9.0P 计划型限价提示（程序根据阶段一结构生成）",
            "",
            "**优先级：市场周期 + 方向背景 > 独立信号棒。**",
            f"- cycle_position=**{cycle}** → 默认优先考虑 **限价单**（§11），"
            "尤其在通道/区间 **边界** 或 **顺势回撤/反弹结构位**（非中部）。",
            "- 若无强信号棒：§9.0=否，**必须** 继续写 **§9.0P** 并尝试背景限价三价。",
            "- §9.0P=是：signal_bar.bar=null、quality=invalid；entry_bar pending；"
            "三价写入 decision，不要只在 watch_points 写触发条件。",
            "- 定价：先定结构 TP1/TP2，再定结构 stop；RR>1.0 时程序自动向外扩 stop（保持 TP 不变）。",
        ]
        if near_support is not None:
            lines.append(
                f"- 价格靠近下方支撑 **{support_label}**（约 {near_support:.4f}）→ "
                "可评估 **做多限价单**（回撤至支撑买入）。"
            )
        if near_resist is not None:
            lines.append(
                f"- 价格靠近上方阻力 **{resist_label}**（约 {near_resist:.4f}）→ "
                "可评估 **做空限价单**（反弹至阻力卖出）。"
            )
        if near_support is None and near_resist is None:
            lines.append(
                "- 未识别到极近的支撑/阻力；若仍在通道/区间边界区域，"
                "请结合 K 线摆动高低点与 EMA 自行定价。"
            )
        if direction == "neutral":
            lines.append(
                "- 阶段一 direction=neutral：**§9.0P 默认 wait**（禁止双边边界挂单）。"
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_experience(
        entries: list[Any],
        *,
        max_chars_per_entry: int = 400,
    ) -> str:
        """Render experience library entries as a text block."""
        lines = [
            "## 经验库(最近案例,供参考)",
            "以下案例仅作对照，**不得**因相似就改变对本图结构/方向的独立判断。",
        ]
        for i, entry in enumerate(entries, 1):
            if isinstance(entry, dict):
                blob = json.dumps(entry, ensure_ascii=False, indent=2)
            elif hasattr(entry, "content"):
                blob = json.dumps(
                    getattr(entry, "content", entry),
                    ensure_ascii=False,
                    indent=2,
                )
            else:
                blob = str(entry)
            if len(blob) > max_chars_per_entry:
                blob = blob[: max_chars_per_entry - 3] + "..."
            lines.append(f"\n### 案例 {i}\n```json\n{blob}\n```")
        return "\n".join(lines)
