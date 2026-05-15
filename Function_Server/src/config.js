// FS 集中常數。論文小專案不引入 env-var / runtime config——直接改這檔。
// 部署假設：FS 與 EMQX / TDD / STT / Tr / Olw 同台 GPU server。

// GPU server 的 LAN IP（broker / FS 對外暴露皆用之）。部署換機器只改這一行。
export const LAN_HOST = "LAN_HOST";

// 同主機 intra-host 連線（FS 連 TDD）走 loopback 較快、不依賴 LAN_HOST。
export const TDD_URL = "http://localhost:5002";

// Mic TD form 的 broker host 必須對齊；換 broker 機器時連 Mic TM 內 {{MQTT_BROKER}} 一起改。
export const BROKER_URL = `mqtt://${LAN_HOST}:1883`;

export const FS_PORT = 5001;
// FS 對外 URL：magic_url 與 admin 頁分享用。
// 內網 LAN 暴露用 LAN_HOST，讓手機掃 QR 能連得到。
export const FS_BASE_URL = `http://${LAN_HOST}:${FS_PORT}`;

// HTTP /health probe（針對 STT / Translator / Ollama Wrapper）
export const HEALTH_PROBE_INTERVAL_MS = 15_000;
export const HEALTH_PROBE_TIMEOUT_MS = 2_000;
