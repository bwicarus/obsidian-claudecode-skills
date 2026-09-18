// 制卡草稿里**每一张卡**的字段，按卡带 nodeIds（2026-09-19 用户拍板）。
//
// 起因：用户圈两个词想要一组可左右滑的草稿，而 nodeIds 只能**按调用**给，
// 两个词属于两个知识点 → 模型只能拆成两次调用 → 侧栏两张独立草稿。
// 它拆得对，是接口不允许它一次表达。改成按卡给之后：一次投递 = 一个 gid =
// 一组轮播，而 rc-flashcard 的身份逻辑（保存/恢复/入库按组 id + 组内序号）
// 一行都不用动 —— 这正是选它而不是"UI 合并两个组"的理由，后者会让 A 组的
// 记录被套到 B 组的卡上。
//
// ⚠ 这条约定有 5 处站点（`python scripts/contract_sites.py anki-draft-card-fields`），
//   其中**两处是重建对象而不是透传**：放行了却不搬字段，表现是「校验全过就是不生效」。
//   本测试逐处钉住，特别是那两处重建。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
// ⚠ 统一换行：断言比对的是源码文本，而检出可能是 CRLF。
const read = (path) =>
  readFileSync(new URL(path, ROOT), "utf8").replace(/\r\n/g, "\n");

const MCP = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs");
const ENVELOPE = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs");
const INBOUND = read("_server_deploy/static/pdf/rc-computer-voice.js");
const FLASHCARD = read("_server_deploy/static/pdf/rc-flashcard.js");

test("① AI 能看见：两支卡 schema 都收 nodeIds", () => {
  // additionalProperties:false —— 不在 properties 里 AI 根本传不了。
  const cards = MCP.slice(
    MCP.indexOf('properties["cards"] = new JsonObject'),
    MCP.indexOf('properties["nodeIds"] = KjNodeIdsSchema();') >= 0
      ? MCP.length
      : MCP.length);
  const basic = cards.indexOf('["const"] = "basic"');
  const cloze = cards.indexOf('["const"] = "cloze"');
  assert.ok(basic > 0 && cloze > basic, "两支卡 schema 都要在");
  const basicBlock = cards.slice(basic, cloze);
  const clozeBlock = cards.slice(cloze, cloze + 1200);
  // 允许被 WithDescription(...) 包一层 —— 断言的是「这支卡能带 nodeIds」，
  // 不是「必须原样写成某个调用」。写死写法只会在加一句描述时误报。
  assert.match(basicBlock, /\["nodeIds"\][\s\S]{0,80}KjNodeIdsSchema\(/,
    "basic 卡要能带自己的 nodeIds");
  assert.match(clozeBlock, /\["nodeIds"\][\s\S]{0,80}KjNodeIdsSchema\(/,
    "cloze 卡要能带自己的 nodeIds");
});

test("② 跨机信封：全等闸要放行可选的 nodeIds", () => {
  // Exact 是 SetEquals；多一个键直接拒。可选字段必须走 ExactWithOptional。
  // ⚠ 只看**制卡草稿**那段：同文件里 ValidatePageCardReplacementCards 是另一种卡
  //   （页面卡），它照旧全等，不该被这条连坐 —— 第一版我断言了全文件，误报。
  // ⚠ 必须按**定义**切，不能按名字第一次出现切：那个位置是调用点，
  //   两个调用点之间根本没有校验代码，切出来的 13K 字符一条断言都碰不到 ——
  //   第一版就这么切的，测试红着而代码是对的。
  const draft = ENVELOPE.slice(
    ENVELOPE.indexOf("private static void ValidateAnkiDraftCards"),
    ENVELOPE.indexOf("private static void ValidatePageCardReplacementCards"));
  assert.ok(draft.length > 200, "找不到制卡草稿的校验段");
  assert.doesNotMatch(
    draft,
    /Exact\(card, "type", "front", "back"\);/,
    "还留着全等版 = 带 nodeIds 的卡会被整张拒掉");
  assert.match(
    draft,
    /ExactWithOptional\([\s\S]{0,200}"nodeIds"/,
    "要以可选字段的方式放行 nodeIds");
  assert.match(
    draft,
    /ValidateOptionalCardNodeIds\(card\);/,
    "放行之后要真的校验它 —— 给了一个坏 id 比不给更糟");
});

test("③ 阅读器入站闸：放行之后必须**搬**（这处是重建，不是透传）", () => {
  const start = INBOUND.indexOf('"Reader 结果 cards["');
  assert.ok(start > 0, "找不到入站闸");
  const block = INBOUND.slice(start - 400, start + 1400);
  assert.match(block, /"nodeIds"/, "放行表里要有 nodeIds");
  // 只放行不搬 = 校验全过就是不生效。normalized 是逐字段重建的，必须显式搬。
  assert.match(
    block,
    /normalized\.nodeIds\s*=|normalized\["nodeIds"\]\s*=/,
    "重建 normalized 时必须把 nodeIds 搬过去");
});

test("④ 入库参数：每张卡的归属以它自己的 nodeIds 为准", () => {
  // bridgeCard 同样是重建；而 runComputerExport 原来只看整组的 source.kjNodes。
  assert.match(
    FLASHCARD,
    /card\.nodeIds|_nodeIds/,
    "入库路径要认得每张卡自己的 nodeIds");
  const exportStart = FLASHCARD.indexOf("function runComputerExport(");
  assert.ok(exportStart > 0);
  const block = FLASHCARD.slice(exportStart, exportStart + 1200);
  assert.match(
    block,
    /c\.nodeIds|card\.nodeIds|cardNodeIds/,
    "runComputerExport 要优先取这张卡自己的归属，取不到才回落整组");
});

test("⑤ 整组 nodeIds 仍然有效（按卡的是覆盖，不是替代）", () => {
  // 同一知识点的多张卡照旧可以只给一份整组归属 —— 不能因为新增按卡就把它废掉。
  assert.match(MCP, /properties\["nodeIds"\] = KjNodeIdsSchema\(\);/,
    "顶层 nodeIds 仍在");
  const exportStart = FLASHCARD.indexOf("function runComputerExport(");
  const block = FLASHCARD.slice(exportStart, exportStart + 1200);
  assert.match(block, /source\s*&&\s*ctx\.source\.kjNodes|ctx\.source\)\s*&&\s*ctx\.source\.kjNodes|source\.kjNodes/,
    "整组归属仍是回落");
});
