#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PIDFILE=".bridge.pid"

# 杀旧进程：先查 pid 文件，再查端口兜底
if [ -f "$PIDFILE" ]; then
    oldpid=$(cat "$PIDFILE")
    kill "$oldpid" 2>/dev/null && echo "⏹ 旧服务已停止 (pid=$oldpid)"
    rm -f "$PIDFILE"
fi
# 兜底：按端口杀
port_pid=$(lsof -ti :8900 2>/dev/null || true)
if [ -n "$port_pid" ]; then
    echo "$port_pid" | xargs kill 2>/dev/null
    echo "⏹ 端口 8900 占用进程已停止"
fi
pkill -f "kiro-cli.*acp" 2>/dev/null && echo "⏹ 残留 ACP 进程已清理" || true
sleep 2

exec ./start.sh
