# GLM-4-flash 备用通道精简：实现与验收

## 交付行为

- 仅精确匹配 `glm-4-flash`（大小写及两端空白归一化）。不匹配 flashx、GLM-5 或其他主通道模型。直接配置该模型和运行中 fallback 到该模型均走同一小契约。
- 已进入该路径时不向模型请求阶段一诊断/闸门树：本地提供兼容上下文，标注 local_compatibility。`proceed` 只代表继续请求决策，不表示市场条件/安全闸门通过。
- 实际模型请求只有决策、下一根K线预期、后续走势预期。不发送教材决策树/完整策略库/逐节点推理要求；关闭此路径的思考要求。其他模型提示词保持原逻辑。
- 只发送已收盘K线，按旧→新排列并单独标明最新已收盘K线；不是行情预测准确率升级。
- 模型返回的 decision_trace、terminal、node_overrides 等不作为判断依据，均不进入此路径校验。先保留核心决策/预期，再运行既有归一化、交易指标和连续性检查，最后生成本地示意树。
- 入场/止损/目标缺失、非有限数、方向/价格关系错误的计划，明确改为观望并在决策说明中解释，保留原模型意见供内部审计；不会为了显示交易而编造止损/目标。第二目标可以不提供，旧双目标接口以第一目标兼容填充，不额外推算目标。
- 走势方向和解释保留；不把程序概率冒充模型返回。按用户后续要求，下一市场周期的概率由本地描述性规则补全，概率来源收拢至可展开的“分析详情”，卡片仅展示概率；下一根K线概率仍不要求模型提供。具体规则与本轮验收见 `CYCLE_PROBABILITIES.md`。
- 两个决策树面板采用简洁文案，来源说明收拢至可展开的“分析详情”，不把兼容路由写成风控通过；最新展示修复与129项回归见 `FINAL_DISPLAY_FIX.md`。短路径采用全图适配，避免原600%缩放落在空白区域；其他路径缩放设置不变。
- `reasoning_content is very short` 对此非思考模型不再报警；其他模型仍保留此诊断。模型树已被移出契约，因此不再产生 decision_trace.answer 枚举错误。没有全局屏蔽校验/网络错误。
- CMD以“备用通道”显示切换/进度；文件日志保留真实诊断信息。

## 修改文件

- `pa_agent/ai/glm_flash_lite.py`：精确识别、简短提示词、本地诊断占位、核心契约、价格安全降级、示意树。
- `pa_agent/orchestrator/two_stage.py`：直达及运行中切换路由、实际发送消息归档、单次模型请求、按实际路由切换校验。
- `pa_agent/ai/json_validator.py`：小契约入口；不校验模型树；正常价格/风险归一化后再附加本地树。
- `pa_agent/ai/deepseek_client.py`：精确路径的短思考警告处理。
- `pa_agent/gui/main_window.py`：本地树不混入主通道的闸门树。
- `pa_agent/gui/decision_tree_panel.py`、`decision_flow_viz.py`：本地来源提示及短路径缩放。
- `pa_agent/gui/future_trend_panel.py`：没有概率时仍显示方向及解释。
- `pa_agent/util/logging.py`：备用通道中性显示，不修改文件日志原始记录。
- `tests/unit/test_glm_flash_lite.py`：契约/风险/切换/警告/真实Qt控件回归。
- `scripts/smoke_glm_flash_lite.py`：单次真实API验收及已有记录重渲染，支持从任意目录运行；不写正式pending、不下单、不推送、不改配置。

## 最终自动测试

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest -o addopts='' tests/unit/test_glm_flash_lite.py tests/unit/test_provider_fallback_chain.py tests/unit/test_json_validator.py tests/unit/test_relaxed_validation.py tests/unit/test_json_salvage.py tests/unit/test_rate_limit_console_suppression.py tests/unit/test_validation_retry.py tests/unit/test_decision_panel.py tests/unit/test_trade_metrics_validation.py tests/integration/test_two_stage_happy_path.py tests/integration/test_gate_shortcircuit.py -q
```

结果：**109 passed in 3.09s**。

额外扩大测试发现 `tests/integration/test_two_stage_no_order_with_prices.py::test_no_order_with_non_null_price_fails_stage2` 失败：该旧测试要求“不下单但带entry_price=0”报错，而本次未修改的 `stage2_normalizer.py` 已会将价格清空为None。直接调用未修改的 normalize_stage2 已复现这一行为。未改旧测试、未为迁就断言关闭归一化。此项不计入上述109项通过结果。

`git diff --check` 通过（仅有已有文件行尾LF/CRLF提示）。

## 真实接口与GUI验证

使用项目配置中的备用通道凭据，仅内存选用；未输出凭据。使用已保存的历史行情快照，不是实时行情或盈利验证。

- 输入：`records/pending/2026-10-29_09-10-51_XAUUSD_5m.json`，100根K线。
- 前两次真实请求暴露第二目标/止损目标缺失问题，完整失败记录分别保留于 `live_20261029_093633/record.json`、`live_20261029_093832/record.json`；不是抹掉失败后只报成功。
- 修正兼容和安全降级后，最终真实请求记录：`live_20261029_094010/record.json`。
- 最终运行：Stage1Started → Stage1Done → Stage2Started → Stage2Done → RecordSaved；模型请求1次，exception=null，warnings=[]，最终不下单，trace_source=local_rules。
- 生产Qt控件的决策与未来走势面板、二元决策树、路径可视化均实际绑定该真实返回并渲染。已检查截图，而非仅断言“生成了PNG”。
- 初次offscreen渲染没有自动发现中文字体；验收脚本加载Windows已有微软雅黑字体并应用项目dark.qss，没有安装新字体或依赖。路径初次缩放空白的问题已修复为本地短路径全图适配。
- 已从 `D:\tools\data` 使用脚本绝对路径重渲染同一真实记录，API请求0次，验证异目录可运行。
- Qt offscreen仍会在测试stderr输出字体目录探测/propagateSizeHints提示，不是生产模型警告；中文截图已验证正常。这些测试环境提示没有被全局屏蔽。

截图与摘要：

- `live_20261029_094010/decision_and_forecast.png`
- `live_20261029_094010/tree.png`
- `live_20261029_094010/flow.png`
- `live_20261029_094010/result.json`

## 使用及边界

重启正在运行的 PA_Agent Python 进程后加载新代码；本轮没有代替用户重启/停止其正在运行的分析进程，也未验证旧进程热更新。

复用证据、不再调用API：

```powershell
D:\cl\PA_Agent\.venv\Scripts\python.exe D:\cl\PA_Agent\scripts\smoke_glm_flash_lite.py --render-record D:\cl\PA_Agent\docs\glm_flash_simplification\live_20261029_094010\record.json
```

如需重新跑一条真实请求，将参数换为 `--live`；一次请求一个新的带时间戳目录。禁止把本验收等同于预测有效性、全市场可靠性或交易安全认证。

## 工作区保护

开始时已有的校验、备用链、配置和日志改动全部保留，没有reset、提交或改API凭据。验收末尾额外观察到工作区 `.gitignore` 改动及 `start_pa_agent.bat` 暂存删除；这两项并非本轮执行的修改，未还原或覆盖。

最初启动的Cursor委托已按用户要求取消，job状态killed，无返回报告；其停止前后文件状态一致，后续实现和验收均由主模型完成。完整委托原文及取消回执另存 `CANCELLED_DELEGATION.txt`。
