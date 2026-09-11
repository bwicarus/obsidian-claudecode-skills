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

// ⚠ 「不许出现 X」这类断言必须先砍注释再扫。
//   整文件扫会把**解释"为什么不用 X"的注释**也算成一次出现 ——
//   2026-09-12 就这么红过：文件头写着"不用 startUpdatingLocation()：那是
//   连续轨迹"，而这句话正是在承诺不用它。仓库在 Python 侧早栽过同一跤
//   （test_voice_entry.py 的 _literals），这里补上同一条纪律。
function codeOnly(text) {
  return text
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("//"))
    .filter((line) => !line.trimStart().startsWith("///"))
    .join("\n");
}
const PROVIDER_CODE = codeOnly(PROVIDER);

test("Swift 提供者：开关先行 + 不连续追踪 + 反解节流", () => {
  assert.match(PROVIDER, /guard isEnabled else \{ return \}/,
    "开关关着 refresh 是空操作");
  assert.match(PROVIDER, /requestLocation\(\)/, "一次性定位");
  assert.ok(!PROVIDER_CODE.includes("startUpdatingLocation"),
    "绝不开连续追踪 —— 记录目标是建筑物，不是轨迹");
  assert.match(PROVIDER, /distance\(from: previous\) <= 50/,
    "移动 <=50m 复用缓存地名，不重复反解");
  assert.match(PROVIDER, /kCLLocationAccuracyNearestTenMeters/, "建筑物级精度");
  assert.match(PROVIDER, /mark\.name,/, "地名优先取 POI/建筑名");
});

test("桥：五个无参 location action 且错误响应 switch 覆盖", () => {
  // ⚠ 2026-09-12 从三个变五个：后台档（不开 App 也更新地点）另开了一对开关
  //   动作。它们**必须出现在每一处 switch** —— 漏一处编译照样过，运行时那
  //   一支静默走空；上次 book-identity 就是这么把 CI 挂了的。
  for (const action of [
    "device-location-status", "device-location-enable", "device-location-disable",
    "device-location-bg-enable", "device-location-bg-disable",
  ]) {
    assert.ok(BRIDGE.includes(`"${action}"`), action);
  }
  assert.match(BRIDGE,
    /case \.status, \.bookIdentity, \.locationStatus, \.locationEnable, \.locationDisable,[\s\S]{0,120}?\.locationBackgroundDisable:/,
    "parse 无参组");
  assert.match(BRIDGE,
    /case \.locationStatus, \.locationEnable, \.locationDisable,[\s\S]{0,120}?\.locationBackgroundDisable:\s*\n\s*payload\["enabled"\] = false/,
    "错误响应 switch 覆盖（上次 book-identity 漏这里 CI 挂过）");
});

test("后台档：只用显著位置变化，watching 由设备声明，且有离线队列", () => {
  // ⚠ **不许 startUpdatingLocation** —— 那是连续轨迹，与本文件开头那条
  //   "不连续追踪"的纪律直接抵触，也过不了审。
  assert.match(PROVIDER, /startMonitoringSignificantLocationChanges\(\)/,
    "后台只用显著位置变化");
  assert.ok(!/startUpdatingLocation\(\)/.test(PROVIDER_CODE),
    "不许连续追踪");
  assert.match(PROVIDER, /"watching": true/,
    "报给服务器时声明「我在盯着」—— 判新旧那侧据此分辨「没挪窝」与「不知道」");
  assert.match(PROVIDER, /pendingFixes/,
    "离线补送队列：电脑睡着是常态，而后台唤醒只有一次机会");
});

test("Info.plist：始终权限说明与 location 后台模式都在场", () => {
  assert.match(PLIST, /NSLocationAlwaysAndWhenInUseUsageDescription/);
  assert.match(PLIST, /<string>location<\/string>/, "后台模式");
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

test("runtime 响应字段白名单：五个 location action 都放行后台档那两个字段", () => {
  // ⚠ 这处白名单**卡的是响应**，不是请求 —— 和 exactKeys 那处是两回事。
  //   2026-09-12 的实际事故：Swift 回包多了 background / alwaysAuthorized，
  //   这里不认 → 判整个响应无效 → 抛错 → 面板把「学习地点记录」整节藏掉。
  //   用户看到的是"没有这个选项"，而不是"这个选项坏了"。
  for (const action of [
    "device-location-status", "device-location-enable", "device-location-disable",
    "device-location-bg-enable", "device-location-bg-disable",
  ]) {
    const at = RUNTIME.indexOf(`'${action}': new Set(`);
    assert.notStrictEqual(at, -1, `${action} 没进响应白名单`);
    // 窗口切到**这一条自己的 `])` 为止** —— 固定字数会溢进下一个条目，
    // 于是在邻居那里找到字段、测试假绿（2026-09-12 变异检验当场抓到）。
    const close = RUNTIME.indexOf("])", at);
    assert.notStrictEqual(close, -1, `${action} 的白名单没有结尾`);
    const window = RUNTIME.slice(at, close);
    for (const key of ["enabled", "authorized", "hasFix",
                       "background", "alwaysAuthorized"]) {
      assert.ok(window.includes(`'${key}'`),
        `${action} 的响应白名单少了 ${key}`);
    }
  }
});

test("面板：在 App 里失败要出声，不许整节蒸发", () => {
  // ⚠ 原来只要取状态失败就把标题/开关/说明一起隐藏，一个字都不说，
  //   于是"坏了"和"这个功能不存在"长得一模一样（用户 2026-09-12 报的
  //   「没有你说的学习地点记录选项」就是这么来的）。
  assert.match(SETTINGS, /定位通道没接上/,
    "App 内失败要显示原因，而不是让整节消失");
  assert.match(SETTINGS, /if \(_nativePrefsApi\(\)\) \{/,
    "判据用「有没有原生偏好通道」—— 与「本机」tab 显不显示同一个条件");
});

test("设置面板：开关走本地路由，无桥环境整段隐藏", () => {
  assert.match(SETTINGS, /rcset-nat-loc-on/);
  assert.match(SETTINGS, /fetch\('\/pdf\/api\/device-location-pref'\)/);
  assert.match(SETTINGS, /r\.status === 404/, "404 → 隐藏，不给一个永远无效的开关");
  assert.match(SETTINGS, /系统定位权限未授予/, "开了但没授权要出声");
});
