#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PIDFILE=".bridge.pid"

# 杀掉旧进程
if [ -f "$PIDFILE" ]; then
    oldpid=$(cat "$PIDFILE")
    if kill -0 "$oldpid" 2>/dev/null; then
        kill "$oldpid" 2>/dev/null
        echo "⏹ 旧服务已停止 (pid=$oldpid)"
        sleep 1
    fi
    rm -f "$PIDFILE"
fi
pkill -f "kiro-cli.*acp" 2>/dev/null && echo "⏹ 残留 ACP 进程已清理" || true
sleep 1

# 启动
exec ./start.sh
