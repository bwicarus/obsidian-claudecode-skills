// 原生选区划线：**原生自己算坐标、直接落本地库**。
//
// 2026-09-21 起不再经 `__bwReaderHighlightExactText`。那条路要先用
// `_pdfExactTextPage` 把目标页在网页里渲出来取 `__charBoxes`
// （`dataset.loaded === '1'` 是硬条件）—— 也就是说"原生划线"反过来把网页渲染
// 变成了必需品，两套渲染器同时渲同一本书。用户报的崩溃就指着这个。
//
// 原生那侧本来就有自己的字符层和选区核心，点坐标的矩形它自己算得出来，所以
// 直接拼出**与网页保存时一模一样的记录**交给本地 runtime。这条测试守两件事：
// 别退回那条依赖渲染的老路；记录形状别和网页那侧漂移（多一个字段就被白名单整条拒）。
//
// 链路四段，任一段断掉这个菜单项就是个哑按钮：
//   ① 选区层把「划线」放进菜单，并带上颜色键
//   ② document 补页码 + 把归一化矩形乘回点坐标
//   ③ 壳转成 nativeSelectionHighlight；允许清单是这条命令唯一的闸
//   ④ 注入脚本直接落本地库，不依赖网页渲染
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const DOCUMENT = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
const HIGHLIGHT = read("_server_deploy/static/pdf/reader.src/17-highlight.js");
const SELECTION = read("ios/BWReader/App/ReaderNativePDFSelection.swift");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");

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

test("② document 补上页码，并把矩形还原成点坐标一起带出去", () => {
  assert.match(DOCUMENT, /struct HighlightRequest/,
    "划线要带完整几何：页码 + 点坐标矩形 + 页面尺寸");
  // 选区核心给的本来就是点（ReaderNativePDFSelection 为了绘制才除以页宽高），
  // 存储要的也是点 —— 这里必须乘回去，否则高亮会缩到页面左上角一小块。
  assert.match(DOCUMENT, /rect\.minX \* chars\.pageWidth/);
  assert.match(DOCUMENT, /rect\.maxY \* chars\.pageHeight/);
  assert.match(SELECTION, /CGRect\(x: \$0\[0\] \/ width/,
    "选区桥那侧仍是除法归一化 —— 它改了，上面的乘法就得跟着改");
});

test("③ 壳发的是 nativeSelectionHighlight，且它在允许清单里", () => {
  assert.match(WEBVIEW, /"action": "nativeSelectionHighlight"/);
  const allow = WEBVIEW.slice(WEBVIEW.indexOf("let allowed: Set<String>"),
                              WEBVIEW.indexOf("guard let action = command[\"action\"]"));
  assert.match(allow, /"nativeSelectionHighlight"/, "命令不在允许清单里就是个哑按钮");
  assert.match(WEBVIEW, /nativePDFDocument\?\.matches\(bookID: bookID, contentSHA256: contentSHA256\) == true/);
});

test("④ 直接落本地库，且记录形状与网页保存时一致", () => {
  const branch = SCRIPT.slice(SCRIPT.indexOf("action === 'nativeSelectionHighlight'"),
                              SCRIPT.indexOf("action === 'nativeSelectionLookup'"));
  assert.ok(branch.length > 400, "找不到这条命令的实现");

  // ⚠ 不许再回到 __bwReaderHighlightExactText：它要先把那一页在网页里渲出来
  //   （_pdfExactTextPage 硬要求 dataset.loaded === '1'），那正是两套渲染器
  //   同时渲同一本书的来源。原生这侧自己算得出坐标，不需要网页渲染。
  // ⚠ 只看代码不看注释：说明「为什么不再走那条路」必然要提它的名字，
  //   把注释也算违规就会逼人删掉原因 —— 那正是最不该删的东西。
  const code = branch.split(/\r?\n/).filter((line) => !/^\s*\/\//.test(line)).join("\n");
  assert.doesNotMatch(code, /__bwReaderHighlightExactText/,
    "回到那条路就等于把网页渲染又变成必需品");
  assert.match(branch, /runtime\.savePDFHighlight\(/, "直接交给本地 runtime 落库");

  // 记录形状与网页那侧同源：reader.src 的 saveHighlight payload 有哪些键，
  // 这里就只能有哪些键 —— 多一个会被本地 runtime 的白名单整条拒掉。
  const allowed = new Set(
    (/var allowed = new Set\(\[([\s\S]*?)\]\)/.exec(RUNTIME)?.[1] || "")
      .match(/'([a-z_]+)'/g)?.map((x) => x.slice(1, -1)) || []);
  assert.ok(allowed.size >= 10, "没解析到本地 runtime 的字段白名单");
  // 只抽**传给 savePDFHighlight 的那个对象字面量**里的键；按行首抽会漏掉写在
  // 同一行上的 page/rects/text（第一版就是这么漏的）。
  const literal = code.slice(code.indexOf("savePDFHighlight({"));
  const sent = [...new Set(
    (literal.slice(0, literal.indexOf("});")).match(/([a-z_]+)\s*:/g) || [])
      .map((x) => x.replace(/\s*:$/, "")))];
  assert.ok(sent.length >= 8, "没解析到发出去的字段：" + sent.join(","));
  for (const key of sent) {
    assert.ok(allowed.has(key), `字段 ${key} 不在本地 runtime 的白名单里，整条写入会被拒`);
  }
  for (const key of ["file", "page", "rects", "color", "text", "page_w", "page_h", "id"]) {
    assert.ok(sent.includes(key), `记录里缺 ${key}`);
  }
  // mutationId 形状：本地直写要求它存在且合法，否则会退回普通写入路径。
  assert.match(branch, /'c_' \+ Array\.from\(bytes/);
});
