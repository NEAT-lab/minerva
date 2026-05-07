import json
import sqlite3
import time

import paho.mqtt.client as mqtt
from flask import Flask, abort, jsonify, request
from flask_cors import CORS


FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5002

BROKER_HOST = "localhost"
BROKER_PORT = 1883
REGISTRATION_TOPIC = "registration/register"

DB_FILE = "things.db"


app = Flask(__name__)
CORS(app)


def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS things (
                id            TEXT PRIMARY KEY,
                td_content    TEXT NOT NULL,
                registered_at INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL
            );
        """)
        conn.commit()
    print(f"TDD initialized: {DB_FILE}")


def upsert_td(td):
    """共用：將 TD upsert 到 things 表；HTTP 與 MQTT 兩條路徑都呼叫這個"""
    if not isinstance(td, dict):
        return False
    types = td.get("@type", [])
    if isinstance(types, list) and "tm:ThingModel" in types:
        return False  # 拒絕 TM（未實例化）
    thing_id = td.get("id")
    if not thing_id:
        return False
    raw = json.dumps(td)
    if "{{" in raw:
        return False  # 拒絕含未替換 placeholder
    now = int(time.time())
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO things (id, td_content, registered_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                td_content = excluded.td_content,
                updated_at = excluded.updated_at
        """, (thing_id, raw, now, now))
        conn.commit()
    print(f"Registered updated: {thing_id}")
    return True


# ---- HTTP endpoints --------------------------------------------------

@app.post("/things")
def register_http():
    td = request.get_json(silent=True)
    if td is None:
        abort(400, "request body must be valid JSON")
    if not upsert_td(td):
        abort(400, "TD missing required 'id' field")
    return jsonify(td), 201


@app.get("/things")
def list_things():
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute("SELECT td_content FROM things").fetchall()
    return jsonify([json.loads(row[0]) for row in rows])


@app.get("/things/<path:thing_id>")
def get_thing(thing_id):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT td_content FROM things WHERE id = ?", (thing_id,)
        ).fetchone()
    if row is None:
        abort(404)
    return jsonify(json.loads(row[0]))


@app.delete("/things/<path:thing_id>")
def delete_thing(thing_id):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("DELETE FROM things WHERE id = ?", (thing_id,))
        conn.commit()
    if cur.rowcount == 0:
        abort(404)
    print(f"Deregistered: {thing_id}")
    return "", 204


# ---- MQTT subscriber -------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(REGISTRATION_TOPIC, qos=1)
        print(f"MQTT connected, subscribed to {REGISTRATION_TOPIC}")
    else:
        print(f"MQTT connect failed: reason_code={reason_code}")


def on_message(client, userdata, msg):
    try:
        td = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print(f"MQTT message JSON parse error: {e}")
        return
    if not upsert_td(td):
        print(f"忽略 MQTT 訊息（TD 缺 id）")


mqtt_client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="registration_server",
    protocol=mqtt.MQTTv5,
)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message


def connect_broker_with_retry():
    """Broker 還沒起時不要直接 crash，等到連上為止"""
    while True:
        try:
            mqtt_client.connect(BROKER_HOST, BROKER_PORT, 60)
            return
        except Exception as e:
            print(f"等 broker 中... {e}")
            time.sleep(5)


if __name__ == "__main__":
    init_db()
    connect_broker_with_retry()
    mqtt_client.loop_start()
    print(f"Registration Server (TDD) listening on {FLASK_HOST}:{FLASK_PORT}")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=True, use_reloader=False)
