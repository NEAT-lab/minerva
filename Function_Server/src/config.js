// FS 集中常數。論文小專案不引入 env-var / runtime config——直接改這檔。
// 部署假設：FS 與 EMQX / TDD / STT / Tr / Olw 同台 GPU server。

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// GPU server 的 LAN IP（broker / FS 對外暴露皆用之）。部署換機器只改這一行。
export const LAN_HOST = "LAN_HOST";

// TLS：手機瀏覽器存取麥克風需 secure context。把 mkcert 簽出的憑證放到 certs/，
// FS 就自動走 HTTPS；憑證不存在時退回 HTTP（純後端 demo / 桌機 localhost 不受影響）。
//   mkcert LAN_HOST 後：
//     cp LAN_HOST.pem      Function_Server/certs/cert.pem
//     cp LAN_HOST-key.pem  Function_Server/certs/key.pem
const CERT_DIR = path.resolve(__dirname, "..", "certs");
export const TLS_KEY_PATH = path.join(CERT_DIR, "key.pem");
export const TLS_CERT_PATH = path.join(CERT_DIR, "cert.pem");
export const TLS_ENABLED =
  fs.existsSync(TLS_KEY_PATH) && fs.existsSync(TLS_CERT_PATH);

// 同主機 intra-host 連線（FS 連 TDD）走 loopback 較快、不依賴 LAN_HOST。
export const TDD_URL = "http://localhost:5002";

// Mic TD form 的 broker host 必須對齊；換 broker 機器時連 Mic TM 內 {{MQTT_BROKER}} 一起改。
export const BROKER_URL = `mqtt://${LAN_HOST}:1883`;

export const FS_PORT = 5001;
// FS 對外 URL：magic_url 與 admin 頁分享用。
// 內網 LAN 暴露用 LAN_HOST，讓手機掃 QR 能連得到；協定隨 TLS_ENABLED 切換。
export const FS_BASE_URL = `${TLS_ENABLED ? "https" : "http"}://${LAN_HOST}:${FS_PORT}`;

// HTTP /health probe（針對 STT / Translator / Ollama Wrapper）
export const HEALTH_PROBE_INTERVAL_MS = 15_000;
export const HEALTH_PROBE_TIMEOUT_MS = 2_000;
