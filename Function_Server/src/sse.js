// SSE broadcast hub。orchestrator / closeRoom / route handler 共用。
// 維護 Map<room_id, Set<Response>>；連線關閉自動清。

const clients = new Map();

export function addClient(room_id, res) {
  if (!clients.has(room_id)) clients.set(room_id, new Set());
  clients.get(room_id).add(res);
  res.on("close", () => removeClient(room_id, res));
}

export function removeClient(room_id, res) {
  clients.get(room_id)?.delete(res);
  if (clients.get(room_id)?.size === 0) clients.delete(room_id);
}

// SSE wire format：每 event 一個 `event: <name>` + `data: <json>\n\n`
export function broadcast(room_id, event, data) {
  const set = clients.get(room_id);
  if (!set) return;
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of set) {
    try {
      res.write(payload);
    } catch {
      // 連線已壞，'close' handler 會清；這裡安靜處理避免 cascade
    }
  }
}

// closeRoom 時把該 room 全部 client 連線結束（先發 meeting_ended 給 client，再 end）
export function endRoomClients(room_id) {
  const set = clients.get(room_id);
  if (!set) return;
  for (const res of set) {
    try {
      res.end();
    } catch {
      /* ignore */
    }
  }
  clients.delete(room_id);
}
