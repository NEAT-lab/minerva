import sqlite3
import json
import time
import threading
from flask import Flask, jsonify
from flask_cors import CORS
import paho.mqtt.client as mqtt

app = Flask(__name__)
CORS(app)

FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5002

FLASK_BROKER_HOST = "127.0.0.1"
FLASK_BROKER_PORT = 1883
CLIENT_ID = "registration_server"
DB_FILE = "things.db"
OFFLINE_THRESHOLD = 30 # 裝置超過 30 秒沒動靜視為下線
CHECK_INTERVAL = 10    # 背景檢查間隔 (秒)

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.executescript('''
            CREATE TABLE IF NOT EXISTS things (
                id TEXT PRIMARY KEY, -- 設備唯一識別碼
                title TEXT NOT NULL, -- 對應 TD 頂層的 title
                status TEXT NOT NULL DEFAULT 'online', -- 設備狀態
                last_seen INTEGER, -- 最後上線時間 (Unix timestamp)
                td_content TEXT NOT NULL, -- 設備的完整 TD 內容
                created_at INTEGER, -- 創建時間
                updated_at INTEGER -- 更新時間
            );

            CREATE INDEX IF NOT EXISTS idx_status ON things(status);
            CREATE INDEX IF NOT EXISTS idx_last_seen ON things(last_seen);
        ''')
        conn.commit()
        print("Database initialized successfully.")

@app.route("/things", methods=["GET"])
def get_things():
    current_time = time.time()
    cutoff = current_time - 30 
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row # 允許使用名稱存取欄位
        cursor = conn.cursor()
        # 只需回傳 function server 最近 30 秒內上線的設備 TD
        cursor.execute("SELECT td_content FROM things WHERE last_seen >= ? AND status = 'online'", [cutoff]) 
        rows = cursor.fetchall()

        things = []
        for row in rows:
            td = json.loads(row['td_content'])
            things.append(td)
        
        return jsonify(things)

def check_offline_devices():
    """背景執行緒：定期檢查並將過期裝置標記為 offline"""
    while True:
        try:
            now = int(time.time())
            cutoff = now - OFFLINE_THRESHOLD
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                # 尋找超過 30 秒沒更新且目前狀態為 online 的裝置
                cursor.execute('''
                    UPDATE things 
                    SET status = 'offline', updated_at = ? 
                    WHERE last_seen < ? AND status = 'online'
                ''', [now, cutoff])
                if cursor.rowcount > 0:
                    print(f"Background Task: Marked {cursor.rowcount} stale devices as offline.")
                conn.commit()
        except Exception as e:
            print(f"Background Task Error: {e}")
        time.sleep(CHECK_INTERVAL)

mqtt_client = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv5)
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print("Connected to MQTT Broker!")
        client.subscribe("registration/register")
        client.subscribe("registration/offline") 
    else:
        print("Failed to connect, return code {}".format(rc))

def on_message(client, userdata, msg):
    try:
        if msg.topic == "registration/register":
            td = json.loads(msg.payload.decode("utf-8"))
            thing_id = td.get("id")
            title = td.get("title", "Unknown")
            
            if not thing_id: return

            now = int(time.time())
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO things (id, title, status, last_seen, td_content, updated_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        status = excluded.status,
                        last_seen = excluded.last_seen,
                        td_content = excluded.td_content,
                        updated_at = excluded.updated_at
                ''', [thing_id, title, "online", now, json.dumps(td), now, now])
                conn.commit()

            print(f"Device registered/updated: {thing_id}")
        
        elif msg.topic == "registration/offline":
            # 處理來自 LWT 或手動發布的下線通知
            thing_id = msg.payload.decode("utf-8")
            if thing_id:
                now = int(time.time())
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE things SET status = 'offline', updated_at = ? WHERE id = ?
                    ''', [now, thing_id])
                    conn.commit()
                print(f"Device marked offline via Topic: {thing_id}")

    except Exception as e:
        print(f"Error processing message: {e}")

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

if __name__ == "__main__":
    init_db()
    threading.Thread(target=check_offline_devices, daemon=True).start()
    
    try:
        mqtt_client.connect(FLASK_BROKER_HOST, FLASK_BROKER_PORT, 60)
        mqtt_client.loop_start()
        print(f"Registration Server is running on {FLASK_HOST}:{FLASK_PORT}")
        app.run(host=FLASK_HOST, port=FLASK_PORT, debug=True, use_reloader=False)
    except Exception as e:
        print(f"MQTT Connection Error: {e}")
    finally:
        mqtt_client.disconnect()
        mqtt_client.loop_stop()