import concurrent.futures
import json
import os
import tempfile
import threading
import time

import requests
import torch
import whisper
from flask import Flask, abort, jsonify, request


# ---- Configuration ----

FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5003

TDD_URL = os.environ.get("TDD_URL", "http://localhost:5002")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")    # tiny / base / small / medium / large-v3
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# 對外 register 用 advertise host：0.0.0.0 對 broker / FS 沒意義，改用 hostname
ADVERTISE_HOST = os.environ.get("ADVERTISE_HOST", "localhost")

# Thread pool：限制 GPU 上同時 in-flight 的推論數，避免 OOM
# 預設 1 worker：同卡通常還跑 Ollama（Llama 3 ~5-8GB）+ Translator（~2GB），
# Whisper 多並行容易撞 OOM；GPU 餘裕大時可 STT_WORKERS=2 或更高
INFERENCE_WORKERS = int(os.environ.get("STT_WORKERS", "1"))
INFERENCE_TIMEOUT_SEC = 5                                    # 單次推論最久等 5s；正常 GPU 推論 1-3s 完成，超過視為異常 504
inference_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=INFERENCE_WORKERS,
    thread_name_prefix="whisper",
)


# Load TD 並替換 placeholder
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(THIS_DIR, "stt.td.json"), encoding="utf-8") as f:
    td_text = f.read()
td_text = td_text.replace("{{HOST}}", ADVERTISE_HOST).replace("{{PORT}}", str(FLASK_PORT))
td = json.loads(td_text)


# Load Whisper 模型
print(f"Loading Whisper {WHISPER_MODEL} on {DEVICE}...")
model = whisper.load_model(WHISPER_MODEL, device=DEVICE)
print(f"Model loaded.")


# Flask app
app = Flask(__name__)


# Whisper 在弱訊號 / 噪音上的常見幻覺（YouTube 字幕訓練殘影）；命中即視為空字串
HALLUCINATIONS = {
    "you", "bye", "goodbye", ".", "thank you", "thanks", "thanks.",
    "thank you for watching", "thanks for watching", "thanks for watching!",
    "subtitles by the amara.org community", "subscribe", "please subscribe",
    "see you", "see you next time", "see you in the next video",
    "i'll see you in the next video", "i'll see you next time",
    "bye-bye", "bye!", "music", "[music]", "(music)", "♪",
}

# 預期語言：Whisper 對非語音 audio 常 fallback 到低資源語言（nn / cy / mt 等冷門語言）
# 偵測到非預期語言 → 強烈 signal 為 false positive，直接判為非語音
EXPECTED_LANGUAGES = {"en", "zh", "ja", "ko", "es", "fr", "de"}


def is_hallucination(text):
    """判斷 transcript 是否落入已知 Whisper 幻覺 pattern"""
    normalized = text.strip().lower().rstrip(".,!?").strip()
    return normalized in HALLUCINATIONS or len(normalized) <= 1


def is_low_quality(result):
    """進一步信心檢查：no_speech_prob 平均值過高 = 低信心；視為非語音"""
    segments = result.get("segments", [])
    if not segments:
        return False
    avg_no_speech = sum(s.get("no_speech_prob", 0) for s in segments) / len(segments)
    return avg_no_speech > 0.5


def run_inference(audio_bytes, language):
    """實際 Whisper 推論 + 三層過濾；於 thread pool worker 中執行"""
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as f:
        f.write(audio_bytes)
        f.flush()
        # 反幻覺參數：降低 "Thanks for watching" / "Thank you" 等 YouTube 訓練資料殘影
        kwargs = {
            "temperature": 0.0,                  # deterministic decode
            "no_speech_threshold": 0.7,          # 提高無聲判定門檻（default 0.6）
            "logprob_threshold": -0.5,           # 提高低信心 segment 過濾門檻（default -1.0）
            "compression_ratio_threshold": 2.0,  # 降低（catch repeated text earlier; default 2.4）
            "condition_on_previous_text": False, # 不靠前文 context，避免幻覺擴散
        }
        if language:
            kwargs["language"] = language
        result = model.transcribe(f.name, **kwargs)

    text = result["text"].strip()
    detected_lang = result.get("language", language or "unknown")

    # Layer 1：偵測到非預期語言 -> 通常是 Whisper 對 noise audio 的 fallback
    if detected_lang not in EXPECTED_LANGUAGES:
        print(f"Non-target language '{detected_lang}': dropped (text='{text[:60]}')")
        text = ""
    # Layer 2：transcript 命中已知幻覺
    elif is_hallucination(text):
        print(f"Hallucination filtered: '{text}'")
        text = ""
    # Layer 3：Whisper 自評信心低（no_speech_prob 平均過高）
    elif is_low_quality(result):
        avg = sum(s.get("no_speech_prob", 0) for s in result.get("segments", [])) / max(1, len(result.get("segments", [])))
        print(f"Low confidence (avg no_speech_prob={avg:.2f}): dropped (text='{text[:60]}')")
        text = ""

    return {"text": text, "language": detected_lang}


@app.post("/transcribe")
def transcribe():
    """接收裸 OggOpus binary，提交至 thread pool，回傳 {text, language}"""
    audio_bytes = request.get_data()
    if not audio_bytes:
        abort(400, "empty request body")
    language = request.args.get("language")  # 可選

    future = inference_pool.submit(run_inference, audio_bytes, language)
    try:
        result = future.result(timeout=INFERENCE_TIMEOUT_SEC)
    except concurrent.futures.TimeoutError:
        abort(504, f"inference timeout (> {INFERENCE_TIMEOUT_SEC}s)")
    return jsonify(result)


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "model": WHISPER_MODEL,
        "device": DEVICE,
        "cuda_available": torch.cuda.is_available(),
        "workers": INFERENCE_WORKERS,
    })


# Register TD 至 TDD（背景 thread；TDD 沒起也不擋 Flask 啟動）
def register_td_with_retry():
    """startup 時 POST TD 至 TDD；失敗每 5s 重試，直到成功"""
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
    print(f"STT Server listening on {FLASK_HOST}:{FLASK_PORT} "
          f"(model={WHISPER_MODEL}, device={DEVICE}, workers={INFERENCE_WORKERS})")
    # threaded=True：Flask 每個 HTTP 請求獨立 thread，配 inference_pool 達成
    # 「HTTP 不阻塞 + GPU 推論受 pool 限流」
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False, threaded=True)
