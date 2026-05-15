// 初始化 / 驗證 FS_DB schema。
// 用法：node scripts/init-db.js
//
// import db.js 即自動 CREATE TABLE IF NOT EXISTS（idempotent，重跑安全）。
// 後續做一輪 smoke test 確認 schema、prepared statements、mic 排他性查詢三者皆通。

import { db, stmt, findMicConflicts } from "../src/db.js";
import { randomUUID } from "node:crypto";

console.log(`[init-db] DB path: ${db.name}`);

const tables = db
  .prepare(`SELECT name FROM sqlite_master WHERE type='table' ORDER BY name`)
  .all()
  .map((r) => r.name);
console.log(`[init-db] tables: ${tables.join(", ")}`);

const indices = db
  .prepare(
    `SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name`
  )
  .all()
  .map((r) => r.name);
console.log(`[init-db] indices: ${indices.join(", ")}`);

// Smoke test：寫一輪 room + attendee + utterance，驗證 prepared statements 與 mic 排他性。
const testRoomId = `__smoketest_${randomUUID().slice(0, 8)}`;
const testMicId = `__smoketest_mic_${randomUUID().slice(0, 8)}`;
const now = Date.now();

db.transaction(() => {
  stmt.insertRoom.run(testRoomId, "smoke test room", now);
  const info = stmt.insertAttendee.run(
    testRoomId,
    "Test User",
    testMicId,
    randomUUID(),
    now
  );
  const attendeeId = info.lastInsertRowid;
  const utt = stmt.insertUtterance.run(testRoomId, now, 800, attendeeId);
  stmt.updateUtteranceStt.run("Hello", 350, utt.lastInsertRowid);
})();

const conflicts = findMicConflicts([testMicId, "__never_used_mic"]);
const expected = [testMicId];
const passed =
  conflicts.length === expected.length &&
  conflicts[0] === testMicId;
console.log(
  `[init-db] mic conflict check: ${passed ? "PASS" : "FAIL"} (got ${JSON.stringify(conflicts)})`
);

// 清掉 smoke test row（不污染論文資料）
db.transaction(() => {
  db.prepare(`DELETE FROM utterances WHERE room_id = ?`).run(testRoomId);
  db.prepare(`DELETE FROM attendees WHERE room_id = ?`).run(testRoomId);
  db.prepare(`DELETE FROM rooms WHERE room_id = ?`).run(testRoomId);
})();

console.log(`[init-db] schema ready, smoke test cleaned up`);
