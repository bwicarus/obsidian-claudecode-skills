// 词锚卡（钉在正文某一段上的卡片）在原生接管时不能永久隐身。
//
// 这是用户报的「卡片在插入时要求目标页在显示」的真实形态：
// 卡确实存下来了（__pageBindPersist 的 deferred 路径 2026-08-31 就支持了），
// 但 ensureMounted 在 binding 解不出来时会**先把它藏起来等重试** ——
// 那次重试由 08-charlayer 在 __charBoxes 挂上后触发，而接管后网页**不再渲页**，
// __charBoxes 永远不会挂上。于是卡片一直藏着：存了、没报错、就是不出现。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const STICKY = read("_server_deploy/static/pdf/rc-stickynote.js");
const BINDCARD = read("_server_deploy/static/pdf/reader.src/34-bindcard.js");
const CARDS = read("ios/BWReader/App/ReaderNativePageCards.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));

test("① 插入本身不要求目标页渲染", () => {
  const persist = body(BINDCARD, "window.__pageBindPersist = function",
                       "var placement = deferred");
  assert.match(persist, /g\.why === 'page-not-rendered'/);
  assert.match(persist, /if \(!g \|\| \(!g\.ok && !deferred\)\)/,
    "只有 page-not-rendered 走 deferred，其它失败仍 fail closed");
  assert.match(BINDCARD, /var placement = deferred \? \{ deferredPdfPage: g\.page \} : _bindScreenPoint\(g\)/);
});

test("② 接管时不再「藏着等一个永远不来的重试」", () => {
  const mount = body(STICKY, "var _tmp = res && (res.why === 'page-not-rendered'",
                     "// 全量重挂/重定位");
  assert.match(mount, /RC\.readerNavigation\.nativeViewport/);
  assert.match(mount, /_tmp = false/);
  // 顺序要紧：先算 _tmp、再按接管翻掉，最后才决定显不显示。
  assert.ok(mount.indexOf("nativeViewport") < mount.indexOf("if (!_tmp) ctl.root.style.display"));
});

test("③ 藏着＝原生也看不见（visible 是从 DOM 量的）", () => {
  // nativePlacementState 的 visible 来自 getBoundingClientRect；display:none 的
  // 卡片量出来是 0×0，于是整张卡在原生那侧也不画 —— 这是这条链的关键一环。
  assert.match(STICKY, /visible: rect\.width > 0 && rect\.height > 0/);
  assert.match(CARDS, /if item\.visible && rect\.maxX > 0/);
});

test("④ 词锚描边由原生补，因为网页那份画不出来", () => {
  // .pgmark 是网页在 pgbind-layer 里画的，要 __charBoxes；接管后 item.markers
  // 是空的 —— 这张卡钉在正文哪一段，屏幕上完全看不出来。
  const fallback = CARDS.slice(CARDS.indexOf("if item.markers.isEmpty, item.bound"));
  assert.match(fallback, /let boxes = nativeMarkers, !boxes\.isEmpty/);
  assert.match(fallback, /RoundedRectangle\(cornerRadius: 3\)/);
  // 不猜序号：序号是网页排的，猜一个可能跟别处对不上。
  assert.doesNotMatch(fallback.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n"),
    /marker\.number|ordinal/);
  // 原生解不出绑定时返回空数组而不是 nil —— 那是「确实没钉在正文上」。
  assert.match(WEBVIEW, /return geometry\.bindingRects\.map \{/);
});
