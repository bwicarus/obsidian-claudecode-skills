import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const WORDPOP = readFileSync(
  new URL("_server_deploy/static/pdf/rc-wordpop.js", ROOT),
  "utf8",
);
const PDF_TEMPLATE = readFileSync(
  new URL("_server_deploy/templates/pdf_reader.html", ROOT),
  "utf8",
);
const EPUB_TEMPLATE = readFileSync(
  new URL("_server_deploy/templates/epub_html_reader.html", ROOT),
  "utf8",
);
const HTML_TEMPLATE = readFileSync(
  new URL("_server_deploy/templates/html_reader.html", ROOT),
  "utf8",
);
const PDF_ADAPTER = readFileSync(
  new URL("_server_deploy/static/pdf/pdf-adapter.js", ROOT),
  "utf8",
);
const PDF_WORD_ACTIONS = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js", ROOT),
  "utf8",
);
const PDF_RUNTIME_BINDING = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/27-rc-adapter.js", ROOT),
  "utf8",
);
const PDF_VOCAB = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/12-vocab-sentences.js", ROOT),
  "utf8",
);
const EPUB_RUNTIME = readFileSync(
  new URL("_server_deploy/static/pdf/epub-html.js", ROOT),
  "utf8",
);
const WEB_IMMERSIVE = readFileSync(
  new URL("_server_deploy/static/pdf/web-immersive.js", ROOT),
  "utf8",
);

class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.style = {};
    this.innerHTML = "";
    this.textContent = "";
    this.title = "";
    this.offsetWidth = 320;
    this.offsetHeight = 140;
    this.dataset = {};
    this.jpExampleTarget = null;
    this.listeners = new Map();
    this.attributes = new Map();
    this.classList = {
      toggle() {},
      add() {},
    };
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  querySelector(selector) {
    if (String(selector).startsWith('[data-jpex-id="')) return this.jpExampleTarget;
    return null;
  }
  querySelectorAll() { return []; }

  dispatchClick(target) {
    for (const listener of this.listeners.get("click") || []) {
      listener({ type: "click", target });
    }
  }

  appendChild() {}
  remove() {}
}

function makeHarness() {
  const elements = new Map();
  // Important regression fixture: the PWA template owns this element before
  // rc-wordpop loads; the shared module must still bind its delegated click.
  const pop = new FakeElement("word-pop");
  elements.set("word-pop", pop);
  const resultContent = new FakeElement("result-content");
  elements.set("result-content", resultContent);
  const vocabActions = new FakeElement("vocab-actions");
  elements.set("vocab-actions", vocabActions);
  const documentListeners = new Map();

  const document = {
    visibilityState: "visible",
    head: {
      appendChild(element) {
        if (element.id) elements.set(element.id, element);
      },
    },
    body: {
      appendChild(element) {
        if (element.id) elements.set(element.id, element);
      },
    },
    createElement() {
      return new FakeElement();
    },
    getElementById(id) {
      return elements.get(id) || null;
    },
    querySelectorAll() {
      return [];
    },
    addEventListener(type, listener) {
      if (!documentListeners.has(type)) documentListeners.set(type, []);
      documentListeners.get(type).push(listener);
    },
    removeEventListener() {},
  };
  const sandbox = {
    console,
    document,
    navigator: { onLine: true },
    location: { href: "https://reader.example/pdf/view?ui=shared" },
    innerWidth: 1024,
    innerHeight: 768,
    pageXOffset: 0,
    pageYOffset: 0,
    setTimeout() { return 1; },
    clearTimeout() {},
    setInterval() { return 1; },
    clearInterval() {},
    fetch: async () => ({
      ok: true,
      json: async () => ({
        ok: true,
        word: "was",
        lemma: "be",
        definition: "v. 是",
        mastered: false,
      }),
    }),
    AbortController,
    TextDecoder,
    URL,
    URLSearchParams,
    Map,
    Promise,
    Date,
    Math,
    encodeURIComponent,
    decodeURIComponent,
    RC: {
      toast() {},
      reqJson: async () => ({ ok: true }),
      md(value) { return String(value || ""); },
      typeset() {},
    },
  };
  sandbox.RC.result = {
    _resultReqId: 0,
    openResult(title, src, body) {
      this._resultReqId += 1;
      resultContent.innerHTML = body;
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  return { sandbox, pop, resultContent, vocabActions };
}

test("PWA 预置 word-pop 的掌握按钮通过真实 click 委托调用 late-bound 动作", async () => {
  const { sandbox, pop } = makeHarness();
  vm.runInContext(WORDPOP, vm.createContext(sandbox), {
    filename: "rc-wordpop.js",
  });

  sandbox.RC.wordpop.show({
    word: "was",
    noBreathe: true,
    rect: { left: 20, top: 30, right: 80, bottom: 50 },
  });
  // Let fetch → json → render settle without firing the fake refresh timer.
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));

  assert.match(pop.innerHTML, /data-wp-act="master"/);
  assert.equal(pop.listeners.get("click")?.length, 1);

  const masterButton = new FakeElement("wp-master-btn");
  masterButton.setAttribute("data-wp-act", "master");
  masterButton.closest = (selector) =>
    selector === "[data-wp-act]" ? masterButton : null;
  let calledWith = null;
  // PDF's reader bundle can win selected globals after rc-wordpop loads.
  // Delegation must resolve at click time, not capture the earlier function.
  sandbox._wordPopMaster = (button) => {
    calledWith = button;
  };
  pop.dispatchClick(masterButton);
  assert.equal(calledWith, masterButton);

  // A second render must keep the one delegated listener rather than stacking.
  sandbox.RC.wordpop.show({
    word: "was",
    rect: { left: 20, top: 30, right: 80, bottom: 50 },
  });
  assert.equal(pop.listeners.get("click")?.length, 1);
});

test("App v3 日语词典小框恢复当前形/原形、中文富字段且不混入罗马字或英文例句", async () => {
  const { sandbox, pop, resultContent } = makeHarness();
  const translatedExample = new FakeElement("translated-example");
  pop.jpExampleTarget = translatedExample;
  const translationRequests = [];
  let finishTranslation;
  sandbox.RC.reqJson = async (method, url, body) => {
    translationRequests.push({ method, url, body });
    return await new Promise((resolve) => { finishTranslation = resolve; });
  };
  sandbox.RC.offlineDictionary = {
    CONTRACT: "bw-offline-dictionary/1",
    isLocalMode: () => true,
    lookupJapaneseLegacy: async () => ({
      ok: true,
      jp: true,
      word: "纏めた",
      lemma: "纏める",
      forms: ["纏める"],
      reading: "まとめる",
      reading_kata: "マトメル",
      accent: 0,
      romaji: "matomeru",
      pos: "一段动词 / 及物动词",
      // Prove the renderer uses structured v3 senses rather than this flattened
      // compatibility field.
      translation: "基本翻译",
      definition: "基本翻译",
      zh_senses: [
        { glosses: ["收集，集中"], examples: [{ ja: "意見を纏める", zh: "集中意見" }] },
        { pos: "non-lemma", glosses: ["纏める matomeru alt-of"] },
        { glosses: ["纏めるcontinuative纏めるstem"] },
        { glosses: ["組織，協調"] },
        { glosses: ["結合，混合，融合，合併"] },
        { glosses: ["概括，總結"] },
      ],
      examples: [
        { ja: "意見を纏めた。", en: "The opinions were summarized." },
        { ja: "意見を纏める", zh: "集中意見" },
        { ja: "三つの組織を纏める", zh: "合併三個組織" },
      ],
      // Exercise the renderer's semantic fallback too: old downloaded data
      // can have lemma != surface while inflect is null.
      inflect: null,
      source: "local-jmdict",
      local_zh: true,
      mastered: false,
    }),
  };
  vm.runInContext(WORDPOP, vm.createContext(sandbox), {
    filename: "rc-wordpop.js",
  });

  sandbox.RC.wordpop.show({
    word: "纏めた",
    ctx: "意見を纏めた",
    langs: ["ja"],
    showAnki: false,
    noBreathe: true,
    rect: { left: 20, top: 30, right: 80, bottom: 50 },
  });
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));

  const visibleText = pop.innerHTML.replace(/<[^>]+>/g, "");
  assert.match(pop.innerHTML, /纏める/);
  assert.match(visibleText, /まとめる/);
  assert.match(pop.innerHTML, /平板/);
  assert.match(pop.innerHTML, /当前形 <b>纏めた<\/b>/);
  assert.match(pop.innerHTML, /原形 <b>纏める<\/b>/);
  assert.match(pop.innerHTML, /活用→原形/);
  assert.match(pop.innerHTML, /收集，集中；組織，協調；結合，混合，融合，合併；概括，總結/);
  assert.doesNotMatch(pop.innerHTML, /基本翻译/);
  assert.doesNotMatch(pop.innerHTML, /matomeru|alt-of/);
  assert.doesNotMatch(pop.innerHTML, /The opinions were summarized/);
  assert.match(pop.innerHTML, /意見を纏める/);
  assert.match(pop.innerHTML, /集中意見/);
  assert.match(pop.innerHTML, /意見を纏めた。/);
  assert.match(pop.innerHTML, /Pi 中文翻译中/);
  assert.match(pop.innerHTML, /点这里展开完整字典/);
  assert.match(pop.innerHTML, /☆ 标记掌握/);
  assert.match(pop.innerHTML, /📊 语法/);
  assert.doesNotMatch(pop.innerHTML, /wp-pos-tag|一段动词 \/ 及物动词/);
  assert.doesNotMatch(pop.innerHTML, /🎴 Anki|🖌 标记/);

  assert.deepEqual(JSON.parse(JSON.stringify(translationRequests)), [{
    method: "POST",
    url: "/pdf/api/translate-sentence",
    body: { text: "意見を纏めた。", backend: "ai" },
  }]);
  finishTranslation({ ok: true, zh: "归纳了意见。" });
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(translatedExample.textContent, "归纳了意见。");
  assert.equal(translatedExample.dataset.zhdone, "1");

  await sandbox.dictStreamJP("纏めた", "意見を纏めた");
  assert.match(resultContent.innerHTML, /当前形 <b>纏めた<\/b>/);
  assert.match(resultContent.innerHTML, /原形 <b>纏める<\/b>/);
  assert.match(resultContent.innerHTML, /收集，集中/);
  assert.match(resultContent.innerHTML, /集中意見/);
  assert.match(resultContent.innerHTML, /意見を纏めた。/);
  assert.match(resultContent.innerHTML, /归纳了意见。/);
  assert.doesNotMatch(resultContent.innerHTML, /matomeru|alt-of/);
  assert.doesNotMatch(resultContent.innerHTML, /The opinions were summarized/);
  assert.doesNotMatch(resultContent.innerHTML, /一段动词|及物动词/);
  assert.equal(translationRequests.length, 1, "完整框应复用 exact JA 的会话/Pi 缓存");
});

test("本地例句的迟到 Pi 中文回填不得覆盖后来打开的查词框", async () => {
  const { sandbox, pop } = makeHarness();
  const target = new FakeElement("current-example");
  pop.jpExampleTarget = target;
  const pending = new Map();
  const requested = [];
  sandbox.RC.reqJson = async (method, url, body) => {
    assert.equal(method, "POST");
    assert.equal(url, "/pdf/api/translate-sentence");
    assert.equal(body.backend, "ai");
    requested.push(body.text);
    return await new Promise((resolve) => pending.set(body.text, resolve));
  };
  sandbox.RC.offlineDictionary = {
    CONTRACT: "bw-offline-dictionary/1",
    isLocalMode: () => true,
    lookupJapaneseLegacy: async (word) => ({
      ok: true,
      jp: true,
      word,
      lemma: word,
      reading: "よみ",
      accent: 0,
      translation: "中文义",
      zh_senses: [{ glosses: ["中文义"] }],
      examples: [{ ja: `${word}の例。`, en: `example for ${word}` }],
      source: "local-jmdict",
      local_zh: true,
    }),
  };
  vm.runInContext(WORDPOP, vm.createContext(sandbox), { filename: "rc-wordpop.js" });

  sandbox.RC.wordpop.show({ word: "古い", langs: ["ja"], noBreathe: true });
  await new Promise((resolve) => setImmediate(resolve));
  sandbox.RC.wordpop.show({ word: "新しい", langs: ["ja"], noBreathe: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requested, ["古いの例。"], "Pi 例句翻译必须全模块单并发");
  target.textContent = "新词仍在等待";
  pending.get("古いの例。")({ ok: true, zh: "旧词的迟到翻译" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(target.textContent, "新词仍在等待");
  assert.deepEqual(requested, ["古いの例。", "新しいの例。"], "前一项落定后才启动下一句");
  pending.get("新しいの例。")({ ok: true, zh: "新词的正确翻译" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(target.textContent, "新词的正确翻译");
});

test("App 本地词典未命中的自定义日语词组不自动交给 ReaderPC 或 Pi", async () => {
  const { sandbox, pop } = makeHarness();
  const lookupRequests = [];
  const cachedResults = [];
  let piRequests = 0;
  sandbox.fetch = async () => {
    piRequests += 1;
    throw new Error("unexpected Pi dictionary request");
  };
  sandbox.RC.offlineDictionary = {
    CONTRACT: "bw-offline-dictionary/1",
    isLocalMode: () => true,
    lookupJapaneseLegacy: async (term) => ({
      ok: false,
      jp: true,
      query: term,
      source: "local-jmdict",
      code: "BW_OFFLINE_DICTIONARY_NO_MATCH",
    }),
  };
  sandbox.RC.computerVoice = {
    async lookupJapaneseFallback(request) {
      lookupRequests.push(structuredClone(request));
      return {
        term: request.term,
        mode: request.mode,
        language: "zh-CN",
        text: "根本顾不上那件事",
        source: "pc-codex-cli",
        cached: false,
      };
    },
  };
  sandbox.__BW_READER_RUNTIME__ = {
    dictionaryFallbackCache: {
      async get() { return null; },
      async put(request, result) {
        cachedResults.push({
          request: structuredClone(request),
          result: structuredClone(result),
        });
        return { ...result, cached: true };
      },
    },
  };
  vm.runInContext(WORDPOP, vm.createContext(sandbox), {
    filename: "rc-wordpop.js",
  });

  sandbox.RC.wordpop.show({
    word: "それどころではない",
    ctx: "締切が迫っていて、それどころではない。",
    langs: ["ja"],
    noBreathe: true,
    rect: { left: 20, top: 30, right: 180, bottom: 50 },
  });
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(lookupRequests.length, 0);
  assert.equal(cachedResults.length, 0);
  assert.equal(piRequests, 0);
  assert.match(pop.innerHTML, /未查到/);
});

test("扩展没有 App 私有词典时不自动调用 ReaderPC 或 Pi", async () => {
  const { sandbox, pop } = makeHarness();
  const lookupRequests = [];
  let piRequests = 0;
  sandbox.fetch = async () => {
    piRequests += 1;
    throw new Error("unexpected Pi dictionary request");
  };
  sandbox.RC.computerVoice = {
    async lookupJapaneseFallback(request) {
      lookupRequests.push(structuredClone(request));
      return {
        term: request.term,
        mode: request.mode,
        language: "zh-CN",
        text: "并非无计可施",
        source: "pc-codex-cli",
        cached: false,
      };
    },
  };
  vm.runInContext(WORDPOP, vm.createContext(sandbox), {
    filename: "rc-wordpop.js",
  });

  sandbox.RC.wordpop.show({
    word: "手がないわけではない",
    ctx: "まだ手がないわけではない。",
    langs: ["ja"],
    noBreathe: true,
    rect: { left: 20, top: 30, right: 180, bottom: 50 },
  });
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(piRequests, 0);
  assert.equal(lookupRequests.length, 0);
  assert.match(pop.innerHTML, /未查到/);
});

test("扩展日语预热既不静默访问 Pi 也不自动占用 ReaderPC CLI", async () => {
  const { sandbox } = makeHarness();
  let piRequests = 0;
  let cliRequests = 0;
  sandbox.requestIdleCallback = (callback) => {
    callback();
    return 1;
  };
  sandbox.fetch = async () => {
    piRequests += 1;
    throw new Error("unexpected Pi dictionary prewarm");
  };
  sandbox.RC.computerVoice = {
    async lookupJapaneseFallback() {
      cliRequests += 1;
      throw new Error("unexpected ReaderPC CLI prewarm");
    },
  };
  vm.runInContext(WORDPOP, vm.createContext(sandbox), {
    filename: "rc-wordpop.js",
  });

  sandbox.RC.wordpop.prewarm(["それどころではない"]);
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(piRequests, 0);
  assert.equal(cliRequests, 0);
});

test("掌握与取消掌握先提交本地仓库，服务器失败也不回滚或触发整页确认刷新", async () => {
  const { sandbox, pop } = makeHarness();
  const localChanges = [];
  const repositoryChanges = [];
  let fullRefreshes = 0;
  let repositoryValue = true;
  sandbox.BWReaderRuntime = {
    vocabularyState: {
      CONTRACT: "vocabulary-state/1",
      lookup(spec, property) {
        assert.equal(property, "mastered");
        assert.equal(spec.lemma, "be");
        return { enabled: repositoryValue };
      },
      setMastered(spec, enabled) {
        repositoryValue = enabled;
        repositoryChanges.push({
          lemma: spec.lemma,
          word: spec.word,
          language: spec.language,
          enabled,
        });
        return { durable: Promise.resolve({ ok: true }) };
      },
    },
  };
  sandbox.__masteredLocal = new Set(["be"]);
  sandbox.applyVocabLocalOverride = (lemma, mastered, meta) => {
    localChanges.push({ lemma, mastered, word: meta?.word, forms: meta?.forms });
    return () => localChanges.push({ rollback: true, lemma, mastered });
  };
  sandbox.refreshVocabUnderlinesForAllPages = () => { fullRefreshes += 1; };
  vm.runInContext(WORDPOP, vm.createContext(sandbox), {
    filename: "rc-wordpop.js",
  });
  // Compatibility sync is deliberately rejected: local vocabulary state must
  // remain authoritative and must not call the host projection's undo handle.
  sandbox.RC.reqJson = () => Promise.reject(new Error("server unavailable"));

  sandbox.RC.wordpop.show({
    word: "was",
    noBreathe: true,
    rect: { left: 20, top: 30, right: 80, bottom: 50 },
  });
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));

  // 服务器 quick 响应故意说 mastered=false；本地事实源仍必须决定按钮。
  assert.match(pop.innerHTML, /✓ 已掌握 100/);

  const cancelButton = new FakeElement("wp-master-btn");
  sandbox._wordPopMaster(cancelButton);
  assert.deepEqual(JSON.parse(JSON.stringify(localChanges[0])), {
    lemma: "be",
    mastered: false,
    word: "was",
    forms: [],
  });
  assert.equal(fullRefreshes, 0);
  assert.equal(cancelButton.textContent, "☆ 标记掌握");

  const masterButton = new FakeElement("wp-master-btn");
  sandbox._wordPopMaster(masterButton);
  assert.deepEqual(JSON.parse(JSON.stringify(localChanges[1])), {
    lemma: "be",
    mastered: true,
    word: "was",
    forms: [],
  });
  assert.equal(fullRefreshes, 0);
  assert.equal(masterButton.textContent, "✓ 已掌握 100");
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(JSON.parse(JSON.stringify(repositoryChanges)), [
    { lemma: "be", word: "was", language: "en", enabled: false },
    { lemma: "be", word: "was", language: "en", enabled: true },
  ]);
  assert.equal(repositoryValue, true);
  assert.equal(localChanges.some((change) => change.rollback), false);
});

test("PDF / EPUB / HTML load rc-wordpop before their host driver", () => {
  assert.ok(PDF_TEMPLATE.indexOf('id="word-pop"') < PDF_TEMPLATE.indexOf("rc-wordpop.js"));
  assert.ok(PDF_TEMPLATE.indexOf("rc-wordpop.js") < PDF_TEMPLATE.indexOf("pdf-adapter.js"));
  assert.ok(
    PDF_TEMPLATE.indexOf("pdf-adapter.js") <
      PDF_TEMPLATE.indexOf('<script type="module" src="/static/pdf/reader.js'),
  );

  assert.ok(EPUB_TEMPLATE.indexOf('id="word-pop"') < EPUB_TEMPLATE.indexOf("rc-wordpop.js"));
  assert.ok(
    EPUB_TEMPLATE.indexOf("rc-wordpop.js") <
      EPUB_TEMPLATE.indexOf('<script src="/static/pdf/epub-html.js'),
  );

  // HTML creates #word-pop lazily in _ensurePop; it has no pre-owned node.
  assert.ok(
    HTML_TEMPLATE.indexOf("rc-wordpop.js") <
      HTML_TEMPLATE.indexOf('<script src="/static/pdf/html-reader.js'),
  );
  assert.match(WORDPOP, /if \(!p\)[\s\S]*?_bindWpDelegate\(p\);/);
});

test("PDF shared gate is established before reader bundle can overwrite actions", () => {
  assert.match(PDF_ADAPTER, /window\.__uiShared\s*=/);
  for (const action of [
    "_expandWordFull",
    "_speakCurWord",
    "_wordPopMaster",
    "_wordPopGrammar",
  ]) {
    assert.match(
      PDF_WORD_ACTIONS,
      new RegExp(`if \\(!window\\.__uiShared\\) \\{\\s*window\\.${action}\\s*=`),
      action,
    );
  }
});

test("PDF 共享词组操作只更新本地字符投影，不把整页重拉当作确认步骤", () => {
  assert.match(PDF_ADAPTER, /if \(h\.phraseFavoriteUpdate\) h\.phraseFavoriteUpdate\(t, nowFav\)/);
  assert.match(PDF_ADAPTER, /if \(h\.phraseMasteryUpdate\) h\.phraseMasteryUpdate\(t, mastered\)/);
  assert.match(PDF_RUNTIME_BINDING, /phraseFavoriteUpdate:\s*\(text, enabled\)/);
  assert.match(PDF_RUNTIME_BINDING, /_applyPhraseMergesAll\(\)/);
  assert.match(PDF_RUNTIME_BINDING, /phraseMasteryUpdate:\s*\(text, enabled\)/);

  const sharedCallbacks = PDF_ADAPTER.slice(
    PDF_ADAPTER.indexOf("var _showOpts"),
    PDF_ADAPTER.indexOf("// ── 点击已有常亮高亮"),
  );
  assert.doesNotMatch(sharedCallbacks, /refreshCharsWForAllPages/);
  assert.match(PDF_WORD_ACTIONS, /function _applyVocabularyPhraseProjection\(records\)/);
  const phraseLoader = PDF_WORD_ACTIONS.slice(
    PDF_WORD_ACTIONS.indexOf("async function _loadPhraseFavs()"),
    PDF_WORD_ACTIONS.indexOf("window.onPhrase"),
  );
  assert.match(phraseLoader, /repo\.snapshot\(\)/);
  assert.match(phraseLoader, /_applyVocabularyPhraseProjection\(repo\.snapshot\(\)\)/);
  assert.match(
    PDF_RUNTIME_BINDING,
    /document\.addEventListener\('bw:vocabulary-state-change'/,
  );
});

test("三个宿主的 mastery 本地投影都保留候选，取消掌握不退化成确认 GET", () => {
  assert.match(PDF_VOCAB, /window\.applyVocabLocalOverride\s*=\s*function/);
  assert.match(PDF_VOCAB, /return function restoreVocabLocalOverride/);
  assert.doesNotMatch(
    PDF_WORD_ACTIONS,
    /pw\.__vocabMarks\s*=\s*after/,
    "PDF 乐观更新不得删除全候选目录",
  );

  assert.match(EPUB_RUNTIME, /window\.applyVocabLocalOverride\s*=\s*function/);
  assert.match(EPUB_RUNTIME, /function _rerenderVisibleVocab/);
  assert.doesNotMatch(
    EPUB_RUNTIME,
    /onMastered:\s*function\s*\(\)\s*\{\s*if\s*\(window\.refreshVocabUnderlinesForAllPages/,
    "EPUB wordpop 成功回调不得再拉整份 mastery map",
  );

  const webApply = WEB_IMMERSIVE.match(
    /window\.applyVocabLocalOverride\s*=\s*function[\s\S]*?\n  };/,
  )?.[0] || "";
  assert.match(webApply, /_setVocabUnderlineLocal/);
  assert.doesNotMatch(webApply, /refreshVocabUnderlinesForAllPages/);
  assert.doesNotMatch(webApply, /fetch\s*\(/);
});

test("迟到的掌握词表 GET 不得覆盖尚未同步的本地掌握或取消掌握", async () => {
  const start = PDF_VOCAB.indexOf("const _VOVR_KEY = 'vocab-override-v1';");
  const end = PDF_VOCAB.indexOf("// 找点击位置最近的非空格 char index", start);
  assert.ok(start >= 0 && end > start, "must find the canonical local mastery runtime");
  const source = PDF_VOCAB.slice(start, end);

  async function runCase({ initial, remote, next }) {
    const storage = new Map([
      ["vocab-mastered-v1", JSON.stringify({ ts: Date.now(), set: initial })],
    ]);
    let releaseRemote;
    const sandbox = {
      console,
      Date,
      JSON,
      Map,
      Set,
      localStorage: {
        getItem(key) { return storage.get(key) ?? null; },
        setItem(key, value) { storage.set(key, String(value)); },
      },
      document: { querySelectorAll() { return []; } },
      renderVocabUnderlines() {},
      fetch() {
        return new Promise((resolve) => {
          releaseRemote = () => resolve({
            json: async () => ({ ok: true, mastered: remote }),
          });
        });
      },
      encodeURIComponent,
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.runInContext(source, vm.createContext(sandbox), {
      filename: "reader.src/12-vocab-sentences.local-mastery.js",
    });

    sandbox.applyVocabLocalOverride("be", next, {
      word: "was",
      forms: ["been", "being"],
    });
    releaseRemote();
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    return sandbox;
  }

  const mastered = await runCase({ initial: [], remote: [], next: true });
  assert.equal(mastered.__masteredLocal.has("be"), true);
  assert.equal(mastered.__vocabOverride.get("be"), true);

  const unmastered = await runCase({
    initial: ["be"],
    remote: ["be"],
    next: false,
  });
  assert.equal(unmastered.__masteredLocal.has("be"), false);
  assert.equal(unmastered.__vocabOverride.get("be"), false);
});
