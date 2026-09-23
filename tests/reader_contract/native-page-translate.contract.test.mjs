// 整页翻译（译页）在原生正文上要看得见。
//
// 这一条防的是接管后最典型的失效形态：**动作生效了，结果看不见**。网页那侧
// `_pageTranslateApplyAll` 只处理 `[data-loaded="1"]` 的页 —— 原生接管后一页都没有，
// 于是点了「译页」什么都不发生。原生自己按可见页去取。
//
// 切分/分配/字号只能有一处判据：同一行译文在两个表面上必须落在同一个位置，
// 否则「刚才那行」指的是不同的东西。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const SRC = read("_server_deploy/static/pdf/reader.src/10-pagetranslate.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const POLICY = read("_server_deploy/static/reader-runtime/interaction-policy.js");
const CSS = read("_server_deploy/static/pdf/pdf-styles.css");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");

test("① 切分只有一处，且用点坐标（两个表面各自缩放）", () => {
  const slices = body(SRC, "function _pageTranslateSlices(sentences)", "function _drawPageTranslate");
  assert.match(slices, /_mergeLines\(raw\)/, "按视觉行合并");
  assert.match(slices, /h \* 0\.40/, "字号 = 行高 × 0.4");
  assert.match(slices, /r\[1\] - fs \* 0\.34/, "落在字框顶部留白（与振假名同一位置）");
  // ⚠ 切片里不能有 css 像素：sx/sy 是 DOM 路径专属的换算。
  assert.doesNotMatch(code(slices), /\bsx\b|\bsy\b/, "切片是点坐标，不含 DOM 缩放");
  // DOM 路径必须消费同一个函数，不能留一份自己的算法。
  const dom = body(SRC, "function _drawPageTranslate", "window.__bwReaderPageTranslateSlices");
  assert.match(dom, /_pageTranslateSlices\(sentences\)/);
  assert.doesNotMatch(code(dom), /h \* 0\.40/, "DOM 路径不许再算一遍字号");
});

test("② 原生入口不做「翻过就跳过」的去重", () => {
  const entry = body(SRC, "window.__bwReaderPageTranslateSlices",
                     "window.__bwReaderPageTranslateOn");
  assert.match(entry, /if \(!_pageTrOn \|\| !page\) return \[\]/, "开关关着回空");
  // ⚠ pw.__pageTrSeq 是 DOM 把「已翻过」记在元素上；原生每次重取都要拿到完整结果，
  // 跳过等于第二次翻到这页就空白。
  assert.doesNotMatch(code(entry), /__pageTrSeq/);
  assert.match(entry, /@interaction document\.page-translate\.read/);
  assert.match(POLICY, /'document\.page-translate\.read'/);
  assert.match(READER, /__bwReaderPageTranslateSlices/, "改完 reader.src 要拼合");
});

test("③ 原生按页高归一化字号，不存 pt", () => {
  const slice = body(DOC, "struct TranslationSlice", "@Published private(set) var translationSlices");
  assert.match(slice, /let fontScale: Double/);
  // 存 pt 的话放大页面译文还是小的 —— 归一化 × 屏幕页高才跟着缩放。
  const draw = body(DOC, "for slice in translationSlices[number] ?? []", "for stroke in ink[number]");
  assert.match(draw, /slice\.fontScale \* frame\.height/);
});

test("④ 观感跟 .page-tr-rt 一致：白底 + 深蓝 + 左对齐", () => {
  assert.match(CSS, /\.page-tr-rt\{[^}]*text-align:left/, "网页是左对齐");
  const draw = body(DOC, "for slice in translationSlices[number] ?? []", "for stroke in ink[number]");
  // ⚠ Canvas 的 draw(_:in:) 居中；用它译文会在行上飘到中间跟原文对不上。
  // 左对齐：从框左沿按点绘制（draw(at:)），不是居中的 draw(in:)。
  assert.match(draw, /draw\(at: CGPoint\(x: box\.minX,/);
  assert.match(draw, /UIColor\.white\.withAlphaComponent\(0\.86\)/, "白底与 CSS 的 .86 同一档");
  assert.match(draw, /weight: \.semibold/);
});

test("⑤ 点完顶栏那些工具按钮，原生要重取一次", () => {
  const perform = body(WEBVIEW, "private func performNativeConversationCommand",
                       "func updateNativePDFSelection");
  // 顶栏「阅读工具」点的是网页工具栏按钮；不重取的话表现是「点了译页没反应」。
  assert.match(perform, /command\["action"\] as\? String == "liveAction"/);
  assert.match(perform, /refreshNativePageOverlays\(force: true\)/, "数据可能变了：可见页全部重取");
});
