#!/usr/bin/env python3
"""定时任务入口：跑一次真实分析并记账（会消耗 LLM tokens）。

先结算到期预测，再记录一条新预测（真实行情 + 真实 API），最后打印记分板。
给 cron 的 no_agent 脚本模式用：stdout 会被原样投递。
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
    print(digest(run("resolve")))
    print(digest(run("record")))
    print(digest(run("status")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
