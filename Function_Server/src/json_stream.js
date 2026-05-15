// 增量解析 LLM streaming 出的 JSON，每完成一個 top-level field 就回傳。
//
// 策略：累積整個 buffer，逐字元 scan，追蹤 string / depth；
// 每次 scan 到「depth=1 且不在 string 內」的 `,` 或 closing `}`，
// 嘗試把 `buf[0..pos]` + `}` 補成可 parse 的 JSON，比對和上次 emit 的差異。
//
// 設計理由：partial-json npm 套件能做但要多一個依賴；scanner 邏輯小、可控、好除錯。

const TOP_KEYS = ["recast", "grammar_corrections", "suggested_replies", "keyword_hints"];

export function makeFieldExtractor() {
  let buf = "";
  let scanPos = 0;
  let depth = 0;
  let inString = false;
  let escape = false;
  let lastSafeEnd = -1;        // 上一個 depth-1 的 `,` 或 closing `}` 的位置（含）
  const emitted = new Set();   // 已 emit 過的 top-level key

  // 把 `buf[0..endExclusive]` 補 `}` 後嘗試 JSON.parse。
  function tryParseUpTo(endExclusive) {
    let slice = buf.slice(0, endExclusive);
    // 若最後字元是 `,`，去掉
    if (slice.endsWith(",")) slice = slice.slice(0, -1);
    // 若 slice 還沒以 `{` 結束的對應 `}`，補一個
    if (!slice.trimEnd().endsWith("}")) slice += "}";
    try {
      return JSON.parse(slice);
    } catch {
      return null;
    }
  }

  return {
    /**
     * 餵新 chunk，回傳這次新完成的欄位（{ key: value } 物件，可能為空）。
     */
    feed(chunk) {
      buf += chunk;

      for (let i = scanPos; i < buf.length; i++) {
        const c = buf[i];

        if (escape) { escape = false; continue; }
        if (inString) {
          if (c === "\\") escape = true;
          else if (c === '"') inString = false;
          continue;
        }
        if (c === '"') { inString = true; continue; }
        if (c === "{" || c === "[") depth++;
        else if (c === "}" || c === "]") {
          depth--;
          if (depth === 0 && c === "}") {
            // 整個 root object 收尾
            lastSafeEnd = i + 1;
          }
        } else if (c === "," && depth === 1) {
          lastSafeEnd = i + 1;
        }
      }
      scanPos = buf.length;

      if (lastSafeEnd < 0) return {};

      const partial = tryParseUpTo(lastSafeEnd);
      if (!partial) return {};

      const newFields = {};
      for (const k of TOP_KEYS) {
        if (k in partial && !emitted.has(k)) {
          newFields[k] = partial[k];
          emitted.add(k);
        }
      }
      return newFields;
    },

    /**
     * Stream 結束時做最終 parse，回傳完整物件（含可能還沒 emit 的最後欄位）。
     */
    finalize() {
      try {
        return JSON.parse(buf);
      } catch {
        // 容錯：嘗試補 `}` 收尾
        try {
          return JSON.parse(buf + "}");
        } catch {
          return null;
        }
      }
    },

    /** debug 用 */
    rawBuffer() { return buf; },
  };
}
