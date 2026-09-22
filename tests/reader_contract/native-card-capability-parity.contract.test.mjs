// 页卡能力对齐：网页那侧每加一个放置动作，原生就必须认它。
//
// ⚠ 这条测试存在的理由，是 2026-09-22 反复发生的同一件事：用户一条条报
// "这个也没有、那个也没有"（形态、长按选中、拖边缘删除/收藏、换色…），
// 而我每次只补被点名的那一个。根因不是哪一处写错，是**没有地方能回答
// "一共差几处"** —— 跟 CLAUDE.md 里 contract_sites 那段是同一个病。
//
// 所以这里把"网页有几个动作"变成可检测的：加了动作却没在原生注册 → 红。
// 确实不打算做的，写进下面的 DELIBERATE 并给出理由，而不是默默不接。

import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { test } from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");
const STICKY = read("_server_deploy/static/pdf/rc-stickynote.js");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
const CARDS = read("ios/BWReader/App/ReaderNativePageCards.swift");

/** 明确不接的动作，必须写清理由 —— 空着不算数。 */
const DELIBERATE = {
  // collapse/expand 是 form 的两个特例；原生统一用 form 那个循环按钮，
  // 但两个键仍然注册着（老调用点还在用），所以这里不需要豁免任何东西。
};

const placementActionBody = () => {
  const start = STICKY.indexOf("async function nativePlacementAction");
  assert.ok(start > 0, "找不到 nativePlacementAction");
  const end = STICKY.indexOf("\n  function ", start + 10);
  return STICKY.slice(start, end > start ? end : undefined);
};

test("网页每个放置动作，原生都注册了对应控件", () => {
  const body = placementActionBody();
  const keys = [...new Set([...body.matchAll(/command\.key === '([a-zA-Z]+)'/g)].map((m) => m[1]))];
  assert.ok(keys.length >= 10, `放置动作只解析出 ${keys.length} 个，正则大概失效了`);

  const batch = [...SCRIPT.matchAll(/for \(const key of \[([^\]]+)\]\)/g)]
    .flatMap((m) => [...m[1].matchAll(/'([a-zA-Z]+)'/g)].map((x) => x[1]));
  const single = [...SCRIPT.matchAll(/controls\.([a-zA-Z]+) = registerAction/g)].map((m) => m[1]);
  const registered = new Set([...batch, ...single]);

  const missing = keys.filter((key) => !registered.has(key) && !(key in DELIBERATE));
  assert.deepEqual(
    missing, [],
    `这些动作网页有、原生没接：${missing.join(", ")}。` +
    "要么在 pagePlacements 里注册，要么写进 DELIBERATE 并说明理由。",
  );
});

test("原生只会调它真的注册过的控件", () => {
  // 反向：原生 UI 里写死的 controls["x"] 必须真有人注册，否则就是个坏按钮
  // （点下去只会得到"这张卡没有可用的操作"——比没有更糟）。
  const used = [...new Set([...CARDS.matchAll(/item\.controls\["([a-zA-Z]+)"\]/g)].map((m) => m[1]))];
  assert.ok(used.length > 0, "没解析到原生用了哪些控件，正则大概失效了");
  const batch = [...SCRIPT.matchAll(/for \(const key of \[([^\]]+)\]\)/g)]
    .flatMap((m) => [...m[1].matchAll(/'([a-zA-Z]+)'/g)].map((x) => x[1]));
  const single = [...SCRIPT.matchAll(/controls\.([a-zA-Z]+) = registerAction/g)].map((m) => m[1]);
  const registered = new Set([...batch, ...single]);
  const unknown = used.filter((key) => !registered.has(key));
  assert.deepEqual(unknown, [], `原生调了没人注册的控件：${unknown.join(", ")}`);
});

test("卡面与形态的那几组取值只有一个来源", () => {
  // 色板：原生不许自己写一张颜色表 —— 抄了就会漂。
  assert.match(STICKY, /palette: COLORS\.map/);
  assert.doesNotMatch(
    CARDS.split(String.fromCharCode(10)).filter((l) => !/^\s*\/\//.test(l)).join(String.fromCharCode(10)),
    /#fff8c5|#cfe3ff|#d5f2d9|#ffd9e8|#2d3440|#1f3a2e/,
    "原生抄了一份色板；色板只能来自 rc-stickynote 的 COLORS",
  );
  // 形态裁剪：钉住的卡不进长条态，两侧必须是同一条规则。
  assert.match(STICKY, /if \(next === 'min' && wordBindOf\(note\)\) next = 'full'/);
  assert.match(CARDS, /case "dot": return item\.pinned \? "full" : "min"/);
});
