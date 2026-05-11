"""端到端 smoke test：訂閱 broker 的 Mic audio events，逐筆送給 STT，印結果。

Usage:
    python test_live.py [--broker HOST] [--stt URL] [--lang en]

需要 STT Server 已啟動（python app.py）。
"""
import argparse
import time

import paho.mqtt.client as mqtt
import requests


parser = argparse.ArgumentParser()
parser.add_argument("--broker", default="140.116.245.220")
parser.add_argument("--port", type=int, default=1883)
parser.add_argument("--stt", default="http://localhost:5003/transcribe")
parser.add_argument("--lang", default=None, help="ISO 639-1 hint, e.g. en / zh")
args = parser.parse_args()


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe("things/+/events/audio", qos=1)
        print(f"[ready] 訂閱中... 對著 Mic 講話")
    else:
        print(f"[error] connect failed: rc={reason_code}")


def on_message(client, userdata, msg):
    received_ts = time.time()
    size_kb = len(msg.payload) / 1024
    node_id = msg.topic.split("/")[1]
    print(f"\n[recv] {node_id[:8]} {size_kb:.1f}KB", end=" ", flush=True)

    url = args.stt + (f"?language={args.lang}" if args.lang else "")
    t0 = time.time()
    try:
        r = requests.post(
            url, data=msg.payload,
            headers={"Content-Type": "audio/ogg; codecs=opus"},
            timeout=60,
        )
        elapsed = time.time() - t0
        if r.status_code == 200:
            data = r.json()
            print(f"({elapsed:.2f}s, {data.get('language')})")
            print(f"       → {data['text']}")
        else:
            print(f"\n[error] STT {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"\n[error] {type(e).__name__}: {e}")


client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="stt_live_test",
    protocol=mqtt.MQTTv5,
)
client.on_connect = on_connect
client.on_message = on_message
client.connect(args.broker, args.port, keepalive=60)
client.loop_forever()
