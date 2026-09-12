// 功能重叠的工具必须互相点名。
//
// 2026-09-13 按「动作 × 对象」把 35 个工具排了一遍。结论跟直觉相反：
// **大部分"重复"是有意的互补，而且描述里已经写了消歧** ——
//   · `reader_page_cards` 自称「fallback semantic index for cards absent
//     from the snapshot」（相对快照内嵌的 ⟦CARD_START⟧ 标记）；
//   · `reader_highlights` 自称"包含那些在正文里锚不上、因而没被内联标出的"；
//   · `reader_web_note` / `reader_make_note` 都点了 `reader_note_create` 的名。
//
// 所以这里守的不是"别重复"，而是**重叠了就要说清楚谁管什么**。合并成一个带
// scope 参数的工具不是改进：那只是把分支从工具名挪进参数，模型照样要选，
// 而且选错时报错更晚。
//
// 唯一两处当时没写消歧的（本测试新增的那两条）：`reader_page_cards` 与
// `reader_learning_cards` —— 名字都叫 cards、动作都是"列出来"，一个是
// **页面上的摆放**、一个是**仓库里的学习实体**。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");
const DIR = "extensions/bw-reader-webext/windows/ComputerVoiceAudio/";
const MCP = read(DIR + "ReaderContextMcpServer.cs");
const HIGHLIGHT_GUIDE = read(DIR + "ReaderCapabilities/highlight.md");
const MATRIX = read(DIR + "ReaderCapabilities/capability-matrix.md");

/// 取一个工具的描述（C# 里是逐段拼接的字面量，先还原成一整串）。
function describe(constant) {
  const at = MCP.indexOf(`["name"] = ${constant},`);
  assert.notStrictEqual(at, -1, `找不到工具 ${constant}`);
  const end = MCP.indexOf('["inputSchema"]', at);
  assert.ok(end > at, `${constant} 后面没有 inputSchema`);
  return MCP.slice(at, end).replace(/"\s*\+\s*"/g, "");
}

const PAIRS = [
  ["WebHighlightToolName", "reader_highlight_range", "网页高亮要指回书内那条"],
  ["WebNoteToolName", "reader_note_create", "网页便签要指回书内那条"],
  ["MakeNoteToolName", "reader_note_create", "存成笔记文件 ≠ 贴便签，要说清"],
  ["CommandToolName", "reader_card", "命令串发卡片要指向专用工具"],
  ["PageCardsToolName", "reader_learning_cards", "页面摆放 ↔ 学习实体"],
  ["LearningCardsToolName", "reader_page_cards", "学习实体 ↔ 页面摆放"],
  ["PageCardsToolName", "reader_page_card_edit", "列出来之后怎么改"],
];

for (const [constant, sibling, why] of PAIRS) {
  test(`${constant} 点名 ${sibling}——${why}`, () => {
    assert.match(
      describe(constant),
      new RegExp(sibling.replace(/_/g, "_")),
      `${constant} 的描述里没提 ${sibling}：重叠了却不说谁管什么，`
        + `模型只能靠猜`,
    );
  });
}

test("reader_highlight_text 是老客户端兼容名，不许出现在推荐里", () => {
  // ⚠ 它有处理分支但**不在 tools/list 里**（源码注释：「Legacy compatibility
  //   only … tools/list intentionally advertises marker ranges instead」）。
  //   2026-09-13 我改 highlight.md 时把它写成了"宿主没有标记表时用这个" ——
  //   推荐一个模型看不见的工具比不写更糟：它会去找，找不到，然后自己另想办法。
  assert.doesNotMatch(
    MCP,
    /\["name"\] = HighlightTextToolName/,
    "它一旦真的进了 tools/list，这条测试和几处指南都要重写",
  );
  assert.match(
    MCP,
    /Legacy compatibility only/,
    "处理分支上那句「只为老客户端保留」是这条纪律的出处，别删",
  );
  const guide = HIGHLIGHT_GUIDE + "\n" + MATRIX;
  assert.doesNotMatch(
    guide,
    /\|\s*`reader_highlight_text`/,
    "指南的表格里不许把它列成一个可选项",
  );
  assert.match(
    HIGHLIGHT_GUIDE,
    /不在\s*`tools\/list`\s*里/,
    "指南要明说它调不到，否则下一个人还会把它写回推荐里",
  );
});
