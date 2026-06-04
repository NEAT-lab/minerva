import Database from "better-sqlite3";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DB_PATH = path.resolve(__dirname, "..", "fs.sqlite");

export const db = new Database(DB_PATH);
db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");

const SCHEMA = `
CREATE TABLE IF NOT EXISTS rooms (
    room_id      TEXT PRIMARY KEY,
    name         TEXT,
    created_at   INTEGER NOT NULL,
    closed_at    INTEGER
);

CREATE TABLE IF NOT EXISTS attendees (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    mic_id       TEXT,                 -- NULL = 手機麥克風（語音走 magic link HTTP 上傳，非 RPi Mic Thing）
    access_token TEXT NOT NULL UNIQUE,
    joined_at    INTEGER NOT NULL,
    left_at      INTEGER,
    FOREIGN KEY (room_id) REFERENCES rooms(room_id)
);

CREATE INDEX IF NOT EXISTS idx_attendees_token ON attendees(access_token);
CREATE INDEX IF NOT EXISTS idx_attendees_mic   ON attendees(mic_id);
CREATE INDEX IF NOT EXISTS idx_attendees_room  ON attendees(room_id);

CREATE TABLE IF NOT EXISTS utterances (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id             TEXT NOT NULL,
    ts                  INTEGER NOT NULL,
    duration_ms         INTEGER,
    audio_path          TEXT,
    speaker_attendee_id INTEGER,
    en_text             TEXT,
    zh_text             TEXT,
    recast              TEXT,
    replies_json        TEXT,
    grammar_corrections_json TEXT,
    keywords_json       TEXT,
    stt_ms              INTEGER,
    translate_ms        INTEGER,
    ollama_ms           INTEGER,
    total_ms            INTEGER,
    error_log           TEXT,
    FOREIGN KEY (room_id) REFERENCES rooms(room_id),
    FOREIGN KEY (speaker_attendee_id) REFERENCES attendees(id)
);

CREATE INDEX IF NOT EXISTS idx_utterance_room_ts ON utterances(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_utterance_speaker ON utterances(speaker_attendee_id);
`;

db.exec(SCHEMA);

// Migration：早期 schema 的 attendees.mic_id 為 NOT NULL；手機與會者需要 NULL。
// 既有 DB 用 SQLite table-rebuild 放寬約束並保留資料。
// 冪等：放寬後 notnull=0，下次啟動不再執行。
const micCol = db
  .prepare("PRAGMA table_info(attendees)")
  .all()
  .find((c) => c.name === "mic_id");
if (micCol && micCol.notnull === 1) {
  db.pragma("foreign_keys = OFF"); // pragma 在 transaction 內為 no-op，須先設於 transaction 外
  db.transaction(() => {
    db.exec(`
      CREATE TABLE attendees_new (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          room_id      TEXT NOT NULL,
          name         TEXT NOT NULL,
          mic_id       TEXT,
          access_token TEXT NOT NULL UNIQUE,
          joined_at    INTEGER NOT NULL,
          left_at      INTEGER,
          FOREIGN KEY (room_id) REFERENCES rooms(room_id)
      );
      INSERT INTO attendees_new (id, room_id, name, mic_id, access_token, joined_at, left_at)
          SELECT id, room_id, name, mic_id, access_token, joined_at, left_at FROM attendees;
      DROP TABLE attendees;
      ALTER TABLE attendees_new RENAME TO attendees;
      CREATE INDEX IF NOT EXISTS idx_attendees_token ON attendees(access_token);
      CREATE INDEX IF NOT EXISTS idx_attendees_mic   ON attendees(mic_id);
      CREATE INDEX IF NOT EXISTS idx_attendees_room  ON attendees(room_id);
    `);
  })();
  db.pragma("foreign_keys = ON");
  console.log("[db] migrated attendees.mic_id -> nullable");
}

export const stmt = {
  insertRoom: db.prepare(
    `INSERT INTO rooms (room_id, name, created_at) VALUES (?, ?, ?)`
  ),
  closeRoom: db.prepare(
    `UPDATE rooms SET closed_at = ? WHERE room_id = ? AND closed_at IS NULL`
  ),
  getRoom: db.prepare(`SELECT * FROM rooms WHERE room_id = ?`),
  listRooms: db.prepare(
    `SELECT r.*,
            (SELECT COUNT(*) FROM attendees a WHERE a.room_id = r.room_id) AS attendee_count
     FROM rooms r ORDER BY r.created_at DESC`
  ),
  listOpenRooms: db.prepare(
    `SELECT r.*,
            (SELECT COUNT(*) FROM attendees a WHERE a.room_id = r.room_id) AS attendee_count
     FROM rooms r WHERE r.closed_at IS NULL ORDER BY r.created_at DESC`
  ),
  listClosedRooms: db.prepare(
    `SELECT r.*,
            (SELECT COUNT(*) FROM attendees a WHERE a.room_id = r.room_id) AS attendee_count
     FROM rooms r WHERE r.closed_at IS NOT NULL ORDER BY r.closed_at DESC`
  ),

  insertAttendee: db.prepare(
    `INSERT INTO attendees (room_id, name, mic_id, access_token, joined_at)
     VALUES (?, ?, ?, ?, ?)`
  ),
  findAttendeeByToken: db.prepare(
    `SELECT * FROM attendees WHERE access_token = ?`
  ),
  listAttendeesByRoom: db.prepare(
    `SELECT * FROM attendees WHERE room_id = ? ORDER BY id ASC`
  ),

  insertUtterance: db.prepare(
    `INSERT INTO utterances (room_id, ts, duration_ms, speaker_attendee_id)
     VALUES (?, ?, ?, ?)`
  ),
  updateUtteranceStt: db.prepare(
    `UPDATE utterances SET en_text = ?, stt_ms = ? WHERE id = ?`
  ),
  updateUtteranceTranslate: db.prepare(
    `UPDATE utterances SET zh_text = ?, translate_ms = ? WHERE id = ?`
  ),
  updateUtteranceOllama: db.prepare(
    `UPDATE utterances SET recast = ?, replies_json = ?, grammar_corrections_json = ?, keywords_json = ?, ollama_ms = ? WHERE id = ?`
  ),
  updateUtteranceTotal: db.prepare(
    `UPDATE utterances SET total_ms = ? WHERE id = ?`
  ),
  updateUtteranceError: db.prepare(
    `UPDATE utterances SET error_log = ? WHERE id = ?`
  ),
  listUtterancesByRoom: db.prepare(
    `SELECT * FROM utterances WHERE room_id = ? ORDER BY ts ASC`
  ),
  // 同上但 join attendee.name，給 Display 端 history fetch 用
  listUtterancesByRoomWithSpeaker: db.prepare(
    `SELECT u.*, a.name AS speaker_name
     FROM utterances u
     LEFT JOIN attendees a ON u.speaker_attendee_id = a.id
     WHERE u.room_id = ?
     ORDER BY u.ts ASC`
  ),
  recentContextByRoom: db.prepare(
    `SELECT u.en_text, a.name AS speaker_name
     FROM utterances u
     LEFT JOIN attendees a ON u.speaker_attendee_id = a.id
     WHERE u.room_id = ? AND u.en_text IS NOT NULL
     ORDER BY u.ts DESC
     LIMIT 5`
  ),
  // 同上但排除目前 utterance（orchestrator 寫入 en_text 後撈 prior context 用）
  recentContextByRoomExcluding: db.prepare(
    `SELECT u.en_text, a.name AS speaker_name
     FROM utterances u
     LEFT JOIN attendees a ON u.speaker_attendee_id = a.id
     WHERE u.room_id = ? AND u.id != ? AND u.en_text IS NOT NULL
     ORDER BY u.ts DESC
     LIMIT 5`
  ),
};

// Mic 排他性檢查：mic_ids 中已在某 open room 之 attendees 的子集
export function findMicConflicts(micIds) {
  if (micIds.length === 0) return [];
  const placeholders = micIds.map(() => "?").join(",");
  const rows = db
    .prepare(
      `SELECT DISTINCT a.mic_id FROM attendees a
       JOIN rooms r ON a.room_id = r.room_id
       WHERE r.closed_at IS NULL AND a.mic_id IN (${placeholders})`
    )
    .all(...micIds);
  return rows.map((r) => r.mic_id);
}
