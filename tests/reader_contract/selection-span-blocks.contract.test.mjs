// 拖选跨越同段多行时，中间的行不能被丢掉。
//
// 这些块几何是从《料理师》part1 第 26 页 rawdict 实测导出的。那本书每一行
// 单独成块，用户选中五行的一段，画出来只剩零散几截，「已选」预览也串成了
// 「食心配中が毒ありの」——中间的字被整块过滤掉后拼出来的。
//
// 原实现用 BFS 求一条从起点块到终点块的**路径**，而用户拖出的是一个连续的
// **区间**。BFS 一找到通路就停：这一页上 17 与 20 的行距恰好在连通阈值内，
// 于是路径就是 17→20，中间的 18、19 整行被丢弃。
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";

const SRC = fs.readFileSync("_server_deploy/static/pdf/reader.js", "utf8");

function loadFn(names) {
  const ctx = { console };
  vm.createContext(ctx);
  for (const name of names) {
    const at = SRC.indexOf(`function ${name}(`);
    assert.ok(at >= 0, `找不到 ${name}`);
    let depth = 0;
    const open = SRC.indexOf("{", at);
    let end = -1;
    for (let j = open; j < SRC.length; j++) {
      if (SRC[j] === "{") depth++;
      else if (SRC[j] === "}") { depth--; if (!depth) { end = j + 1; break; } }
    }
    assert.ok(end > 0, `${name} 括号不匹配`);
    vm.runInContext(SRC.slice(at, end), ctx);
  }
  return ctx;
}

// 实测几何（第 26 页）：17-20 是正文那一段的四行，15/16 是页面别处的块
const REAL_BLOCKS = {
  "15": {
    "id": 15,
    "left": 1271.616943359375,
    "right": 1484.279052734375,
    "top": 688.086181640625,
    "bottom": 745.9589233398438,
    "charHeight": 47.81940714518229,
    "charWidth": 39.8494873046875,
    "axis": "horizontal"
  },
  "16": {
    "id": 16,
    "left": 69.5511703491211,
    "right": 106.68616485595703,
    "top": 2350.064208984375,
    "bottom": 2375.2998046875,
    "charHeight": 24.619873046875,
    "charWidth": 20.516571044921875,
    "axis": "horizontal"
  },
  "17": {
    "id": 17,
    "left": 473.3172607421875,
    "right": 918.9213256835938,
    "top": 1333.26611328125,
    "bottom": 1389.118896484375,
    "charHeight": 44.30228969029018,
    "charWidth": 36.91855948311942,
    "axis": "horizontal"
  },
  "18": {
    "id": 18,
    "left": 473.9327392578125,
    "right": 921.1939697265625,
    "top": 1376.9464111328125,
    "bottom": 1432.1678466796875,
    "charHeight": 41.596775599888396,
    "charWidth": 34.66398184640067,
    "axis": "horizontal"
  },
  "19": {
    "id": 19,
    "left": 475.77923583984375,
    "right": 921.7305908203125,
    "top": 1425.2430419921875,
    "bottom": 1474.5618896484375,
    "charHeight": 40.2124755859375,
    "charWidth": 33.51040242513021,
    "axis": "horizontal"
  },
  "20": {
    "id": 20,
    "left": 474.5482482910156,
    "right": 919.978759765625,
    "top": 1468.5872802734375,
    "bottom": 1559.83935546875,
    "charHeight": 37.15077311197917,
    "charWidth": 30.95897928873698,
    "axis": "horizontal"
  }
};

test("同段多行拖选：中间的行必须都在选中范围内", () => {
  const ctx = loadFn([
    "_charBlockOverlapRatio", "_charBlockGap", "_charBlocksConnected",
    "_charConnectedBlockPath", "_charSpanBlocks",
  ]);
  const blocks = new Map(Object.values(REAL_BLOCKS).map((b) => [b.id, b]));

  const span = ctx._charSpanBlocks(blocks, 17, 20);
  for (const id of [17, 18, 19, 20]) {
    assert.ok(span.has(id), `段内第 ${id} 块丢失——用户会看到高亮缺行`);
  }

  // 这一条记录 bug 本身：旧的路径语义在同一份数据上确实丢掉中间行。
  const path = ctx._charConnectedBlockPath(blocks, 17, 20);
  assert.ok(path && !path.has(18),
    "若这里不再丢 18，说明连通阈值变了，本测试的前提需要重新确认");
});

test("旁边气泡/别处的块不因为区间语义被吞进来", () => {
  const ctx = loadFn([
    "_charBlockOverlapRatio", "_charBlockGap", "_charBlocksConnected",
    "_charSpanBlocks",
  ]);
  const blocks = new Map(Object.values(REAL_BLOCKS).map((b) => [b.id, b]));
  const span = ctx._charSpanBlocks(blocks, 17, 20);
  for (const id of [15, 16]) {
    assert.ok(!span.has(id), `第 ${id} 块与本段不连通，不该被选中`);
  }
});

test("选区过滤器真的用区间语义，而不是只有那个函数存在", () => {
  // 前一版测试只验证 _charSpanBlocks 自己算得对，却没验证选区**用不用**它。
  // 把调用点换回 _charConnectedBlockPath，测试照样全绿——那是个永远为真的
  // 断言。这一条钉住调用点本身。
  const at = SRC.indexOf("function _charRangeBlockFilter(");
  assert.ok(at >= 0, "找不到 _charRangeBlockFilter");
  const body = SRC.slice(at, at + 1600);
  assert.match(body, /_charSpanBlocks\(blocks, sb, eb\)/,
    "跨块过滤必须走区间语义；路径语义会丢掉段落中间的行");
  assert.doesNotMatch(body, /_charConnectedBlockPath\(blocks, sb, eb\)/,
    "路径语义不得回潮");
});
