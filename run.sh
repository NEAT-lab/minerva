#!/bin/bash

# 載入本機實際 IP（git 不追蹤的 env.sh，內含 export LAN_HOST / OLLAMA_HOST）
[ -f env.sh ] && source env.sh
: "${LAN_HOST:=localhost}"
: "${OLLAMA_HOST:=localhost}"
export LAN_HOST OLLAMA_HOST

(
cd Registration_Server
source registration_env/bin/activate
python3 app.py
) > Registration_Server/registration_server.log 2>&1 &

(
cd STT_Server
source venv/bin/activate
python3 app.py
) > STT_Server/stt_server.log 2>&1 &

(
cd Translator_Server
source venv/bin/activate
python3 app.py
) > Translator_Server/translator_server.log 2>&1 &

(
cd Ollama_Wrapper_Server
source venv/bin/activate
OLLAMA_URL=http://${OLLAMA_HOST}:11434 python3 app.py
) > Ollama_Wrapper_Server/ollama_wrapper_server.log 2>&1 &

# 等推論服務載入模型並向 TDD 註冊，FS 啟動時才能 consume 它們（首次載模型可調大）
sleep 15

(
cd Function_Server
npm start
) > Function_Server/function_server.log 2>&1 &

echo "===================================="
echo "Minerva 啟動完成"
echo "  Function Server : ${LAN_HOST}:5001  (/admin/ 管理介面)"
echo "  TDD             : :5002"
echo "  STT             : :5003"
echo "  Translator      : :5004"
echo "  Ollama Wrapper  : :5005  -> ollama ${OLLAMA_HOST}:11434"
echo "  日誌：各服務資料夾的 *.log"
echo "  Ctrl-C 停止"
echo "===================================="

wait
