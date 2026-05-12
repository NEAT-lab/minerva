#!/usr/bin/env bash
# Translator Server 安裝腳本。可用 ./setup.sh 或 source setup.sh

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
echo "  NLLB_MODEL=facebook/nllb-200-distilled-600M  (或 1.3B / 3.3B)"
echo "  TRANSLATOR_WORKERS=1   (GPU 餘裕可調 2)"
echo "  TDD_URL=http://localhost:5002"
echo "  ADVERTISE_HOST=<本機對 Function Server 的 hostname/IP>"
echo
echo "首次跑會下載 NLLB 模型（600M ≈ 2.4GB；首次下載約 3-5 分鐘）"
