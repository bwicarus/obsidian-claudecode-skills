import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const CHAR_LAYER = read("_server_deploy/static/pdf/reader.src/08-charlayer.js");
const SELECTION = read("_server_deploy/static/pdf/reader.src/13-selection.js");

function section(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(from, -1, `missing ${start}`);
  assert.notEqual(to, -1, `missing ${end}`);
  return source.slice(from, to);
}

const sandbox = vm.createContext({ Map, Math, Set });
vm.runInContext(
  `${section(CHAR_LAYER, "function _selectionUsesBlockFilter", "async function loadCharsAndBindLayer")}
   ${section(SELECTION, "function _charBlockId", "// 找选中范围所在的句子")}
   ${section(SELECTION, "function _lineExpandFromChar", "// 段扩展（三击）")}
   globalThis.readerSelectionContract = {
     map: _mapCharBoxes,
     text: _charsRangeToText,
     blockFilter: _charRangeBlockFilter,
     line: _lineExpandFromChar,
   };`,
  sandbox,
);
const api = sandbox.readerSelectionContract;

function rawChar(c, bk, x, y, extra = {}) {
  const { width = 8, height = 10, ...rest } = extra;
  const value = {
    c,
    w: -1,
    x0: x,
    y0: y,
    x1: x + width,
    y1: y + height,
    ...rest,
  };
  if (bk != null) value.bk = bk;
  return value;
}

test("Apple 每行独立 bk 时横排拖选沿 X 重叠的行块连通", () => {
  // Vision 的 observation/bk 是逐行的；历史 Y/X 排序会形成 A1,B1,C1,A2…。
  const mapped = api.map([
    rawChar("左", 1, 10, 0, { width: 40 }),
    rawChar("中", 2, 110, 0, { width: 40 }),
    rawChar("右", 3, 210, 0, { width: 40 }),
    rawChar("下", 4, 10, 18, { width: 40 }),
    rawChar("央", 5, 110, 18, { width: 40 }),
    rawChar("方", 6, 210, 18, { width: 40 }),
  ], 1, "apple", "apple-vision-structured/2:hash", "exact");

  assert.equal(mapped.map((c) => c.c).join(""), "左中右下央方");
  const onlyLeft = api.blockFilter(mapped, 0, 3);
  assert.deepEqual(
    mapped.filter(onlyLeft).map((c) => c.c),
    ["左", "下"],
  );
  assert.equal(api.text(mapped, 0, 3), "左下");
});

test("内嵌文字层不用 bk 挖掉拖选范围中的字符", () => {
  // PDF item 身份不是语义选区：端点恰好同 bk 时，中间 item 也必须保留。
  const mapped = api.map([
    rawChar("我", 7, 0, 0),
    rawChar("坚", 7, 9, 0),
    rawChar("通", 20, 0, 18),
    rawChar("过", 20, 9, 18),
    rawChar("协", 7, 0, 36),
    rawChar("议", 7, 9, 36),
  ], 1, "embedded", "embedded-v1-test", "estimated");

  const passThrough = api.blockFilter(mapped, 0, 5);
  assert.equal(mapped.every(passThrough), true);
  assert.equal(api.text(mapped, 0, 5), "我坚通过协议");
});

test("Pi vision 普通文字连续选取，Pi manga 仍隔离气泡和分栏", () => {
  const input = [
    rawChar("左", 1, 10, 0, { width: 40 }),
    rawChar("中", 2, 110, 0, { width: 40 }),
    rawChar("右", 3, 210, 0, { width: 40 }),
    rawChar("下", 4, 10, 18, { width: 40 }),
    rawChar("央", 5, 110, 18, { width: 40 }),
    rawChar("方", 6, 210, 18, { width: 40 }),
  ];
  const vision = api.map(input, 1, "pi", "pi-vision/1:digest", "exact");
  const manga = api.map(input, 1, "pi", "pi-manga/1:digest", "estimated");

  assert.equal(vision.every(api.blockFilter(vision, 0, 3)), true);
  assert.equal(api.text(vision, 0, 3), "左中右下");
  assert.deepEqual(
    manga.filter(api.blockFilter(manga, 0, 3)).map((c) => c.c),
    ["左", "下"],
  );
});

test("竖排拖选沿 Y 重叠且 X 间距小的列块连通", () => {
  const chars = [
    { c: "右", bk: 11, w: -1, left: 210, top: 10, width: 10, height: 60 },
    { c: "旁", bk: 20, w: -1, left: 110, top: 10, width: 10, height: 60 },
    { c: "隔", bk: 30, w: -1, left: 10, top: 10, width: 10, height: 60 },
    { c: "次", bk: 12, w: -1, left: 192, top: 10, width: 10, height: 60 },
  ];
  const onlyRight = api.blockFilter(chars, 0, 3);
  assert.deepEqual(chars.filter(onlyRight).map((c) => c.c), ["右", "次"]);
});

test("公式字符按视觉 Y/X 留在正文中段而不按 bk 搬到末尾", () => {
  const mapped = api.map([
    rawChar("前", 1, 10, 0, { width: 40 }),
    rawChar("后", 2, 10, 40, { width: 40 }),
    rawChar("式", 950000, 10, 20, { width: 40, fml: true }),
  ], 1);
  assert.equal(mapped.map((c) => c.c).join(""), "前式后");
  assert.equal(mapped[1].fml, true);
});

test("完全无 bk 的旧字符继续按历史 Y/X reading order 排序并不过滤", () => {
  const mapped = api.map([
    rawChar("后", null, 100, 0),
    rawChar("前", null, 10, 0),
  ], 1);
  assert.equal(mapped.map((c) => c.c).join(""), "前后");
  const passThrough = api.blockFilter(mapped, 0, 1);
  assert.equal(mapped.every(passThrough), true);
});

test("双击同行遇到块变化或明显水平空白就停止", () => {
  const acrossBlock = api.map([
    rawChar("甲", 1, 0, 0),
    rawChar("乙", 1, 9, 0),
    rawChar("丙", 2, 18, 0),
  ], 1);
  assert.deepEqual({ ...api.line(acrossBlock, 0) }, { start: 0, end: 1 });

  const acrossGap = api.map([
    rawChar("甲", 1, 0, 0),
    rawChar("乙", 1, 9, 0),
    rawChar("丙", 1, 50, 0),
  ], 1);
  assert.deepEqual({ ...api.line(acrossGap, 0) }, { start: 0, end: 1 });
});

test("同 bk 精确过滤且不连通端点不会吞掉中间块", () => {
  const interleaved = [
    { c: "本", bk: 20, w: -1, left: 0, top: 0, width: 8, height: 10 },
    { c: "旁", bk: 30, w: -1, left: 9, top: 0, width: 8, height: 10 },
    { c: "文", bk: 20, w: -1, left: 18, top: 0, width: 8, height: 10 },
  ];
  const sameBlock = api.blockFilter(interleaved, 0, 2);
  assert.equal(sameBlock(interleaved[0]), true);
  assert.equal(sameBlock(interleaved[1]), false);
  assert.equal(sameBlock(interleaved[2]), true);
  assert.equal(api.text(interleaved, 0, 2), "本文");

  const disconnected = [
    { c: "甲", bk: 40, w: -1, left: 0, top: 0, width: 30, height: 10 },
    { c: "不", bk: 100, w: -1, left: 100, top: 0, width: 30, height: 10 },
    { c: "乙", bk: 10, w: -1, left: 200, top: 0, width: 30, height: 10 },
  ];
  const endpoints = api.blockFilter(disconnected, 0, 2);
  assert.deepEqual(disconnected.filter(endpoints).map((c) => c.c), ["甲", "乙"]);
});
