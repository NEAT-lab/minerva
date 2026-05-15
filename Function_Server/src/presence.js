// Hybrid presence (§4.6 + §4.7 + §4.8)：
//   - Mic：MQTT LWT 訂閱 `presence/{mic_id}`
//   - STT / Translator / Ollama Wrapper：HTTP /health probe loop（15s）
// 統一寫入單一 Map<thing_id, boolean>；變化透過 listener 廣播。

import mqtt from "mqtt";
import {
  BROKER_URL,
  HEALTH_PROBE_INTERVAL_MS,
  HEALTH_PROBE_TIMEOUT_MS,
} from "./config.js";
import { fetchAllTDs, findHttpBase } from "./td_loader.js";

export const onlineStatus = new Map();
const listeners = new Set();

export function onPresenceChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function setOnline(thingId, online) {
  const prev = onlineStatus.get(thingId);
  if (prev === online) return;
  onlineStatus.set(thingId, online);
  for (const fn of listeners) fn(thingId, online, prev);
}

// (a) MQTT presence/+ subscribe — for Mic（§4.6）
export function startMqttPresence() {
  const client = mqtt.connect(BROKER_URL, {
    clientId: `fs_node_presence_${Date.now()}`,
    clean: true,
  });
  client.on("connect", () => {
    console.log(`[presence] mqtt connected ${BROKER_URL}`);
    client.subscribe("presence/+", { qos: 1 });
  });
  client.on("message", (topic, payload) => {
    const rawKey = topic.slice("presence/".length);
    const msg = payload.toString();
    // mic 端 PRESENCE_TOPIC 為 `presence/<raw_uuid>`，TD id 為 `urn:uuid:<raw_uuid>`。
    // 對齊 HTTP probe 用 td.id 寫入的格式，這裡 raw UUID 補 urn:uuid: 前綴。
    const thingId = rawKey.startsWith("urn:") ? rawKey : `urn:uuid:${rawKey}`;
    // 約定：retained "online" = 上線；空字串（LWT 或 clean shutdown）= 下線
    setOnline(thingId, msg === "online");
  });
  client.on("error", (err) =>
    console.error("[presence] mqtt error:", err.message)
  );
  return client;
}

// (b) HTTP /health probe loop — for STT / Tr / Olw（§4.7）
async function probeAll() {
  let tds;
  try {
    tds = await fetchAllTDs();
  } catch (err) {
    console.error("[presence] fetchAllTDs failed:", err.message);
    return;
  }
  await Promise.all(
    tds.map(async (td) => {
      const base = findHttpBase(td);
      if (!base) return; // Mic 無 HTTP form，由 MQTT presence 管
      try {
        const r = await fetch(`${base}/health`, {
          signal: AbortSignal.timeout(HEALTH_PROBE_TIMEOUT_MS),
        });
        setOnline(td.id, r.ok);
      } catch {
        setOnline(td.id, false);
      }
    })
  );
}

export function startHttpProbe() {
  probeAll(); // 立刻跑一次，不等首個 interval
  return setInterval(probeAll, HEALTH_PROBE_INTERVAL_MS);
}
