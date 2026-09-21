// 原生选区划线：**复用阅读器自己的划线路径**，不另写一套保存。
//
// 原生 PDF 主阅读区上选中文字后，选区菜单里的「划线」走的是
// `window.__bwReaderHighlightExactText` —— AI 划线用的同一个入口、同一套存储、
// 同一份色板。这条测试守的是这件事别被"顺手在原生这边写个保存"取代：两套保存
// 一旦并存，高亮就会按你从哪儿划的而落在不同地方，而这种分叉要很久才会被发现。
//
// 链路有四段，任一段断掉这个菜单项就是个哑按钮：
//   ① 选区层把「划线」放进菜单，并带上颜色键
//   ② document 把它抛给壳
//   ③ 壳转成 nativeSelectionHighlight 命令，且该命令在允许清单里
//   ④ 注入脚本收到后调阅读器的入口，而不是自己写库
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const DOCUMENT = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
const HIGHLIGHT = read("_server_deploy/static/pdf/reader.src/17-highlight.js");

test("① 选区菜单里有划线，且四支笔的键名与阅读器色板一致", () => {
  assert.match(DOCUMENT, /UIMenu\(title: "划线"/, "菜单里要有这一项");
  for (const key of ["yellow", "green", "blue", "pink"]) {
    assert.match(DOCUMENT, new RegExp(`key: "${key}"`), `缺少 ${key}`);
  }
  // ⚠ 这四个键是**用户自己的墨水**：改名等于改写他笔记里的既有数据。
  //   阅读器那侧的色板是唯一来源，这里只能跟着它。
  const palette = HIGHLIGHT.slice(HIGHLIGHT.indexOf("const colors = { yellow:"));
  for (const key of ["yellow", "green", "blue", "pink"]) {
    assert.match(palette.slice(0, 200), new RegExp(`${key}:`), `阅读器色板里没有 ${key}`);
  }
});

test("② 选区层把划线抛给 document，document 再抛给壳", () => {
  assert.match(DOCUMENT, /var onHighlight: \(\(ReaderNativePDFSelection\.Value, String\) -> Void\)\?/);
  assert.match(DOCUMENT, /overlay\.onHighlight = \{[\s\S]{0,200}self\?\.onHighlight\?\(number,/,
    "页码要由 document 补上：选区层自己不知道它是第几页");
});

test("③ 壳发的是 nativeSelectionHighlight，且它在允许清单里", () => {
  assert.match(WEBVIEW, /"action": "nativeSelectionHighlight"/);
  // 允许清单是这条命令唯一的闸；漏了它，菜单点下去只会回"阅读页尚未准备好"。
  const allow = WEBVIEW.slice(WEBVIEW.indexOf("let allowed: Set<String>"),
                              WEBVIEW.indexOf("guard let action = command[\"action\"]"));
  assert.match(allow, /"nativeSelectionHighlight"/, "命令不在允许清单里就是个哑按钮");
  // 身份校验：书对不上就不写。高亮落到别的书上比不写糟得多。
  assert.match(WEBVIEW, /nativePDFDocument\?\.matches\(bookID: bookID, contentSHA256: contentSHA256\) == true/);
});

test("④ 注入脚本调阅读器的入口，不自己写库", () => {
  const branch = SCRIPT.slice(SCRIPT.indexOf("action === 'nativeSelectionHighlight'"),
                              SCRIPT.indexOf("action === 'readingSettingsRead'"));
  assert.ok(branch.length > 200, "找不到这条命令的实现");
  assert.match(branch, /window\.__bwReaderHighlightExactText\(/,
    "必须走阅读器自己的划线入口 —— 另写一套保存会让高亮按来源分叉");
  assert.doesNotMatch(branch, /fetch\(|localStore|indexedDB/i,
    "这一段不该自己发请求或碰存储：保存是阅读器那侧的事");
  // 书身份取阅读器那一份，不另存一份：两份对不上时划线会静默落到别处。
  assert.match(branch, /__PDF_CFG\s*&&\s*window\.__PDF_CFG\.file_rel/);
  assert.match(
    read("_server_deploy/static/pdf/reader.src/01-boot.js"),
    /const FILE_REL = window\.__PDF_CFG\.file_rel/,
    "阅读器那侧的书身份也来自 __PDF_CFG.file_rel —— 它换了来源，这里就得跟着换");
  // mutationId 的形状由阅读器校验（^c_[a-f0-9]{8,32}$），这里生成的必须能过。
  assert.match(branch, /'c_' \+ Array\.from\(bytes/);
  assert.match(HIGHLIGHT, /\^c_\[a-f0-9\]\{8,32\}\$/,
    "阅读器那侧的 mutationId 形状变了，这里生成的就过不去了");
});
