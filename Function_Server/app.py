import json
import time
import requests
import threading
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import paho.mqtt.client as mqtt

app = Flask(__name__)
CORS(app)
rooms = {} # {"room-A": ["mic-01", "mic-02", "mic-03", "llm-01"], "room-B": ["mic-04", "mic-05", "llm-02"]}

FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5001
FLASK_BROKER_HOST = "127.0.0.1"
FLASK_BROKER_PORT = 1883
REGISTRATION_SERVER_URL = "http://127.0.0.1:5002"
CLIENT_ID = "function_server"

mqtt_client = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv5)

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print("Connected to MQTT Broker!")
        client.subscribe("minerva/nodes/+/room_id_status")
        
def on_message(client, userdata, msg):
    try:
        topic_parts = msg.topic.split("/")
        if len(topic_parts) == 4 and topic_parts[0] == "minerva" and topic_parts[1] == "nodes":
            node_id = topic_parts[2]
            
            if topic_parts[3] == "room_id_status":
                room_id = msg.payload.decode("utf-8").strip()
                
                for r_id in list(rooms.keys()):
                    if node_id in rooms[r_id]:
                        rooms[r_id].remove(node_id)
                        if not rooms[r_id]:
                            del rooms[r_id]
                
                if room_id:
                    if room_id not in rooms:
                        rooms[room_id] = []
                    if node_id not in rooms[room_id]:
                        rooms[room_id].append(node_id)

    except Exception as e:
        print(f"Error processing message: {e}")
            

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

# Flask REST API
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/online_nodes", methods=["GET"])
def online_nodes():
    """回傳所有上線節點，並根據 rooms 字典標記 idle/busy，TD 保持不被污染"""
    try:
        r = requests.get(f"{REGISTRATION_SERVER_URL}/things", timeout=5)
        if r.status_code == 200:
            tds = r.json()
            # 建立一個快速查找表：node_id -> room_id
            node_room_map = {}
            for r_id, participants in rooms.items():
                for node_id in participants:
                    node_room_map[node_id] = r_id
            
            result = []
            for td in tds:
                node_id = td.get("id")
                room_id = node_room_map.get(node_id)
                result.append({
                    "td": td,
                    "status": "busy" if room_id else "idle",
                    "room_id": room_id
                })
            return jsonify(result), 200
        return jsonify({"error": f"Registration Server returned {r.status_code}"}), r.status_code
    except Exception as e:
        return jsonify({"error": "Proxy to Registration Server failed"}), 500

@app.route("/rooms/create", methods=["POST"])
def create_room():
    data = request.json
    if not data or "room_id" not in data or "mics" not in data or "llm" not in data:
        return jsonify({"error": "Missing room_id, mics, or llm"}), 400
    
    room_id = str(data["room_id"]).strip()
    llm_id = str(data["llm"]).strip()
    mics = list(data["mics"])

    all_nodes = mics + [llm_id]

    for node_id in all_nodes:
        mqtt_client.publish(f"minerva/nodes/{node_id}/room_id_set", room_id) # 發送會議室 ID 給樹莓派，讓它屬於這個會議室
        pair_instr = json.dumps({"actions": "pair", "room_id": room_id})
        mqtt_client.publish(f"minerva/pair/{node_id}", pair_instr, retain=True) # 發給樹莓派做配對

    return jsonify({"status": "created", "room_id": room_id, "participants": all_nodes}), 201


@app.route("/rooms/<room_id>", methods=["DELETE"])
def delete_room(room_id):
    target_nodes = []
    if room_id not in rooms:
        return jsonify({"error": "Room not found"}), 404

    try:
        target_nodes = rooms[room_id]
        for node_id in target_nodes:
            mqtt_client.publish(f"minerva/nodes/{node_id}/room_id_set", "") # 發送空字串給樹莓派，讓它不再屬於任何會議室
            pair_instr = json.dumps({"actions": "pair", "room_id": ""})
            mqtt_client.publish(f"minerva/pair/{node_id}", pair_instr, retain=True) # 發給樹莓派做取消配對

    except Exception as e:
        print(f"Error deleting room: {e}")
        return jsonify({"error": "Error deleting room"}), 500
    
    return jsonify({"status": "deleted", "room_id": room_id}), 200


@app.route("/rooms", methods=["GET"])
def list_rooms():
    result = [{"room_id": r_id, "participants": nodes} for r_id, nodes in rooms.items()]
    return jsonify(result), 200


if __name__ == "__main__":
    try:
        mqtt_client.connect(FLASK_BROKER_HOST, FLASK_BROKER_PORT, 60)
        mqtt_client.loop_start()
        print(f"Function Server is running on {FLASK_HOST}:{FLASK_PORT}")
        app.run(host=FLASK_HOST, port=FLASK_PORT, debug=True, use_reloader=False)
    except Exception as e:
        print(f"MQTT Connection Error: {e}")
    finally:
        mqtt_client.disconnect()
        mqtt_client.loop_stop()