// 学习卡只有一条改法：本体。页面跟着本体走。（Codex 使用反馈，2026-09-13）
//
// 之前是两套：reader_page_card_edit（改放置，不动已导出的 Anki）和
// reader_learning_card_edit（改本体，默认同步 Anki）。用的人的原话：
// 「所以我才会纠结选哪一个」。用户拍板：直接改本体，页面上的内容跟着变。
//
// 这条链有四处要同时成立，缺一处功能就退回"两套"：
//   ① App 的页面卡片条目带 learning.id（本体的 card_* 批次号）；
//   ② ⟦CARD_START⟧ 标记也带 learning="…"，模型从正文里就能拿到；
//   ③ 桥的单卡校验放行 learning（放行不等于搬 —— 但这条是透传，放行即可）；
//   ④ 页面编辑不再收 cards，错误直接指向本体工具。
//   ⑤ 本体改完，当前页的放置立刻跟上（走已有的 pageCardMutate 事务）。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");
const RCV = read("_server_deploy/static/pdf/rc-computer-voice.js");
const NLR = read("_server_deploy/static/pdf/native-local-runtime.js");
const VENDOR = read("extensions/bw-reader-webext/vendor/rc-computer-voice.js");
const DIR = "extensions/bw-reader-webext/windows/ComputerVoiceAudio/";
const MCP = read(DIR + "ReaderContextMcpServer.cs");
const QUERY = read(DIR + "ReaderQuery.cs");

test("① App 的页面卡片条目带本体 id", () => {
  const at = NLR.indexOf("function nativePageContextCards(");
  const body = NLR.slice(at, at + 6000);
  assert.match(body, /projected\.learning = \{/, "anki 放置要带 learning");
  assert.match(body, /\^card_\[a-f0-9\]\{4,64\}\$/, "只认合法的 card_* 批次号");
  const norm = RCV.slice(RCV.indexOf("function localPageCardRecords("));
  assert.match(norm.slice(0, 5000), /normalized\.learning = \{ id: learning\.id, cards: learning\.cards \}/,
    "规范化那层要把 learning 放过去，否则条目在这里就被剥掉了");
});

test("② 标记里带 learning 属性", () => {
  const at = RCV.indexOf("function localCardMarker(");
  assert.match(RCV.slice(at, at + 1500), /learning="' \+ localContextMarkerAttribute\(card\.learning\.id/);
});

test("③ 桥的单卡校验放行 learning，且形状有校验", () => {
  assert.match(QUERY, /RequireExactFieldsWithOptional\(\s*card,\s*new\[\] \{ "learning" \}/);
  assert.match(QUERY, /Reader 单卡查询 learning 字段无效/);
});

test("④ 页面编辑不收 cards；描述与错误都指向本体", () => {
  const at = MCP.indexOf('["name"] = PageCardEditToolName,');
  const desc = MCP.slice(at, MCP.indexOf('["inputSchema"]', at)).replace(/"\s*\+\s*"/g, "");
  assert.match(desc, /NON-learning card placement/);
  assert.match(desc, /reader_learning_card_edit/);
  assert.doesNotMatch(desc, /strictly typed basic\/cloze/);
  const learn = MCP.indexOf('["name"] = LearningCardEditToolName,');
  const learnDesc = MCP.slice(learn, MCP.indexOf('["inputSchema"]', learn)).replace(/"\s*\+\s*"/g, "");
  assert.match(learnDesc, /This is the ONLY way to edit a learning card/);
  assert.match(learnDesc, /placement of this card on the page[\s\S]*refreshed in the same call/);
});

test("⑤ 本体改完，当前页的放置跟着换", () => {
  const at = RCV.indexOf("function refreshLearningCardPlacements(");
  assert.ok(at > 0, "缺跟随函数");
  const body = RCV.slice(at, at + 3000);
  assert.match(body, /runtime\.pageCardMutate\(/, "要走已有的页面卡片事务，不另造写路径");
  assert.match(body, /replacement: \{ cards: applied\.cards \}/);
  assert.match(body, /localPageCardRecords\(runtime, page\)/, "每次重读列表版本，别用过期的 revision 连改两张");
  assert.match(RCV, /refreshLearningCardPlacements\(applied\)/, "本体编辑成功路径要调用它");
});

test("vendor 副本跟上（扩展不走 nginx）", () => {
  assert.match(VENDOR, /function refreshLearningCardPlacements\(/);
  assert.match(VENDOR, /learning="' \+ localContextMarkerAttribute/);
});

test("快照 brief：只留身份/选区/最近动作，正文整段不发", () => {
  assert.match(MCP, /\["brief"\] = new JsonObject/);
  const trim = MCP.slice(MCP.indexOf("internal static void TrimForModel("));
  assert.match(trim.slice(0, 2500), /"text", "highlightSource", "embeds", "selectionRegions"/);
  assert.match(trim.slice(0, 2500), /briefHint/, "没正文要说明为什么没有，别让模型以为这页是空的");
  assert.match(MCP, /TryReadSnapshotArguments\(arguments, out bool brief\)/);
});
