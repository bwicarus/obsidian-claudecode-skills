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
