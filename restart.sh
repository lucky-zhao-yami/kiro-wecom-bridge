#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "⏹ 停止旧服务 ..."

# 按端口杀 main.py
port_pid=$(lsof -ti :8900 2>/dev/null || true)
[ -n "$port_pid" ] && echo "$port_pid" | xargs kill 2>/dev/null && echo "  killed main (pid=$port_pid)"

# 杀残留 kiro-cli acp 子进程（包括 kiro-cli-chat acp）
for p in $(pgrep -f "kiro-cli.*acp" 2>/dev/null || true); do
    kill "$p" 2>/dev/null && echo "  killed acp (pid=$p)"
done

# 杀 memory_server
for p in $(pgrep -f "memory_server.py" 2>/dev/null || true); do
    kill "$p" 2>/dev/null && echo "  killed memory_server (pid=$p)"
done

rm -f .bridge.pid
sleep 2

echo "🚀 启动 ..."
nohup ./start.sh >> /tmp/wecom-bridge.log 2>&1 &
echo "✅ 已启动 pid=$!"
