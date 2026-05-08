import io
import json
import os
import queue
import sys
import threading
import time
import uuid

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

CAPTURE_RATE = 48000                # USB mic 硬體限制（大多只支援 44.1k/48k）
EMIT_RATE = 16000                   # 對外發送的取樣率：VAD 與 Whisper STT 都吃 16kHz；亦控制 payload 大小
DOWNSAMPLE_FACTOR = CAPTURE_RATE // EMIT_RATE  # 3，48k stride-3 = 16k
BLOCK_SAMPLES_EMIT = 480            # 30ms @ 16kHz（VAD frame 大小）
BLOCK_SAMPLES_CAPTURE = BLOCK_SAMPLES_EMIT * DOWNSAMPLE_FACTOR  # 1440 samples @ 48kHz
VAD_MODE = 3                        # 0-3，越高越積極過濾非語音；3 = 只認確定的 speech，避免被環境噪音觸發
START_CONFIRM_FRAMES = 5            # 連續 N frame 為 speech 才確認 utterance 開始（5 = 150ms，抗短促噪音如鍵盤聲）
MIN_SILENCE_MS = 900                # 連續靜音多久算 utterance 結束
RMS_GATE = 500                      # int16 RMS 門檻：低於此值的 chunk 直接判為靜音（避免像風扇/冷氣等持續性低噪）
MAX_UTTERANCE_MS = 60000            # 發言長度 60s，Opus @ 24kbps × 30s ≈ 90KB，遠低於 1MB broker cap
MAX_UTTERANCE_CHUNKS = (MAX_UTTERANCE_MS * EMIT_RATE // 1000) // BLOCK_SAMPLES_EMIT

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
    chunk_int16_16k = chunk_int16_48k[::DOWNSAMPLE_FACTOR]            # stride-3 降到 16kHz（VAD + emit 共用）

    # 能量門檻預過濾：低於 RMS_GATE 的 chunk 視為靜音不送 VAD（filter 持續性低噪）
    rms = int(np.sqrt(np.mean(chunk_int16_16k.astype(np.int32) ** 2)))
    if rms < RMS_GATE:
        speech_event = vad_iter(np.zeros_like(chunk_int16_16k))       # 餵零讓 VAD 計算 silence_frames
    else:
        speech_event = vad_iter(chunk_int16_16k)

    if speech_event and "start" in speech_event:
        recording = True
        utterance_start_ts = int(time.time() * 1000)
        recording_chunk_count = 0
        encode_queue.put(("start", utterance_start_ts))

    if recording:
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
