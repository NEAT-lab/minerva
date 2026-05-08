#!/usr/bin/env bash
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "[1/3] 安裝系統套件 (PortAudio / libsndfile)..."
sudo apt update
sudo apt install -y python3-venv portaudio19-dev libsndfile1

echo "[2/3] 建立 venv..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

echo "[3/3] 安裝 Python 套件..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo
echo "完成。啟動方式："
echo "  MQTT_BROKER_HOST=<Function_Server_IP> python app.py"
