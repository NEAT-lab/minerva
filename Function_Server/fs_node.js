// Function Server entry point。
// 載入順序：servient（top-level await 啟動）、presence、rooms、Express。

import express from "express";
import http from "node:http";
import https from "node:https";
import fs from "node:fs";
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
import { consumeSharedThings, handleUtterance } from "./src/orchestrator.js";
import { fetchAllTDs } from "./src/td_loader.js";
import {
  FS_PORT,
  FS_BASE_URL,
  TLS_ENABLED,
  TLS_CERT_PATH,
  TLS_KEY_PATH,
} from "./src/config.js";
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

// 根路徑導向管理介面（首頁本身無內容）
app.get("/", (req, res) => res.redirect("/admin/"));

// --- Admin Web UI ---
// 信任網路內部使用，無 auth；論文小專案不引入 admin 帳號系統。
// express.static 預設 redirect /admin 到 /admin/、自動 serve /admin/ 的 index.html。
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

// --- Display web app（Magic Link 頁）---
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

// 手機麥克風語音上傳：與會者的 magic link 頁按住說話、放開即 POST 一段 webm/opus。
// 走 token 驗證的應用層入口（非 WoT Mic Thing），匯入與 RPi mic 相同的 handleUtterance。
// 僅限手機與會者（mic_id IS NULL）；RPi 與會者的聲音來自實體裝置，拒絕重複入口。
app.post(
  "/api/displays/:room/:token/utterance",
  express.raw({ type: () => true, limit: "12mb" }),
  (req, res) => {
    const att = verifyToken(req.params.room, req.params.token);
    if (!att) return res.status(403).json({ error: "invalid_token" });
    if (att.mic_id != null) {
      return res.status(409).json({ error: "not_a_phone_mic" });
    }
    const room = stmt.getRoom.get(req.params.room);
    if (!room || room.closed_at != null) {
      return res.status(409).json({ error: "room_closed" });
    }
    const audio = req.body;
    if (!audio || audio.length === 0) {
      return res.status(400).json({ error: "empty_audio" });
    }
    const ctx = { room_id: req.params.room, attendee_id: att.id, name: att.name };
    // 不 await：後端流水線（STT → Tr/Olw）可能數秒，先回 202 讓手機端釋放、可立刻錄下一句。
    handleUtterance(`phone:${att.id}`, ctx, audio).catch((err) =>
      console.error(`[fs] phone utterance att=${att.id} error:`, err)
    );
    res.status(202).json({ ok: true });
  }
);

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
  // room 已關閉時立刻推 meeting_ended 並結束，不掛長連線
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

// 有 mkcert 憑證走 HTTPS（手機麥克風 secure context 需要）；否則退回 HTTP。
const server = TLS_ENABLED
  ? https.createServer(
      { key: fs.readFileSync(TLS_KEY_PATH), cert: fs.readFileSync(TLS_CERT_PATH) },
      app
    )
  : http.createServer(app);

server.listen(FS_PORT, "0.0.0.0", () => {
  console.log(
    `[fs] listening on ${FS_BASE_URL} (${TLS_ENABLED ? "https" : "http"}, bind 0.0.0.0:${FS_PORT})`
  );
  if (!TLS_ENABLED) {
    console.log(
      "[fs] TLS 關閉（certs/ 無憑證）；手機麥克風需 HTTPS，請放 mkcert 憑證後重啟"
    );
  }
});

process.on("SIGINT", () => {
  console.log("\n[fs] shutting down");
  servient.shutdown();
  process.exit(0);
});
