// selectedItems:把「选中的文字」和「聚焦的对象」合并成一份给模型看的清单。
//
// 现状核实发现的关键事实:这两个槽位此前互相独立、且**已经可能同时非空**——
// BuildFocus 只在 kind=="text" 时才会覆盖 _selection,聚焦一张卡片不会清掉
// 之前选中的文字。所以合并不需要发明新的"同时多选"前端手势,只是把两份
// 已经存在的信号并到一处。真正的"同时选中好几个同类东西"(比如两条高亮)
// 现在完全没有交互入口,这里也不假装有。
//
// 顺带修了核实时发现的三个真 bug:
//   · EPUB/HTML 的文字聚焦上报缺 page,被 Windows 侧 fail-closed 整条丢弃
//   · 取消一张钉住的卡片会把 focus 整个清空,连带清掉其它还钉着的卡片
//   · Pi 一直在写 page_context.selection,Windows 侧从没读过,服务器托管
//     书籍的选区经这条链路整段丢失
//
// 注:本机只有 .NET runtime 无 SDK,C# 部分是文本校验,不能替代编译。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const CSDIR = join(ROOT, "extensions/bw-reader-webext/windows/ComputerVoiceAudio");
const SNAPSHOT = readFileSync(join(CSDIR, "DirectContextSnapshot.cs"), "utf8");
const MCP = readFileSync(join(CSDIR, "ReaderContextMcpServer.cs"), "utf8");
const EPUB_HTML = readFileSync(
  join(ROOT, "_server_deploy/static/pdf/epub-html.js"), "utf8");
const HTML_READER = readFileSync(
  join(ROOT, "_server_deploy/static/pdf/html-reader.js"), "utf8");
const VOICECALL = readFileSync(
  join(ROOT, "_server_deploy/static/pdf/rc-voicecall.js"), "utf8");

function buildSelectionItemsBody() {
  const start = SNAPSHOT.indexOf("private JsonArray BuildSelectionItems()");
  assert.ok(start > 0, "找不到 BuildSelectionItems");
  const end = SNAPSHOT.indexOf("\n    private void FoldJournal", start);
  assert.ok(end > start);
  return SNAPSHOT.slice(start, end);
}

test("highlight 现在是合法的 focus kind", () => {
  assert.match(SNAPSHOT, /kind is "text" or "image" or "card"\s*\n\s*or "drawing" or "region" or "highlight";/);
});

test("聚焦对象的通用字段里带上了颜色", () => {
  const start = SNAPSHOT.indexOf('"alt",\n            "text",\n            "file",');
  assert.ok(start > 0);
  const body = SNAPSHOT.slice(start, start + 200);
  assert.match(body, /"color",/);
});

test("kind==text 的 focus 不重复计入——它只是 selection 的影子", () => {
  const body = buildSelectionItemsBody();
  assert.match(body, /focusKind != "text"/);
});

test("卡片用批次号 cid 而不是假装能定位到单卡", () => {
  const body = buildSelectionItemsBody();
  assert.match(body, /StringValue\(focusRef\["cid"\]\)/);
  const cidAt = body.indexOf('StringValue(focusRef["cid"])');
  const idAt = body.indexOf('StringValue(focusRef["id"])');
  assert.ok(cidAt > 0 && idAt > cidAt, "cid 必须比 id 优先尝试");
});

test("最多两条——不是开放式多选列表", () => {
  const body = buildSelectionItemsBody();
  // 文字一条 + 聚焦一条,没有循环去累积任意多个 focus。
  assert.equal((body.match(/items\.Add\(/g) || []).length, 2);
});

test("字段真的挂到了顶层快照上", () => {
  assert.match(SNAPSHOT, /\["selectedItems"\] = BuildSelectionItems\(\),/);
});

test("待接收兜底与陈旧标记都带这个字段,不能因为是补丁路径就漏掉", () => {
  // 两个手写构造的 JsonObject(不经过 BuildToolPayload 正常序列化)必须
  // 也带上 selectedItems,否则那两条路径下模型会读到缺字段而不是空数组。
  const pendingAt = MCP.indexOf('["reason"] = "snapshot-not-received",');
  assert.ok(pendingAt > 0);
  assert.match(MCP.slice(pendingAt, pendingAt + 150), /\["selectedItems"\] = new JsonArray\(\),/);

  const staleAt = MCP.indexOf('["reason"] = "active-reading-stale",');
  assert.ok(staleAt > 0);
  assert.match(MCP.slice(staleAt, staleAt + 400), /snapshot\["selectedItems"\] = new JsonArray\(\);/);
});

test("工具描述说明 kind 词汇与 ⟦⟧ 标记不是同一套,且卡片 ref 是批次号", () => {
  assert.match(MCP, /selectedItems merges what the user has selected/);
  assert.match(MCP, /not the same one/);
  assert.match(MCP, /a card's ref is its\s*\n?\s*\/\/?\s*batch id|batch id/);
});

// ── Pi 侧选区曾被整段丢弃 ────────────────────────────────────────────
test("page.context 分支现在真的读取并应用 Pi 报的 selection", () => {
  const start = SNAPSHOT.indexOf('if (contextEvent.Type == "page.context")');
  const end = SNAPSHOT.indexOf('else if (contextEvent.Type == "focus")', start);
  const body = SNAPSHOT.slice(start, end);
  assert.match(body, /pageContext\?\["selection"\]/);
  assert.match(body, /ActiveSelection\(reportedSelection, activeReading\)/);
});

test("Pi 选区同样受 400 字符与控制符约束,不比 WSS 那条路松", () => {
  const start = SNAPSHOT.indexOf('if (contextEvent.Type == "page.context")');
  const end = SNAPSHOT.indexOf('else if (contextEvent.Type == "focus")', start);
  const body = SNAPSHOT.slice(start, end);
  assert.match(body, /Length: > 400/);
  assert.match(body, /Any\(char\.IsControl\)/);
});

test("这一刻没有选区时清空而不是维持上一次的旧值", () => {
  const start = SNAPSHOT.indexOf('if (contextEvent.Type == "page.context")');
  const end = SNAPSHOT.indexOf('else if (contextEvent.Type == "focus")', start);
  const body = SNAPSHOT.slice(start, end);
  assert.match(body, /page-context-no-selection/);
});

// ── 客户端三个真 bug ─────────────────────────────────────────────────
test("EPUB 文字聚焦上报带上 page,不再被 fail-closed 丢弃整条事件", () => {
  const at = EPUB_HTML.indexOf("RC.outgoing.focus('text',");
  assert.ok(at > 0);
  assert.match(EPUB_HTML.slice(at, at + 120), /page:\s*_curTopIdx/);
});

test("HTML 文字聚焦上报带上 page(固定 1,如实反映只有一页,不是瞎编)", () => {
  const at = HTML_READER.indexOf("RC.outgoing.focus('text',");
  assert.ok(at > 0);
  assert.match(HTML_READER.slice(at, at + 120), /page:\s*1/);
});

test("取消一张钉住的卡片,只在这是最后一张时才清空 focus", () => {
  const start = VOICECALL.indexOf("function _pinForget(");
  const end = VOICECALL.indexOf("\n  function _effectivePins", start);
  assert.ok(end > start);
  const body = VOICECALL.slice(start, end);
  assert.match(body, /Object\.keys\(_pins\.map\)\.length === 0/,
    "必须先删除这一条、再判断是否清空,否则永远判不到 0");
  // 判空必须在删除这一条**之后**,不能在删除之前就去数。
  const deleteAt = body.indexOf("delete _pins.map[label]");
  const checkAt = body.indexOf("Object.keys(_pins.map).length === 0");
  assert.ok(deleteAt > 0 && checkAt > deleteAt);
});
