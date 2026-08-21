// 高亮矩形要按视觉行分段：选到半行就该在半行处停，不能画成一个方块。
//
// `_charsRangeToRects` 有个 `_sameBk` 捷径（#56）：同一个排版块内直接合并，
// 不做换行判断。它的前提是「一个块 = 一个视觉行」——OCR justified 文本的常见
// 形态，块内字符 top 会因括号/标点抖动，一抖就分段会让下划线在括号处断掉。
//
// 但那个前提不普遍成立。实测《料理师》part1 第 27 页，块 30 一个块装了两行，
// 于是两行被并成一个矩形：用户选到第三行一半，画出来却是覆盖前两行的整块方块。
//
// 修法是「放宽容差」而不是「取消判断」：同块时仍要求两者垂直方向实质重叠。
// 同一行的字互相盖住大半，下一行的字几乎不盖 —— 这个量能同时容忍括号抖动、
// 又挡住换行。两个真实夹具分别钉住这两侧，缺一条就会把另一个 bug 放回来。
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";

const SRC = fs.readFileSync("_server_deploy/static/pdf/reader.js", "utf8");
const FIXTURES = "tests/reader_contract/fixtures";

function fnBody(name) {
  const at = SRC.indexOf(`function ${name}(`);
  assert.ok(at >= 0, `找不到 ${name}`);
  let depth = 0;
  const open = SRC.indexOf("{", at);
  for (let j = open; j < SRC.length; j++) {
    if (SRC[j] === "{") depth++;
    else if (SRC[j] === "}") { depth--; if (!depth) return SRC.slice(at, j + 1); }
  }
  assert.fail(`${name} 括号不匹配`);
}

function rectsFor(fixture) {
  const ctx = { console };
  vm.createContext(ctx);
  for (const n of [
    "_charBlockId", "_charLineKey", "_charLineGeometry", "_charBlockGeometry",
    "_charBlockGap", "_charBlockOverlapRatio",
    "_charBlocksConnected", "_charConnectedBlockPath", "_charSpanBlocks",
    "_charRangeBlockFilter", "_charRangeToVisualRects", "_charsRangeToRects",
  ]) vm.runInContext(fnBody(n), ctx);

  const d = JSON.parse(fs.readFileSync(`${FIXTURES}/${fixture}.json`, "utf8"));
  const chars = d.chars.map((c) => ({
    ...c, sp: false, w: -1,
    _x0: c.left, _y0: c.top, _x1: c.left + c.width, _y1: c.top + c.height,
  }));
  return { rects: ctx._charsRangeToRects(chars, d.s, d.e), data: d };
}

test("一个块装两行时，高亮按行分段而不是画成方块", () => {
  const { rects } = rectsFor("manga-two-lines-one-block");
  assert.equal(rects.length, 3,
    `选区跨三行（末行只到一半），应得 3 个矩形，实得 ${rects.length} 个。` +
    `2 个说明前两行又被并成方块了。`);

  const heights = rects.map(([, y0, , y1]) => y1 - y0);
  const median = [...heights].sort((a, b) => a - b)[1];
  for (const h of heights) {
    assert.ok(h < median * 1.6,
      `有矩形高 ${h.toFixed(0)}，远超单行高 ${median.toFixed(0)} —— 多行被并了`);
  }

  // 末行只选了一半：它必须明显短于前两整行。
  const widths = rects.map(([x0, , x1]) => x1 - x0);
  assert.ok(widths[2] < widths[0] * 0.6,
    `末行宽 ${widths[2].toFixed(0)} 不该接近整行 ${widths[0].toFixed(0)}`);
});

test("单视觉行内的 top 抖动（括号）不得把一行拆开 —— #56 不能回潮", () => {
  const { rects, data } = rectsFor("bracket-jitter-one-line");
  const tops = data.chars.map((c) => c.top);
  const jitter = Math.max(...tops) - Math.min(...tops);
  const charH = data.chars.reduce((s, c) => s + c.height, 0) / data.chars.length;
  assert.ok(jitter > charH * 0.25,
    "夹具前提失效：这一行的 top 抖动已不明显，换一个样例");

  // 实测基线：这一行本来就分成 2 段（x 方向另有间隙，与 #56 无关）。
  // 关键是本次改动不得让它变多 —— 变多就说明抖动又开始拆行了。
  assert.ok(rects.length <= 2,
    `括号抖动行被拆成 ${rects.length} 段（基线 2）——同块容差收得太紧了`);
});
