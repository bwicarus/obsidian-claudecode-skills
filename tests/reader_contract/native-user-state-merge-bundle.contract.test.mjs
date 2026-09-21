// 合并规则必须**只有一份**：node 测的那份文件，就是 App 在 JavaScriptCore 里跑的那份。
//
// ⚠ 在 Swift 里照抄一遍规则，等于把「两台设备各改各的怎么合」变成两种答案 ——
// 而这种分歧只在真撞上时才暴露，表现为数据丢失。所以这条闸门盯两件事：
// ① 打包器把源文件**原样**烤进包并在校验时逐字比对；
// ② Swift 那侧只负责跑它，不含任何合并判据。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const PACKAGER = read("ios/BWReader/package_local_reader.py");
const SWIFT = read("ios/BWReader/App/ReaderUserStateMerge.swift");
const MODULE = read("_server_deploy/static/reader-runtime/user-state-merge.js");

const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|#|\/\*|\*|\/\/\/)/.test(line)).join("\n");

test("① 原样烤进包，且校验时逐字比对", () => {
  assert.match(PACKAGER, /write_bytes\(root, "native\/user-state-merge\.js", native_user_state_merge\(\)/);
  assert.match(PACKAGER, /merge_core\.read_text\(encoding="utf-8"\) != native_user_state_merge\(\)/,
    "包里那份和源文件不一致就该打包失败，不是发一份陈旧副本");
  // 不裁剪、不改写：它本来就是纯函数模块。
  const producer = PACKAGER.slice(PACKAGER.indexOf("def native_user_state_merge()"),
                                 PACKAGER.indexOf("def native_pdf_selection_core()"));
  assert.match(producer, /return text/);
  assert.doesNotMatch(code(producer), /section\(|replace\(/, "不许在打包时动它");
});

test("② Swift 侧只跑它，不含任何合并判据", () => {
  assert.match(SWIFT, /native\/user-state-merge\.js/);
  assert.match(SWIFT, /objectForKeyedSubscript\("mergeDomain"\)/);
  const body = code(SWIFT);
  for (const rule of ["deleted", "rev", "tombstone", "versionOf", "mergeCollection", "mergeStrokeMap"]) {
    assert.ok(!body.includes(rule), "Swift 里出现了合并判据：" + rule);
  }
});

test("③ 拿不到合并器时不许退化成「整域取一边」", () => {
  // 静默丢掉另一台设备的改动是这条链上最贵的失败。init? 失败 → 调用方必须
  // 面对它，所以这里刻意不做单例。
  assert.match(SWIFT, /init\?\(\)/);
  assert.doesNotMatch(code(SWIFT), /static let shared/);
});

test("④ 模块本身不碰存储、不联网、不认识 CloudKit", () => {
  const body = code(MODULE);
  for (const forbidden of ["fetch(", "indexedDB", "localStorage", "CKRecord", "CloudKit", "XMLHttpRequest"]) {
    assert.ok(!body.includes(forbidden), "合并模块里出现了 " + forbidden);
  }
  assert.match(MODULE, /user-state-merge\/1/, "契约号在，换形状时好定位");
});

test("⑤ 规范化 JSON 的键序不能变 —— 否则每次同步都以为域改过了", () => {
  assert.match(SWIFT, /options: \[\.sortedKeys, \.fragmentsAllowed\]/);
  // 模块里那份 canonical 也是键排序后序列化（与 native-local-runtime 同一套）。
  assert.match(MODULE, /var keys = Object\.keys\(value\)\.sort\(\)/);
});
