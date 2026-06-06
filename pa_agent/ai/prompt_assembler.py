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

- **思考过程**：扩展思考、内部推理、以及写入 JSON 的 `reason`、`diagnosis_confidence_reasoning`、`trade_confidence_reasoning`、`estimated_win_rate_reasoning` 等说明，**全程使用简体中文**。禁止用英文写推理段落或中英混杂的长句（常见缩写如 HH、HL、Spike、TR 可保留）。
- **最终输出**：阶段一诊断 JSON、阶段二决策 JSON 中所有面向用户的字符串（含 `reasoning`、`key_factors`、`risk_assessment`、`watch_points`、`gate_trace`/`decision_trace` 的 `question` 与 `reason` 等）**一律使用简体中文**。
- **仅允许英文或固定英文枚举**：JSON 字段名（schema 键名）、规定的枚举取值（如 `proceed`、`wait`、`bullish`、`bearish`）、策略文件名、K 线序号格式（如 `K1`、`K42-K1`）。
- **价格行为术语**：思考与 JSON 说明中优先使用下列简体中文 PA 术语（见下节），避免自造词或仅用英文描述。
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

英文缩写（可保留）：SB/EB、OB/IB、H1/H2、L1/L2、MTR、AIL/AIS、20GB。
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
    "若 token 紧张：可缩短思考、将 bar_by_bar_summary 缩至 8 根，"
    "但 gate_trace 与 gate_result 必须写在 JSON 末尾且不可省略。"
).strip()

_STAGE2_TAIL_REMINDER = (
    "【最后一步·必做】思考结束后，立即在 assistant 正文 `content` 输出完整阶段二裸 JSON"
    "（含 decision、decision_trace、terminal）。思考用简体中文并尽量简洁；`content` 不得为空。"
    "若 token 紧张，优先保证 `content` 有 JSON，可缩短思考。\n"
    "⚠️ 禁止在 content 中只写思考过程或分隔符（如 ---输出JSON---）而不附 JSON——"
    "这会导致校验直接失败。哪怕只输出最小骨架 {\"decision\":{\"order_type\":\"不下单\",...}} 也比没有强。"
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
  "market_phase": "stable|transitioning",
  "transition_risk": null,
  "detected_patterns": [],
  "key_signals": [],
  "htf_context": "",
  "entry_setup": "",
  "strategy_files_needed": ["下跌通道分析识别.txt", "下跌通道交易策略.txt"],
  "risk_warning": "",
  "bar_analysis": {
    "always_in": "long|short|neutral",
    "last_closed_bar": "K1",
    "bar_type": "trend_bull|trend_bear|doji|inside|outside_bull|outside_bear|flat|other",
    "signal_bar": {
      "bar": "K2",
      "quality": "strong|medium|weak|invalid",
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
      "context_effect": "strengthens_bull|weakens_bull|strengthens_bear|weakens_bear|neutral|transition",
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
      "bar_range": "由你填写，如 K42-K1"
    }
  ],
  "gate_result": "proceed"
}
```

## 阶段一闸门（二元决策树 §1–§2，必须执行）

在输出诊断 JSON 前，按《二元决策.txt》与内置提示文本**依次**评估以下节点，并写入 gate_trace：
**当 gate_result=proceed 时，必须包含节点 1.2、1.3、2.1、2.2、2.5 共 5 条（§1.1/§2.3/§2.4 由程序判定，AI 不输出）**（每条独立 reason 与 bar_range，禁止照抄示例）：
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
- 必须输出 `bar_by_bar_summary`，**至少 8 条**（分析窗口≥8根时），覆盖最近 **K8–K1** 每一根已收盘 K 线各 1 条（可简写 reason，但**不可只写 K5–K1 共 5 条**）；最多 12 条。数据不足 8 根则覆盖全部。
- 每条只写该 K 线对当前结构的增量作用，不写下单价格、不写止损止盈。
- `role` 只能使用示例中的 8 个英文枚举；延续/跟随棒统一写 `confirmation`，不要写 `continuation`。
- K线序号方向：K1 是最新已收盘，K2 是它前一根；判断 K2 的后续跟随时看 K1，判断 K3 的后续跟随时看 K2/K1；K1 的跟随通常为 pending。
- `bar_type` **必须与程序 K线几何特征表中该 K 线的 bar_type 完全一致，禁止覆盖**。程序的几何判定是权威来源；如果你认为实体是阳线但程序判定为 `trend_bear`，你的判断必须服从程序——可以在 `reason` 里说明（如"程序判定 trend_bear，下影线较长，但整体收阴"），但 `bar_type` 字段必须填程序值。写错会导致校验失败。
- `context_effect` 必须使用 **strengthens_bull / strengthens_bear**（带 s），禁止写 strengthen_bull、strengthen_bear。

**node_overrides（可选，默认不输出）：**
程序已为 §1.1/§2.3/§2.4 填充权威判定，**默认不要输出这些节点**。
仅当你识别到程序规则**未捕捉到**的明确结构性依据时，在顶层 `node_overrides` 数组中提交覆盖：
```json
"node_overrides": [
  {"node_id": "2.3", "answer": "是", "branch": "bearish", "override_reason": "近3根出现强势看跌反转，斜率窗口未捕捉到该结构突变"}
]
```
约束：§1.1/§9.1 为锁定节点不可覆盖；安全闸门（§10.3/§14）只能朝更保守方向；§2.3 answer/branch 须自洽（bullish/bearish↔是，neutral↔中性）；不输出时请勿包含该字段。

**§2.3 覆盖门槛（三项全部满足才允许提交）：**
1. 指明具体是哪根 K 线（如 K2、K1）、哪个结构特征（如强势空头趋势棒跌破颈线、MTR 双确认）导致方向突变；
2. 该特征明确超出程序三信号（EMA斜率/收盘重心位移/波段结构枢轴）的计算范围——例如出现程序窗口未捕捉到的突破/假突破/多空角力转换；
3. override_reason 须用具体 K 线序号和价格结构描述，不接受"整体看跌""趋势感觉已变"等模糊表述。

**§2.4 覆盖门槛（三项全部满足才允许提交）：**
1. 程序判定的 §2.4 reason 字段中**已出现** "⚠️ 近N根K线…与全窗口…结论存在背离"预警，或近期 K 线有明确的 EMA 跌破/站上事件（收盘价穿越 EMA20 且后续 K 线未立即修复）；
2. 指明具体是哪几根 K 线导致短期背离（如"K1-K5 有4根收于 EMA 下方"）；
3. override_reason 须同时说明：①短窗口背离的具体数据（几根中几根）；②为何认为该背离足以推翻全窗口 AIL/AIS 判定而非只是正常回撤。

规则：
- answer 只能是：是 / 否 / 中性 / 等待 / 不适用（**禁止**写「部分」「待确认」「待定」等——部分一致用 **中性**，尚需下一根K线确认用 **等待**）
- 任一闸门导致「等待/unknown」时，gate_result 设为 wait 或 unknown，并在最后一条 trace 写明 reason
- gate_result=proceed 表示可通过闸门进入阶段二；wait/unknown 表示不应进入策略与下单评估
- gate_trace 与 cycle_position、direction 不得矛盾
- 每条 gate_trace.reason 须非空且说明依据（勿只写「通过」「是」等套话）
- gate_result=proceed 时，**最后一条** gate_trace.reason 须含「闸门通过」或「进入阶段二」
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
- 扫描类节点（如禁止行为）：answer 用 **是**（通过）或 **否**（触犯），勿写「通过」
- **禁止照抄**本提示 JSON 示例里的占位文字或说明中的举例数字；必须对应当前 K 线表与你在 reason 中的分析
- 跳过节点（skipped:true）：answer=不适用，bar_range 填字符串 `不适用`（**禁止填 null**）
- question 只写问题本身，不要把 bar_range 写进 question

diagnosis_confidence 必须为 0-100 的整数(满分100),表示对 cycle_position 等诊断结论的综合置信评分。
禁止使用 high、medium、low 等字符串;分数越高表示对当前市场状态判断越有把握。

diagnosis_confidence 分档说明:
- 90-100:周期位置非常典型,K线特征完全匹配频谱定义,多时间框架方向一致,信号充分无矛盾
- 70-89:周期位置较明确,主要特征吻合频谱定义,可能有个别模糊信号但不影响核心判断
- 50-69:周期位置存在歧义(如 trending_tr vs normal_channel),信号部分矛盾,需更多K线确认;市场可能处于过渡阶段
- 30-49:信号严重矛盾,周期位置难以判定,K线特征与多种状态都有部分重叠
- 0-29:数据不足以支撑任何诊断,或市场状态极度混乱(如极端交易区间)
""".strip()

_STAGE2_OUTPUT_CONTRACT = """
请严格按照以下 JSON 格式输出决策结果，不要输出任何其他内容。
**硬约束：思考结束后，必须在 assistant 正文 `content` 输出下方完整阶段二 JSON；不得仅在思考区分析而让 `content` 为空。**
**思考过程与 JSON 内所有说明性文字必须使用简体中文**（仅 JSON 键名与规定枚举除外）。
禁止用 markdown 代码围栏（不要写 ```json 或结尾的 ```），只输出裸 JSON 对象。
JSON 字符串内不要用英文双引号强调，改用「」或不用引号。
重要规则：当 order_type 为“不下单”时，entry_price、take_profit_price、stop_loss_price、order_direction 必须全部为 null。

```json
{
  "decision": {
    "order_direction": "做多|做空|null",
    "order_type": "限价单|突破单|市价单|不下单",
    "entry_price": null,
    "entry_basis_bar": null,
    "entry_basis_extreme": null,
    "entry_rule": null,
    "take_profit_price": null,
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
      "type": "H2|L2|MTR|wedge|tr_boundary|trendline|none"
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

**⚠️ bar_analysis.bar_type 强制规则：必须直接沿用阶段一 `bar_analysis.bar_type` 的值，禁止在阶段二重新推断或修改。** 阶段一几何特征表是程序的确定性计算结果，是权威来源。如果你认为 K1 的棒型与实际不符，可以在 reasoning 里说明，但 `bar_type` 字段必须等于阶段一给出的值。

**⚠️ bar_type 两套分类体系说明（不矛盾，两个维度互补）：**
- **几何特征表（程序预计算）中的"类型"列**：描述单根 K 线的**内部几何**（实体比、收盘位置）。`trend_bear` = 实体占主导、收盘接近低点。
- **bar_analysis.bar_type 与 bar_by_bar_summary.bar_type**：描述该 K 线与**前一根**的关系结构。`outside_bear` = 高低点均超出前棒（外包）且收盘偏低。
- **同一根 K 线完全可以同时是两种**：例如 K1 几何上是 `trend_bear`（实体 62%）、关系上是 `outside_bear`（外包吞没前棒）——两者不矛盾，是不同维度的描述。不要因为几何表显示 `trend_bear` 就认为关系分类"错误"。
- `diagnosis_summary.direction` 必须与 `stage1.direction` **保持一致**，除非你在阶段二的 decision_trace 中以 **node_id="2.3"** 明确记录方向变更及原因。
- **例外（无需 2.3 节点）**：
  - 阶段一 direction=**neutral** → 阶段二 direction=bullish/bearish：程序判不了方向时 AI 阶段二识别出方向属于正常补充，校验器已尅5豪免。不强制补写 2.3，但建议补（给本人看更清晰）。
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
2. **§9** 入场信号二元检查（须先确认信号 K 线质量、二次入场与入场棒跟随）：
   - **§9.0、§9.4、§9.6、§9.7 由 AI 判定**，须写入 decision_trace
   - **§9.1/§9.2/§9.3/§9.5 由程序填充，AI 不输出**（程序依据几何特征确定性判断）
3. **§10** 风险收益（必须按序）：**10.1 止损明确 → 10.2 止损不过大 → 10.3 交易者方程**（勿编造具体手数、合约数或资金规模）
4. **§11 下单方式由程序填充，AI 不输出**（程序依据 cycle_position 路由，仅当 10.3=是 且下单时填充）
5. **§14** 禁止行为清单：下单前快速扫描，触犯任一条 → order_type=不下单
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
- 必须使用 **decision 中已填写的 entry_price / stop_loss_price / take_profit_price** 做数值计算，**禁止**用 K 线收盘、信号棒极点间距或「计划中的 1.8 点/3 点」代替三价
- **突破单须先定 entry 再定 stop/target**：按下方「极值±1跳动」公式写入 `entry_price` 后，再用这三价做 10.3；程序校验前会把错误的突破 entry **校正**为极值±跳动。校正后若盈亏比/方程仍不达标，**10.3 必须判否**且 `order_type=不下单`
- `decision_trace[10.3].reason` 中的入场/止损/目标数字必须与 `decision` 三价一致（勿用未写入 decision 的中间价）
- 做多：风险点数 = entry − stop，回报点数 = take_profit − entry；做空：风险 = stop − entry，回报 = entry − take_profit
- 盈亏比 = 回报 ÷ 风险（程序与界面只认此公式；reasoning 中写的 RR 必须与三价一致，否则校验失败）
- 有下单时：盈亏比不得低于当前交易倾向的底线（保守≥1.5，均衡≥1.2，激进/极度激进≥1.0），且须满足 **胜率%×回报 > (100−胜率)%×风险**
- 不满足上述任一条 → **10.3 必须判「否」**，order_type=**不下单**，不得输出限价/突破/市价单
- **10.3 通过之前**不得输出具体下单类型；**10.3 之后**才写 §11
- 因方程不通过而放弃：terminal.node_id 应为 **10.3**，outcome=reject 或 wait
- 完成 10.3 后，必须把你在方程中使用的**胜率主观估计**写入 decision.estimated_win_rate（0–100 整数），并在 estimated_win_rate_reasoning 简要说明依据；**禁止**留空或仅从 trace 文字里暗示

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
- §9.0–§9.7 必须引用 `bar_analysis.signal_bar.bar` 与阶段一 `bar_by_bar_summary` 中的对应 K 线；只有在“计划型限价/突破挂单，尚无已收盘信号棒”时，`signal_bar.bar` 才可为 null，且必须设 `quality="invalid"`、`pattern="none"`，并在 9.0 写明“等待信号确认/接受该瑕疵”。若限价单/突破单尚未触发，`bar_analysis.entry_bar.bar` 可为 null，但必须设 `strength="not_triggered"`、`freshness="pending"`，并在 9.7 写明“等待触发，尚无入场棒”。
- 信号棒、入场棒、确认棒必须时间顺序合理：信号棒序号通常大于入场棒序号（更早），入场棒之后的跟随看更新的 K 线。
- 如果信号棒之后已经出现 2–3 根无跟随、反向强 K、或 `entry_bar.freshness=stale|invalid`，不得继续把旧信号当作新的突破单依据。
- 如果最新 K1 是 doji、弱入场棒、无跟随或反向确认，必须降低 trade_confidence；除非有非常明确的二次入场/突破测试证据，否则 order_type=不下单。
- 当 `bar_analysis.signal_bar.quality=weak|invalid`，或已触发入场棒但 `entry_bar.follow_through=false` 时，若仍下单，必须在 §9 和 reasoning 中明确说明为何该弱点未使信号失效；否则应等待。挂单未触发时不得把 `follow_through=false` 当作失败跟随，应写 `pending`。

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

**⚠️ terminal.node_id 和 outcome 的语义规则（必须区分以下两种情形）：**

情形 A：**有入场计划，但交易者方程不通过**（有具体止损、止盈数字，但盈亏比不达标）
→ `terminal.node_id = "10.3"`，`outcome = "reject"`
→ 典型表现：10.3 trace 里有具体数值计算，方程结果为负

情形 B：**根本没有入场计划可评估**（§9.0=否/等待，或 §10.1=否 因无止损锚点）
→ `terminal.node_id = "9.0"`（或最早的否定节点），`outcome = "wait"`
→ **不能** terminal 在 10.3，因为从未有过可评估的交易方案
→ **不能** 写 outcome="reject"——拒绝一个不存在的方案在语义上是无意义的

常见错误：§9.0=否 → §10.1=否 → §10.3 写"不适用"或"否" → terminal=10.3/reject
正确做法：§9.0=否 时 terminal 应是 §9.0，outcome=wait，10.3 不应出现在 trace 里（或标 skipped=true）

阶段一 gate_result 为 wait/unknown 时：系统会短路，不应调用本阶段。

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

def _stage1_output_reminder_for_mode(analysis_mode: str = "original") -> str:
    """Return Stage 1 output rules adjusted for the selected analysis mode."""
    mode = (analysis_mode or "original").strip().lower()
    if mode == "optimized":
        return _STAGE1_OUTPUT_REMINDER
    return (
        _STAGE1_OUTPUT_REMINDER
        + "\n\n"
        + """
## 原始分析过程闸门硬规则（覆盖上文任何相反描述）

- 当前为 **原始分析过程**：不要使用“程序已经判定 / 由程序填充 / AI 不输出”作为省略 gate_trace 节点的理由。
- 即使你在思考中认为某个节点已被程序预判，最终 JSON 仍必须在 `gate_trace` 中显式写出该节点。
- 当 `gate_result="proceed"` 时，`gate_trace` 必须至少包含以下节点，且每个节点都要有独立的 `question`、`answer`、`reason`、`bar_range`：
  `0.1`、`0.2`、`1.1`、`1.2`、`1.3`、`2.1`、`2.2`、`2.3`、`2.4`、`2.5`。
- `0.1`/`0.2` 是阶段一前置可读性与继续分析条件闸门；`1.1` 是数据是否足够；`2.3` 是方向；`2.4` 是 Always In。原始模式必须由你自己写入 `gate_trace`。
- 不要在 `gate_trace` 中跳过 `0.1`、`0.2`、`1.1`、`2.3`、`2.4`；否则校验会失败，阶段二不会执行。
""".strip()
    )


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

STAGE1_TASK_PROMPT_TXT_FILES: tuple[str, ...] = (
    "市场诊断框架.txt",
    "文件16-K线信号识别.txt",
    "逐棒分析检查单.txt",
)

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

    def _load_full_strategy_library(self) -> bool:
        cfg = self._prompt_settings
        if cfg is None:
            return False
        return bool(getattr(cfg, "stage2_load_full_strategy_library", False))

    # ── Process-level system-prompt cache ────────────────────────────────────
    # DeepSeek KV Cache hits require the *prefix* of consecutive requests to
    # be byte-identical.  System prompts are fully static (persona + txt files)
    # and never change during a session, so we cache them at the process level.
    # Key = (prompt_dir_str, stage) so different PromptAssembler instances that
    # point to the same directory share the cache.

    @functools.cached_property
    def _system_prompt_stage1(self) -> str:
        """Stage 1 system prompt (cached for the lifetime of this instance)."""
        return self._build_stage1_system_prompt_inner()

    @functools.cached_property
    def _system_prompt_stage2(self) -> str:
        """Stage 2 system prompt (cached for the lifetime of this instance)."""
        return self._build_stage2_system_prompt_inner()

    def _build_stage1_system_prompt(self) -> str:
        """Return cached Stage 1 system prompt."""
        return self._system_prompt_stage1

    def _build_stage2_system_prompt(self) -> str:
        """Return cached Stage 2 system prompt."""
        return self._system_prompt_stage2

    def _build_stage1_system_prompt_inner(self) -> str:
        """Stage 1 system: persona + gate-only decision tree (§0–§2)."""
        system_parts = [_LANGUAGE_ZH_RULE, _PA_TERMINOLOGY_ZH, _THINKING_CONTENT_OUTPUT_RULE]
        system_parts.extend(self._load(name) for name in COMMON_SYSTEM_STAGE1_TXT_FILES)
        return "\n\n---\n\n".join(p for p in system_parts if p)

    def _build_stage2_system_prompt_inner(self) -> str:
        """Stage 2 system: persona + full decision tree."""
        system_parts = [_LANGUAGE_ZH_RULE, _PA_TERMINOLOGY_ZH, _THINKING_CONTENT_OUTPUT_RULE]
        system_parts.extend(self._load(name) for name in COMMON_SYSTEM_STAGE2_TXT_FILES)
        return "\n\n---\n\n".join(p for p in system_parts if p)

    # ── File loading ──────────────────────────────────────────────────────────

    def _load(self, filename: str) -> str:
        """Load a prompt file by name. Returns empty string on error."""
        path = self._prompt_dir / filename
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to load prompt file %s: %s", filename, exc)
            return f"[ERROR: could not load {filename}]"

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
    def _render_kline_feature_table(frame: KlineFrame, limit: int | None = None) -> str:
        """Render方案 A single-bar geometry features for prompt grounding."""
        shown = limit if limit is not None else len(frame.bars)
        lines = [
            f"（几何特征：最近 {shown} 根已收盘 K 线；多棒形态已用完整窗口计算）",
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

    # ── Stage 1 ───────────────────────────────────────────────────────────────

    def build_stage1(
        self,
        frame: KlineFrame,
        *,
        analysis_mode: str = "original",
    ) -> list[dict]:
        """Build the message list for Stage 1 (market diagnosis)."""
        system_content = self._build_stage1_system_prompt()
        user_content = self._build_stage1_user_prompt(
            frame,
            analysis_mode=analysis_mode,
        )

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    def build_incremental_stage1(
        self,
        frame: KlineFrame,
        previous_record: AnalysisRecord,
        new_bar_count: int,
        *,
        analysis_mode: str = "original",
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
        if not prev_assistant_content:
            raise ValueError(
                f"build_incremental_stage1: previous_record.stage1_response "
                f"has no 'content' field. "
                f"stage1_response type={type(prev_s1_response).__name__}, "
                f"keys={list(prev_s1_response.keys()) if isinstance(prev_s1_response, dict) else 'N/A'}. "
                f"record.meta: {getattr(previous_record, 'meta', '<missing>')!r}"
            )

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
            {"role": "assistant", "content": prev_assistant_content},
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
            import copy

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
                "  • §2.3 覆盖须指明具体 K 线序号+结构特征，且该特征超出三信号（EMA斜率/收盘重心/波段枢轴）的计算范围；\n"
                "  • §2.4 覆盖须先确认 reason 中是否已出现短窗口背离预警（⚠️标记），"
                "并给出具体背离数据（几根中几根收盘偏向反方向）及推翻全窗口判定的理由；\n"
                "  • override_reason 必须具体，不接受「整体看跌」「感觉已变」等模糊描述。"
            )
            return "\n".join(hint_lines)
        except Exception as exc:  # noqa: BLE001
            logger.warning("_render_program_prefill_hint failed: %s", exc)
            return ""

    def _build_stage1_user_prompt(
        self,
        frame: KlineFrame,
        *,
        analysis_mode: str = "original",
    ) -> str:
        """Build the Stage 1 task turn; stage-specific rules stay out of system."""
        pattern_block = self._stage1_pattern_supplement()
        use_prefill = (analysis_mode or "original").strip().lower() == "optimized"
        prefill_hint = self._render_program_prefill_hint(frame) if use_prefill else ""
        stage1_parts = [
            *(self._load(name) for name in STAGE1_TASK_PROMPT_TXT_FILES),
            *([pattern_block] if pattern_block else []),
            _stage1_output_reminder_for_mode(analysis_mode),
        ]
        stage1_context = "\n\n---\n\n".join(p for p in stage1_parts if p)
        kline_table = self._render_kline_table(frame)
        feature_table = self._render_kline_feature_table(frame)
        n_bars = len(frame.bars)
        return (
            "## 阶段一任务\n\n"
            "你现在只执行阶段一：市场诊断与闸门判断。不要评估具体下单、止损、止盈或仓位。\n\n"
            f"{stage1_context}\n\n"
            "---\n\n"
            f"## 当前分析目标\n\n"
            f"品种:{frame.symbol} 周期:{frame.timeframe} K线数量:{n_bars}\n"
            f"（K线序号：1=最新已收盘，最大 K{n_bars}；"
            f"每个决策节点的 bar_range 由你自行选择子区间，勿超出 K{n_bars}-K1）\n\n"
            f"## ⚠️ 分析窗口分层规则（强制，必须遵守）\n\n"
            f"你收到全部 {n_bars} 根 K 线数据，但分析深度必须严格分层：\n\n"
            f"**详细分析区 K1–K40（最近约 10 小时）：**\n"
            f"- bar_by_bar_summary 覆盖此区间\n"
            f"- 通道/波段/趋势结构识别、信号棒识别、趋势棒计数、反转判断\n"
            f"- market_phase 和 cycle_position 判断基于**此区间**的结构特征\n"
            f"- 各闸门节点的 bar_range 优先选取此区间\n\n"
            f"**结构背景区 K41–K{n_bars}（更早期）：**\n"
            f"- **只提取重要的 swing highs/lows（波段高点和低点）**\n"
            f"- 将这些关键价位写入 `htf_context` 字段，格式示例：\n"
            f"  「K65 高点 2685、K80 低点 2632、K92 高点 2698」\n"
            f"- **禁止** 对 K41–K{n_bars} 做逐棒分析、通道识别、趋势结构、信号判断\n"
            f"- **禁止** 在 bar_by_bar_summary 中涵盖 K41 及更早的 K 线\n"
            f"- **禁止** 用 K41–K{n_bars} 的结构判断大时间框架（HTF）方向——"
            f"其作用仅限于提供价格水平参考（潜在的支撑/阻力/磁力位）\n"
            f"- K41–K{n_bars} 的高低价位只在 node 2.2 中作为**次要参考**，"
            f"不作为 HTF 方向的否决依据\n\n"
            f"## K线数据(序号1=最新已收盘K线,序号越大越早;不含当前未收盘K线;"
            f"阳阴列由程序按收盘价与开盘价计算:收盘>开盘=阳线,收盘<开盘=阴线,相等=平)\n\n"
            f"{kline_table}\n\n"
            "## K线几何特征(程序预计算，仅作客观辅助；类型为单棒分类，不替代周期判断；"
            "基于当前 N 根已收盘 K 线，指标非全历史延续)\n\n"
            f"{feature_table}\n\n"
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
        use_prefill = (analysis_mode or "original").strip().lower() == "optimized"
        prefill_hint = self._render_program_prefill_hint(frame) if use_prefill else ""
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
        Injects prefill_hint so the AI knows the updated §2.3/§2.4 verdicts
        even though the full K-line table is not re-sent.
        """
        use_prefill = (analysis_mode or "original").strip().lower() == "optimized"
        prefill_hint = self._render_program_prefill_hint(frame) if use_prefill else ""
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
            "- 必须输出顶层字段 **incremental_delta**（不可省略），结构示例：\n"
            '  "incremental_delta": {"new_closed_bars":["K1"],'
            '"changed_fields":["direction","cycle_position"],'
            '"summary":"相对上一轮：新增K1突破区间上沿，方向由中性转偏多"}\n'
            "- new_closed_bars 长度必须等于「新增已收盘K线」数量（1根则只写 [\"K1\"]）。\n"
            "- 并在 summary / risk_warning / gate_trace 中说明相对上一轮变化。\n"
            "- gate_result=proceed 时 gate_trace 仍须覆盖 §1.2、§1.3、§2.1、§2.2、§2.5（§1.1/§2.3/§2.4 由程序填充）。\n"
            "- 输出仍必须是完整阶段一 JSON，而不是差异补丁。\n\n"
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
            include_kline_table=True,
            decision_stance=decision_stance,
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
    ) -> list[dict]:
        """Build Stage 2 as a true continuation of the Stage 1 conversation.

        Structure:
          [0] system    — Stage 2 system prompt (full decision tree, same as Stage 1)
          [1] user      — Stage 1 original user prompt (with K-line table, from stage1_messages)
          [2] user      — Stage 2 task prompt (without K-line table; embeds S1 diagnosis JSON)

        Note: the ``assistant`` role is intentionally omitted.  Including it would
        make ``messages[2]`` (the S2 user turn) unique every cycle because the
        prefix preceding it changes — the assistant content contains the S1 reply
        JSON which varies each run.  Without the assistant message, the prefix is:
          system (static) + user[S1] (static per symbol/tf/barcount)
        and the S2 user turn only needs to re-send the dynamic diagnosis JSON,
        keeping the large strategy-file block fully cached.
        """
        system_content = self._build_stage2_system_prompt()

        # Extract Stage 1 user message (contains K-line table; no need to rebuild)
        stage1_user_content = ""
        for msg in stage1_messages:
            if msg.get("role") == "user":
                stage1_user_content = msg["content"]
                break

        # Stage 2 user prompt: include_kline_table=False → "沿用上一轮" fallback
        stage2_user_content = self._build_stage2_user_prompt(
            frame=frame,
            stage1_json=stage1_json,
            strategy_files=strategy_files,
            experience_entries=experience_entries,
            include_kline_table=False,
            decision_stance=decision_stance,
            previous_record=previous_record,
        )

        return [
            {"role": "system",    "content": system_content},
            {"role": "user",      "content": stage1_user_content},
            {"role": "user",      "content": stage2_user_content},
        ]

    def _build_stage2_user_prompt(
        self,
        *,
        frame: KlineFrame,
        stage1_json: dict,
        strategy_files: list[str],
        experience_entries: list[Any],
        include_kline_table: bool,
        decision_stance: str = "conservative",
        previous_record: Any | None = None,
    ) -> str:
        """Build the Stage 2 task turn for standalone or continuation mode."""
        stance_block = build_decision_stance_guidance(normalize_stance(decision_stance))
        transition_block = self._render_transition_guidance(stage1_json)
        stage2_parts = [
            stance_block,
            transition_block,
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
        stage2_parts.append(_NEXT_BAR_PREDICTION_INSTRUCTION)
        stage2_parts.append(_NEXT_CYCLE_PREDICTION_INSTRUCTION)
        stage2_context = "\n\n---\n\n".join(p for p in stage2_parts if p)

        kline_table = self._render_kline_table(frame)
        feature_table = self._render_kline_feature_table(frame)
        # gate_trace and gate_result are already included inside the compact
        # stage1 diagnosis JSON block above; a separate gate_block section
        # was redundant (they existed twice in the same uncached message).

        from pa_agent.util.price_tick import format_breakout_tick_hint

        n_bars = len(frame.bars)
        breakout_tick_hint = format_breakout_tick_hint(frame)
        kline_block = (
            f"## K线数据(与阶段一相同, 共{n_bars}根，含阳阴列；各节点 bar_range 由你据实填写)\n\n"
            f"{kline_table}\n\n"
            "## K线几何特征(程序预计算，仅作逐棒客观辅助；不得替代交易者方程；"
            "基于当前 N 根已收盘 K 线，指标非全历史延续)\n\n"
            f"{feature_table}\n\n"
            if include_kline_table
            else (
                f"## K线数据\n\n"
                f"沿用上一轮阶段一用户消息中的同一份 K线数据，共 {n_bars} 根。"
                f"各节点 bar_range 由你据实填写，必要时可回溯上方阶段一用户消息查看具体价格。\n\n"
            )
        )
        if breakout_tick_hint and include_kline_table:
            kline_block += f"{breakout_tick_hint}\n\n"
        prev_pred_block = self._render_previous_prediction(previous_record)
        return (
            "## 阶段二任务\n\n"
            "你现在独立执行阶段二：交易决策、风险收益和下单方式评估（基于阶段一诊断结果）。\n"
            "以下 JSON 是程序校验通过后的阶段一诊断结果，请以此为权威依据；阶段一 K 线数据见上方阶段一用户消息。\n\n"
            f"{stage2_context}\n\n"
            "---\n\n"
            f"## 阶段一诊断结果\n\n```json\n"
            f"{json.dumps(self._compact_stage1_for_stage2(stage1_json), ensure_ascii=False, indent=2)}"
            f"\n```\n\n"
            f"{kline_block}"
            f"{prev_pred_block + chr(10) if prev_pred_block else ''}"
            f"请根据以上诊断和K线数据,按《二元决策.txt》§3–§15 输出 JSON 决策结果"
            f"(含 decision_trace 与 terminal)。\n"
            f"注意:如果判断不下单,entry_price、take_profit_price、stop_loss_price、order_direction 必须全部为 null。\n\n"
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
            "entry_setup",
            "strategy_files_needed",
            "risk_warning",
            "bar_analysis",
            "bar_by_bar_summary",
            "gate_trace",
            "gate_result",
        )
        return {k: stage1_json[k] for k in keys if k in stage1_json}

    @staticmethod
    def _render_transition_guidance(stage1_json: dict) -> str:
        """Render dynamic risk guidance from Stage 1 market_phase fields."""
        if stage1_json.get("market_phase") != "transitioning":
            return ""
        risk = stage1_json.get("transition_risk") or "medium"
        if risk == "high":
            size = "正常仓位的50%"
            selectivity = "只接受最清晰的二次入场、突破回踩或边界信号"
        elif risk == "medium":
            size = "正常仓位的75%"
            selectivity = "选择性入场，放弃弱信号和中间位置"
        else:
            size = "小幅降低"
            selectivity = "保持正常流程，但在 reason 中说明状态转换风险"
        return (
            "## 状态转换期风险指导\n\n"
            f"阶段一判断 market_phase=transitioning，transition_risk={risk}。\n"
            f"- 仓位倾向：{size}。\n"
            f"- 入场选择：{selectivity}。\n"
            "- 不因为状态转换而跳过 §9、§10、§14；只是提高信号质量门槛并降低交易频率。"
        )

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
