// 选区 OCR（文字层坏掉时重新识别这块）在原生接管时。
//
// ⚠ 这里最容易走错的一步，是把它当成"把服务端那套 OCR 接过来"。在 App 里
// `/pdf/api/ocr-selection` 由本地 runtime 接管，跑的是 **App 自己的** OCR
// （NativeBookOCRBridge），写回的也是 App 自己的字符层 —— 这条 fetch 根本不出网。
// 若改成直连服务端那套，只会校正"刚选中的那段文字"，页面的字符层还是旧的：
// 一个看起来成功、下次选还是错的假修复。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const MISC = read("_server_deploy/static/pdf/reader.src/21-misc-ai.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));

test("① 走的是本地 runtime 接管的那条端点（App 内不出网）", () => {
  const entry = body(MISC, "window.__bwReaderOcrSelection = async function", "window.onOcrSel");
  assert.match(entry, /'\/pdf\/api\/ocr-selection'/);
  assert.match(RUNTIME, /path === '\/pdf\/api\/ocr-selection' && method === 'POST'/,
    "本地 runtime 必须还接着这条路，否则它就真出网了");
  assert.match(entry, /localStorage\.setItem\('pdf-cv:'/, "cv 存下来，网页重渲第一拉就命中新版");
  assert.match(READER, /__bwReaderOcrSelection/, "改完 reader.src 要拼合");
});

test("② bbox 由原生给：接管后网页算不出它", () => {
  const menu = body(DOC, "func editMenuInteraction", "private func highlightAction");
  assert.match(menu, /UIAction\(title: "OCR"/);
  assert.match(menu, /union = union\.union\(rect\)/, "选区各矩形的并集");
  // 网页那侧的 onOcrSel 要 _charSel.pw.__charBoxes —— 接管后不存在。
  assert.match(MISC, /if \(!pw \|\| !pw\.__charBoxes \|\| !lastSelText\)/);
});

test("③ 识别完这一页的缓存都要失效 —— 包括 pageText", () => {
  const invalidate = body(DOC, "ocrUpdates = NativeBookOCRManager.shared",
                          "private func displayedSize");
  assert.match(invalidate, /self\.pageTexts\[page\] = nil/);
  assert.match(invalidate, /self\.characterPages\[page\] = nil/);
  assert.match(invalidate, /self\.pageTexts = \[:\]/, "整本失效时也要清");
  // ⚠ 不清 pageTexts 的话，助手拿到的还是识别**前**那版正文，而且是静默的。
});

test("④ 结果要出声，命令过两道闸", () => {
  const run = body(WEBVIEW, "private func recognizeNativeSelection", "/// 点了已有划线");
  // 识别完悄无声息的话，用户不知道该不该再选一次。
  assert.match(run, /nativeConversation\.report\("已重新识别："/);
  assert.match(run, /nativeConversation\.report\(receipt\["error"\]/);
  const allow = WEBVIEW.slice(WEBVIEW.indexOf("let allowed: Set<String>"),
                              WEBVIEW.indexOf("guard let action = command[\"action\"]", WEBVIEW.indexOf("let allowed: Set<String>")));
  assert.match(allow, /"nativeOcrSelection"/);
  const keys = SCRIPT.slice(SCRIPT.indexOf("const parameterKeys"),
                            SCRIPT.indexOf("const action = command.action;"));
  assert.match(keys, /nativeOcrSelection/);
});
