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
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  return { sandbox, pop };
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
