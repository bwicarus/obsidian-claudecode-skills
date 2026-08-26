// 网页表面的实时输出下行(方案 A,2026-08-26):HTTP 取件型 lease。
// AI 端语义与 App 场景逐字一致 —— 同一个 router、同一个 Accept、
// 同一套 normalize/receiver,只是传输从 WSS 推送换成长轮询取件。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (relative) => readFileSync(join(root, relative), "utf8");

const PICKUP = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderHttpPickup.cs");
const SERVER = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectBridgeServer.cs");
const VOICE = read("_server_deploy/static/pdf/rc-computer-voice.js");
const BACKGROUND = read("extensions/bw-reader-webext/background.js");

test("HTTP 取件 lease 接在同一个 router 上,回执走同一个 Accept", () => {
  assert.match(PICKUP, /_router\.Attach\(/, "取件会话即普通 lease");
  assert.match(PICKUP, /realtimeOutput\.Accept\(session\.Lease, ack\)/,
    "回执与 WSS 同一个 Accept —— AI 拿到的是真实送达回执");
  assert.match(PICKUP, /SessionIdleTimeout =\s*\n\s*TimeSpan\.FromSeconds\(60\)/,
    "60s 无心跳收租约");
  assert.match(PICKUP, /_router\.Detach\(session\.Lease\)/,
    "过期会话必须 Detach,否则 router 里留僵尸目标");
});

test("端点 CORS 纪律与 snapshot POST 同款,GET 长轮询有上限", () => {
  // ⚠ 轮询必须 POST 不是风格:扩展特权 fetch 的 GET 不带 Origin 头
  // (POST 一律带),GET 版在真机上 100% origin-refused;而放宽无 Origin
  // 的 GET 会让恶意网页用 no-cors GET 无声排空队列(2026-08-26 实锤)。
  assert.match(SERVER, /"\/reader-output\/pending",\s*\n\s*new\[\] \{ "POST", "OPTIONS" \}/);
  assert.match(SERVER, /"\/reader-output\/receipt",\s*\n\s*new\[\] \{ "POST", "OPTIONS" \}/);
  assert.match(BACKGROUND,
    /fetch\(READER_OUTPUT_PENDING_URL, \{\s*\n\s*method: "POST"/,
    "扩展轮询必须 POST —— GET 不带 Origin 过不了白名单");
  assert.match(SERVER, /Math\.Clamp\(parsed, 0, 30\)/, "wait 有上限");
  assert.match(SERVER, /PrepareOutputCors/, "同一套 origin 允许集");
});

test("页面取件执行复用同一 normalize 与 receiver,失败折成拒绝回执", () => {
  const at = VOICE.indexOf("function executePickedRealtimeOutput");
  assert.ok(at >= 0);
  const body = VOICE.slice(at, at + 2000);
  assert.match(body, /normalizeReaderRealtimeOutput\(rawPayload\)/);
  assert.match(body, /RC\.voicecall && RC\.voicecall\.acceptRealtimeOutput/,
    "与 WSS 路径同一个接收器");
  assert.match(body, /outcome: "rejected"/,
    "normalize 失败折成拒绝回执(带 correlation),不静默吞");
  assert.match(VOICE, /bw-realtime-output-pickup/,
    "扩展消息监听在场(App 内无 chrome.runtime 自然不挂)");
});

test("background 长轮询:绑定驱动、按 source 校验、回执尽力送达", () => {
  assert.match(BACKGROUND, /reader-output\/pending/);
  assert.match(BACKGROUND, /payload\.sourceInstanceId !== sourceInstanceId/,
    "错源事件不投给页面");
  assert.match(BACKGROUND, /identity\.sourceInstanceId !== sourceInstanceId/,
    "绑定失效即停循环 —— 桥端租约随之超时");
  assert.match(BACKGROUND, /readerEnsurePickupLoop\(viewport\.sourceInstanceId/,
    "快照 POST 成功即启动/续期取件");
});

test("取件看门狗:内容脚本每 20s 唤醒 SW 拉活循环(352s 停摆的修复)", () => {
  const CONTENT = read("extensions/bw-reader-webext/content.js");
  assert.match(CONTENT, /BW_READER_PICKUP_KEEPALIVE/,
    "content 侧看门狗在场 —— 页面安静时 SW 被杀后循环才有人拉活");
  assert.match(BACKGROUND,
    /READER_RELAY_MESSAGES = new Set\(\[[^\]]*"BW_READER_PICKUP_KEEPALIVE"/s,
    "keepalive 必须在 relay 白名单里,否则消息被静默丢弃");
  const at = BACKGROUND.indexOf('message.type === "BW_READER_PICKUP_KEEPALIVE"');
  assert.ok(at >= 0, "background 有 keepalive 处理分支");
  const body = BACKGROUND.slice(at, at + 1200);
  assert.match(body, /readerStoredVisualBinding/,
    "SW 冷启后必须从持久化存储恢复绑定 —— 只查内存表等于看门狗白叫");
  assert.match(body, /readerEnsurePickupLoop/);
});
