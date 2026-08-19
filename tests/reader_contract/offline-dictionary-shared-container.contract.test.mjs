import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const DICT = read("ios/BWReader/App/ReaderOfflineDictionary.swift");
const CONTRACT = read("ios/BWReader/Shared/ReaderNativeBridgeContract.swift");

// C 组 #19 第一步（用户 2026-08-18 的边界：只做 App 与 Safari 扩展）。
//
// 扩展打 Pi 的端点里 notes/highlights/phrases 的权威存储**本来就在 Pi**，
// 改问 App 去不掉 Pi。真正能"来自 App"的只有 App 本地就有的东西 —— 其中最高频
// 的是查词（每点一个词一次 /pdf/api/dict-quick）。
//
// 障碍：词典装在 App 私有的 Application Support，而 Safari 的 native handler 跑在
// **扩展进程**里，那条路径在那儿解析出的是扩展自己的目录。共享容器是两个进程都
// 看得见的唯一地方。

test("词典根目录在 App Group 共享容器里", () => {
  assert.match(
    DICT,
    /containerURL\(\s*forSecurityApplicationGroupIdentifier:\s*ReaderNativeBridgeContract\.appGroupIdentifier/,
  );
  // 用的必须是既有的那个 App Group（快照与本机笔记状态早就在用它）
  assert.match(CONTRACT, /appGroupIdentifier = "group\.space\.bwicarus\.bwreader2"/);
  // 不能再回落到 App 私有目录 —— 那样扩展永远读不到
  const body = DICT.slice(
    DICT.indexOf("static func applicationRoot() throws -> URL {"),
    DICT.indexOf("private static func migrateLegacyApplicationRootIfNeeded"),
  );
  assert.ok(body.length > 0);
  assert.doesNotMatch(body, /applicationSupportDirectory/);
});

test("已经装好的词典自动迁移，不让用户重下几百 MB", () => {
  assert.match(DICT, /private static func migrateLegacyApplicationRootIfNeeded\(to target: URL\)/);
  const body = DICT.slice(
    DICT.indexOf("private static func migrateLegacyApplicationRootIfNeeded"),
    DICT.indexOf("static func releaseRoot()"),
  );
  assert.ok(body.length > 0);
  // 只在目标不存在时迁移（幂等）
  assert.match(body, /guard !manager\.fileExists\(atPath: target\.path\) else \{ return \}/);
  // 旧位置就是 App 私有的 Application Support
  assert.match(body, /applicationSupportDirectory/);
  // 用 move 不用 copy：复制会在迁移期间占双份空间
  assert.match(body, /manager\.moveItem\(at: legacy, to: target\)/);
  assert.doesNotMatch(body, /copyItem/);
});

test("迁移失败留痕且不删任何东西", () => {
  const body = DICT.slice(
    DICT.indexOf("private static func migrateLegacyApplicationRootIfNeeded"),
    DICT.indexOf("static func releaseRoot()"),
  );
  // 迁移出错时宁可多占一份空间，也不能把用户下好的词典弄丢
  assert.doesNotMatch(body, /removeItem|removeAll/);
  // 静默失败的话，"我明明下过"就成了一桩无法解释的怪事
  assert.match(body, /NSLog\("\[bw-dict\] 词典迁移到共享容器失败/);
});
