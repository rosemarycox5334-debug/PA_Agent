UV ?= uv

# ===== 原有指令（直接使用当前环境的 python / pytest / ruff）=====

# 启动 GUI
run:
	python -m pa_agent.main

# 运行测试
test:
	pytest -q

# 代码检查
lint:
	ruff check . && black --check .

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

.PHONY: run test lint uv-run uv-test uv-lint setup-secrets
