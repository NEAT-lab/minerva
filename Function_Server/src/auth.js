import { stmt } from "./db.js";

// 驗 magic link：以 access_token 查 attendee、確認屬於指定 room。
// 通過回 attendee row，不通過回 null（callsite 自己回 403）。
export function verifyToken(room_id, token) {
  if (!room_id || !token) return null;
  const att = stmt.findAttendeeByToken.get(token);
  if (!att) return null;
  if (att.room_id !== room_id) return null;
  return att;
}
