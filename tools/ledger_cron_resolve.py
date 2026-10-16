#!/usr/bin/env python3
"""定时任务入口：结算到期预测 + 打印记分板（零 LLM 成本，只用公共行情）。

给 cron 的 no_agent 脚本模式用：stdout 会被原样投递。
只有真正的结论行会输出（INFO 日志被过滤掉），因此投递内容很短。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "tools" / "trading_ledger.py"
_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,\d]*\s+(INFO|WARNING|DEBUG|ERROR)\b")


def _python() -> str:
    candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.exists() else sys.executable


def run(*args: str) -> str:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [_python(), str(LEDGER), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def digest(raw: str) -> str:
    return "\n".join(line for line in raw.splitlines() if line.strip() and not _LOG_LINE.match(line))


def main() -> int:
    resolved = digest(run("resolve"))
    if "已结算" not in resolved:
        # 没有到期预测：保持静默（空 stdout 不投递任何消息）
        return 0
    print(resolved)
    print(digest(run("status")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
