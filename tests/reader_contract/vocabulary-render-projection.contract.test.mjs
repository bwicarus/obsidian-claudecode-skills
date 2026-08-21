import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const PDF_CHAR = readFileSync(
  new URL(
    "../../_server_deploy/static/pdf/reader.src/08-charlayer.js",
    import.meta.url,
  ),
  "utf8",
);
const PDF_VOCAB = readFileSync(
  new URL(
    "../../_server_deploy/static/pdf/reader.src/12-vocab-sentences.js",
    import.meta.url,
  ),
  "utf8",
);
const PDF_WORDPOP = readFileSync(
  new URL(
    "../../_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js",
    import.meta.url,
  ),
  "utf8",
);
const EPUB = readFileSync(
  new URL("../../_server_deploy/static/pdf/epub-html.js", import.meta.url),
  "utf8",
);
const WEB = readFileSync(
  new URL("../../_server_deploy/static/pdf/web-immersive.js", import.meta.url),
  "utf8",
);

function between(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.ok(from >= 0, `missing start marker: ${start}`);
  assert.ok(to > from, `missing end marker: ${end}`);
  return source.slice(from, to);
}

function fakeRepository(predicate) {
  const calls = [];
  return {
    CONTRACT: "vocabulary-state/1",
    calls,
    isMastered(spec) {
      calls.push(structuredClone(spec));
      return predicate(spec);
    },
  };
}

test("PDF renderer resolves inflected surface through shared lemma before legacy fallback", () => {
  const helper = between(
    PDF_CHAR,
    "function _vocabularyStateRepo()",
    "function renderVocabUnderlines",
  );
  const repository = fakeRepository(
    (spec) =>
      spec.kind === "word" &&
      spec.language === "en" &&
      spec.lemma === "be" &&
      spec.word === "was",
  );
  const sandbox = {
    window: {
      BWReaderRuntime: { vocabularyState: repository },
    },
  };
  vm.runInContext(
    `${helper}
     globalThis.result = _vocabularyStateMarkMastered({
       word: "was", lemma: "be", label_slug: "new"
     });`,
    vm.createContext(sandbox),
  );
  assert.equal(sandbox.result, true);
  assert.deepEqual(repository.calls[0], {
    kind: "word",
    language: "en",
    lemma: "be",
    word: "was",
    surface: "was",
    forms: [],
  });

  sandbox.window.BWReaderRuntime = {};
  vm.runInContext(
    "globalThis.missing = _vocabularyStateMarkMastered({word:'was',lemma:'be'});",
    sandbox,
  );
  assert.equal(sandbox.missing, false);
});

test("PDF renderer keeps server-mastered hidden unless an explicit local unknown overrides it", () => {
  const renderer = between(
    PDF_CHAR,
    "function _vocabularyStateRepo()",
    "function _vocabUnderlineEnabled()",
  );
  const appended = [];
  const layer = {
    innerHTML: "stale",
    appendChild(value) { appended.push(value); },
  };
  const page = {
    clientWidth: 100,
    clientHeight: 100,
    __pageWPt: 100,
    __pageHPt: 100,
    querySelector(selector) {
      if (selector === ".vocab-layer") return layer;
      if (selector === "canvas") return { clientWidth: 100, clientHeight: 100 };
      return null;
    },
  };
  const sandbox = {
    _vocabUnderlineEnabled: () => true,
    ensurePageLayer: () => layer,
    document: {
      createElement: () => ({ className: "", style: {} }),
    },
    window: {
      BWReaderRuntime: {},
      __masteredLocal: new Set(),
      __vocabOverride: new Map(),
    },
    page,
    marks: [{
      word: "中学生",
      lemma: "中学生",
      surface: "中学生",
      forms: ["中學生"],
      label_slug: "mastered",
      rects: [[1, 2, 20, 8]],
    }],
  };
  vm.runInContext(
    `${renderer}
     renderVocabUnderlines(page, marks);`,
    vm.createContext(sandbox),
  );
  assert.equal(layer.innerHTML, "");
  assert.equal(appended.length, 0, "empty positive-set must not resurrect mastered marks");

  sandbox.window.__vocabOverride.set("中学生", false);
  vm.runInContext("renderVocabUnderlines(page, marks);", sandbox);
  assert.equal(appended.length, 1, "explicit local unknown must win over stale server mastery");
});

test("native overlay enrichment repaints the loaded page and ignores stale or foreign events", () => {
  const source = between(
    PDF_CHAR,
    "const _nativePageOverlayEnrichment",
    "async function loadCharsAndBindLayer",
  );
  let listener = null;
  const painted = [];
  const wrap = { isConnected: true, __pageTextRevision: "local-revision-2" };
  const sandbox = {
    FILE_REL: "localbook:book-a",
    document: {
      querySelectorAll(selector) {
        assert.equal(selector, '[data-page-num="3"]');
        return [wrap];
      },
    },
    renderVocabUnderlines(_wrap, marks) {
      painted.push(marks.map((mark) => mark.word));
    },
    renderRubyLayer() {},
    renderVocabSentences() {},
    window: {
      __BW_NATIVE_LOCAL_READER__: true,
      addEventListener(type, callback) {
        assert.equal(type, "bw:native-page-overlay-enrichment");
        listener = callback;
      },
    },
  };
  vm.runInContext(source, vm.createContext(sandbox));
  assert.equal(typeof listener, "function");
  const detail = {
    contract: "reader-native-page-overlay-enrichment/1",
    file: "localbook:book-a",
    page: 3,
    localRevision: "local-revision-2",
    savedAt: 20,
    vocab_marks: [{ word: "new" }],
    vocab_sentences: [{ text: "new sentence" }],
    mastered_furi: ["既知"],
  };
  listener({ detail });
  assert.deepEqual(painted, [["new"]]);
  assert.deepEqual(wrap.__vocabSentences, detail.vocab_sentences);
  assert.equal(wrap.__masteredFuri.has("既知"), true);

  listener({ detail: { ...detail, savedAt: 10, vocab_marks: [{ word: "old" }] } });
  assert.deepEqual(painted, [["new"]]);
  listener({ detail: { ...detail, file: "localbook:book-b", savedAt: 30 } });
  assert.deepEqual(painted, [["new"]]);
  listener({
    detail: {
      ...detail,
      localRevision: "replacement-revision",
      savedAt: 30,
      vocab_marks: [{ word: "replacement" }],
    },
  });
  assert.deepEqual(painted, [["new"]], "rects for another text revision stay off this page");
});

test("phrase refresh cannot erase native enrichment that arrived before page chars", async () => {
  const refresh = between(
    PDF_WORDPOP,
    "let _charsWGen = 0;",
    "async function refreshCharsWForAllPages()",
  );
  const page = {
    __charBoxes: [],
    __pageTextRevision: "local-revision-2",
  };
  const painted = [];
  const sandbox = {
    FILE_REL: "localbook:book-a",
    CHARS_VER: 8,
    encodeURIComponent,
    _nativePageOverlayEnrichment: new Map([[2, {
      localRevision: "local-revision-2",
      vocab_marks: [{ word: "cached", rects: [] }],
    }]]),
    localStorage: { setItem() {} },
    fetch(url) {
      return Promise.resolve({
        json: () => Promise.resolve(
          String(url).includes("page-overlay")
            ? { ok: true, cv: "local-revision-2", vocab_marks: [] }
            : {
              ok: true,
              revision: "local-revision-2",
              chars: [],
              furigana: [],
            },
        ),
      });
    },
    _applyPhraseMergesLocal() {},
    _rubyEnabled: () => false,
    renderVocabUnderlines(_page, marks) {
      painted.push(marks.map((mark) => mark.word));
    },
    page,
    result: null,
  };
  vm.runInContext(
    `${refresh}
     _charsWGen = 1;
     result = _refreshOnePageCharsW(page, 2, 1);`,
    vm.createContext(sandbox),
  );
  await sandbox.result;
  assert.deepEqual(
    JSON.parse(JSON.stringify(page.__vocabMarks)),
    [{ word: "cached", rects: [] }],
  );
  assert.deepEqual(painted, [["cached"]]);
});

test("EPUB renderer queries word and phrase records with the host language", () => {
  const helper = between(
    EPUB,
    "function _epVocabularyStateRepo()",
    "var _vocabMasteredLocal",
  );
  const repository = fakeRepository(
    (spec) =>
      (spec.kind === "word" && spec.language === "en" && spec.lemma === "be") ||
      (spec.kind === "phrase" && spec.language === "ja" && spec.text === "お世話になります"),
  );
  const sandbox = {
    Array,
    window: {
      BWReaderRuntime: { vocabularyState: repository },
    },
  };
  vm.runInContext(
    `${helper}
     globalThis.wordResult =
       _epVocabularyStateMastered("word", "en", "was", "be", []);
     globalThis.phraseResult =
       _epVocabularyStateMastered(
         "phrase", "ja", "お世話になります", "お世話になります", []
       );`,
    vm.createContext(sandbox),
  );
  assert.equal(sandbox.wordResult, true);
  assert.equal(sandbox.phraseResult, true);
  assert.deepEqual(
    repository.calls.map(({ kind, language }) => [kind, language]),
    [["word", "en"], ["phrase", "ja"]],
  );
});

test("web renderer combines shared repository projection with the session fallback", () => {
  const helper = between(
    WEB,
    "function vocabWordKey",
    "function clearFallbackUnderlines",
  );
  const repository = fakeRepository(
    (spec) => spec.language === "en" && spec.lemma === "be" && spec.word === "was",
  );
  const sandbox = {
    Array,
    VMASTER: Object.create(null),
    window: {
      BWReaderRuntime: { vocabularyState: repository },
    },
  };
  vm.runInContext(
    `${helper}
     globalThis.sharedResult =
       vocabOccurrenceHidden({surface:"was",lemma:"be"});
     VMASTER.be = true;
     window.BWReaderRuntime = {};
     globalThis.legacyResult =
       vocabOccurrenceHidden({surface:"was",lemma:"be"});`,
    vm.createContext(sandbox),
  );
  assert.equal(sandbox.sharedResult, true);
  assert.equal(sandbox.legacyResult, true);
});

test("all host subscriptions repaint locally and never own vocabulary writes", () => {
  assert.match(
    PDF_CHAR,
    /if \(_vocabularyStateMarkMastered\(m\)\) return false;/,
  );
  for (const call of [
    /_epVocabularyStateMastered\('phrase', lang0, key, l0\)/,
    /_epVocabularyStateMastered\('word', 'en', surface, infoLemma\)/,
    /_epVocabularyStateMastered\('word', 'ja', surfaceJa, lemmaJa\)/,
  ]) {
    assert.match(EPUB, call);
  }
  assert.match(
    WEB,
    /VMASTER\[[^\]]+\]\s*\|\|\s*vocabularyStateOccurrenceMastered\(o\)/,
  );

  const subscriptions = [
    between(
      PDF_VOCAB,
      "(function _bindVocabularyStateUnderlineProjection()",
      "// 找点击位置最近的非空格 char index",
    ),
    between(
      EPUB,
      "(function _bindEpVocabularyStateProjection()",
      "// rc-wordpop 的统一 mastery 本地投影",
    ),
    between(
      WEB,
      "(function bindVocabularyStateProjection()",
      "// mastery 是纯本地投影",
    ),
  ];
  for (const source of subscriptions) {
    assert.match(source, /repo\.subscribe/);
    assert.doesNotMatch(source, /\bfetch\s*\(/);
    assert.doesNotMatch(source, /\.setMastered\s*\(/);
    assert.doesNotMatch(source, /\.setPhraseFavorite\s*\(/);
  }
  for (const source of [PDF_CHAR, PDF_VOCAB, EPUB, WEB]) {
    assert.doesNotMatch(source, /\.setMastered\s*\(/);
    assert.doesNotMatch(source, /\.setPhraseFavorite\s*\(/);
  }
});
