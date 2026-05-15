// Function Server entry point。
// 載入順序：servient（top-level await 啟動）→ presence → rooms → Express。

import express from "express";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { servient } from "./src/servient.js"; // side effect：servient.start()
import {
  onlineStatus,
  onPresenceChange,
  startMqttPresence,
  startHttpProbe,
} from "./src/presence.js";
import {
  listAvailableMics,
  listRooms,
  createRoom,
  closeRoom,
  rebuildOnStartup,
} from "./src/rooms.js";
import { consumeSharedThings } from "./src/orchestrator.js";
import { fetchAllTDs } from "./src/td_loader.js";
import { FS_PORT, FS_BASE_URL } from "./src/config.js";
import { verifyToken } from "./src/auth.js";
import { addClient } from "./src/sse.js";
import { stmt } from "./src/db.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

console.log("[fs] servient started");

// --- Consume shared inference Things（STT / Translator / Ollama Wrapper）---
await consumeSharedThings();

// --- Presence ---
onPresenceChange((thingId, online) => {
  const ts = new Date().toISOString().slice(11, 19);
  console.log(`[${ts}] [presence] ${thingId} -> ${online ? "online" : "offline"}`);
});
startMqttPresence();
startHttpProbe();
console.log("[fs] presence loops running");

// --- Rebuild rooms on startup (warm restart) ---
await rebuildOnStartup();

// --- Express ---
const app = express();
app.use(express.json());

// --- Admin Web UI（§9.2 / §14.2 M6）---
// 信任網路內部使用，無 auth；論文小專案不引入 admin 帳號系統。
// express.static 預設 redirect /admin → /admin/、自動 serve /admin/ 的 index.html。
app.use("/admin", express.static(path.join(__dirname, "public/admin")));

// admin 端列某 room 的所有 attendee（含重組 magic URL，方便重看連結）
app.get("/api/rooms/:id/attendees", (req, res) => {
  const room = stmt.getRoom.get(req.params.id);
  if (!room) return res.status(404).json({ error: "room_not_found" });
  const attendees = stmt.listAttendeesByRoom.all(req.params.id);
  res.json(
    attendees.map((a) => ({
      id: a.id,
      name: a.name,
      mic_id: a.mic_id,
      magic_url: `${FS_BASE_URL}/displays/${room.room_id}/${a.access_token}`,
    }))
  );
});

// --- Display web app（Magic Link 頁，§9.2 / §D / §E）---
app.get("/displays/:room/:token", (req, res) => {
  const att = verifyToken(req.params.room, req.params.token);
  if (!att) return res.status(403).send("Invalid or expired magic link.");
  res.sendFile(path.join(__dirname, "public/displays/index.html"));
});

app.get("/api/displays/:room/:token/self", (req, res) => {
  const att = verifyToken(req.params.room, req.params.token);
  if (!att) return res.status(403).json({ error: "invalid_token" });
  const room = stmt.getRoom.get(req.params.room);
  res.json({
    attendee_id: att.id,
    name: att.name,
    mic_id: att.mic_id,
    room_id: req.params.room,
    room_name: room?.name ?? "",
    room_closed_at: room?.closed_at ?? null,
  });
});

app.get("/api/displays/:room/:token/history", (req, res) => {
  const att = verifyToken(req.params.room, req.params.token);
  if (!att) return res.status(403).json({ error: "invalid_token" });
  const rows = stmt.listUtterancesByRoomWithSpeaker.all(req.params.room);
  res.json(
    rows.map((u) => ({
      utterance_id: u.id,
      ts: u.ts,
      speaker_attendee_id: u.speaker_attendee_id,
      speaker_name: u.speaker_name,
      en_text: u.en_text,
      zh_text: u.zh_text,
      recast: u.recast,
      replies: u.replies_json ? JSON.parse(u.replies_json) : null,
      grammar_corrections: u.grammar_corrections_json ? JSON.parse(u.grammar_corrections_json) : null,
      keyword_hints: u.keywords_json ? JSON.parse(u.keywords_json) : null,
    }))
  );
});

app.get("/api/displays/:room/:token/stream", (req, res) => {
  const att = verifyToken(req.params.room, req.params.token);
  if (!att) return res.status(403).end();
  const room = stmt.getRoom.get(req.params.room);
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });
  res.write(`: connected as ${att.name}\n\n`);
  // room 已關閉 → 立刻推 meeting_ended 並結束，不掛長連線
  if (room?.closed_at != null) {
    res.write(
      `event: meeting_ended\ndata: ${JSON.stringify({ closed_at: room.closed_at })}\n\n`
    );
    return res.end();
  }
  addClient(req.params.room, res);
});

app.get("/api/things", async (req, res, next) => {
  try {
    const tds = await fetchAllTDs();
    res.json(
      tds.map((td) => ({ td, online: onlineStatus.get(td.id) === true }))
    );
  } catch (err) {
    next(err);
  }
});

app.get("/api/mics/available", async (req, res, next) => {
  try {
    res.json(await listAvailableMics());
  } catch (err) {
    next(err);
  }
});

app.post("/api/rooms", async (req, res, next) => {
  try {
    const result = await createRoom(req.body ?? {});
    res.status(201).json(result);
  } catch (err) {
    if (err.status) return res.status(err.status).json(err.body);
    next(err);
  }
});

app.get("/api/rooms", (req, res, next) => {
  try {
    res.json(listRooms({ status: req.query.status }));
  } catch (err) {
    next(err);
  }
});

app.post("/api/rooms/:id/close", async (req, res, next) => {
  try {
    res.json(await closeRoom(req.params.id));
  } catch (err) {
    if (err.status) return res.status(err.status).json(err.body);
    next(err);
  }
});

app.use((err, req, res, _next) => {
  console.error("[fs] unhandled:", err);
  res.status(500).json({ error: "internal_error", message: err.message });
});

app.listen(FS_PORT, "0.0.0.0", () => {
  console.log(`[fs] listening on ${FS_BASE_URL} (bind 0.0.0.0:${FS_PORT})`);
});

process.on("SIGINT", () => {
  console.log("\n[fs] shutting down");
  servient.shutdown();
  process.exit(0);
});
