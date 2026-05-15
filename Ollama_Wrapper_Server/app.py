import json
import os
import threading
import time

import requests
from flask import Flask, abort, jsonify, request


# Configuration
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5005

TDD_URL = os.environ.get("TDD_URL", "http://localhost:5002")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
ADVERTISE_HOST = os.environ.get("ADVERTISE_HOST", "localhost")

# Wrapper 永遠走 streaming（NDJSON pass-through）；timeout 指 Ollama headers / first-byte 等待時間，
# stream 開始後 inter-chunk gap 也用同一個值。10s 對非 thinking 模型已寬鬆。
INFERENCE_TIMEOUT_SEC = 10

# 注：Wrapper 不綁特定模型，model 名稱由 caller（Function Server）在 body.model 帶；
# 預載由 caller 啟動時自己 dummy chat 一次完成。


# Load TD 並替換 placeholder
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(THIS_DIR, "ollama_wrapper.td.json"), encoding="utf-8") as f:
    td_text = f.read()
td_text = td_text.replace("{{HOST}}", ADVERTISE_HOST).replace("{{PORT}}", str(FLASK_PORT))
td = json.loads(td_text)


# Flask app
app = Flask(__name__)


def stream_ollama(payload):
    """把 Ollama 的 NDJSON 一行一行 yield 回給 caller。強制 stream=True。"""
    payload = {**payload, "stream": True}
    with requests.post(
        f"{OLLAMA_URL}/api/chat",
        json=payload,
        stream=True,
        timeout=INFERENCE_TIMEOUT_SEC,
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=False):
            if line:
                yield line + b"\n"


@app.post("/api/chat")
def chat():
    """W3C TD action endpoint：proxy 到 Ollama /api/chat，永遠 streaming（NDJSON chunked）。"""
    body = request.get_json(silent=True)
    if not body:
        abort(400, "request body must be valid JSON")
    if "model" not in body or "messages" not in body:
        abort(400, "'model' and 'messages' are required")

    try:
        gen = stream_ollama(body)
        return app.response_class(gen, mimetype="application/x-ndjson")
    except requests.exceptions.RequestException as e:
        abort(502, f"Ollama backend error: {e}")


@app.get("/health")
def health():
    """檢查 wrapper + Ollama backend 連通性"""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        ollama_ok = r.status_code == 200
        models = [m["name"] for m in r.json().get("models", [])] if ollama_ok else []
    except Exception:
        ollama_ok = False
        models = []
    return jsonify({
        "status": "ok" if ollama_ok else "degraded",
        "ollama_backend": OLLAMA_URL,
        "ollama_reachable": ollama_ok,
        "available_models": models,
    })


# Register TD 至 TDD（背景 thread；TDD 沒起也不擋 Flask 啟動）
def register_td_with_retry():
    """startup 時 POST TD 至 TDD；失敗每 5s 重試直到成功"""
    while True:
        try:
            r = requests.post(f"{TDD_URL}/things", json=td, timeout=5)
            if r.status_code in (200, 201):
                print(f"Registered TD with TDD: {td['id']}")
                return
            print(f"TDD register failed: {r.status_code} {r.text}")
        except Exception as e:
            print(f"等 TDD 中... {e}")
        time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=register_td_with_retry, daemon=True).start()
    print(f"Ollama Wrapper listening on {FLASK_HOST}:{FLASK_PORT}")
    print(f"  Ollama backend: {OLLAMA_URL}")
    print(f"  Timeout: {INFERENCE_TIMEOUT_SEC}s (first-byte + inter-chunk)")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False, threaded=True)
