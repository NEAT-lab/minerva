// Orchestrator：handleUtterance 串起一輪 utterance 的後端流水線。
// 流程：
//   1. INSERT utterance row（拿 utterance_id）
//   2. STT 同步等回後 UPDATE en_text + stt_ms
//   3. 並行跑 Translator（en 至 zh_Hant）與 Ollama（mega-prompt，含 5 句 speaker-tagged prior context）
//   4. 各階段 *_ms 寫 DB；錯誤寫 error_log（多階段失敗以 "|" 串接）

import { stmt } from "./db.js";
import { wot } from "./servient.js";
import { fetchTD } from "./td_loader.js";
import { broadcast } from "./sse.js";
import { MEGA_SYSTEM_PROMPT } from "./mega_prompt.js";
import { makeFieldExtractor } from "./json_stream.js";

const STT_LANGUAGE = "en";
const SRC_NLLB = "eng_Latn";
const TGT_NLLB = "zho_Hant";
const OLLAMA_MODEL = "qwen3:8b";
// Qwen3:8b 是 hybrid thinking model，用 top-level `think: false`（Ollama 0.24.0）關推理。
const OLLAMA_OPTIONS = { num_predict: 1500, temperature: 0.3 };

// 用 JSON Schema 取代 format:"json" 強制每個 grammar_correction 必有 reason 欄位。
// 避免 Case 2 出現 reason:undefined 的問題。
const RESPONSE_SCHEMA = {
  type: "object",
  required: ["recast", "grammar_corrections", "suggested_replies", "keyword_hints"],
  properties: {
    recast: { type: "string" },
    grammar_corrections: {
      type: "array",
      items: {
        type: "object",
        required: ["wrong", "correct", "reason"],
        properties: {
          wrong: { type: "string" },
          correct: { type: "string" },
          reason: { type: "string", minLength: 4 },
        },
      },
    },
    suggested_replies: {
      type: "array",
      items: { type: "string" },
    },
    keyword_hints: {
      type: "array",
      items: {
        type: "object",
        required: ["term_en", "term_zh", "points"],
        properties: {
          term_en: { type: "string" },
          term_zh: { type: "string" },
          points: { type: "array", items: { type: "string" } },
        },
      },
    },
  },
};

const STT_ID = "urn:uuid:stt-001";
const TR_ID = "urn:uuid:translator-001";
const OLW_ID = "urn:uuid:ollama-wrapper-001";

let stt, tr, olw;
// STT 的 raw form 用於 binary call（見 callStt 註解）；仍 wot.consume 對齊「4 first-class Things」
let sttForm;
// Ollama chat 用 raw fetch 做 streaming（node-wot 0.9 不支援 streaming response）
let olwChatForm;

export async function consumeSharedThings() {
  const [sttTd, trTd, olwTd] = await Promise.all([
    fetchTD(STT_ID),
    fetchTD(TR_ID),
    fetchTD(OLW_ID),
  ]);
  sttForm = sttTd.actions?.transcribe?.forms?.[0];
  olwChatForm = olwTd.actions?.chat?.forms?.[0];
  [stt, tr, olw] = await Promise.all([
    wot.consume(sttTd),
    wot.consume(trTd),
    wot.consume(olwTd),
  ]);
  console.log("[orchestrator] consumed STT / Translator / Ollama");
  warmupOllama().catch(() => {});
}

// 啟動時對 Ollama 丟一筆 num_predict=1 的 dummy chat，把模型載入 GPU，
// 避免第一筆正式 utterance 撞到 cold-start latency。
async function warmupOllama() {
  if (!olwChatForm) return;
  const t0 = Date.now();
  try {
    const r = await fetch(olwChatForm.href, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: OLLAMA_MODEL,
        messages: [{ role: "user", content: "hi" }],
        stream: false,
        options: { num_predict: 1 },
      }),
    });
    if (r.ok) {
      console.log(`[orchestrator] ollama warmup ${OLLAMA_MODEL} ready in ${Date.now() - t0}ms`);
    } else {
      console.warn(`[orchestrator] ollama warmup http ${r.status}`);
    }
  } catch (err) {
    console.warn(`[orchestrator] ollama warmup error: ${err.message}`);
  }
}

// 展開 W3C URI Template Form-style query `{?var1,var2}`
function expandUriTemplate(href, vars) {
  return href.replace(/\{\?([^}]+)\}/g, (_, group) => {
    const pairs = group
      .split(",")
      .filter((n) => vars[n] !== undefined && vars[n] !== null)
      .map((n) => `${n}=${encodeURIComponent(vars[n])}`);
    return pairs.length ? `?${pairs.join("&")}` : "";
  });
}

function buildMegaUserPrompt(contextRows, speakerName, utterance) {
  const parts = [];
  if (contextRows.length > 0) {
    // SQL 撈出來是 DESC（最新到最舊），反轉成正序給模型
    const lines = contextRows
      .slice()
      .reverse()
      .map((r) => `- ${r.speaker_name ?? "Unknown"}: ${r.en_text}`)
      .join("\n");
    parts.push(`Prior context:\n${lines}\n`);
  }
  parts.push(`New utterance from ${speakerName}: "${utterance}"`);
  parts.push("\nOutput JSON:");
  return parts.join("\n");
}

// STT 走 raw fetch（W3C TD 仍為合約：href / contentType 從 TD form 抽）。
// 原因：node-wot 0.9 對 InteractionInput 為 Buffer 之 binary 內容走 JSON codec 序列化，
// 送出去 body 為 JSON 字串而非 OggOpus binary，STT 端 ffmpeg 解碼會拋 CRC mismatch + EOF。
// Translator / Ollama input 為 JSON、不受此問題影響，仍走 wot.invokeAction。
async function callStt(audioBuffer) {
  const t0 = Date.now();
  const url = expandUriTemplate(sttForm.href, { language: STT_LANGUAGE });
  const r = await fetch(url, {
    method: "POST",
    body: audioBuffer,
    headers: {
      "Content-Type": sttForm.contentType ?? "application/octet-stream",
    },
  });
  if (!r.ok) {
    const text = (await r.text()).slice(0, 200);
    throw new Error(`stt http ${r.status}: ${text}`);
  }
  const data = await r.json();
  return {
    text: data?.text ?? "",
    language: data?.language,
    ms: Date.now() - t0,
  };
}

async function callTranslate(text) {
  const t0 = Date.now();
  const out = await tr.invokeAction("translate", {
    text,
    source: SRC_NLLB,
    target: TGT_NLLB,
  });
  const data = await out.value();
  return { text: data?.text ?? "", ms: Date.now() - t0 };
}

// Safety net：reason 漏寫時補通用文字、過濾 no-op correction（wrong === correct）。
// 集中在這裡讓 streaming / final 都能複用。
function sanitizeCorrections(rawCorr) {
  if (!Array.isArray(rawCorr)) return [];
  return rawCorr
    .filter((c) => {
      if (!c || typeof c.wrong !== "string" || typeof c.correct !== "string") return false;
      // 過濾 no-op：wrong 與 correct 去掉空白後一樣（模型偶爾會列出無實質差異的條目）
      if (c.wrong.trim() === c.correct.trim()) return false;
      return true;
    })
    .map((c) => ({
      wrong: c.wrong,
      correct: c.correct,
      reason: typeof c.reason === "string" && c.reason.trim().length > 0
        ? c.reason
        : "潤飾調整",
    }));
}

// streaming 版 callOllama：每完成一個 top-level field 就呼叫 onField(name, value)。
// 完成後回傳完整聚合結果（給 DB 寫入用）。
async function callOllama(speakerName, utterance, contextRows, onField) {
  const t0 = Date.now();

  const body = {
    model: OLLAMA_MODEL,
    messages: [
      { role: "system", content: MEGA_SYSTEM_PROMPT },
      {
        role: "user",
        content: buildMegaUserPrompt(contextRows, speakerName, utterance),
      },
    ],
    stream: true,
    format: RESPONSE_SCHEMA,
    // Qwen3 thinking 旗標；Ollama 0.24+ 認 top-level think:false
    think: false,
    options: OLLAMA_OPTIONS,
  };

  const r = await fetch(olwChatForm.href, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" },
  });
  if (!r.ok) {
    const text = (await r.text()).slice(0, 200);
    throw new Error(`olw http ${r.status}: ${text}`);
  }

  const extractor = makeFieldExtractor();
  const decoder = new TextDecoder("utf-8");
  let ndBuf = "";

  function dispatchNew(newFields) {
    for (const key of Object.keys(newFields)) {
      let value = newFields[key];
      if (key === "grammar_corrections") value = sanitizeCorrections(value);
      onField(key, value);
    }
  }

  for await (const chunk of r.body) {
    ndBuf += decoder.decode(chunk, { stream: true });
    let nl;
    while ((nl = ndBuf.indexOf("\n")) >= 0) {
      const line = ndBuf.slice(0, nl).trim();
      ndBuf = ndBuf.slice(nl + 1);
      if (!line) continue;
      let event;
      try { event = JSON.parse(line); } catch { continue; }
      const piece = event?.message?.content;
      if (typeof piece === "string" && piece.length > 0) {
        const newFields = extractor.feed(piece);
        if (Object.keys(newFields).length > 0) dispatchNew(newFields);
      }
    }
  }

  // 流結束後 final parse 拿完整結果（給 DB 寫；streaming 中每個欄位完成已 emit 過）。
  // closing `}` 也會觸發 extractor.feed 補 emit 最後一個欄位，這裡只是雙保險。
  const finalParsed = extractor.finalize() ?? {};
  const corrections = sanitizeCorrections(finalParsed.grammar_corrections);

  return {
    recast: finalParsed.recast ?? "",
    replies: Array.isArray(finalParsed.suggested_replies) ? finalParsed.suggested_replies : [],
    corrections,
    hints: Array.isArray(finalParsed.keyword_hints) ? finalParsed.keyword_hints : [],
    ms: Date.now() - t0,
  };
}

// audio 為一段 self-contained 的 Opus binary（RPi 為 OggOpus、手機瀏覽器多為 webm/opus）。
// 兩種來源（RPi Mic Thing event / 手機 magic link HTTP 上傳）匯流到這條同一條後端流水線；
// mic_id 對手機為 `phone:{attendee_id}`，僅用於 log 與 speaker_mic_id。
export async function handleUtterance(mic_id, ctx, audio) {
  const ts = Date.now();
  if (!audio || audio.byteLength === 0) {
    console.warn(`[orch] empty audio mic=${mic_id}, skip`);
    return;
  }

  // 1. 先跑 STT，沒文字就 silently drop（不 INSERT、不 broadcast）
  let text, sttLang, sttMs;
  try {
    const r = await callStt(audio);
    text = (r.text ?? "").trim();
    sttLang = r.language;
    sttMs = r.ms;
  } catch (err) {
    console.error(`[orch] stt error mic=${mic_id}:`, err.message);
    return;
  }
  if (!text) {
    // Whisper 反幻覺過濾後回空字串（純噪音）時不留 DB、不發 SSE
    console.log(`[orch] empty stt mic=${mic_id} ${audio.byteLength}B, drop`);
    return;
  }

  // 2. INSERT utterance + 寫 en_text/stt_ms，拿 uttId
  const info = stmt.insertUtterance.run(ctx.room_id, ts, null, ctx.attendee_id);
  const uttId = Number(info.lastInsertRowid);
  stmt.updateUtteranceStt.run(text, sttMs, uttId);
  console.log(
    `[orch] U${uttId} mic=${mic_id} speaker=${ctx.name} stt ${sttMs}ms: "${text}"`
  );

  // 3. broadcast：先 utterance_created 讓前端建 row，立即跟 stt 補 en_text
  broadcast(ctx.room_id, "utterance_created", {
    utterance_id: uttId,
    ts,
    speaker_attendee_id: ctx.attendee_id,
    speaker_name: ctx.name,
    speaker_mic_id: mic_id,
  });
  broadcast(ctx.room_id, "stt", {
    utterance_id: uttId,
    en_text: text,
    source_lang: sttLang,
  });

  // 3. 撈 prior context（不含本輪）
  const contextRows = stmt.recentContextByRoomExcluding.all(ctx.room_id, uttId);

  // 4. 並行：Translator + Ollama（Ollama 走 streaming，每完成一個欄位就 broadcast analysis）
  const olwStartTs = Date.now();
  const onOlwField = (key, value) => {
    // streaming: 每個 top-level field 完成就推一個 partial analysis
    const payload = { utterance_id: uttId };
    if (key === "grammar_corrections") payload.grammar_corrections = value;
    else if (key === "suggested_replies") payload.replies = value;
    else if (key === "keyword_hints") payload.keyword_hints = value;
    else if (key === "recast") payload.recast = value;
    broadcast(ctx.room_id, "analysis", payload);
    const dt = Date.now() - olwStartTs;
    console.log(`[orch] U${uttId} olw streaming +${key} @${dt}ms`);
  };
  const [trResult, olwResult] = await Promise.allSettled([
    callTranslate(text),
    callOllama(ctx.name, text, contextRows, onOlwField),
  ]);

  const errors = [];

  if (trResult.status === "fulfilled") {
    stmt.updateUtteranceTranslate.run(
      trResult.value.text,
      trResult.value.ms,
      uttId
    );
    console.log(
      `[orch] U${uttId} tr ${trResult.value.ms}ms: "${trResult.value.text}"`
    );
    broadcast(ctx.room_id, "translation", {
      utterance_id: uttId,
      zh_text: trResult.value.text,
    });
  } else {
    errors.push(`tr: ${trResult.reason.message}`);
    console.error(`[orch] U${uttId} tr error:`, trResult.reason.message);
  }

  if (olwResult.status === "fulfilled") {
    stmt.updateUtteranceOllama.run(
      olwResult.value.recast,
      JSON.stringify(olwResult.value.replies),
      JSON.stringify(olwResult.value.corrections),
      JSON.stringify(olwResult.value.hints),
      olwResult.value.ms,
      uttId
    );
    console.log(
      `[orch] U${uttId} olw ${olwResult.value.ms}ms recast="${olwResult.value.recast}" corrections=${olwResult.value.corrections.length} hints=${olwResult.value.hints.length}`
    );
    // Safety net：streaming parser 若漏 emit 某欄位，最後補一發完整 payload。
    // Display 是 merge mode，重複 emit 同欄位值是冪等的。
    broadcast(ctx.room_id, "analysis", {
      utterance_id: uttId,
      recast: olwResult.value.recast,
      replies: olwResult.value.replies,
      grammar_corrections: olwResult.value.corrections,
      keyword_hints: olwResult.value.hints,
    });
  } else {
    errors.push(`olw: ${olwResult.reason.message}`);
    console.error(`[orch] U${uttId} olw error:`, olwResult.reason.message);
  }

  if (errors.length > 0) {
    stmt.updateUtteranceError.run(errors.join(" | "), uttId);
  }

  const total_ms = Date.now() - ts;
  stmt.updateUtteranceTotal.run(total_ms, uttId);
  console.log(`[orch] U${uttId} total ${total_ms}ms`);
}
