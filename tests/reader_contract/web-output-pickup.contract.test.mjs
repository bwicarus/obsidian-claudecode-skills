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

test("地图卡:静态图是基底,活地图是叠加(渐进增强)", () => {
  const VOICE = read("_server_deploy/static/pdf/rc-voicecall.js");
  // 为什么这条重要:侧栏/钉页/回放等实例不走 _igWire,那里只有静态图。
  // 若把 <img> 换成占位 div,那些实例会变成空框 —— 用户看到的是"图没了"。
  assert.match(VOICE, /class="vc-ig-img"/,
    "静态 <img> 必须始终渲染,不能被占位 div 取代");
  assert.match(VOICE, /_upgradeMapCells\(root\)/,
    "活地图在 _igWire 里叠加上去");
  assert.match(VOICE, /cell\.classList\.add\('vc-map-ready'\)/,
    "就位后才盖住静态图 —— 挂载失败时看到的仍是图");
  // 引擎唯一实现:卡内与全屏共用,否则两处行为会漂移
  const mounts = VOICE.match(/_mountMapView\(/g) || [];
  assert.ok(mounts.length >= 3,
    "卡内与全屏共用同一个 _mountMapView(定义 + 两处调用)");
  assert.match(VOICE, /destroy: function \(\)/,
    "必须能销毁:每张关掉的地图卡都留一对监听会随卡片数量累积");
});

test("地图瓦片尺寸必须钉死 —— 卡片的全局图片样式会把它压出缝", () => {
  const VOICE = read("_server_deploy/static/pdf/rc-voicecall.js");
  // 实测:.vc-card-bd img{max-width:100%;border-radius:6px} 把每张 256px
  // 瓦片压窄并倒角,而定位仍按 256 步进 —— 2×2 时缝隙正好拼成一个十字。
  const at = VOICE.indexOf(".vc-map-tile{");
  assert.ok(at >= 0, "瓦片样式在场");
  const rule = VOICE.slice(at, at + 260);
  assert.match(rule, /width:256px!important/, "宽度要压过全局 img 规则");
  assert.match(rule, /height:256px!important/);
  assert.match(rule, /max-width:none!important/, "全局 max-width 必须被压过");
  assert.match(rule, /border-radius:0!important/, "倒角会让瓦片之间露缝");
});

test("瓦片来源:谷歌经桥代取,凭据不出本机;失败整体退回 OSM", () => {
  const VOICE = read("_server_deploy/static/pdf/rc-voicecall.js");
  assert.match(VOICE, /\/map\/tile\?z=/,
    "设备端 URL 不带 session/key —— 官方瓦片接口两者都要,只能由桥代取");
  // ⚠ 只查真实 URL 用法,别查字面量 —— 代码里的注释正解释了'不要用那个
  // 端点',宽泛的正则会把注释也判成违规(第一次写就撞上了)。
  assert.doesNotMatch(VOICE, /https:\/\/mt[0-9]\.google\.com/,
    "那个到处能搜到的端点是未公开的,用它违反服务条款");
  assert.match(VOICE, /_mapProviderDown/, "挂过一次就整轮不再试,避免抖动");
  assert.match(VOICE, /onProvider/,
    "用谁的图就署谁的名 —— 退回 OSM 还挂着 © Google 是错的");
});
