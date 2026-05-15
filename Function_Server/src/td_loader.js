import { TDD_URL } from "./config.js";

export async function fetchAllTDs() {
  const r = await fetch(`${TDD_URL}/things`);
  if (!r.ok) throw new Error(`TDD GET /things failed: ${r.status}`);
  return r.json();
}

export async function fetchTD(thingId) {
  const r = await fetch(`${TDD_URL}/things/${encodeURIComponent(thingId)}`);
  if (!r.ok) throw new Error(`TDD GET /things/${thingId} failed: ${r.status}`);
  return r.json();
}

// 從 TD 的 affordance forms 抽 HTTP base URL（給 /health probe 用）。
// Mic TD 全部 form 為 mqtt://，回 null；由 MQTT LWT presence 處理。
export function findHttpBase(td) {
  const affordanceGroups = [td.actions, td.properties, td.events];
  for (const group of affordanceGroups) {
    for (const aff of Object.values(group ?? {})) {
      for (const form of aff.forms ?? []) {
        const href = form.href ?? "";
        if (href.startsWith("http://") || href.startsWith("https://")) {
          const u = new URL(href);
          return `${u.protocol}//${u.host}`;
        }
      }
    }
  }
  return null;
}
