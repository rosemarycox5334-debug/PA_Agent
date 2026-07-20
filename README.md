# PA Agent — AI K线分析辅助工具（桌面端）

**交流 QQ 群：975328619**

---

面向主观交易者的 **价格行为（Price Action）** AI 辅助决策工具。从 **MT5 / TradingView / yfinance / AkShare** 读取 K 线，将结构化 K 线数据与预计算特征送入大模型做**两阶段分析**（市场诊断 → 交易决策），**不是**截图识图，**不连接券商、不执行下单**。

---

## 主要功能

- 📈 **多数据源**：MT5（Windows）、TradingView（全平台）、yfinance（期货/加密货币）、AkShare（A 股）
- 🧠 **两阶段 AI 分析**：市场诊断 → 策略路由 → 交易决策（限价/突破/市价或不下单）
- 🔄 **增量分析与持续跟踪**：新增 K 线时复用上次结论；开启 `keep_analysis` 后新 K 线收盘自动触发新一轮分析
- 🌳 **决策树可视化**：赛博科幻风格可交互流程图，自动播放闸门→策略路径动画
- 🔮 **未来走势预期**：AI 预测下一根 K 线方向和下一个市场周期位置
- 💬 **分析后自由追问**：完整对话会话管理器，实时推理流 + Token 进度条，对话历史持久化
- 📚 **经验库**：按周期位置检索历史案例供分析参考
- 📝 **完整落盘**：Prompt、原始响应、诊断/决策 JSON、Token 用量、追问记录
- 🛡️ **可配置校验体系**：JSON 校验、一致性检查、语义校验、截断修复、失败自动重试
- 🔒 **API Key** 本地加密存储

---

## 环境要求

| 项目     | 要求                                                                    |
| -------- | ----------------------------------------------------------------------- |
| 操作系统 | Windows 10 / 11（主支持）、macOS 12+（TradingView 数据源）              |
| Python   | 3.11+                                                                    |
| 数据源   | MT5 / TradingView / yfinance / AkShare **至少配置一种**                  |
| 网络     | 可访问所配置的 AI API（如 DeepSeek、PackyAPI 等）                        |

---

## 快速开始

直接在系统中安装（推荐部署在本机）：

```cmd
pip install -e .
python -m pa_agent.main
```

首次启动后在**设置**中填写 **Base URL**、**模型名** 与 **API Key**。

> 如需隔离环境也可创建虚拟环境：`python -m venv .venv` 后激活再 `pip install -e .`。

**安装内容**：PyQt6（GUI 框架）+ pyqtgraph（K 线图表绘图）+ numpy/pandas（数据处理）+ openai（AI API 客户端）+ **akshare/baostock（A 股数据源）** + json 校验、模型定义等全套依赖。

> 若需运行测试（pytest）或代码格式化（ruff/black），额外安装：`pip install -e ".[dev]"`。

### uv 隔离环境（可选）

项目也支持使用 [uv](https://docs.astral.sh/uv/) 进行环境隔离，依赖版本通过 `uv.lock` 锁定，保证可复现安装，且不会污染系统 Python。

```cmd
# 1. 安装 uv（仅需一次）
pip install uv
# 或官方脚本：curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 首次运行或依赖变更时，make 自动创建 .venv 并同步依赖
make uv-run

# 3. 之后每次启动
make uv-run
# 或手动：uv run python -m pa_agent.main
```

> 运行测试：`make uv-test`，代码检查：`make uv-lint`。

---

## 详细说明

完整操作界面说明见 [`PA_Agent使用文档.md`](PA_Agent使用文档.md)，配置字段说明见 [`config/README.md`](config/README.md)。

---

**免责声明**：本工具仅供学习与研究，不构成投资建议。交易有风险，决策后果自负。

本项目采用 [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE) 发布。

---

## 群友反馈榜单

感谢群友的使用反馈与鼓励，以下为群友评价截图（按时间从早到晚排列）：

<p align="center">
  <img src="qunyou/BD58CB2D6E4F45CC17CF832C506A982C.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/653EC872A0D6883A34B7B37B692C8B1D.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260619-205140.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260619-235505.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260620-150714.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260620-150833.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260620-220824.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260623-125929.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/91003065F07407E92B50964AE7F8A944.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260624-191001.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260628-014043.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260628-213700.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260629-163821.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260701-212522.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/BB4AE8110A7011426BD29D5CE8B5F73B.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/F383D366F2254692418DB18AAA617ACE.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/AD48DF6289CB6A9D51FE0B8EE2EC38C2.jpg" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/F61C8DCDB67924B64B33403D20047E0B.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ_1783089951396.png" alt="群友反馈" width="480" />
</p>

---

## 打赏与支持

如果你觉得这个程序对你有帮助的话，可以打赏激励作者继续优化程序，感谢你的支持和鼓励！

（作者会优先解决打赏人的问题，因为人太多了！回复不过来！）

<p align="center">
  <img src="赞助码.jpeg" alt="打赏二维码" width="420" />
</p>
