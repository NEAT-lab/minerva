#!/bin/bash
# 停止 run.sh 啟動的所有 Minerva 服務

pkill -f "python3 app.py"   # Registration / STT / Translator / Ollama Wrapper
pkill -f "fs_node.js"       # Function Server (node)

echo "已停止所有 Minerva 服務"
