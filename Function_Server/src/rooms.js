// Rooms API：createRoom / closeRoom / listRooms / listAvailableMics + FS 重啟恢復。

import { randomUUID } from "node:crypto";
import { db, stmt, findMicConflicts } from "./db.js";
import { wot } from "./servient.js";
import { fetchTD, fetchAllTDs } from "./td_loader.js";
import { onlineStatus } from "./presence.js";
import { handleUtterance } from "./orchestrator.js";
import { broadcast, endRoomClients } from "./sse.js";
import { FS_BASE_URL } from "./config.js";

// In-memory state（FS 重啟由 rebuildOnStartup 重建）
const subscriptions = new Map(); // Map<room_id, Set<Subscription>>
export const micToAttendee = new Map(); // Map<mic_id, {room_id, attendee_id, name}>

function isMic(td) {
  return !!td.events?.audio;
}

function magicUrl(room_id, access_token) {
  return `${FS_BASE_URL}/displays/${room_id}/${access_token}`;
}

function httpError(status, code, extra = {}) {
  const err = new Error(code);
  err.status = status;
  err.body = { error: code, ...extra };
  return err;
}

export async function listAvailableMics() {
  const tds = await fetchAllTDs();
  const micTds = tds.filter(isMic);
  const inUse = new Set(findMicConflicts(micTds.map((td) => td.id)));
  return micTds
    .filter(
      (td) => onlineStatus.get(td.id) === true && !inUse.has(td.id)
    )
    .map((td) => ({ mic_id: td.id, title: td.title }));
}

export function listRooms({ status } = {}) {
  if (status === "open") return stmt.listOpenRooms.all();
  if (status === "closed") return stmt.listClosedRooms.all();
  return stmt.listRooms.all();
}

// Workaround：node-wot 0.9 binding-mqtt 從 form.href 的 url.pathname 抓 subscribe topic filter；
// 若 pathname 為空且 mqv:topic 有值，filter 會變空字串，broker 拒絕並斷線（'Connection closed'）。
// 把 mqv:topic 補進 href path，binding 即可正確 subscribe。
function patchMicTdForNodeWot(td) {
  for (const form of td.events?.audio?.forms ?? []) {
    if (!form.href?.startsWith("mqtt://") || !form["mqv:topic"]) continue;
    const u = new URL(form.href);
    if (!u.pathname || u.pathname === "/") {
      form.href = `${u.protocol}//${u.host}/${form["mqv:topic"]}`;
    }
  }
  return td;
}

async function subscribeMic(mic_id, ctx) {
  const td = patchMicTdForNodeWot(await fetchTD(mic_id));
  console.log(`[rooms] consuming mic=${mic_id} href=${td.events?.audio?.forms?.[0]?.href}`);
  const consumed = await wot.consume(td);
  console.log(`[rooms] consumed mic=${mic_id} title="${consumed.getThingDescription().title}"`);
  const sub = await consumed.subscribeEvent("audio", async (output) => {
    console.log(`[rooms] >>> audio event arrived mic=${mic_id}`);
    try {
      await handleUtterance(mic_id, ctx, output);
    } catch (err) {
      console.error(`[rooms] handleUtterance threw:`, err);
    }
  });
  console.log(`[rooms] subscribeEvent("audio") returned for mic=${mic_id} sub=${!!sub}`);
  return sub;
}

export async function createRoom({ name, mics }) {
  // 驗證 input shape
  if (!Array.isArray(mics) || mics.length === 0) {
    throw httpError(400, "bad_request", {
      message: "mics must be non-empty array",
    });
  }
  for (const m of mics) {
    if (!m?.mic_id || !m?.user_name) {
      throw httpError(400, "bad_request", {
        message: "each mic entry needs mic_id + user_name",
      });
    }
  }
  // 表單內若有 mic 重複，前端理論上會擋；後端再防一道
  const micIds = mics.map((m) => m.mic_id);
  if (new Set(micIds).size !== micIds.length) {
    throw httpError(400, "duplicate_mic_in_request");
  }

  // mic online 驗證
  const offline = micIds.filter((id) => onlineStatus.get(id) !== true);
  if (offline.length) {
    throw httpError(422, "mic_offline", { mic_ids: offline });
  }

  // mic 排他性 SQL 檢查
  const conflicts = findMicConflicts(micIds);
  if (conflicts.length) {
    throw httpError(422, "mic_in_use", { mic_ids: conflicts });
  }

  // INSERT room + attendees in transaction
  const room_id = randomUUID();
  const now = Date.now();
  const attendees = [];
  db.transaction(() => {
    stmt.insertRoom.run(room_id, name ?? "", now);
    for (const m of mics) {
      const access_token = randomUUID();
      const info = stmt.insertAttendee.run(
        room_id,
        m.user_name,
        m.mic_id,
        access_token,
        now
      );
      attendees.push({
        id: info.lastInsertRowid,
        name: m.user_name,
        mic_id: m.mic_id,
        access_token,
      });
    }
  })();

  // consume mic + subscribeEvent
  const subs = new Set();
  try {
    for (const att of attendees) {
      const ctx = {
        room_id,
        attendee_id: att.id,
        name: att.name,
      };
      micToAttendee.set(att.mic_id, ctx);
      const sub = await subscribeMic(att.mic_id, ctx);
      subs.add(sub);
    }
  } catch (err) {
    // 任一隻 mic 訂閱失敗時 rollback：停掉已建 subs、清 in-memory、刪 DB row
    for (const s of subs) {
      try {
        await s.stop();
      } catch {
        /* ignore */
      }
    }
    for (const att of attendees) micToAttendee.delete(att.mic_id);
    db.transaction(() => {
      db.prepare(`DELETE FROM attendees WHERE room_id = ?`).run(room_id);
      db.prepare(`DELETE FROM rooms WHERE room_id = ?`).run(room_id);
    })();
    throw httpError(500, "mic_subscribe_failed", { message: err.message });
  }
  subscriptions.set(room_id, subs);

  console.log(
    `[rooms] created room=${room_id} name="${name ?? ""}" mics=[${micIds.join(", ")}]`
  );

  return {
    room_id,
    attendees: attendees.map((a) => ({
      id: a.id,
      name: a.name,
      mic_id: a.mic_id,
      magic_url: magicUrl(room_id, a.access_token),
    })),
  };
}

export async function closeRoom(room_id) {
  const room = stmt.getRoom.get(room_id);
  if (!room) throw httpError(404, "room_not_found");
  if (room.closed_at != null) {
    return { ok: true, already_closed: true };
  }

  const subs = subscriptions.get(room_id);
  if (subs) {
    for (const s of subs) {
      try {
        await s.stop();
      } catch (err) {
        console.error(
          `[rooms] unsubscribe error room=${room_id}:`,
          err.message
        );
      }
    }
    subscriptions.delete(room_id);
  }
  for (const a of stmt.listAttendeesByRoom.all(room_id)) {
    micToAttendee.delete(a.mic_id);
  }
  const closed_at = Date.now();
  stmt.closeRoom.run(closed_at, room_id);
  // 通知 Display 端「會議結束」，再 end 該 room 之 SSE 連線（避免瀏覽器自動 reconnect）
  broadcast(room_id, "meeting_ended", { closed_at });
  setTimeout(() => endRoomClients(room_id), 200);
  console.log(`[rooms] closed room=${room_id}`);
  return { ok: true };
}

// FS 重啟：從 DB 讀回 open rooms，逐一 consume mic + subscribeEvent
export async function rebuildOnStartup() {
  const openRooms = stmt.listOpenRooms.all();
  for (const room of openRooms) {
    const attendees = stmt.listAttendeesByRoom.all(room.room_id);
    const subs = new Set();
    for (const att of attendees) {
      try {
        const ctx = {
          room_id: room.room_id,
          attendee_id: att.id,
          name: att.name,
        };
        micToAttendee.set(att.mic_id, ctx);
        const sub = await subscribeMic(att.mic_id, ctx);
        subs.add(sub);
      } catch (err) {
        console.error(
          `[rooms] rebuild mic ${att.mic_id} (room=${room.room_id}) failed:`,
          err.message
        );
      }
    }
    subscriptions.set(room.room_id, subs);
    console.log(
      `[rooms] rebuilt room=${room.room_id} attendees=${attendees.length}`
    );
  }
}
