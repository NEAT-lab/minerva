// 全域 Servient：FS 啟動時 start 一次，供 rooms / orchestrator 等模組 import 用。
// node-wot 套件為 CommonJS，ESM 需走 default import + 解構。

import wotCore from "@node-wot/core";
import wotHttp from "@node-wot/binding-http";
import wotMqtt from "@node-wot/binding-mqtt";
import { BROKER_URL } from "./config.js";

const { Servient } = wotCore;
const { HttpClientFactory } = wotHttp;
const { MqttClientFactory } = wotMqtt;

export const servient = new Servient();
servient.addClientFactory(new HttpClientFactory());
servient.addClientFactory(new MqttClientFactory({ broker: BROKER_URL }));

export const wot = await servient.start();
