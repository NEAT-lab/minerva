#!/usr/bin/env bash
# STT Server 安裝腳本。可用 ./setup.sh 或 source setup.sh
# （source 時 venv 會自動留在當前 shell active 狀態）
# 不用 `set -e`：sourced 時 set -e 會殺掉 caller terminal

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$HERE"

echo "[1/3] 安裝系統套件 (ffmpeg)..."
sudo apt update
sudo apt install -y python3-venv ffmpeg

echo "[2/3] 建立 venv..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

echo "[3/3] 安裝 Python 套件..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo
if [ "${BASH_SOURCE[0]:-$0}" != "$0" ]; then
    echo "完成。venv 已啟用（你 source 進來），可以直接："
    echo "  python app.py"
else
    echo "完成。啟動方式："
    echo "  source venv/bin/activate"
    echo "  python app.py"
fi
echo
echo "可選環境變數："
echo "  WHISPER_MODEL=medium   (tiny/base/small/medium/large-v3)"
echo "  TDD_URL=http://localhost:5002"
echo "  ADVERTISE_HOST=<本機對 Function Server 的 hostname/IP>"
