#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# 检查 .env
if [ ! -f .env ]; then
    echo "❌ 缺少 .env 文件，请复制 .env.example 并填写配置"
    exit 1
fi

# 检查 channels.json
if [ ! -f channels.json ]; then
    echo "❌ 缺少 channels.json，请复制 channels.example.json 并填写配置"
    exit 1
fi

# 检查 kiro-cli
if ! command -v kiro-cli &>/dev/null; then
    echo "❌ kiro-cli 未安装或不在 PATH 中"
    exit 1
fi

# 检查 Python 依赖
pip install -q -r requirements.txt 2>/dev/null

echo "🚀 启动 kiro-wecom-bridge ..."
exec python3 main.py
