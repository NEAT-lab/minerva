#!/usr/bin/env bash
# Ollama Wrapper 安裝腳本。可用 ./setup.sh 或 source setup.sh
# 前提：Ollama daemon 已在跑（http://localhost:11434），且已 pull 需要的模型

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$HERE"

echo "[1/2] 建立 venv..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

echo "[2/2] 安裝 Python 套件..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo
if [ "${BASH_SOURCE[0]:-$0}" != "$0" ]; then
    echo "完成。venv 已啟用，可以直接："
    echo "  python app.py"
else
    echo "完成。啟動方式："
    echo "  source venv/bin/activate"
    echo "  python app.py"
fi
echo
echo "可選環境變數："
echo "  OLLAMA_URL=http://localhost:11434"
echo "  OLLAMA_WORKERS=2"
echo "  TDD_URL=http://localhost:5002"
echo "  ADVERTISE_HOST=<本機對 Function Server 的 hostname/IP>"
