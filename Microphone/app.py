import io
import json
import os
import queue
import sys
import threading
import time
import uuid
from collections import deque

import numpy as np
import paho.mqtt.client as mqtt
import paho.mqtt.properties as props
import paho.mqtt.packettypes as packettypes
import soundfile as sf
import sounddevice as sd
import webrtcvad


# Configuration
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ID_FILE = os.path.join(THIS_DIR, ".node_id")

def load_or_create_node_id():
    """確保 NODE_ID 跨重啟穩定：env 最優先，否則讀/寫 .node_id 檔"""
    env_id = os.environ.get("MIC_NODE_ID")
    if env_id:
        return env_id
    if os.path.exists(ID_FILE):
        with open(ID_FILE) as f:
            value = f.read().strip()
            if value:
                return value
    new_id = str(uuid.uuid4())
    with open(ID_FILE, "w") as f:
        f.write(new_id)
    return new_id

NODE_ID = load_or_create_node_id()
BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "localhost")
BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))

REGISTRATION_TOPIC = "registration/register"
PRESENCE_TOPIC = f"presence/{NODE_ID}"
AUDIO_TOPIC = f"things/{NODE_ID}/events/audio"

# Audio pipeline 固定參數（採樣率對齊 Whisper / WebRTC VAD spec，不應改動）
CAPTURE_RATE = 48000                # USB mic 硬體常見支援率
EMIT_RATE = 16000                   # WebRTC VAD 與 Whisper 標準輸入率
DOWNSAMPLE_FACTOR = CAPTURE_RATE // EMIT_RATE  # 3
BLOCK_SAMPLES_EMIT = 480            # 30ms @ 16kHz（VAD frame size）
BLOCK_SAMPLES_CAPTURE = BLOCK_SAMPLES_EMIT * DOWNSAMPLE_FACTOR  # 1440 samples @ 48kHz

# VAD / 能量參數預設值（適用一般辦公室 / 會議室；極端環境用 calibrate.py 校準後 override）
VAD_MODE = 2                        # 0-3，2 為一般 voice IoT 預設（Mycroft 等）
START_CONFIRM_FRAMES = 6            # 連續 N frame 為 speech 才確認 utterance start（6 = 180ms）
                                    # 第一層防禦：短於 180ms 的瞬間（敲擊、椅子聲、輕觸）連 VAD 都不觸發
MIN_SILENCE_MS = 800                # 連續靜音 N ms 後判定 utterance 結束
RMS_GATE = 800                      # 啟動門檻：未 recording 時 RMS < 此值不送 VAD（防 false start）
SILENCE_FLOOR = 700                 # recording 中 RMS < 此值強制計入 silence（環境噪音持續時也能正確 end）
MIN_UTTERANCE_MS = 300              # 第二層防禦：< 300ms 必為瞬間雜音直接丟；保留「好/OK/對」等短回應
                                    # 真噪音 (≥ 300ms) 交給 STT 的反幻覺過濾擋掉
MAX_UTTERANCE_MS = 60000            # 單 utterance 上限：Opus @ 24kbps × 60s ≈ 180KB，安全低於 1MB MQTT cap

# 若有 calibration.json（calibrate.py 產出）則覆蓋上方門檻參數
CALIBRATION_PATH = os.path.join(THIS_DIR, "calibration.json")
if os.path.exists(CALIBRATION_PATH):
    with open(CALIBRATION_PATH) as _f:
        _calib = json.load(_f)
    for _k in ("VAD_MODE", "START_CONFIRM_FRAMES", "MIN_SILENCE_MS",
               "RMS_GATE", "SILENCE_FLOOR", "MIN_UTTERANCE_MS"):
        if _k in _calib:
            globals()[_k] = _calib[_k]
    print(f"Loaded calibration: VAD_MODE={VAD_MODE} RMS_GATE={RMS_GATE} "
          f"SILENCE_FLOOR={SILENCE_FLOOR} MIN_SILENCE_MS={MIN_SILENCE_MS}")

MAX_UTTERANCE_CHUNKS = (MAX_UTTERANCE_MS * EMIT_RATE // 1000) // BLOCK_SAMPLES_EMIT
PRE_ROLL_FRAMES = START_CONFIRM_FRAMES + 2  # 多留 2 frame 餘裕，cover 軟開頭

# Load TM 並實例化為 TD
sys.path.insert(0, THIS_DIR)
from instantiate import instantiate

td = instantiate(
    os.path.join(THIS_DIR, "microphone.tm.json"),
    NODE_ID=NODE_ID,
    MQTT_BROKER=f"{BROKER_HOST}:{BROKER_PORT}",
)


# MQTT setup
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print(f"MQTT connected as {NODE_ID}")
        # 自己 publish "online"（retained），讓 FS 能立刻看到
        client.publish(PRESENCE_TOPIC, "online", qos=1, retain=True)
        # 透過 MQTT 註冊 TD（一次性）
        client.publish(REGISTRATION_TOPIC, json.dumps(td), qos=1)
        print(f"Registered {NODE_ID} via {REGISTRATION_TOPIC}")
    else:
        print(f"MQTT connect failed: reason_code={reason_code}")

def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    print(f"MQTT disconnected: reason_code={reason_code}, flags={disconnect_flags}")

mqtt_client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id=NODE_ID,
    protocol=mqtt.MQTTv5,
)
mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
# LWT：crash 或網路線斷連時 broker 自動把 presence 設空，Function Server 立刻知道
mqtt_client.will_set(PRESENCE_TOPIC, payload="", qos=1, retain=True)

def connect_broker_with_retry():
    while True:
        try:
            mqtt_client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            return
        except Exception as e:
            print(f"等 broker 中... {e}")
            time.sleep(5)


# WebRTC VAD（輕量、無 ML runtime 依賴；ARMv8.0 RPi 也能跑）
class WebRtcVadIterator:
    """webrtcvad wrapper：回傳 {'start': sample_idx} / {'end': sample_idx} / None"""
    def __init__(self, sampling_rate, frame_samples,
                 mode, start_confirm_frames, min_silence_ms):
        self.vad = webrtcvad.Vad(mode)
        self.sampling_rate = sampling_rate
        self.frame_samples = frame_samples
        frame_ms = frame_samples * 1000 // sampling_rate
        self.start_confirm_frames = start_confirm_frames
        self.min_silence_frames = min_silence_ms // frame_ms
        self.reset_states()

    def reset_states(self):
        self.triggered = False
        self.speech_frames = 0
        self.silence_frames = 0
        self.current_sample = 0

    def __call__(self, chunk_int16):
        self.current_sample += len(chunk_int16)
        is_speech = self.vad.is_speech(chunk_int16.tobytes(), self.sampling_rate)

        if not self.triggered:
            if is_speech:
                self.speech_frames += 1
                if self.speech_frames >= self.start_confirm_frames:
                    self.triggered = True
                    start = self.current_sample - self.speech_frames * self.frame_samples
                    self.silence_frames = 0
                    return {"start": start}
            else:
                self.speech_frames = 0
            return None

        # triggered = True
        if is_speech:
            self.silence_frames = 0
        else:
            self.silence_frames += 1
            if self.silence_frames >= self.min_silence_frames:
                end = self.current_sample - self.silence_frames * self.frame_samples
                self.triggered = False
                self.speech_frames = 0
                self.silence_frames = 0
                return {"end": end}
        return None

vad_iter = WebRtcVadIterator(
    sampling_rate=EMIT_RATE,
    frame_samples=BLOCK_SAMPLES_EMIT,
    mode=VAD_MODE,
    start_confirm_frames=START_CONFIRM_FRAMES,
    min_silence_ms=MIN_SILENCE_MS,
)

def find_input_device():
    """挑第一個有 input channel 的裝置；RPi 預設常指向沒 input 的內建音效"""
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            return i
    return None

# Utterance state
recording = False
utterance_start_ts = 0
recording_chunk_count = 0

# Pre-roll buffer：保留 utterance 觸發前的近期 audio，避免 START_CONFIRM_FRAMES 期間的開頭字被吃掉
pre_roll_buffer = deque(maxlen=PRE_ROLL_FRAMES)

# Streaming encoder：audio callback 不直接編碼（避免 utterance 越長收尾越久），改投遞 event 到 worker。
# Worker 維持一個常駐 OggOpus encoder，每個 chunk 即進即編，end 時 close → publish。
# 讓編碼的延遲不隨 utterance 長度線性增長，固定 ~5ms。
encode_queue = queue.Queue()

def encoder_worker():
    """背景 thread：streaming OggOpus encoder。
    收到 ('start', ts) 開新 encoder；('chunk', np_int16) 傳入編碼；('end', None) 收尾並 publish"""
    sf_file = None
    buf = None
    start_ts = None
    samples_written = 0

    while True:
        item = encode_queue.get()
        if item is None:
            return
        kind, value = item

        if kind == "start":
            buf = io.BytesIO()
            sf_file = sf.SoundFile(
                buf, mode="w", samplerate=EMIT_RATE, channels=1,
                format="OGG", subtype="OPUS",
            )
            start_ts = value
            samples_written = 0

        elif kind == "chunk":
            if sf_file is not None:
                sf_file.write(value)
                samples_written += len(value)

        elif kind == "end":
            if sf_file is not None:
                sf_file.close()                  # 寫 OGG end-of-stream marker
                opus_bytes = buf.getvalue()
                duration_ms = int(samples_written * 1000 / EMIT_RATE)

                if duration_ms < MIN_UTTERANCE_MS:
                    # 過短：通常是噪音瞬間誤觸發；丟棄可降低下游 STT 幻覺
                    print(f"Utterance dropped (too short): {duration_ms} ms")
                else:
                    p = props.Properties(packettypes.PacketTypes.PUBLISH)
                    p.ContentType = "audio/ogg; codecs=opus"
                    p.UserProperty = [("ts", str(start_ts)), ("duration_ms", str(duration_ms))]
                    mqtt_client.publish(AUDIO_TOPIC, opus_bytes, qos=1, properties=p)
                    print(f"Utterance emitted: {duration_ms} ms, {len(opus_bytes)} bytes opus")

                sf_file = None
                buf = None
                start_ts = None
                samples_written = 0

def on_audio_chunk(indata, frames, time_info, status):
    """sounddevice callback：每 1440 samples (30ms @48kHz) 執行一次；只做輕量工作"""
    global recording, utterance_start_ts, recording_chunk_count

    if status:
        print(f"sounddevice status: {status}")

    chunk_float_48k = indata[:, 0]                                    # mono float32 @ 48kHz
    chunk_int16_48k = (chunk_float_48k * 32767).astype(np.int16)
    chunk_int16_16k = chunk_int16_48k[::DOWNSAMPLE_FACTOR]            # stride-3 降到 16kHz

    # 持續維護 pre-roll ring buffer：start 觸發時用來補回 confirm 期間被吃掉的開頭 frame
    pre_roll_buffer.append(chunk_int16_16k)

    # 兩階段能量門檻：
    #   未 recording → RMS_GATE 防 false start（背景噪音不觸發 utterance）
    #   recording 中 → SILENCE_FLOOR 確保「真安靜」時能正確 end（避免持續低噪讓 VAD 永遠不 silence）
    #   兩值之間的曖昧帶交給 WebRTC VAD 自行判斷，不誤切句中軟母音
    rms = int(np.sqrt(np.mean(chunk_int16_16k.astype(np.int32) ** 2)))
    threshold = SILENCE_FLOOR if recording else RMS_GATE
    if rms < threshold:
        speech_event = vad_iter(np.zeros_like(chunk_int16_16k))
    else:
        speech_event = vad_iter(chunk_int16_16k)

    if speech_event and "start" in speech_event:
        recording = True
        utterance_start_ts = int(time.time() * 1000)
        print(f"Speech detected, recording... (rms={rms})")
        encode_queue.put(("start", utterance_start_ts))
        # 倒帶：把 pre-roll buffer（含當前 chunk）依序餵進 encoder，補回 confirm 期間的開頭
        for c in pre_roll_buffer:
            encode_queue.put(("chunk", c))
        recording_chunk_count = len(pre_roll_buffer)
    elif recording:                                                    # 用 elif：剛 start 時 pre-roll 已含當前 chunk，不重複餵
        encode_queue.put(("chunk", chunk_int16_16k))
        recording_chunk_count += 1
        # 長獨白超過 hard cap：強制收尾並開新 utterance，保證單筆 payload 有上限
        if recording_chunk_count >= MAX_UTTERANCE_CHUNKS:
            encode_queue.put(("end", None))
            utterance_start_ts = int(time.time() * 1000)
            recording_chunk_count = 0
            encode_queue.put(("start", utterance_start_ts))

    if speech_event and "end" in speech_event:
        encode_queue.put(("end", None))
        recording = False
        recording_chunk_count = 0


if __name__ == "__main__":
    connect_broker_with_retry()
    mqtt_client.loop_start()
    threading.Thread(target=encoder_worker, daemon=True).start()

    input_device = find_input_device()
    print(f"Microphone Servient {NODE_ID} listening (device={input_device})...")
    print(f"Audio events → {AUDIO_TOPIC}")

    try:
        with sd.InputStream(
            device=input_device,
            samplerate=CAPTURE_RATE,
            channels=1,
            blocksize=BLOCK_SAMPLES_CAPTURE,
            dtype="float32",
            callback=on_audio_chunk,
        ):
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"Audio device error: {e}")
        sys.exit(1)
    finally:
        # 主動 publish 空字串 retained，讓 Function Server 立刻知道我們離線
        # （不必等 broker keepalive timeout 才發 LWT）
        mqtt_client.publish(PRESENCE_TOPIC, "", qos=1, retain=True)
        time.sleep(0.5)             # 等 publish flush
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        print("Bye.")
