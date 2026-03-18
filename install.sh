#!/bin/bash
set -e
cd "$(dirname "$0")"

sudo apt install -y python3-full python3-pip python3-venv 2>/dev/null || true

rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

if [ "$1" = "--full" ]; then
    echo "📦 安装记忆系统依赖（sentence-transformers + sqlite-vec）..."
    pip install -r requirements-memory.txt
fi

echo "✅ 安装完成"
echo "  启动前请先: source .venv/bin/activate"
[ "$1" != "--full" ] && echo "  如需记忆功能: bash install.sh --full"
