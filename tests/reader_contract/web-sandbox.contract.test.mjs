import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { DocumentHost } from "./helpers.mjs";

const ROOT = new URL("../../", import.meta.url);
const TEMPLATE = readFileSync(
  new URL("_server_deploy/templates/pdf_reader.html", ROOT),
  "utf8",
);
const ADAPTER = readFileSync(
  new URL("_server_deploy/static/pdf/web-adapter.js", ROOT),
  "utf8",
);
const SELECTION_CONTROLLER = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/33-selection-controller.js", ROOT),
  "utf8",
);
const PDF_RC_ADAPTER = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/27-rc-adapter.js", ROOT),
  "utf8",
);
const READER_BUNDLE = readFileSync(
  new URL("_server_deploy/static/pdf/reader.js", ROOT),
  "utf8",
);
const WORD_SELECTION_ACTIONS = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js", ROOT),
  "utf8",
);
const AI_SELECTION_ACTIONS = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/21-misc-ai.js", ROOT),
  "utf8",
);
const LEGACY_LOCAL_HOST = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/32-extension-host.js", ROOT),
  "utf8",
);
const LEGACY_PAGE_BRIDGE = readFileSync(
  new URL("_server_deploy/static/pdf/pwa-extension-bridge.js", ROOT),
  "utf8",
);
const LEGACY_EXTENSION_ADAPTER = readFileSync(
  new URL("extensions/bw-reader-webext/src/pwa-adapter.js", ROOT),
  "utf8",
);
const LEGACY_EXTENSION_UI = readFileSync(
  new URL("extensions/bw-reader-webext/content.js", ROOT),
  "utf8",
);
const LEGACY_EXTENSION_SHELL = readFileSync(
  new URL("extensions/bw-reader-webext/src/shell.js", ROOT),
  "utf8",
);
const PROXY = readFileSync(
  new URL("_server_deploy/html_reader.py", ROOT),
  "utf8",
);
const APP = readFileSync(
  new URL("_server_deploy/app.py", ROOT),
  "utf8",
);

const NONCE = "n".repeat(43);
const ORIGIN = "https://bwicarus.taile44d0c.ts.net";

test("PWA 网页代理与 RBI 退役，旧 live 入口只校验后跳转原站", () => {
  const retired = [
    "pdf_web_proxy",
    "pdf_web_page_mirror",
    "pdf_web_res_mirror",
    "pdf_web_frame",
    "pdf_web_res",
    "pdf_web_rbi",
    "pdf_web_rbi_live",
    "pdf_api_web_fetch",
    "pdf_api_web_cookie",
    "pdf_api_rbi_ticket",
  ];
  for (const name of retired) {
    const body = PROXY.match(
      new RegExp(`    def ${name}\\([^\\n]*\\):[\\s\\S]*?(?=\\n    @bp\\.route|\\n    def _serve_res)`),
    )?.[0] || "";
    assert.match(body, /return _retired_web_response\(\)/, name);
  }
  const live = PROXY.match(
    /    def pdf_web_live\(\):[\s\S]*?(?=\n    @bp\.route\("\/web"\))/,
  )?.[0] || "";
  assert.match(live, /_retired_web_redirect_target/);
  assert.match(live, /redirect\(target, code=302\)/);
  assert.match(live, /Referrer-Policy/);
  assert.doesNotMatch(live, /render_template/);

  const providerEntries = APP.match(
    /READER_PROVIDER_ENTRY_PATHS = frozenset\(\{[\s\S]*?\}\)/,
  )?.[0] || "";
  assert.doesNotMatch(providerEntries, /\/pdf\/web\/live/);

  const material = PROXY.match(
    /def web_material\([\s\S]*?(?=\n\ndef _web_last_path)/,
  )?.[0] || "";
  assert.match(material, /read_web_cache/);
  assert.doesNotMatch(material, /_public_http_text|_web_cache_put/);
  assert.match(material, /PWA 已停止联网抓取/);
});

test("代理镜像地址只在合法 scheme/host 结构下剥壳", () => {
  const source = PROXY.match(
    /function _unmirror\(u\)\{[\s\S]*?\n  \}\n  \/\/ ⚠ 导航一律/,
  )?.[0]?.replace(/\n  \/\/ ⚠ 导航一律[\s\S]*$/, "") || "";
  assert.match(
    source,
    /if\(bits\.length >= 2[\s\S]*?\)\{[\s\S]*?var clean = new URLSearchParams/,
  );
  const unmirror = vm.runInNewContext(`(${source})`, {
    URL,
    URLSearchParams,
    B: "https://reader.example/base",
    location: { origin: ORIGIN },
  });
  assert.equal(
    unmirror(
      `${ORIGIN}/pdf/web/p/https/reader.example/article?x=1&__bwcap=secret`,
    ),
    "https://reader.example/article?x=1",
  );
  assert.equal(
    unmirror(`${ORIGIN}/pdf/web/p/not-a-scheme/reader.example/article`),
    "",
  );
  assert.equal(unmirror(`${ORIGIN}/pdf/web/live?url=x`), "");
  assert.equal(
    unmirror("https://outside.example/path"),
    "https://outside.example/path",
  );
});

function adapterHarness() {
  const windowListeners = new Map();
  const documentListeners = new Map();
  const posts = [];
  const fetches = [];
  const selections = [];
  const usedAdapters = [];
  const pdfAdapter = Object.freeze({
    kind: "pdf",
    config: Object.freeze({ isPDF: true, anchorKind: "pdf-char" }),
    getContext() { return { file: "pdf:sentinel" }; },
  });
  const frameWindow = {
    postMessage(message, targetOrigin) {
      posts.push({ message: structuredClone(message), targetOrigin });
    },
  };
  const frame = {
    contentWindow: frameWindow,
    name: `bw-web-bridge:${NONCE}`,
    src: "",
    addEventListener() {},
    getBoundingClientRect() {
      return { left: 0, top: 0, right: 800, bottom: 600 };
    },
  };
  const add = (map, name, listener) => {
    if (!map.has(name)) map.set(name, []);
    map.get(name).push(listener);
  };
  const sandbox = {
    console,
    URL,
    AbortController,
    Uint8Array,
    structuredClone,
    crypto: webcrypto,
    setTimeout: () => 1,
    clearTimeout() {},
    location: {
      origin: ORIGIN,
      href: `${ORIGIN}/pdf/web/live?url=https%3A%2F%2Fexample.com%2Farticle`,
    },
    history: { replaceState() {} },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: async (path, options) => {
      fetches.push({ path, options });
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        text: async () => JSON.stringify({ ok: true, items: [] }),
      };
    },
    document: {
      readyState: "loading",
      body: { classList: { add() {} } },
      addEventListener(name, listener) {
        add(documentListeners, name, listener);
      },
      getElementById(id) {
        return id === "wl-frame" ? frame : null;
      },
    },
    PdfAdapter: pdfAdapter,
    RC: {
      contract: {
        adapterConfig(profile, overrides) {
          return { profile, ...(overrides || {}) };
        },
        selection(value) {
          if (!value || !String(value.text || "").trim()) return null;
          const context = String(value.context ?? value.ctx ?? value.sentence ?? "");
          return { ...value, text: String(value.text), context, ctx: context, sentence: context };
        },
      },
      use(adapter) {
        usedAdapters.push(adapter);
        this._adapter = adapter;
        return this;
      },
      adapter() { return this._adapter || {}; },
    },
    __bwSelectionController: {
      acceptExternal(value) {
        selections.push(structuredClone(value));
        return value;
      },
      clearExternal() {},
    },
    addEventListener(name, listener) {
      add(windowListeners, name, listener);
    },
    __PDF_CFG: {
      web_url: "https://example.com/article",
      web_rbi: "",
      web_bridge_nonce: NONCE,
    },
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(ADAPTER, context, { filename: "web-adapter.js" });
  const emit = (event) => {
    for (const listener of windowListeners.get("message") || []) listener(event);
  };
  return {
    emit, frameWindow, frame, posts, fetches, selections, usedAdapters,
    pdfAdapter, sandbox,
  };
}

test("WebAdapter 独立登记，不修改或冒充 PdfAdapter", () => {
  const h = adapterHarness();
  assert.equal(h.usedAdapters.length, 1);
  const webAdapter = h.usedAdapters[0];
  assert.equal(webAdapter, h.sandbox.WebAdapter);
  assert.notEqual(webAdapter, h.pdfAdapter);
  assert.equal(webAdapter.kind, "web");
  assert.equal(webAdapter.config.isPDF, false);
  assert.equal(webAdapter.config.renderRegion, false);
  assert.equal(webAdapter.config.anchorKind, "web-quote");
  assert.equal(h.pdfAdapter.kind, "pdf");
  assert.deepEqual(h.pdfAdapter.config, { isPDF: true, anchorKind: "pdf-char" });
  assert.equal("createAnchor" in webAdapter, false);
  assert.equal("resolveAnchor" in webAdapter, false);
  assert.equal("noteMount" in webAdapter._host, false);
});

test("reader 绑定 PDF 私有 host 时不会覆盖 WebAdapter，也不会给 Web 暗注入 PDF 便签锚", () => {
  for (const source of [PDF_RC_ADAPTER, READER_BUNDLE]) {
    assert.match(source, /const _readerIsWebHost = !!\(window\.__PDF_CFG && window\.__PDF_CFG\.web_url\)/);
    assert.match(source, /if \(!_readerIsWebHost && window\.RC && RC\.use\) RC\.use\(PdfAdapter\)/);
    assert.match(source, /if \(!_readerIsWebHost && window\.RC && RC\.stickynote\)/);
    assert.doesNotMatch(
      source,
      /try \{ if \(window\.RC && RC\.use\) RC\.use\(PdfAdapter\); \} catch/,
    );
  }
});

test("WebAdapter 启动即生成 kind=web DocumentHost，URL 可导航而 DOM/quote anchor 保持 pending", async () => {
  const h = adapterHarness();
  const host = DocumentHost.createLegacyDocumentHost(h.sandbox.WebAdapter);
  assert.equal(host.kind, "web");
  assert.equal(host.capability("selection").status, "supported");
  assert.equal(host.capability("navigation").status, "supported");
  assert.equal(host.capability("anchors").status, "pending");
  await host.navigate({ url: "https://example.com/next" });
  assert.match(h.frame.src, /\/pdf\/web\/frame\?url=https%3A%2F%2Fexample\.com%2Fnext/);
  await assert.rejects(
    host.createAnchor({ anchor: { kind: "web-quote", quote: "selection" } }),
    (error) => error.code === "BW_CAPABILITY_PENDING" && error.capability === "anchors",
  );
});

test("代理网页选区只进入 SelectionController，不写 window.lastSelText", () => {
  const h = adapterHarness();
  h.emit({
    source: h.frameWindow,
    origin: "null",
    data: {
      __rcweb: "sel",
      __rcwebNonce: NONCE,
      text: "selected from web",
      ctx: "paragraph around selected from web",
      rect: { left: 12, top: 20, right: 112, bottom: 44 },
    },
  });
  assert.equal(h.selections.length, 1);
  assert.equal(h.selections[0].text, "selected from web");
  assert.equal(h.selections[0].context, "paragraph around selected from web");
  assert.equal(h.selections[0].source, "web");
  assert.deepEqual(h.selections[0].rect, {
    left: 12, top: 20, right: 112, bottom: 44, width: 100, height: 24,
  });
  assert.equal("lastSelText" in h.sandbox, false);
});

test("reader.src 外部选区入口更新真实模块词法状态、上下文、预览和工具条", () => {
  const previewUpdates = [];
  const dispatched = [];
  const toolbarClasses = new Set();
  const sandbox = {
    console,
    Date,
    Object,
    Number,
    String,
    window: null,
    innerWidth: 1000,
    innerHeight: 700,
    RC: {
      adapter: () => ({ kind: "web", captureSelection: () => null }),
      contract: {
        selection(value) {
          const text = String(value?.text || "");
          if (!text.trim()) return null;
          const context = String(value?.context || "");
          return { ...value, text, context, ctx: context, sentence: context };
        },
      },
    },
    document: {
      dispatchEvent(event) { dispatched.push(event); },
    },
    CustomEvent: class {
      constructor(type, options) { this.type = type; this.detail = options?.detail; }
    },
  };
  sandbox.window = sandbox;
  const source = `
    let lastSelText = '';
    let _charSel = { pdfGeometry: true };
    const toolbar = {
      offsetWidth: 300, offsetHeight: 48, style: {},
      classList: {
        add(name) { __toolbarClasses.add(name); },
        remove(name) { __toolbarClasses.delete(name); }
      }
    };
    function _updateSelPreview(text) { __previewUpdates.push(text); }
    function _updateGrammarBtnVisibility() {}
    ${SELECTION_CONTROLLER}
    globalThis.__lexicalSelectionState = () => ({
      lastSelText,
      charSel: _charSel,
      sentence: window.__lastSelSentence,
      meta: window.__lastSelMeta,
      toolbarStyle: { ...toolbar.style }
    });
  `;
  sandbox.__previewUpdates = previewUpdates;
  sandbox.__toolbarClasses = toolbarClasses;
  vm.runInNewContext(source, sandbox, { filename: "selection-controller-harness.js" });

  sandbox.__setExternalSelection({
    source: "web",
    text: "real lexical selection",
    context: "real lexical context",
    rect: { left: 25, top: 30, right: 180, bottom: 70 },
    data: { url: "https://example.com/article" },
  });
  const state = sandbox.__lexicalSelectionState();
  assert.equal(state.lastSelText, "real lexical selection");
  assert.equal(state.charSel, null);
  assert.equal(state.sentence, "real lexical context");
  assert.equal(state.meta.kind, "web");
  assert.equal(state.meta.url, "https://example.com/article");
  assert.deepEqual(previewUpdates, ["real lexical selection"]);
  assert.equal(toolbarClasses.has("open"), true);
  assert.equal(state.toolbarStyle.position, "fixed");
  assert.equal(dispatched.at(-1).type, "bw:selection-changed");
  assert.equal("lastSelText" in sandbox, false);
});

test("查词、翻译、解释和对话均从当前 RC adapter 与统一选区取值，PDF 几何回退仍保留", () => {
  const lookup = WORD_SELECTION_ACTIONS.match(
    /window\.onLookupWord = \(\) => \{[\s\S]*?\n\};/,
  )?.[0] || "";
  assert.match(lookup, /__bwSelectionController/);
  assert.match(lookup, /RC\.adapter\(\)/);
  assert.match(lookup, /adapter\.lookupWord/);
  assert.match(lookup, /adapter\.kind === 'pdf' \? _charSel : selected\.rect/);
  assert.doesNotMatch(lookup, /PdfAdapter\.lookupWord/);

  for (const [name, method] of [
    ["onTranslate", "translate"],
    ["onExplain", "explain"],
    ["onChat", "chat"],
  ]) {
    const start = AI_SELECTION_ACTIONS.indexOf(`window.${name} =`);
    const next = AI_SELECTION_ACTIONS.indexOf("\n};", start);
    const body = AI_SELECTION_ACTIONS.slice(start, next + 3);
    assert.ok(start >= 0 && next > start, `${name} source missing`);
    assert.match(body, /_selectionForToolbarAction\(\)/);
    assert.match(body, /_adapterForToolbarAction\(\)/);
    assert.match(body, new RegExp(`adapter\\.${method}`));
    assert.doesNotMatch(body, new RegExp(`PdfAdapter\\.${method}`));
  }
});

test("PDF 本地书籍宿主在旧 web 配置下拒绝注册", () => {
  let registrations = 0;
  const events = [];
  const sandbox = {
    console,
    Object,
    Number,
    String,
    location: { href: "https://example.com/article" },
    document: {
      title: "Web article",
      body: { classList: { contains: () => false } },
      dispatchEvent(event) { events.push(event); },
    },
    BWReaderBookHost: {
      register() {
        registrations += 1;
        throw new Error("旧 web 壳不得注册成书籍宿主");
      },
    },
    __PDF_CFG: { web_url: "https://example.com/article" },
  };
  sandbox.window = sandbox;
  vm.runInNewContext(LEGACY_LOCAL_HOST, sandbox, { filename: "32-extension-host.js" });

  assert.match(
    LEGACY_LOCAL_HOST,
    /if \(window\.__PDF_CFG && window\.__PDF_CFG\.web_url\) return;/,
  );
  assert.equal(registrations, 0);
  assert.equal(sandbox.__bwReaderLocalApi, undefined);
  assert.equal(sandbox.__bwPdfLocalApi, undefined);
  assert.equal(events.length, 0);
});

test("PWA 页面桥与扩展适配器只接受 book-host，不接受 web mode", () => {
  const pageModes = LEGACY_PAGE_BRIDGE.match(
    /const ALLOWED_MODES = new Set\(\[[^\]]+\]\)/,
  )?.[0] || "";
  const extensionModes = LEGACY_EXTENSION_ADAPTER.match(
    /var BOOK_MODES = new Set\(\[[^\]]+\]\)/,
  )?.[0] || "";
  assert.match(LEGACY_PAGE_BRIDGE, /const BOOK_CONTRACT = 'book-host\/1'/);
  assert.match(LEGACY_PAGE_BRIDGE, /value\.contract !== BOOK_CONTRACT/);
  assert.deepEqual(
    pageModes.match(/'(?:pdf|epub|html|favorite)'/g),
    ["'pdf'", "'epub'", "'html'", "'favorite'"],
  );
  assert.deepEqual(
    extensionModes.match(/'(?:pdf|epub|html|favorite)'/g),
    ["'pdf'", "'epub'", "'html'", "'favorite'"],
  );
  assert.doesNotMatch(pageModes, /'web'/);
  assert.doesNotMatch(extensionModes, /'web'/);
  assert.doesNotMatch(LEGACY_EXTENSION_ADAPTER, /hostMode === 'web'/);
  assert.match(
    LEGACY_EXTENSION_ADAPTER,
    /if \(hasCapability\('highlight'\) \|\| hasCapability\('pdfHighlight'\)\)/,
  );
  assert.match(LEGACY_EXTENSION_ADAPTER, /if \(hasCapability\('pinCard'\)\)/);
  assert.match(LEGACY_EXTENSION_ADAPTER, /if \(hasCapability\('anchorFx'\)\)/);
  assert.match(LEGACY_EXTENSION_UI, /pwaCapability\('selectionOcr'\)/);
  assert.match(LEGACY_EXTENSION_UI, /pwaCapability\('pdfHighlight'\)/);
});

test("统一扩展 shell：普通网页只显示跨站能力，书籍 PWA 再按 host capability 显示本地按钮", () => {
  const gated = Array.from(
    LEGACY_EXTENSION_SHELL.matchAll(
      /<button id="([^"]+)" data-pwa-capability="([^"]+)"/g,
    ),
    (match) => [match[1], match[2]],
  );
  assert.deepEqual(gated, [
    ["bw-book-prev", "navigation"],
    ["bw-book-jump", "navigation"],
    ["bw-book-next", "navigation"],
    ["bw-book-zoom-out", "zoom"],
    ["bw-book-fit", "zoom"],
    ["bw-book-zoom-in", "zoom"],
    ["bw-book-layout", "layout"],
    ["bw-book-crop", "crop"],
    ["bw-book-fullscreen", "fullscreen"],
    ["bw-book-settings", "bookSettings"],
    ["bw-book-favorite", "favorite"],
    ["bw-book-user-page", "userPage"],
    ["ruby-toggle", "ruby"],
    ["pagetr-toggle", "pageTranslate"],
    ["bw-ink-btn", "ink"],
    ["bw-note-btn", "stickyNote"],
    ["bw-search-btn", "bookSearch"],
  ]);

  const visibleFor = (capabilities) => gated
    .filter(([, capability]) => !!capabilities[capability])
    .map(([id]) => id);
  const webCapabilities = new Set([
    "ruby", "pageTranslate", "ink", "stickyNote", "bookSearch",
  ]);
  assert.deepEqual(
    gated
      .filter(([, capability]) => webCapabilities.has(capability))
      .map(([id]) => id),
    ["ruby-toggle", "pagetr-toggle", "bw-ink-btn", "bw-note-btn", "bw-search-btn"],
    "普通网页保留跨站阅读能力，不包含书籍导航/缩放等本地按钮",
  );
  assert.deepEqual(
    visibleFor({
      navigation: true,
      zoom: true,
      layout: true,
      crop: true,
      fullscreen: true,
      bookSettings: true,
      favorite: true,
      userPage: true,
      ruby: true,
      pageTranslate: true,
      ink: true,
      stickyNote: true,
      bookSearch: true,
    }),
    gated.map(([id]) => id),
    "书籍宿主完整声明能力时必须显示全部本地与共享按钮",
  );

  assert.match(LEGACY_EXTENSION_SHELL, /const WEB_CAPABILITIES = new Set/);
  assert.match(LEGACY_EXTENSION_SHELL, /WEB_CAPABILITIES\.has\(capability\)/);
  assert.match(LEGACY_EXTENSION_SHELL, /button\.hidden = !allowed/);
  assert.match(LEGACY_EXTENSION_SHELL, /button\.disabled = !allowed/);
  assert.match(LEGACY_EXTENSION_SHELL, /pwaBridge\.on\("READY", syncPwaCapabilityButtons\)/);
  assert.match(LEGACY_EXTENSION_SHELL, /if \(!pwaBridge\) return Promise\.resolve\(false\)/);
  for (const capability of ["ruby", "pageTranslate", "ink", "stickyNote", "bookSearch"]) {
    assert.match(
      LEGACY_EXTENSION_SHELL,
      new RegExp(`if \\(!pwaCapability\\("${capability}"\\)\\) return`),
      `${capability} 点击路径必须二次 fail closed`,
    );
  }
});

test("词组入口与其它工具条动作一样读取当前 adapter，Web 使用 reflow 浮层", () => {
  assert.match(WORD_SELECTION_ACTIONS, /window\.__bwSelectionController/);
  assert.match(WORD_SELECTION_ACTIONS, /const adapter = \(window\.RC && RC\.adapter\) \? RC\.adapter\(\) : null/);
  assert.match(WORD_SELECTION_ACTIONS, /typeof adapter\.lookupPhrase === 'function'/);
  assert.doesNotMatch(
    WORD_SELECTION_ACTIONS,
    /if \(window\.__uiShared && window\.PdfAdapter\)[\s\S]{0,160}PdfAdapter\.lookupPhrase/,
  );
  assert.match(ADAPTER, /lookupPhrase: function \(opts\)/);
  assert.match(ADAPTER, /RC\.phrasepop\.show/);
});

test("父壳只接受当前 sandbox frame、null origin、匹配 nonce 的消息", async () => {
  const h = adapterHarness();
  const ready = {
    data: { __rcweb: "ready", __rcwebNonce: NONCE, title: "Safe title" },
    source: h.frameWindow,
    origin: "null",
  };
  h.emit({ ...ready, source: {} });
  h.emit({ ...ready, origin: ORIGIN });
  h.emit({ ...ready, data: { ...ready.data, __rcwebNonce: "wrong" } });
  assert.equal(h.posts.length, 0);

  h.emit(ready);
  assert.equal(h.posts.length, 1);
  assert.equal(h.posts[0].message.__rcweb, "getText");

  h.emit({
    source: h.frameWindow,
    origin: "null",
    data: {
      __rcweb: "api",
      __rcwebNonce: NONCE,
      id: "forbidden",
      path: "/pdf/api/admin",
      method: "POST",
      body: "{}",
    },
  });
  assert.equal(h.posts.at(-1).message.__rcweb, "api-result");
  assert.equal(h.posts.at(-1).message.payload.status, 400);
  assert.equal(h.fetches.length, 0);

  h.emit({
    source: h.frameWindow,
    origin: "null",
    data: {
      __rcweb: "api",
      __rcwebNonce: NONCE,
      id: "vocab",
      path: "/pdf/api/web-vocab",
      method: "POST",
      body: JSON.stringify({ texts: ["A safe sentence."], threshold: 3, min_words: 10 }),
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.fetches.length, 1);
  assert.equal(h.fetches[0].path, "/pdf/api/web-vocab");
  assert.equal(h.fetches[0].options.credentials, "same-origin");
});
