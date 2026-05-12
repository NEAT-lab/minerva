import concurrent.futures
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

# Thread pool：Ollama 內部多數情況序列化推論，但 wrapper 層接受並發 HTTP 入口
# 預設 2 workers，足夠 multi-FS 場景；Ollama 隊列會自己處理串接
INFERENCE_WORKERS = int(os.environ.get("OLLAMA_WORKERS", "1"))
INFERENCE_TIMEOUT_SEC = 5            # 一般推論 1-3s；超過 5s 視為異常 504 給 client

# 啟動時要預熱的 model（讓 Ollama backend 把 weights 載入 GPU），
# 避免 client 第一筆請求撞到 cold-start latency（首次載入可達 20s+）
WARMUP_MODEL = os.environ.get("OLLAMA_WARMUP_MODEL", "llama3:latest")
warmed_up = False                    # warmup 完成 flag；/health 會回報
inference_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=INFERENCE_WORKERS,
    thread_name_prefix="ollama",
)


# Load TD 並替換 placeholder
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(THIS_DIR, "ollama_wrapper.td.json"), encoding="utf-8") as f:
    td_text = f.read()
td_text = td_text.replace("{{HOST}}", ADVERTISE_HOST).replace("{{PORT}}", str(FLASK_PORT))
td = json.loads(td_text)


# Flask app
app = Flask(__name__)


def call_ollama(payload):
    """轉發到 Ollama backend；強制 stream=False（簡化 W3C action 語意）"""
    payload = {**payload, "stream": False}
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json=payload,
        timeout=INFERENCE_TIMEOUT_SEC,
    )
    r.raise_for_status()
    return r.json()


@app.post("/api/chat")
def chat():
    """W3C TD action endpoint：proxy 到 Ollama /api/chat"""
    body = request.get_json(silent=True)
    if not body:
        abort(400, "request body must be valid JSON")
    if "model" not in body or "messages" not in body:
        abort(400, "'model' and 'messages' are required")

    future = inference_pool.submit(call_ollama, body)
    try:
        result = future.result(timeout=INFERENCE_TIMEOUT_SEC)
    except concurrent.futures.TimeoutError:
        abort(504, f"Ollama timeout (> {INFERENCE_TIMEOUT_SEC}s)")
    except requests.exceptions.RequestException as e:
        abort(502, f"Ollama backend error: {e}")

    return jsonify(result)


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
        "workers": INFERENCE_WORKERS,
        "warmup_model": WARMUP_MODEL,
        "warmed_up": warmed_up,
    })


# Warmup：啟動時背景打一筆 dummy 請求，讓 Ollama backend 把 model 載入 GPU
# 不阻塞 Flask 啟動；第一筆真正請求進來時 model 已 warm
def warmup_model():
    global warmed_up
    print(f"Warming up '{WARMUP_MODEL}' on backend (no timeout)...")
    try:
        t0 = time.time()
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": WARMUP_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "options": {"num_predict": 1},   # 只生 1 個 token 就夠了，目的是觸發載入
            },
            timeout=120,                          # cold load 可能 20-60s，給寬鬆 timeout
        )
        if r.ok:
            warmed_up = True
            print(f"Warmup done in {time.time()-t0:.1f}s — model '{WARMUP_MODEL}' ready")
        else:
            print(f"Warmup failed: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"Warmup error: {type(e).__name__}: {e}")


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
    threading.Thread(target=warmup_model, daemon=True).start()
    print(f"Ollama Wrapper listening on {FLASK_HOST}:{FLASK_PORT}")
    print(f"  Ollama backend: {OLLAMA_URL}")
    print(f"  Warmup model:   {WARMUP_MODEL}")
    print(f"  Workers: {INFERENCE_WORKERS}, timeout: {INFERENCE_TIMEOUT_SEC}s")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False, threaded=True)
