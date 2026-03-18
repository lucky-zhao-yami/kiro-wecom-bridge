#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# 杀掉旧进程（排除自己）
pids=$(pgrep -f "python3.*main\.py" 2>/dev/null | grep -v $$ || true)
if [ -n "$pids" ]; then
    echo "$pids" | xargs kill 2>/dev/null
    echo "⏹ 旧服务已停止"
fi
pkill -f "kiro-cli.*acp" 2>/dev/null && echo "⏹ 残留 ACP 进程已清理" || true
sleep 1

# 启动
exec ./start.sh
