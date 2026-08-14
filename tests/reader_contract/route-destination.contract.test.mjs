// 每条 Reader 路由该打给谁。
//
// 分错边的后果不是"慢一点"，是**数据写进另一份存储**：扩展把高亮写去 Pi，
// App 的那份在 IndexedDB —— 用户划的线换个入口就不见了，而且要等他自己发现。
// 所以这张表的每一项都该是有人做过的决定，不是前缀匹配的副产物。
import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const RD = require("../../extensions/bw-reader-webext/src/route-destination.js");

const MANIFEST = JSON.parse(readFileSync(
  new URL("../../ios/BWReader/native_reader_interface_manifest.json",
    import.meta.url), "utf8"));

test("App 已有实现的路由打 App，不打 Pi", () => {
  for (const path of [
    "/pdf/api/highlights", "/pdf/api/epub-highlights", "/pdf/api/notes",
    "/pdf/api/note-composite", "/pdf/api/reading-pos", "/pdf/api/userpages",
    "/pdf/api/video-player-prefs", "/pdf/api/toc", "/pdf/api/to-note",
    "/pdf/api/img-proxy",
  ]) {
    assert.equal(RD.destinationFor(path), RD.DESTINATION.APP, path);
  }
});

test("需要服务端资源的仍打 Pi", () => {
  for (const path of [
    "/pdf/api/dict", "/pdf/api/translate", "/pdf/api/explain",
    "/pdf/api/epub-assistant", "/pdf/api/page-nodes", "/pdf/api/vocab-list",
  ]) {
    assert.equal(RD.destinationFor(path), RD.DESTINATION.PI, path);
  }
});

test("未列举的默认走 Pi，不猜 App", () => {
  // 保守方向的选择：猜成 App 会让请求打到一个可能没实现该路由的本机服务，
  // 表现为莫名其妙的 404；猜成 Pi 至多是维持现状。
  for (const path of [
    "/pdf/api/brand-new-route", "/pdf/api/", "", "/unrelated",
  ]) {
    assert.equal(RD.destinationFor(path), RD.DESTINATION.PI, path);
  }
});

test("每条判给 App 的路由，manifest 里确实是 App 自己实现的", () => {
  // 这是防漂移的关键一条：把一条 Pi-owned 路由误判给 App，请求会打到
  // 本机服务上，而它并没有这条实现 —— 且失败长得像"App 坏了"。
  const owner = new Map();
  for (const route of MANIFEST.routes) {
    owner.set(route.path, route.owner);
    if (route.path.startsWith("/pdf")) owner.set(route.path.slice(4), route.owner);
  }
  for (const path of RD.APP_OWNED_PATHS) {
    const value = owner.get(path) ?? owner.get(path.replace("/pdf", ""));
    assert.ok(
      value === "local" || value === "native",
      `${path} 在 manifest 里 owner=${value}，不是 App 实现，不该判给 App`,
    );
  }
});

test("留在服务端的路由都写明了原因", () => {
  // 不写原因的话，每隔一段时间就会有人重新问一遍"这个能不能也搬过去"，
  // 然后重新调查一次。
  for (const [path, reason] of Object.entries(RD.SERVER_ONLY_REASONS)) {
    assert.ok(reason.trim().length > 0, `${path} 缺少留在服务端的原因`);
    assert.equal(RD.destinationFor(path), RD.DESTINATION.PI, path);
  }
});

test("不做前缀匹配", () => {
  // 前缀匹配会让新增路由默默落进某一边，而落错边要等用户发现数据不见了。
  const source = readFileSync(new URL(
    "../../extensions/bw-reader-webext/src/route-destination.js",
    import.meta.url), "utf8");
  assert.doesNotMatch(source, /startsWith\(|\.test\(|RegExp/,
    "必须是显式白名单：新路由应当强制有人做一次决定");
});

test("App 名单与服务端名单不重叠", () => {
  for (const path of RD.APP_OWNED_PATHS) {
    assert.ok(!(path in RD.SERVER_ONLY_REASONS),
      `${path} 同时出现在两张表里，无法判断该打给谁`);
  }
});
