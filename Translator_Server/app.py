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
# OPUS-MT (Helsinki-NLP) en→zh：~300MB、greedy decode <300ms、對短社交問候穩
# （改用前的 NLLB-200-distilled-600M 對 'Hello everyone' 等短句有 systematic
# distillation bias，會輸出網頁麵包屑短語 "您的位置:"）
MT_MODEL = os.environ.get("MT_MODEL", "Helsinki-NLP/opus-mt-en-zh")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# Thread pool：MT 推論受 GPU 限制，預設 1 worker；GPU 大可拉到 2
INFERENCE_WORKERS = int(os.environ.get("TRANSLATOR_WORKERS", "1"))
INFERENCE_TIMEOUT_SEC = 10                    # OPUS-MT 正常 < 0.5s，10s 是 safety upper
inference_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=INFERENCE_WORKERS,
    thread_name_prefix="mt",
)


# Load TD 並替換 placeholder

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(THIS_DIR, "translator.td.json"), encoding="utf-8") as f:
    td_text = f.read()
td_text = td_text.replace("{{HOST}}", ADVERTISE_HOST).replace("{{PORT}}", str(FLASK_PORT))
td = json.loads(td_text)


# Load MT model
print(f"Loading MT {MT_MODEL} on {DEVICE}...")
tokenizer = AutoTokenizer.from_pretrained(MT_MODEL)
# GPU 用 fp16 節省 VRAM（OPUS-MT en-zh ~150MB fp16；fp32 ~300MB）
model_kwargs = {"dtype": torch.float16} if DEVICE == "cuda" else {}
model = AutoModelForSeq2SeqLM.from_pretrained(MT_MODEL, **model_kwargs).to(DEVICE)
model.eval()
print(f"Model loaded.")

# OPUS-MT en→zh 輸出簡中（zh_Hans）；用 OpenCC s2twp 轉繁中字形 + 台灣常用詞
# （s2t 只轉字形：軟件→軟體不轉；s2twp 連用詞轉：軟件→軟體、打印機→印表機、鼠標→滑鼠）
opencc_tw = OpenCC("s2twp")


# Flask app
app = Flask(__name__)

# OPUS-MT 為 en→zh single direction，僅接受此語言對；NLLB 風格 source/target 仍走 contract
SUPPORTED = {("eng_Latn", "zho_Hant")}


def run_translation(text, source, target):
    """OPUS-MT en→zh + OpenCC s2twp 轉台灣繁中"""
    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True, max_length=512).to(DEVICE)
    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_length=512,
            num_beams=1,                # greedy decode 最快；OPUS-MT 短/長句皆穩
        )
    translated = tokenizer.batch_decode(output, skip_special_tokens=True)[0]

    if target == "zho_Hant":
        translated = opencc_tw.convert(translated)

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
    if (source, target) not in SUPPORTED:
        abort(400, f"only {SUPPORTED} supported (OPUS-MT en→zh single direction)")

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
        "model": MT_MODEL,
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
          f"(model={MT_MODEL}, device={DEVICE}, workers={INFERENCE_WORKERS})")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False, threaded=True)
