// 蓝色实时选区、保存后的黄色高亮、翻译/解释取的句子 —— 三者必须框住同一批字。
//
// 选区是一段**字符索引区间**，而索引顺序不等于阅读顺序：竖排漫画一页上，右侧
// 气泡的字符索引可能正好夹在用户选中那一段的首尾之间。所以每条从索引区间还原
// 字符的路径都必须过同一个块过滤，少一处，用户就会看到「选的是这段，涂出来连
// 旁边的对话一起」。
//
// 2026-08-16 用户实测报告：「蓝色选中范围和高亮这一段后实际黄色高亮的范围不同，
// 右侧的对话也被高亮了」。当时 _charsRangeToRects 是三条路径里唯一没过滤的。
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";

const SRC = fs.readFileSync("_server_deploy/static/pdf/reader.js", "utf8");

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

function loadFns(names) {
  const ctx = { console };
  vm.createContext(ctx);
  for (const name of names) vm.runInContext(fnBody(name), ctx);
  return ctx;
}

const FILTER_USERS = [
  ["_charRangeToVisualRects", "实时选区与保存高亮共用的矩形"],
  ["_charsRangeToText", "选中文本"],
  ["_buildSentenceFromSel", "翻译/解释取的句子"],
];

test("每条从索引区间还原字符的路径都调共享块过滤，并且真的用上", () => {
  for (const [name, what] of FILTER_USERS) {
    const body = fnBody(name);
    assert.match(body, /_charRangeBlockFilter\(chars, sIdx, eIdx\)/,
      `${name}（${what}）没过块过滤 —— 会把索引夹在中间的别气泡字符一并收进去`);
    // 算出来不等于用上：只删掉循环里那行 `if (!_inBlk(c)) continue;`，
    // 上面的赋值仍在，光看「有没有调用」是发现不了的。
    assert.match(body, /if \(!_inBlk\(c\)\) continue;/,
      `${name}（${what}）算了过滤却没在循环里应用`);
  }
});

test("保存高亮委托给实时选区的同一横排/竖排几何投影", () => {
  const body = fnBody("_charsRangeToRects");
  // 2026-09-02 选区改字符集合(_charSel.keep)后,保存高亮把同一份集合透传给同一个
  // 投影函数 —— 仍是"同一投影",而且比只传区间更贴近实时选区。
  assert.match(body, /return _charRangeToVisualRects\(chars, sIdx, eIdx, 'point'(, keepSet)?\)/);
});

test("没有人再自己抄一份块过滤（块号 min/max 不是区间）", () => {
  // 块号是不透明身份，数值上夹在中间不代表版面上属于这一段。
  // 曾经 _buildSentenceFromSel 就是这么抄的，注释还写着「跟预览同款过滤」。
  for (const [name] of FILTER_USERS) {
    const body = fnBody(name);
    assert.doesNotMatch(body, /Math\.min\(_?sb?\w*,\s*_?eb?\w*\)/i,
      `${name} 又出现了块号 min/max 式的自制过滤`);
  }
});

// —— 行为验证：竖排两栏，右侧气泡的字符索引夹在左侧那段中间 ——
// 左栏 blk 5：x 100..120，两截（索引 0..4 和 10..14）
// 右侧气泡 blk 9：x 400..420（索引 5..9），与左栏 xGap=280 远超 1.8 字宽，不连通
function makeChars() {
  const chars = [];
  const push = (bk, x, y, ch) => {
    chars.push({
      c: ch, bk, sp: false, w: -1,
      left: x, top: y, width: 20, height: 20,
      _x0: x, _y0: y, _x1: x + 20, _y1: y + 20,
    });
  };
  for (let i = 0; i < 5; i++) push(5, 100, 50 + i * 20, "左");
  for (let i = 0; i < 5; i++) push(9, 400, 50 + i * 20, "右");
  for (let i = 0; i < 5; i++) push(5, 100, 150 + i * 20, "左");
  return chars;
}

const GEOM_FNS = [
  "_charBlockId", "_charLineKey", "_charLineGeometry", "_charBlockGeometry",
  "_charBlockGap", "_charBlockOverlapRatio",
  "_charBlocksConnected", "_charConnectedBlockPath", "_charSpanBlocks",
  "_charRangeBlockFilter", "_charRangeToVisualRects", "_charsRangeToRects",
];

test("drag endpoint fallback stays inside the start block's connected component", () => {
  const ctx = loadFns([
    "_charBlockId", "_charLineKey", "_charLineGeometry", "_charBlockGeometry",
    "_charBlockGap", "_charBlockOverlapRatio",
    "_charBlocksConnected", "_charConnectedBlockPath", "_charSpanBlocks",
    "_selectionEndpointFilter", "_findCharAt",
  ]);
  const chars = [
    { c: "正", bk: 5, sp: false, left: 0, top: 50, width: 20, height: 20 },
    { c: "文", bk: 5, sp: false, left: 20, top: 50, width: 20, height: 20 },
    { c: "行", bk: 5, sp: false, left: 40, top: 50, width: 20, height: 20 },
    { c: "末", bk: 5, sp: false, left: 60, top: 50, width: 20, height: 20 },
    { c: "泡", bk: 9, sp: false, left: 280, top: 50, width: 20, height: 20 },
    { c: "泡", bk: 9, sp: false, left: 280, top: 70, width: 20, height: 20 },
  ];

  assert.equal(ctx._findCharAt(chars, 240, 70), 4,
    "precondition: the historical unbounded fallback prefers the distant bubble");
  assert.equal(ctx._findCharAt(chars, 240, 70, 0), 3,
    "a drag that started in the body must snap to the body's last character, not the bubble");
  assert.equal(ctx._findCharAt(chars, 285, 75, 0), 5,
    "an exact hit inside a disconnected text block must remain selectable");
});

test("索引夹在中间的另一个气泡，不会被画进保存的高亮里", () => {
  const ctx = loadFns(GEOM_FNS);
  const chars = makeChars();
  const rects = ctx._charsRangeToRects(chars, 0, 14);
  assert.ok(rects.length > 0, "左栏本身应该有矩形");
  for (const [x0, , x1] of rects) {
    assert.ok(x1 <= 130, `矩形右边缘 ${x1} 越过左栏（120），说明右侧气泡被涂进来了`);
    assert.ok(x0 >= 90, `矩形左边缘 ${x0} 不在左栏`);
  }
});

test("块过滤本身确实认为这两栏不连通（前提校验）", () => {
  // 上一条若因为几何被改得连通了而「通过」，就成了空断言，这里钉住前提。
  const ctx = loadFns(GEOM_FNS);
  const chars = makeChars();
  const blocks = ctx._charBlockGeometry(chars, 0, 14);
  assert.equal(ctx._charBlocksConnected(blocks.get(5), blocks.get(9)), false,
    "测试前提失效：左栏与右侧气泡被判为连通，需要重新构造夹层几何");
});
