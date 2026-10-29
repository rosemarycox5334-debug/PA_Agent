# GLM 备用通道：URL 为 SenseNova 时出现 Key 输入框

## 2026-10-29 修复：运行期切换不再改写用户配置

用户反馈「我设置模型会自动变成 glm，不要乱改我的设置」。根因：主通道失败切到 glm 时，
旧实现把 `settings.provider` 整个替换成 glm 配置，而这个对象正是设置窗口读取的对象，
于是下次打开设置看到的就是 `glm-4-flash / bigmodel`，一保存就把用户配置永久改掉。

现在的做法：

- 新增 `self._active_provider`（仅本次分析有效）；切换只写它，**不再写 settings.provider**。
- `_provider()` 统一给出当前请求实际使用的 provider（有运行期切换用切换后的，否则用配置）。
- 每次 `submit()` 开头把 `_active_provider` 清空，下一次分析从用户配置重新开始。
- 思考参数、`_glm_flash_lite()` 判定、fallback 去重都改读 `_provider()`。
- `settings.json` 里的 model/base_url/api_key 完全不受失败切换影响（已加断言）。

## 先回答：调用 glm-4-flash 需要 key 吗

需要。智谱 BigModel 的 OpenAI 兼容接口要求 `Authorization: Bearer <API Key>`，不是免密钥接口。
本案 key 由用户在设置界面自行填写，**不写进源码、不写进仓库**。

## 本轮行为

1. **触发条件**：主通道 `base_url` 归一化后等于 `https://token.sensenova.cn/v1` 时，设置界面出现一行
   `GLM 备用 Key:` 输入框（密码模式），并附说明"主通道失败会自动切到 glm-4-flash"。
   换成其它地址（如 deepseek/bigmodel）该行自动隐藏——已用真实 Qt 控件验证显隐切换。
2. **保存**：写入 `settings.provider.backup_glm_api_key`，并自动把备用通道挂进 `fallbacks`：
   `model=glm-4-flash`、`base_url=https://open.bigmodel.cn/api/paas/v4`、`thinking=false`。
   清空 Key 保存时，会把这条 glm 备用从 `fallbacks` 移除。
3. **运行期兜底**：每次请求前调用 `ensure_glm_fallback()`，即使配置文件被改坏、`fallbacks` 被清空，
   只要 SenseNova + Key 在，也会重新挂上；已存在则不重复添加（幂等）。
4. **切换后走精简契约**：主通道 401/429/网络错误 → 自动切到 glm-4-flash，并进入此前实现的精简路径
   （只返回决策与走势、决策树本地绘制、不做收益风险检查、盈亏比<1 拉止盈到 1 倍）。

## 验证

- 新增 12 项测试：SenseNova 才挂、幂等、缺 Key 不挂、其它地址不挂、设置读写往返、
  界面显隐、保存后建链、清空 Key 移除链、编排器 401 后切到 glm 且走精简契约。
- **225 passed in 4.13s**（含此前全部精简/展示回归）。
- 真实 Qt 控件：`sensenova visible: True` → 改成 deepseek 后 `False`。

## 使用方式

打开设置 → Base URL 填 `https://token.sensenova.cn/v1` → 在 `GLM 备用 Key:` 里粘贴智谱 Key → 保存。
之后主通道失效会自动走 glm-4-flash，不需要再手动改配置。

（注意：主通道当前那条 SenseNova Key 本身返回 401 Forbidden，需要你先在控制台核对或换新 Key，
否则每次分析都会先失败一次再切备用，浪费约 3–5 秒。）
