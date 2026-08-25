// 活动账本「地点」维度（activity-ledger-design §3.4，用户 2026-08-25 拍板：
// 使用期间权限、建筑物级、带地名）。
//
// 纪律合同：
// - 开关先行：默认关；Swift 只在 enabled 才申请权限/定位。
// - 位置走全局变量注入（beacon flush 是同步的，等不了 Promise）。
// - 采集不可重来：坐标与地名两者都存。
// - Pi 接收端是重建式处理器：loc 必须显式校验+搬运（client 的教训）。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (relative) => readFileSync(join(root, relative), "utf8");

const PROVIDER = read("ios/BWReader/App/ReaderLocationProvider.swift");
const BRIDGE = read("ios/BWReader/App/NativeBookOCRBridge.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const PLIST = read("ios/BWReader/App/Info.plist");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const DWELL = read("_server_deploy/static/pdf/reader.src/30-dwell.js");
const RECEIVER = read("_server_deploy/pdf_reader.py");
const SETTINGS = read("_server_deploy/static/pdf/rc-settings.js");
const MANIFEST = read("ios/BWReader/native_reader_interface_manifest.json");

test("Swift 提供者：开关先行 + 不连续追踪 + 反解节流", () => {
  assert.match(PROVIDER, /guard isEnabled else \{ return \}/,
    "开关关着 refresh 是空操作");
  assert.match(PROVIDER, /requestLocation\(\)/, "一次性定位");
  assert.ok(!PROVIDER.includes("startUpdatingLocation"),
    "绝不开连续追踪 —— 记录目标是建筑物，不是轨迹");
  assert.match(PROVIDER, /distance\(from: previous\) <= 50/,
    "移动 <=50m 复用缓存地名，不重复反解");
  assert.match(PROVIDER, /kCLLocationAccuracyNearestTenMeters/, "建筑物级精度");
  assert.match(PROVIDER, /mark\.name,/, "地名优先取 POI/建筑名");
});

test("桥：三个无参 location action 且错误响应 switch 覆盖", () => {
  for (const action of [
    "device-location-status", "device-location-enable", "device-location-disable",
  ]) {
    assert.ok(BRIDGE.includes(`"${action}"`), action);
  }
  assert.match(BRIDGE,
    /case \.status, \.bookIdentity, \.locationStatus, \.locationEnable, \.locationDisable:/,
    "parse 无参组");
  assert.match(BRIDGE, /case \.locationStatus, \.locationEnable, \.locationDisable:\s*\n\s*payload\["enabled"\] = false/,
    "错误响应 switch 覆盖（上次 book-identity 漏这里 CI 挂过）");
});

test("WebView：位置经全局变量推进页面，前台刷新一次", () => {
  assert.match(WEBVIEW, /window\.__BW_DEVICE_LOCATION__ = \\\(json\)/,
    "推全局变量 —— beacon flush 同步可取");
  assert.match(WEBVIEW, /ReaderLocationProvider\.shared\.refresh\(\)/,
    "进前台取一次");
});

test("Info.plist：使用期间权限用途描述在场且说清数据去向", () => {
  assert.match(PLIST, /NSLocationWhenInUseUsageDescription/);
  assert.match(PLIST, /只保存在你自己的服务器/);
});

test("runtime 开关路由：本地执行、桥缺席 404、manifest 已登记", () => {
  assert.match(RUNTIME, /\/pdf\/api\/device-location-pref/);
  assert.match(RUNTIME, /BW_LOCAL_DEVICE_LOCATION_UNAVAILABLE/,
    "无桥环境 404，面板据此隐藏");
  assert.match(MANIFEST, /"path": "\/pdf\/api\/device-location-pref",\s*\n\s*"match": "exact",\s*\n\s*"owner": "local"/,
    "manifest owner=local");
});

test("dwell flush：只带新鲜位置，形状白名单重建", () => {
  const at = DWELL.indexOf("window.__BW_DEVICE_LOCATION__");
  assert.ok(at >= 0);
  const body = DWELL.slice(at - 600, at + 800);
  assert.match(body, /< 1800/, "超过 30 分钟的位置不带");
  assert.match(body, /Number\.isFinite\(loc\.lat\)/, "形状校验后重建");
  assert.match(body, /loc\.name\.slice\(0, 80\)/, "地名限长");
});

test("Pi 接收端：loc 显式校验+搬运（重建式处理器纪律）", () => {
  const at = RECEIVER.indexOf('_raw_loc = b.get("loc")');
  assert.ok(at >= 0, "接收端读 loc");
  const body = RECEIVER.slice(at - 200, at + 1200);
  assert.match(body, /-90 <= _lat <= 90 and -180 <= _lon <= 180/);
  assert.match(body, /isprintable/, "地名剔除控制字符");
  assert.match(RECEIVER, /rec\["loc"\] = _loc/, "真的搬进每条记录");
});

test("设置面板：开关走本地路由，无桥环境整段隐藏", () => {
  assert.match(SETTINGS, /rcset-nat-loc-on/);
  assert.match(SETTINGS, /fetch\('\/pdf\/api\/device-location-pref'\)/);
  assert.match(SETTINGS, /r\.status === 404/, "404 → 隐藏，不给一个永远无效的开关");
  assert.match(SETTINGS, /系统定位权限未授予/, "开了但没授权要出声");
});
