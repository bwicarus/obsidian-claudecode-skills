// 选中附近的原文,从三种界面一路到 Windows 快照。
//
// 此前模型拿到的是一句孤零零的选中文字,要理解它就得回整页 4000 字正文里
// 自己找位置。而三种界面**各自都已经算好了**上下文:网页 web-adapter 存着
// 1200 字符的块级原文、EPUB 有段落 ctx、PDF 有字符层的句子扩展函数 ——
// 只是没有一条通道把它带出来。
//
// 这条链每一跳都是 fail-closed 校验,漏一跳就是整条 active-reading 被拒
// (不是"少个字段",是选中彻底消失)。所以这里逐跳钉:
//   生产者(PDF/EPUB/网页) → ctxSync pend → rc-computer-voice 组装
//   → Windows 协议校验 → 快照 selection/selectedItems → 落盘恢复
//
// 注:本机只有 .NET runtime 无 SDK,C# 部分是文本校验,不能替代编译。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const read = (p) => readFileSync(join(ROOT, p), "utf8");

const PDF_SRC = read("_server_deploy/static/pdf/reader.src/16-caret-select.js");
const PDF_BUILT = read("_server_deploy/static/pdf/reader.js");
const EPUB = read("_server_deploy/static/pdf/epub-html.js");
const CONTENT = read("extensions/bw-reader-webext/content.js");
const BACKGROUND = read("extensions/bw-reader-webext/background.js");
const VOICE = read("_server_deploy/static/pdf/rc-computer-voice.js");
const SNAPSHOT = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectContextSnapshot.cs");

// ── 生产者 ────────────────────────────────────────────────────────
test("PDF 现算所在句,不读那个必然为空的全局量", () => {
  // __lastSelSentence 在 _ctxSelReport 触发之后才写入,且 _updateSelPreview
  // 一进来就先清空它 —— 在这里读永远是空串。
  const start = PDF_SRC.indexOf("function _ctxSelReport(");
  const body = PDF_SRC.slice(start, start + 1800);
  assert.match(body, /_expandSentenceFromRange\(ch, _charSel\.startIdx, _charSel\.endIdx\)/);
  // 只禁真正的读取,不禁注释里提它 —— 第一版把 /__lastSelSentence/ 一刀切,
  // 结果命中的是解释"为什么不读它"的那行注释,那种断言什么也没保护。
  const code = body.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
  assert.doesNotMatch(code, /window\.__lastSelSentence|=\s*__lastSelSentence/);
  assert.match(body, /sel_context_source: selCtx \? 'pdf-sentence' : ''/);
});

test("PDF 的构建产物跟着更新了", () => {
  // reader.js 是 reader.src 编译出来的;只改源不重建,线上跑的还是旧代码。
  assert.match(PDF_BUILT, /sel_context_source: selCtx \? 'pdf-sentence' : ''/);
});

test("EPUB 从实况选区现算段落,不读上一轮的 cur.ctx", () => {
  // captureSel 先调 _ctxSelReport、之后才更新 cur —— 读 cur 拿到的是上一次
  // 选区的段落,内容会跟当前选中对不上。
  const start = EPUB.indexOf("function _epubSelBlockCtx(");
  assert.ok(start > 0, "找不到 _epubSelBlockCtx");
  const body = EPUB.slice(start, start + 1200);
  assert.match(body, /window\.getSelection\(\)/);
  assert.match(body, /closest\('p,li,td,blockquote,h1,h2,h3,h4,div'\)/);
  assert.match(body, /_countableText\(blk\)/,
    "要用 _countableText —— 它已排除注入的振假名/译文/便签");
  assert.match(body, /cur\.text === txt/,
    "退回 cur 时必须确认是同一次选中,否则串档");
});

test("网页从实况选区取块级原文,并排除扩展自己的 Shadow UI", () => {
  const start = CONTENT.indexOf("var selectionContext = \"\";");
  const body = CONTENT.slice(start, start + 1400);
  assert.match(body, /getElementById\("bw-reader-host"\)/,
    "扩展自己界面里的选区不是书页内容");
  assert.match(body, /closest\("p,li,td,blockquote,div,section,h1,h2,h3,h4"\)/);
});

test("三个生产者都在上下文与选中一样时不带 —— 重复内容只花 token", () => {
  assert.match(PDF_SRC, /sent\.trim\(\) !== txt\.trim\(\)/);
  assert.match(EPUB, /ctx && ctx !== txt/);
  assert.match(CONTENT, /selCtx && selCtx !== selection/);
});

test("清空用空串而不是省略 —— ctxSync 把缺席当没变", () => {
  // 省略字段的话旧上下文会粘在 pend 里随心跳反复重发。
  assert.match(PDF_SRC, /sel_context: selCtx,/);
  assert.match(EPUB, /sel_context: selCtx,/);
});

// ── 扩展转发 ──────────────────────────────────────────────────────
test("background 顶层白名单放行且有长度上限", () => {
  // readerExactKeys 是精确集合比较:新键不加进去,整条快照当场被拒。
  assert.match(BACKGROUND, /"selection", "selectionContext",/);
  assert.match(BACKGROUND, /page\.selectionContext\.length > 1200/);
});

test("background 只在有活动选中时带上下文", () => {
  const at = BACKGROUND.indexOf("const selectionContext = selection");
  assert.ok(at > 0, "找不到 selectionContext 归一化");
  const body = BACKGROUND.slice(at, at + 260);
  assert.match(body, /selection\s*\n?\s*\? readerNormalizeText/,
    "非 active 必须为空 —— 桥的合同不许非 active 带上下文");
});

test("background 转发时给出来历标签", () => {
  assert.match(BACKGROUND, /selectionContextSource: "web-block"/);
});

// ── 阅读器发送端 ──────────────────────────────────────────────────
test("发送端只在 active 时带上下文", () => {
  const at = VOICE.indexOf("var selectionContext = null;");
  const body = VOICE.slice(at, at + 1200);
  assert.match(body, /if \(selectionState === "active"\)/);
});

test("发送端把坏上下文当缺席,不当拒绝", () => {
  // 上下文是增强。它坏了不该让位置和选中本体一起陪葬。
  const at = VOICE.indexOf("var selectionContext = null;");
  const body = VOICE.slice(at, at + 1200);
  assert.match(body, /typeof rawSelCtx === "string"/);
  assert.doesNotMatch(body, /throw/);
});

test("发送端归一化回车并只放行换行与制表", () => {
  const at = VOICE.indexOf("var selectionContext = null;");
  const body = VOICE.slice(at, at + 1200);
  assert.match(body, /replace\(\/\\r\\n\?\/g, "\\n"\)/);
  // 排除 \n(000a) 与 \t(0009) 之外的控制字符
  assert.match(body, /u0000-\\u0008\\u000b\\u000c\\u000e-\\u001f/);
});

// ── Windows 协议 ──────────────────────────────────────────────────
test("协议白名单放行两个新键", () => {
  // allowedKeys 是精确集合:不加就是整条 active-reading 被拒,
  // 而且 WSS 上的非重试错误会把上下文泵整个停死。
  assert.match(SNAPSHOT, /\.Append\("selectionContext"\)/);
  assert.match(SNAPSHOT, /\.Append\("selectionContextSource"\)/);
});

test("非 active 不许带上下文,来历不许没有本体", () => {
  const at = SNAPSHOT.indexOf("string? selectionContext = OptionalActiveText(");
  assert.ok(at > 0);
  const body = SNAPSHOT.slice(at, at + 900);
  assert.match(body, /selectionState != "active"/);
  assert.match(body, /selectionContextSource is not null\s*\n?\s*&& selectionContext is null/);
  assert.match(body, /throw ActiveReadingInvalid\(\)/);
});

test("可选文本字段宽进严出", () => {
  const at = SNAPSHOT.indexOf("private static string? OptionalActiveText(");
  const body = SNAPSHOT.slice(at, at + 900);
  assert.match(body, /TryGetProperty\(name, out JsonElement property\)/,
    "缺席返回 null,不是拒绝");
  assert.match(body, /text\.Length > maximumLength/);
  assert.match(body, /char\.IsControl\(character\)\s*\n?\s*&& character is not \('\\n' or '\\t'\)/);
});

test("record 新增成员,ForwardActiveReading 真的把它传下去", () => {
  assert.match(SNAPSHOT, /string\? SelectionContext = null,\s*\n\s*string\? SelectionContextSource = null\);/);
  // 这一处最容易漏:record 上加了成员,但折叠时的手抄块不补就无声消失。
  assert.match(SNAPSHOT, /activeReading\.SelectionContext,\s*\n\s*activeReading\.SelectionContextSource\);/);
});

test("上下文进快照的 selection 节,缺席时不放 null 占位", () => {
  const at = SNAPSHOT.indexOf("private static JsonObject ActiveSelection(");
  const body = SNAPSHOT.slice(at, at + 1200);
  assert.match(body, /if \(context is not null\)/,
    "「没有上下文」和「上下文为空」是两句不同的话");
  assert.match(body, /selection\["context"\] = context;/);
});

test("selectedItems 里的文本项带上下文", () => {
  const at = SNAPSHOT.indexOf("JsonObject textItem = new()");
  assert.ok(at > 0, "找不到 selectedItems 的文本项");
  const body = SNAPSHOT.slice(at, at + 800);
  assert.match(body, /textItem\["context"\] = context;/);
  assert.match(body, /textItem\["contextSource"\] = contextSource;/);
});

test("重启后上下文要活下来", () => {
  // RestoreSelection 逐键重建:不补这两行,进程存活期间一切正常、
  // 服务一重启 context 就无声消失。
  const at = SNAPSHOT.indexOf("private static JsonObject RestoreSelection(");
  const body = SNAPSHOT.slice(at, at + 1500);
  assert.match(body, /restored\["context"\] = context;/);
  assert.match(body, /context\.Length <= 1200/);
  assert.match(body, /state == "active"/,
    "恢复时的纪律要跟入口一致");
});
