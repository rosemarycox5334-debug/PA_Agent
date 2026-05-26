# PA Agent — AI K线分析辅助工具（桌面端）

**交流 QQ 群：488729337**

---

> 面向主观交易者的 **价格行为（Price Action）** AI 辅助决策工具：从 **MT5 / TradingView / A股数据源**读取 K 线，将**结构化 K 线数据与程序预计算特征**送入大模型做**两阶段分析**（市场诊断 → 交易决策），**不是**截图识图，**不连接券商、不执行下单**。

---

## 目录

- [项目简介](#项目简介)
- [工作原理](#工作原理)
- [环境要求](#环境要求)
- [安装步骤](#安装步骤)
- [启动程序](#启动程序)
- [运行测试](#运行测试)
- [目录结构](#目录结构)
- [配置文件](#配置文件)
- [参与贡献与安全](#参与贡献与安全)
- [详细使用说明](#详细使用说明)
- [图表 K 线与分析快照说明](#图表-k-线与分析快照说明)
- [常见问题](#常见问题)

---

## 项目简介

PA Agent 是一款运行在 Windows 上的桌面辅助工具，帮助交易者按 Al Brooks 风格的价格行为框架理解当前图表，并把“看图”过程结构化成可复核的决策路径与字段。

程序会：

1. 通过 **MT5 / TradingView** 拉取你选定品种、周期的 OHLCV K 线（可含当前未收盘 K，图表实时显示）
2. 本地计算 **EMA20、ATR14、K 线几何特征**（实体比、内包/外包、ii/iii、突破跟随等）
3. 将 **K 线文本表 + 特征表 + 提示词工程模块** 发给大模型（支持 DeepSeek、PackyAPI、云雾等 OpenAI 兼容接口）
4. 经 **阶段一（诊断）** 与 **阶段二（决策）** 输出结构化 JSON，并在界面上绘制入场/止损/止盈参考线

**不会**把 K 线图截图发给 AI；模型读到的是与图表一致的数值化 K 线序列（K1 为最新已收盘棒）。

### 主要功能

- 📈 **MT5 实时 K 线** + 本地蜡烛图、EMA、序号标签
- 🧠 **两阶段 AI 分析**：闸门诊断 → 策略路由 → 交易决策（限价/突破/市价或不下单）
- 📋 **逐 K 摘要**（`bar_by_bar_summary`）与信号链校验，减少“口头看涨、JSON 做空”类矛盾
- 🔄 **增量分析**：在上一轮成功记录基础上只分析新增已收盘 K 线
- 💬 **分析后追问**：刷新并冻结图表后，用**与屏幕一致的 K 线表**继续向 AI 提问
- 📚 **经验库**：按周期位置检索历史案例供阶段二参考
- 📝 **完整落盘**：Prompt、原始响应、诊断/决策 JSON、Token 用量、追问记录
- 🔒 **API Key** 本地 DPAPI 加密存储

更完整的界面说明见仓库内 `[PA_Agent使用文档.md](PA_Agent使用文档.md)`。

---

## 工作原理

```text
MT5 终端 ──拉取 K 线──► 本地缓冲 / 图表显示
                              │
                              ▼
                    提交分析（可选：等待当前 K 收盘）
                              │
         ┌────────────────────┴────────────────────┐
         ▼                                         ▼
   阶段一 · 市场诊断                          策略文件路由
   （周期/方向/闸门/逐K摘要）                  （按诊断加载 prompt）
         │                                         │
         └────────────────────┬────────────────────┘
                              ▼
                    阶段二 · 交易决策
                    （§9 信号链 / §10 风险收益 / §11 下单方式）
                              │
                              ▼
              校验 JSON ──► 图表叠加线 ──► 记录保存 ──► 可追问
```


| 环节        | 说明                                                       |
| --------- | -------------------------------------------------------- |
| 数据来源      | **MetaTrader 5**（需终端已打开并登录）；品种名须与 MT5 市场报价一致（如 `US500m`） |
| 送给 AI 的内容 | K 线表、几何特征表、阶段一诊断结果、路由后的策略提示词；阶段二另含决策树规则                  |
| 图表作用      | 供你肉眼确认；分析时图表可暂停刷新，避免与提交数据不一致                             |
| 输出        | 阶段一/二 JSON；阶段二含 `decision`、`decision_trace`、盈亏比等字段       |
| 边界        | **仅辅助分析，不连接券商下单**                                        |


---

## 环境要求


| 项目           | 要求                                     |
| ------------ | -------------------------------------- |
| 操作系统         | Windows 10 / 11                        |
| Python       | 3.11+（推荐官方安装包，安装时勾选 Add to PATH）       |
| MetaTrader 5 | **必须**安装并登录，用于获取行情 K 线                 |
| 显卡           | 无特殊要求                                  |
| 网络           | 可访问你所配置的 AI API（如 DeepSeek、PackyAPI 等） |


---

## 安装步骤

### 1. 安装 Python 3.11+

从 [python.org](https://www.python.org/downloads/) 下载并安装，勾选 **Add Python to PATH**。

```cmd
python --version
```

### 2. 安装并登录 MetaTrader 5

启动 MT5，登录券商账户，在「市场报价」中确认你要分析的品种名称（注意后缀，如 `m`）。

### 3. 克隆或下载项目

```cmd
git clone <仓库地址>
cd PA_Agent
```

### 4. 创建虚拟环境（推荐）

```cmd
python -m venv .venv
.venv\Scripts\activate
```

### 5. 安装依赖

```cmd
pip install -e ".[dev]"
```

若东财数据源被关闭：可改用 **TradingView** 拉 K 线。数据来源选 TradingView，支持 **A 股**（6 位 + SSE/SZSE）、**港股**（`HKEX` + 代码）与**股票名称**（如 `小米集团`，内置别名表；**品种框保持你输入的文字**，仅在后台按别名拉取 K 线）。交易所选 **（自动）** 时会依次探测合适市场。自定义名称可编辑 `config/tv_symbol_aliases.json`（参考 `tv_symbol_aliases.example.json`）。

> 国内镜像示例：
>
> ```cmd
> pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
> # （GUI 已移除 AkShare 选项；如需仍可自行恢复/启用相关代码）
> ```

### 6. 配置 API

复制配置模板（首次克隆建议执行）：

```cmd
copy config\settings.example.json config\settings.json
```

启动程序后打开 **设置**，填写 **Base URL**、**模型名** 与 **API Key**（支持 DeepSeek 官方或第三方兼容网关）。Key 会加密写入 `config/settings.json`，不会以明文提交到 Git。

字段说明见 `[config/README.md](config/README.md)`。

---

## 启动程序

```cmd
python -m pa_agent.main
```

或安装后：

```cmd
pa-agent
```

也可使用项目根目录的 `run.py`（若存在）。

首次启动若提示数据源未连接，请先确认 MT5 已运行并已登录。

---

## 运行测试

```cmd
pytest
```

跳过端到端 / GUI 测试：

```cmd
pytest -m "not e2e"
```

仅单元测试：

```cmd
pytest -m unit
```

仅属性测试：

```cmd
pytest -m property
```

---

## 目录结构

```
PA_Agent/
├── pa_agent/                  # 主程序包
│   ├── main.py                # 程序入口
│   ├── app_context.py         # 应用上下文
│   ├── ai/                    # Prompt 组装、路由、JSON 校验、API 客户端
│   ├── config/                # 配置模型与加载
│   ├── data/                  # 数据源（MT5 等）与 K 线刷新循环
│   ├── gui/                   # PyQt6 界面（图表、实时流、决策面板）
│   ├── orchestrator/          # 两阶段分析编排、分析后追问
│   ├── records/               # 分析记录读写
│   ├── security/              # API Key 加密（Windows DPAPI）
│   └── util/                  # 工具函数
├── prompt_engineering/        # 价格行为提示词与策略模块（.txt）
├── tests/                     # 单元 / 属性 / 集成 / e2e 测试
├── config/                    # 配置模板与说明（settings.json 本地生成，不提交）
│   ├── settings.example.json
│   └── README.md
├── .github/workflows/         # CI（Windows + pytest）
├── experience/                # 经验库案例
├── records/                   # 分析记录（pending / 归档）
├── logs/                      # 运行日志
├── assets/                    # README 等资源（如打赏二维码）
├── pyproject.toml
└── README.md
```

---

## 配置文件

配置文件位于 `config/`，首次运行自动生成，**勿将含密钥的文件提交到 Git**。


| 文件                                    | 说明                                  |
| ------------------------------------- | ----------------------------------- |
| `config/settings.json`                | 主配置（API Key 存为 `api_key_encrypted`） |
| `config/settings.example.json`        | 无密钥的模板（复制为 `settings.json`）         |
| `config/exception_state.example.json` | 异常计数状态结构参考                          |
| `config/exception_state.json`         | 运行时自动生成，不提交 Git                     |


### 防止密钥被 push 到 GitHub

1. 本机执行一次（可选）：
  ```powershell
   powershell -ExecutionPolicy Bypass -File tools\setup_git_secrets.ps1
  ```
2. 仅在 GUI「设置」或本地 `settings.json` 中配置 Key，不要写进 README / 测试用例。
3. 默认 `pytest` 不跑需真实网络的 `live` 测试。

---

## 参与贡献与安全


| 文档                                   | 说明             |
| ------------------------------------ | -------------- |
| `[CONTRIBUTING.md](CONTRIBUTING.md)` | 开发环境、测试与 PR 约定 |
| `[SECURITY.md](SECURITY.md)`         | 漏洞与密钥泄露报告方式    |
| `[LICENSE](LICENSE)`                 | MIT 许可证        |


---

## 详细使用说明

- 控制栏：**品种 / 周期 / K 线数**、**提交分析**、**增量分析**、**等待收盘**、**演示模式**
- 右侧标签：**实时**（思考流 + 追问）、**决策树**、**决策**、**原始**、**调试** 等

完整操作说明、交易倾向、策略路由表见：`[PA_Agent使用文档.md](PA_Agent使用文档.md)`

**图表为何在分析后少 1 根 K 线？** 见 `[docs/图表K线与分析快照说明.md](docs/图表K线与分析快照说明.md)`

---

## 常见问题

### Q: 启动时提示 `ModuleNotFoundError: No module named 'pa_agent'`

在项目根目录激活虚拟环境后安装：

```cmd
.venv\Scripts\activate
pip install -e ".[dev]"
```

### Q: 提示 MT5 未连接或没有 K 线

1. 确认 MT5 终端已打开且已登录
2. 品种名与 MT5「市场报价」完全一致（含 `m` 等后缀）
3. 该品种在 MT5 中可正常显示 K 线

### Q: 程序是不是把截图发给 AI？

**不是。** 提交的是 K 线 OHLCV 文本表、程序算好的特征，以及提示词；图表仅供本地查看。

### Q: 分析时图表不刷新了？

分析进行中会**暂停图表自动刷新**，避免界面与提交数据不一致。可点 **图表实时更新** 恢复；追问发送时会先刷新一次再冻结，并以该时刻图表数据追问。

### Q: API 调用失败

检查网络、Base URL、模型名与 API Key；若用代理需在系统或网关侧配置。

### Q: `config/settings.json` 损坏

删除后重启，程序会重建默认配置：

```cmd
del config\settings.json
```

### Q: 如何更新

```cmd
git pull
pip install -e ".[dev]"
```

### Q: 日志位置

`logs/` 目录下。

---

**免责声明**：本工具仅供学习与研究，不构成投资建议。交易有风险，决策后果自负。

---

本项目采用 [MIT License](LICENSE) 发布。

---

## 打赏与支持

如果你觉得这个程序对你有帮助的话，可以打赏激励作者继续优化程序，感谢你的支持和鼓励！

（作者会优先解决打赏人的问题，因为人太多了！回复不过来！）

<p align="center">
  <img src="1d935cac3a4a4575bb3e34beda997633.jpeg" alt="打赏二维码" width="420" />
</p>

