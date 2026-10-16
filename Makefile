UV ?= uv

# ===== 原有指令（直接使用当前环境的 python / pytest / ruff）=====

# 启动 GUI
run:
	python -m pa_agent.main

# 运行测试
test:
	pytest -q

# CI 门禁用例集：排除需联网的 live 与无头下挂死的 e2e（见 docs/源码分析报告.md §B.7）
test-ci:
	pytest -q -m "not live and not e2e" --timeout=120

# 代码检查（全量：当前仓库仍有历史债，见 docs/源码分析报告.md）
lint:
	ruff check . && black --check .

# CI 阻断门：真实错误规则（未定义名/重复定义/语法错误），当前全仓可通过
lint-critical:
	ruff check . --select F821,F822,F823,F811,E9,F7

# ===== 对应 uv 隔离环境版本 =====

# 自动创建隔离环境并安装依赖（含 dev 工具）
.venv: pyproject.toml
	$(UV) sync --extra dev

# 使用 uv 启动 GUI
uv-run: .venv
	$(UV) run python -m pa_agent.main

# 使用 uv 运行测试
uv-test: .venv
	$(UV) run pytest -q

# 使用 uv 代码检查
uv-lint: .venv
	$(UV) run ruff check . && $(UV) run black --check .

# 启用 pre-commit，防止 settings / 日志 / 记录被提交
setup-secrets:
	powershell -ExecutionPolicy Bypass -File tools/setup_git_secrets.ps1

.PHONY: run test test-ci lint lint-critical uv-run uv-test uv-lint setup-secrets
