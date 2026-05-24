"""Prompt assembler for Stage 1 (diagnosis) and Stage 2 (decision)."""
from __future__ import annotations

import datetime
import json
import logging
import math
from pathlib import Path
from typing import Any

from pa_agent.ai.decision_stance import build_decision_stance_guidance, normalize_stance
from pa_agent.ai.kline_features import bar_candle_direction_label, compute_kline_geometry_features
from pa_agent.data.base import KlineFrame
from pa_agent.records.schema import AnalysisRecord

logger = logging.getLogger(__name__)

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
    "若 token 紧张，优先保证 `content` 有 JSON，可缩短思考。"
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
    "bar_type": "trend_bull|trend_bear|doji|inside|outside_bull|outside_bear|other",
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
      "bar_type": "trend_bull|trend_bear|doji|inside|outside_bull|outside_bear|other",
      "context_effect": "strengthens_bull|weakens_bull|strengthens_bear|weakens_bear|neutral|transition",
      "follow_through": "yes|no|pending|failed",
      "trapped_side": "bulls|bears|both|none|unknown",
      "reason": "一句话说明该K线对当前市场状态的增量影响"
    }
  ],
  "gate_trace": [
    {
      "node_id": "0.1",
      "question": "是否看得懂当前市场？",
      "answer": "是",
      "action": "继续",
      "reason": "结构清晰",
      "branch": "yes",
      "section": "总原则",
      "bar_range": "由你填写，如 K42-K1"
    }
  ],
  "gate_result": "proceed"
}
```

## 阶段一闸门（二元决策树 §0–§2，必须执行）

在输出诊断 JSON 前，按《二元决策.txt》**依次**评估以下节点，并写入 gate_trace（仅记录你实际评估的节点，通常 6–10 条）：
§0：0.1 看得懂市场 → 0.2 是否具备继续分析的条件（定性，**不是**交易者方程）
§1：1.1 数据足够 → 1.2 识别周期 → 1.3 极端混乱
§2：2.1 惯性方向 → 2.2 大时间框架 → 2.3 多/空/中性 → 2.4 Always In 状态 → 2.5 惯性强度（**answer 只能用 是/否/中性**；方向或 AIL/AIS 写在 branch，勿写「多头」「空头」作 answer）

**禁止在阶段一评估：**
- **0.3**（交易者方程仅为原则；数值检验在阶段二 **10.3**）
- **§9–§11**（入场、风险、下单均属阶段二）

**逐K摘要硬规则：**
- 必须输出 `bar_by_bar_summary`，覆盖最近 8–12 根已收盘 K 线（数据不足则覆盖全部；勿超过 12 条以免截断 gate_trace）。
- 每条只写该 K 线对当前结构的增量作用，不写下单价格、不写止损止盈。
- `role` 只能使用示例中的 8 个英文枚举；延续/跟随棒统一写 `confirmation`，不要写 `continuation`。
- K线序号方向：K1 是最新已收盘，K2 是它前一根；判断 K2 的后续跟随时看 K1，判断 K3 的后续跟随时看 K2/K1；K1 的跟随通常为 pending。
- `bar_type` 必须优先对齐程序提供的 K线几何特征表。

规则：
- answer 只能是：是 / 否 / 中性 / 等待 / 不适用（**禁止**写「部分」「待确认」「待定」等——部分一致用 **中性**，尚需下一根K线确认用 **等待**）
- 任一闸门导致「等待/unknown」时，gate_result 设为 wait 或 unknown，并在最后一条 trace 写明 reason
- gate_result=proceed 表示可通过闸门进入阶段二；wait/unknown 表示不应进入策略与下单评估
- gate_trace 与 cycle_position、direction 不得矛盾

**每条 gate_trace / decision_trace 必须包含 bar_range（K线依据，由你自行判断）：**
- **程序不会替你填写**；你必须根据「本节点实际引用了哪些 K 线」写出序号范围
- 格式：`K{较老序号}-K{较新序号}` 或单根 `K1`（**序号1=最新已收盘**，序号越大越早）
- **每个节点的 bar_range 应不同**（除非该节点确实与上一节点使用完全相同窗口）；禁止所有节点照抄同一个范围
- 区间格式必须为 **K{较老}-K{较新}**（如 K4-K1），**禁止** K1-K4；单根写 K1；全图分析可写「全局」（程序会展开）
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
    "bar_type": "trend_bull|trend_bear|doji|inside|outside_bull|outside_bear|other",
    "signal_bar": {
      "bar": "K2 或 null（计划型挂单尚无已收盘信号棒时为 null）",
      "quality": "strong|medium|weak|invalid",
      "pattern": "H1|H2|L1|L2|MTR|wedge|tr_boundary|breakout_pullback|none",
      "reason": "信号棒质量判断"
    },
    "entry_bar": {
      "bar": "K1 或 null（限价/突破挂单尚未触发时为 null）",
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
**每条 trace 的 answer 只能是以下五选一**：`是`、`否`、`中性`、`等待`、`不适用`。
禁止写「部分符合」「部分是」「上涨通道」等；模糊或分类细节写在 **reason**（方向类节点可另填 **branch**）。

## 阶段二决策路径（二元决策树 §3–§11、§14）

阶段一 gate_result=proceed 时，decision_trace 必须遵守**执行顺序**（可跳过不适用分支，但不可乱序）：

1. **§3–§8** 按 cycle_position 走对应结构分支（尖峰/通道/区间/反转/楔形等）
2. **§9** 入场信号二元检查（9.0→9.7，须先确认信号 K 线质量、二次入场与入场棒跟随）
3. **§10** 风险收益（必须按序）：**10.1 止损明确 → 10.2 止损不过大 → 10.3 交易者方程**（勿编造具体手数、合约数或资金规模）
4. **§11** 下单方式（仅当 10.3 为「是」且拟下单时评估 11.1–11.4）
5. **§14** 禁止行为清单：下单前快速扫描，触犯任一条 → order_type=不下单

**交易者方程（10.3）规则：**
- 必须使用 **decision 中已填写的 entry_price / stop_loss_price / take_profit_price** 做数值计算，**禁止**用 K 线收盘、信号棒极点间距或「计划中的 1.8 点/3 点」代替三价
- 做多：风险点数 = entry − stop，回报点数 = take_profit − entry；做空：风险 = stop − entry，回报 = entry − take_profit
- 盈亏比 = 回报 ÷ 风险（程序与界面只认此公式；reasoning 中写的 RR 必须与三价一致，否则校验失败）
- 有下单时：盈亏比不得低于当前交易倾向的底线（保守≥1.5，均衡≥1.2，激进/极度激进≥1.0），且须满足 **胜率%×回报 > (100−胜率)%×风险**
- 不满足上述任一条 → **10.3 必须判「否」**，order_type=**不下单**，不得输出限价/突破/市价单
- **10.3 通过之前**不得输出具体下单类型；**10.3 之后**才写 §11
- 因方程不通过而放弃：terminal.node_id 应为 **10.3**，outcome=reject 或 wait
- 完成 10.3 后，必须把你在方程中使用的**胜率主观估计**写入 decision.estimated_win_rate（0–100 整数），并在 estimated_win_rate_reasoning 简要说明依据；**禁止**留空或仅从 trace 文字里暗示

**突破单 entry_price 硬规则：**
- order_type="突破单" 时，必须填写 decision.entry_basis_bar、decision.entry_basis_extreme、decision.entry_rule。
- 做多突破单：entry_basis_extreme 必须为 "high"，entry_price 必须位于 entry_basis_bar 高点上方 1 跳动或至少明确高于该高点。
- 做空突破单：entry_basis_extreme 必须为 "low"，entry_price 必须位于 entry_basis_bar 低点下方 1 跳动或至少明确低于该低点。
- 突破单禁止使用 K 线实体中部、当前价附近、EMA 附近或任意折中价作为 entry_price。
- 如果无法从 K线表确定依据 K 线极点或 tick size，不得编造中间价；应输出 order_type="不下单"，并说明等待信号棒极点被突破。
- 限价单/市价单不使用 entry_basis_* 字段，可填 null。

**§9 逐K信号链与新鲜度硬规则：**
- §9.0–§9.7 必须引用 `bar_analysis.signal_bar.bar` 与阶段一 `bar_by_bar_summary` 中的对应 K 线；只有在“计划型限价/突破挂单，尚无已收盘信号棒”时，`signal_bar.bar` 才可为 null，且必须设 `quality="invalid"`、`pattern="none"`，并在 9.0 写明“等待信号确认/接受该瑕疵”。若限价单/突破单尚未触发，`bar_analysis.entry_bar.bar` 可为 null，但必须设 `strength="not_triggered"`、`freshness="pending"`，并在 9.7 写明“等待触发，尚无入场棒”。
- 信号棒、入场棒、确认棒必须时间顺序合理：信号棒序号通常大于入场棒序号（更早），入场棒之后的跟随看更新的 K 线。
- 如果信号棒之后已经出现 2–3 根无跟随、反向强 K、或 `entry_bar.freshness=stale|invalid`，不得继续把旧信号当作新的突破单依据。
- 如果最新 K1 是 doji、弱入场棒、无跟随或反向确认，必须降低 trade_confidence；除非有非常明确的二次入场/突破测试证据，否则 order_type=不下单。
- 当 `bar_analysis.signal_bar.quality=weak|invalid`，或已触发入场棒但 `entry_bar.follow_through=false` 时，若仍下单，必须在 §9 和 reasoning 中明确说明为何该弱点未使信号失效；否则应等待。挂单未触发时不得把 `follow_through=false` 当作失败跟随，应写 `pending`。

**跳过规则：**
- 无持仓：跳过 §12、§13（不写 trace）
- 不适用分支：skipped:true，answer=不适用

terminal 必须与 order_type 一致：
- 有下单 → outcome=trade，terminal.node_id 建议为最后一个 §11 节点
- 不下单 → outcome=wait 或 reject

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
- order_type=「不下单」时：estimated_win_rate 填 **null**，estimated_win_rate_reasoning 可说明为何不交易
- 有下单时：estimated_win_rate 为 **必填整数**（不要填区间字符串，取你判断的最可能值，如 47）
estimated_win_rate_reasoning：必须简要说明依据（如“宽通道顺势 Low1，结构支持约 45–50%，取 47% 用于方程”）
""".strip()

# txt files merged into each stage prompt (order preserved)
# 二元决策.txt lives in system once — shared by Stage 1 and Stage 2 (avoids ~10k×2 chars/run).
COMMON_SYSTEM_PROMPT_TXT_FILES: tuple[str, ...] = (
    "提示词大纲_人设与思维方式.txt",
    "二元决策.txt",
)

STAGE1_TASK_PROMPT_TXT_FILES: tuple[str, ...] = (
    "市场诊断框架.txt",
    "文件16-K线信号识别.txt",
    "逐棒分析检查单.txt",
    "文件13-窄通道与宽通道策略.txt",
    "文件14-楔形形态分析交易.txt",
    "文件15-二次入场机会.txt",
    "文件18-突破失败与突破测试.txt",
    "文件19-H1H2-L1L2计数.txt",
    "文件20-AlwaysIn与20GB.txt",
    "文件21-铁丝网与无交易环境.txt",
    "文件22-信号失败后的磁力位.txt",
)

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
    return [*COMMON_SYSTEM_PROMPT_TXT_FILES, *STAGE1_TASK_PROMPT_TXT_FILES]


def stage2_user_task_txt_files(strategy_files: list[str] | None = None) -> list[str]:
    """Return .txt filenames loaded into the Stage 2 user turn only."""
    files = [
        *(f for f in (strategy_files or []) if f),
        *STAGE2_FULL_STRATEGY_PROMPT_TXT_FILES,
        *STAGE2_BASE_PROMPT_TXT_FILES,
    ]
    return list(dict.fromkeys(files))


def stage2_prompt_txt_files(strategy_files: list[str] | None = None) -> list[str]:
    """Return all .txt files relevant to Stage 2 (system common + user task), for UI/debug."""
    return [*COMMON_SYSTEM_PROMPT_TXT_FILES, *stage2_user_task_txt_files(strategy_files)]


# ── PromptAssembler ────────────────────────────────────────────────────────────

class PromptAssembler:
    """Builds message lists for Stage 1 and Stage 2 API calls."""

    def __init__(
        self,
        prompt_dir: Path,
        experience_reader: Any = None,
    ) -> None:
        self._prompt_dir = prompt_dir
        self._experience_reader = experience_reader

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
            # ts_open is in milliseconds (server time); convert to datetime
            _EPOCH = datetime.datetime(1970, 1, 1)
            dt = (_EPOCH + datetime.timedelta(seconds=bar.ts_open / 1000)).strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"{bar.seq:<4} | {dt:<19} | {bar.open:<9.4f} | {bar.high:<9.4f} | "
                f"{bar.low:<9.4f} | {bar.close:<9.4f} | {yang_yin:<4} | {bar.volume:<9.0f} | "
                f"{ema_str:<10} | {atr_str}"
            )
        return "\n".join(lines)

    @staticmethod
    def _render_kline_feature_table(frame: KlineFrame, limit: int | None = None) -> str:
        """Render方案 A single-bar geometry features for prompt grounding."""
        lines = [
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
        return "\n".join(lines)

    # ── Stage 1 ───────────────────────────────────────────────────────────────

    def build_stage1(self, frame: KlineFrame) -> list[dict]:
        """Build the message list for Stage 1 (market diagnosis)."""
        system_content = self._build_common_system_prompt()
        user_content = self._build_stage1_user_prompt(frame)

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    def build_incremental_stage1(
        self,
        frame: KlineFrame,
        previous_record: AnalysisRecord,
        new_bar_count: int,
    ) -> list[dict]:
        """Build Stage 1 as an incremental update from a previous record."""
        system_content = self._build_common_system_prompt()
        user_content = self._build_incremental_stage1_user_prompt(
            frame,
            previous_record,
            new_bar_count,
        )
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    def _build_common_system_prompt(self) -> str:
        """Build the stable system prompt shared by Stage 1 and Stage 2."""
        system_parts = [_LANGUAGE_ZH_RULE, _PA_TERMINOLOGY_ZH, _THINKING_CONTENT_OUTPUT_RULE]
        system_parts.extend(self._load(name) for name in COMMON_SYSTEM_PROMPT_TXT_FILES)
        return "\n\n---\n\n".join(p for p in system_parts if p)

    def _build_stage1_user_prompt(self, frame: KlineFrame) -> str:
        """Build the Stage 1 task turn; stage-specific rules stay out of system."""
        stage1_parts = [
            *(self._load(name) for name in STAGE1_TASK_PROMPT_TXT_FILES),
            _STAGE1_OUTPUT_REMINDER,
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
            f"## K线数据(序号1=最新已收盘K线,序号越大越早;不含当前未收盘K线;"
            f"阳阴列由程序按收盘价与开盘价计算:收盘>开盘=阳线,收盘<开盘=阴线,相等=平)\n\n"
            f"{kline_table}\n\n"
            "## K线几何特征(程序预计算，仅作客观辅助；类型为单棒分类，不替代周期判断)\n\n"
            f"{feature_table}\n\n"
            f"请根据以上数据，严格输出阶段一 JSON 诊断结果。\n\n"
            f"{_STAGE1_TAIL_REMINDER}"
        )

    def _build_incremental_stage1_user_prompt(
        self,
        frame: KlineFrame,
        previous_record: AnalysisRecord,
        new_bar_count: int,
    ) -> str:
        """Build a Stage 1 update turn using the last completed analysis."""
        stage1_parts = [
            *(self._load(name) for name in STAGE1_TASK_PROMPT_TXT_FILES),
            _STAGE1_OUTPUT_REMINDER,
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
            f"## 新增 K线几何特征(共{new_count}根)\n\n"
            f"{new_feature_table}\n\n"
            f"## 当前完整 K线数据(共{n_bars}根，用于必要时复核整体结构；含阳阴列)\n\n"
            f"{full_kline_table}\n\n"
            f"## 当前完整 K线几何特征(用于逐棒辅助，不替代周期判断)\n\n"
            f"{full_feature_table}\n\n"
            "请基于上一轮结论、新增K线和当前完整K线，严格输出更新后的阶段一 JSON 诊断结果。\n\n"
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
        system_content = self._build_common_system_prompt()
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
    ) -> list[dict]:
        """Build Stage 2 continuation without duplicating the Stage 1 user prompt.

        Stage 1 user turn is huge (framework + 100 bars). Re-sending it for Stage 2
        balloons prompt tokens and often exhausts thinking before any content JSON.
        """
        system_content = next(
            (m.get("content", "") for m in stage1_messages if m.get("role") == "system"),
            self._build_common_system_prompt(),
        )
        return [
            {"role": "system", "content": system_content},
            {"role": "assistant", "content": stage1_reply_content},
            {
                "role": "user",
                "content": self._build_stage2_user_prompt(
                    frame=frame,
                    stage1_json=stage1_json,
                    strategy_files=strategy_files,
                    experience_entries=experience_entries,
                    include_kline_table=True,
                    decision_stance=decision_stance,
                ),
            },
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
    ) -> str:
        """Build the Stage 2 task turn for standalone or continuation mode."""
        stance_block = build_decision_stance_guidance(normalize_stance(decision_stance))
        transition_block = self._render_transition_guidance(stage1_json)
        stage2_parts = [
            stance_block,
            transition_block,
            *(self._load(name) for name in stage2_user_task_txt_files(strategy_files)),
        ]
        if experience_entries:
            stage2_parts.append(self._render_experience(experience_entries))
        stage2_parts.append(_STAGE2_OUTPUT_CONTRACT)
        stage2_context = "\n\n---\n\n".join(p for p in stage2_parts if p)

        kline_table = self._render_kline_table(frame)
        feature_table = self._render_kline_feature_table(frame)
        gate_result = stage1_json.get("gate_result", "proceed")
        gate_trace = stage1_json.get("gate_trace") or []
        gate_block = ""
        if gate_trace:
            gate_block = (
                f"## 阶段一闸门路径 (gate_result={gate_result})\n\n"
                f"```json\n{json.dumps(gate_trace, ensure_ascii=False, indent=2)}\n```\n\n"
            )

        n_bars = len(frame.bars)
        kline_block = (
            f"## K线数据(与阶段一相同, 共{n_bars}根，含阳阴列；各节点 bar_range 由你据实填写)\n\n"
            f"{kline_table}\n\n"
            "## K线几何特征(程序预计算，仅作逐棒客观辅助；不得替代交易者方程)\n\n"
            f"{feature_table}\n\n"
            if include_kline_table
            else f"## K线数据\n\n沿用上一轮阶段一用户消息中的同一份 K线数据，共 {n_bars} 根；各节点 bar_range 由你据实填写。\n\n"
        )
        return (
            "## 阶段二任务\n\n"
            "继续上一轮对话。你已经完成阶段一诊断；现在只执行阶段二：交易决策、风险收益和下单方式评估。\n"
            "上一轮 assistant 消息是阶段一完整响应，下面的 JSON 是程序校验通过后的阶段一诊断结果，若两者有细微格式差异，以此处 JSON 为准。\n\n"
            f"{stage2_context}\n\n"
            "---\n\n"
            f"## 阶段一诊断结果\n\n```json\n{json.dumps(stage1_json, ensure_ascii=False, indent=2)}\n```\n\n"
            f"{gate_block}"
            f"{kline_block}"
            f"请根据以上诊断、闸门路径和K线数据,按《二元决策.txt》§3–§15 输出 JSON 决策结果"
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
        return self._build_common_system_prompt()

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
    def _render_experience(entries: list[Any]) -> str:
        """Render experience library entries as a text block."""
        lines = ["## 经验库(最近案例,供参考)"]
        for i, entry in enumerate(entries, 1):
            if isinstance(entry, dict):
                lines.append(
                    f"\n### 案例 {i}\n```json\n{json.dumps(entry, ensure_ascii=False, indent=2)}\n```"
                )
            else:
                lines.append(f"\n### 案例 {i}\n{entry}")
        return "\n".join(lines)
