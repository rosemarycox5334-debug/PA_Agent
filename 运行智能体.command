#!/bin/bash
cd "$(dirname "$0")"
./.venv/bin/python run.py
if [ $? -ne 0 ]; then
    echo ""
    echo "[错误] 程序异常退出，请查看上方错误信息。"
    echo "按回车键关闭窗口..."
    read
fi
