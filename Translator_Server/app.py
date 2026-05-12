import concurrent.futures
import json
import os
import threading
import time

import requests
import torch
from flask import Flask, abort, jsonify, request
from opencc import OpenCC
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer


# Configuration
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5004

TDD_URL = os.environ.get("TDD_URL", "http://localhost:5002")
ADVERTISE_HOST = os.environ.get("ADVERTISE_HOST", "localhost")
NLLB_MODEL = os.environ.get("NLLB_MODEL", "facebook/nllb-200-distilled-600M")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# Thread pool：NLLB 推論受 GPU 限制，預設 1 worker；GPU 大可拉到 2
INFERENCE_WORKERS = int(os.environ.get("TRANSLATOR_WORKERS", "1"))
INFERENCE_TIMEOUT_SEC = 10                    # NLLB-200 600M 正常 < 1s，10s 是 safety upper
inference_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=INFERENCE_WORKERS,
    thread_name_prefix="nllb",
)


# Load TD 並替換 placeholder

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(THIS_DIR, "translator.td.json"), encoding="utf-8") as f:
    td_text = f.read()
td_text = td_text.replace("{{HOST}}", ADVERTISE_HOST).replace("{{PORT}}", str(FLASK_PORT))
td = json.loads(td_text)


# Load NLLB-200
print(f"Loading NLLB {NLLB_MODEL} on {DEVICE}...")
tokenizer = AutoTokenizer.from_pretrained(NLLB_MODEL)
# GPU 用 float16 節省 VRAM（NLLB 600M fp16 約 1.2GB；fp32 約 2.4GB）
model_kwargs = {"torch_dtype": torch.float16} if DEVICE == "cuda" else {}
model = AutoModelForSeq2SeqLM.from_pretrained(NLLB_MODEL, **model_kwargs).to(DEVICE)
model.eval()
print(f"Model loaded.")

# NLLB 對 zho_Hant target 偶爾仍輸出簡體字；用 OpenCC s2t 強制統一繁體
opencc_s2t = OpenCC("s2t")


# Flask app
app = Flask(__name__)


def run_translation(text, source, target):
    """NLLB 翻譯一筆；繁中 target 走 OpenCC s2t 後處理"""
    tokenizer.src_lang = source
    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True, max_length=512).to(DEVICE)

    target_token_id = tokenizer.convert_tokens_to_ids(target)
    with torch.no_grad():
        output = model.generate(
            **inputs,
            forced_bos_token_id=target_token_id,
            max_length=512,
            num_beams=1,                # greedy decode 最快；NLLB 文獻顯示 num_beams=1 vs 4 BLEU 差 < 1
        )
    translated = tokenizer.batch_decode(output, skip_special_tokens=True)[0]

    if target == "zho_Hant":
        translated = opencc_s2t.convert(translated)

    return translated


@app.post("/translate")
def translate():
    body = request.get_json(silent=True)
    if not body:
        abort(400, "request body must be valid JSON")

    text = body.get("text", "").strip()
    source = body.get("source", "").strip()
    target = body.get("target", "").strip()

    if not text or not source or not target:
        abort(400, "'text', 'source', 'target' are required")

    future = inference_pool.submit(run_translation, text, source, target)
    try:
        result = future.result(timeout=INFERENCE_TIMEOUT_SEC)
    except concurrent.futures.TimeoutError:
        abort(504, f"translation timeout (> {INFERENCE_TIMEOUT_SEC}s)")

    return jsonify({"text": result})


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "model": NLLB_MODEL,
        "device": DEVICE,
        "cuda_available": torch.cuda.is_available(),
        "workers": INFERENCE_WORKERS,
    })


# Register TD 至 TDD（背景 thread；TDD 沒起也不擋 Flask 啟動）
def register_td_with_retry():
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
    print(f"Translator Server listening on {FLASK_HOST}:{FLASK_PORT} "
          f"(model={NLLB_MODEL}, device={DEVICE}, workers={INFERENCE_WORKERS})")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False, threaded=True)
