// 真实改页之后，原生正文要重挂。
//
// 插入页是**真的写回 PDF 文件**的，写回会换掉内容摘要。而原生正文的每一条路
// —— 投影、划线、定位、墨迹表面 —— 都 `guard document.matches(bookID:contentSHA256:)`。
// 摘要一变，它们全都默默什么都不做：屏幕上还是改页前的那一份，却再也不更新。
//
// 这是最难发现的一类失败：不报错、不崩溃，只是"插进去的页没出现"。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const DOCUMENT = read("ios/BWReader/App/ReaderNativePDFDocument.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));

test("① 摘要更新的那一处会触发重挂", () => {
  assert.match(WEBVIEW, /currentLocalBookContentSHA256 = digest[\s\S]{0,400}remountNativePDFIfContentChanged\(digest\)/,
    "摘要赋值之后要立刻判一次 —— 中间隔着别的 await，旧文档会继续被当成有效的");
});

test("② 只在真的不匹配时重挂，且不就地递归", () => {
  const fn = body(WEBVIEW, "private func remountNativePDFIfContentChanged(",
                  "/// 取可见页的**页面叠加数据**");
  assert.match(fn, /!document\.matches\(bookID: bookID, contentSHA256: digest\)/,
    "匹配就什么都不做 —— 每次取摘要都重挂会把书闪一下");
  assert.match(fn, /invalidateNativePDFDocument\(reason: "content-digest-changed"\)/);
  assert.match(fn, /Task \{ @MainActor \[weak self\] in self\?\.mountNativePDFDocumentIfEnabled\(\) \}/,
    "重挂要另起一轮：这次调用可能就发生在 prepare 里面（它也取摘要），就地重挂会递归");
});

test("③ 那些路径确实都按 matches 把关（所以摘要一变就会静默停更）", () => {
  // 这条是上面推理的前提。哪天它们不再按 matches 判，这个重挂就该重新评估。
  assert.match(DOCUMENT, /func matches\(bookID: String, contentSHA256: String\) -> Bool/);
  for (const guardSite of [
    "refreshNativePDFProjection",       // 投影
    "highlightFromNativeSelection",     // 划线
  ]) {
    const fn = WEBVIEW.slice(WEBVIEW.indexOf("func " + guardSite));
    assert.match(fn.slice(0, 1400), /matches\(bookID:/, `${guardSite} 不再按 matches 把关了`);
  }
});
