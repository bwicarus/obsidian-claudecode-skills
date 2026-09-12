// 上下页各留半页，表格不腰斩（用户 2026-09-12）。
//
// 原来是固定 700 字硬切。固定值跟页面密度无关：文字密的页 700 字才半页，
// 图多的页 700 字能装两页 —— 用户看到的就是"上下页几乎整页都塞进来了"。
//
// 这里把函数从源码里抠出来真跑，因为规则全是长度与边界的算术，
// 钉字面量拦不住"改了阈值但没改行为"。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const SOURCE = read("_server_deploy/static/pdf/rc-voicecall.js");
const VENDOR = read("extensions/bw-reader-webext/vendor/rc-voicecall.js");

/// 把 `_NB_MIN` 那三个函数连同常量一起抠出来求值。
function loadSlicer(source) {
  const start = source.indexOf("  var _NB_MIN =");
  assert.ok(start >= 0, "找不到 _NB_MIN");
  const end = source.indexOf("async function _nativeRealtimePageContext", start);
  assert.ok(end > start, "找不到结束锚点");
  const body = source.slice(start, end);
  // eslint-disable-next-line no-new-func
  return new Function(`${body}; return { _nbSlice, _nbTabular, _NB_MAX };`)();
}

/// 造一页普通正文：`lines` 行，每行 `width` 个字。
function page(lines, width = 40) {
  return Array.from({ length: lines }, (_, i) =>
    String(i).padStart(2, "0") + "あ".repeat(width - 2)).join("\n");
}

test("邻页按当前页的一半给，而不是固定 700", () => {
  const { _nbSlice } = loadSlicer(SOURCE);
  const neighbour = page(60);            // 2400 字，远超任何上限
  const dense = _nbSlice(neighbour, 1200, false);   // 当前页 1200 → 想要 600
  const sparse = _nbSlice(neighbour, 600, false);   // 当前页 600  → 想要 300
  assert.ok(dense.length > sparse.length,
    `密页应当给得多：dense=${dense.length} sparse=${sparse.length}`);
  assert.ok(sparse.length < 400, "稀疏页不该还给到 600+：" + sparse.length);
  assert.ok(dense.length <= 760, "上限仍然是 700 那一档：" + dense.length);
});

test("再密的页也不超过上限，再稀疏的页也留得下一点", () => {
  const { _nbSlice, _NB_MAX } = loadSlicer(SOURCE);
  const neighbour = page(60);
  const huge = _nbSlice(neighbour, 100000, false);
  assert.ok(huge.length <= _NB_MAX + 60, "超上限了：" + huge.length);
  const tiny = _nbSlice(neighbour, 10, false);
  assert.ok(tiny.length >= 150, "太吝啬了：" + tiny.length);
});

test("切在行边界上，不会把一行劈成两半", () => {
  const { _nbSlice } = loadSlicer(SOURCE);
  const neighbour = page(60);
  const out = _nbSlice(neighbour, 1000, false).replace(/…$/, "");
  for (const line of out.split("\n")) {
    assert.ok(line.length === 40 || line.length === 0,
      "出现了半行：" + JSON.stringify(line));
  }
});

test("上一页取尾巴、下一页取开头", () => {
  const { _nbSlice } = loadSlicer(SOURCE);
  const neighbour = page(60);
  const tail = _nbSlice(neighbour, 800, true);
  const head = _nbSlice(neighbour, 800, false);
  assert.ok(tail.startsWith("…"), "上一页要标出前面还有：" + tail.slice(0, 8));
  assert.ok(tail.trimEnd().endsWith("あ") && tail.includes("59"),
    "上一页必须包含最后一行");
  assert.ok(head.endsWith("…"), "下一页要标出后面还有");
  assert.ok(head.startsWith("00"), "下一页必须从第一行开始");
});

test("表格不腰斩：切点落在表格里就把这段表格带完", () => {
  const { _nbSlice, _nbTabular } = loadSlicer(SOURCE);
  assert.ok(_nbTabular("項目    区分    値"), "三列应当认成表格行");
  assert.ok(_nbTabular("① 大気汚染 | ② 水質汚濁"), "带竖线的也算");
  assert.ok(!_nbTabular("これは普通の文章であって表ではありません。"),
    "普通句子不该被认成表格");

  // 前面 8 行正文（每行 40 字 = 320 字），紧接着一张 10 行的表。
  // 当前页 800 → 想要 400，切点必然落在表格中间。
  const table = Array.from({ length: 10 }, (_, i) =>
    `行${i}    区分${i}    値${i}`).join("\n");
  const neighbour = page(8) + "\n" + table;
  const out = _nbSlice(neighbour, 800, false);
  for (let i = 0; i < 10; i++) {
    assert.ok(out.includes(`行${i}`), `表格第 ${i} 行被切掉了：\n${out}`);
  }
});

test("vendor 那份跟着更新了——扩展不走 nginx，各带各的副本", () => {
  const { _nbSlice } = loadSlicer(VENDOR);
  const neighbour = page(60);
  assert.ok(_nbSlice(neighbour, 600, false).length < 400,
    "vendor 里还是旧规则");
});
