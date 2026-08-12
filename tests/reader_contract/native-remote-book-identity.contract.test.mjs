import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const manifest = JSON.parse(read("ios/BWReader/native_reader_interface_manifest.json"));
const runtime = read("_server_deploy/static/pdf/native-local-runtime.js");
const gateway = read("ios/BWReader/App/ReaderNativePiGateway.swift");

function route(path) {
  const found = manifest.routes.find((candidate) => candidate.path === path);
  assert.ok(found, `missing native route ${path}`);
  return found;
}

test("book-aware Japanese dictionary and snippet routes declare exact identities", () => {
  assert.deepEqual(route("/pdf/api/dict-jp").remoteBook, {
    mode: "conditional",
    scope: "current",
    requiredMethods: [],
    identities: [{
      methods: ["GET"],
      location: "query",
      pointer: "/file",
      transform: "exact",
    }],
    continuation: null,
  });
  assert.deepEqual(route("/pdf/api/snippets-to-async").remoteBook, {
    mode: "required",
    scope: "current",
    requiredMethods: ["POST"],
    identities: [{
      methods: ["POST"],
      location: "json",
      pointer: "/file",
      transform: "exact",
    }],
    continuation: null,
  });
  assert.equal(
    route("/pdf/api/sync-batch").remoteBook,
    null,
    "the opaque outer batch must never be rewritten as one book request",
  );
});

test("every first-party dict-jp request supplies book, page, languages and sentence context", () => {
  const sources = [
    "_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js",
    "_server_deploy/static/pdf/reader.src/19-dict.js",
    "_server_deploy/static/pdf/rc-phrasepop.js",
    "_server_deploy/static/pdf/rc-wordpop.js",
  ];
  for (const path of sources) {
    const source = read(path);
    assert.match(source, /pdf\/api\/dict-jp\?word=/, path);
    assert.match(source, /&file=/, path);
    assert.match(source, /&page=/, path);
    assert.match(source, /&langs=/, path);
    assert.match(source, /&context=/, path);
  }
  const adapter = read("_server_deploy/static/pdf/pdf-adapter.js");
  assert.match(adapter, /page:\s*page,\s*langs:\s*langs/);
});

test("PDF, EPUB and extension phrase entry points explicitly preserve sentence context", () => {
  const pdfEntry = read("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js");
  assert.match(pdfEntry, /showPhrasePopover\(t,\s*\{[\s\S]{0,180}context:/);
  assert.match(pdfEntry, /adapter\.lookupPhrase\(\{[\s\S]{0,240}context:/);

  const pdfAdapter = read("_server_deploy/static/pdf/pdf-adapter.js");
  assert.ok(
    (pdfAdapter.match(/context:\s*opts\.context\s*\|\|\s*''/g) || []).length >= 2,
    "both cached popup and background dictionary lookup must keep context",
  );

  const epub = read("_server_deploy/static/pdf/epub-html.js");
  assert.match(epub, /RC\.phrasepop\.show\(\{[\s\S]{0,140}context:\s*ctx\s*\|\|\s*''/);
  assert.match(epub, /context:\s*ctx\s*\|\|\s*''\s*\}\s*:\s*null/);

  const extension = read("extensions/bw-reader-webext/content.js");
  assert.match(extension, /RC\.phrasepop\.show\(\{[^\n]*context:\s*s\.context/);

  const dictionary = read("scripts/vocab/dict_sources.py");
  assert.match(dictionary, /不可信引用文本/);
  assert.match(dictionary, /json\.dumps\(str\(context\s+or\s+""\)\[:160\]/);
});

test("snippet clients send structured top-level source identity without loopback links", () => {
  const pdfClients = [
    "_server_deploy/static/pdf/reader.src/17-highlight.js",
    "_server_deploy/static/pdf/reader.src/18-grammar.js",
    "_server_deploy/static/pdf/reader.src/20-result-draft.js",
  ];
  for (const path of pdfClients) {
    const source = read(path);
    assert.match(source, /file:\s*FILE_REL\s*\|\|\s*''/, path);
    assert.match(source, /source:\s*\{\s*kind:\s*'pdf',\s*page:/, path);
    assert.doesNotMatch(
      source,
      /原文出处链接[^\n]*(?:location\.origin|srcUrl)/,
      `${path} must not embed its private origin in snippet text`,
    );
  }

  const sharedResult = read("_server_deploy/static/pdf/rc-result.js");
  assert.match(sharedResult, /function structuredSource\(/);
  assert.equal(
    (sharedResult.match(/source:\s*structuredSource\(/g) || []).length,
    2,
    "both shared draft creation and result-to-Anki must carry structured source",
  );
  assert.doesNotMatch(sharedResult, /var srcUrl\s*=\s*srcInfo\.sourceUrl/);

  const snippets = read("_server_deploy/static/pdf/rc-snippets.js");
  assert.match(snippets, /var body = \{ file: file, source: \{ kind: kind \}/);
  assert.match(snippets, /var snip = \{ text: text \}/);
  assert.doesNotMatch(snippets, /var snip = \{[^\n]*file:/);
});

test("conditional identities are omitted when the request has no remote book fact", () => {
  const epub = read("_server_deploy/static/pdf/epub-html.js");
  assert.match(
    epub,
    /reqJson\('POST', '\/pdf\/api\/epub-furigana', \{ texts: nodes\.map/,
  );
  assert.doesNotMatch(
    epub,
    /reqJson\('POST', '\/pdf\/api\/epub-furigana', \{ file:/,
  );

  const review = read("_server_deploy/static/pdf/rc-review.js");
  assert.match(
    review,
    /if \(!raw\.file && raw\.file_rel\) raw = Object\.assign\(\{\}, raw, \{ file: raw\.file_rel \}\)/,
  );
  assert.deepEqual(route("/pdf/api/review-queue").remoteBook.identities, [{
    methods: ["POST"],
    location: "json",
    pointer: "/context/file",
    transform: "exact",
  }]);

  for (const path of [
    "/api/assistant/chat",
    "/api/assistant/voice-tool",
    "/pdf/api/epub-assistant",
  ]) {
    assert.equal(route(path).remoteBook.mode, "conditional", path);
    assert.deepEqual(route(path).remoteBook.requiredMethods, [], path);
  }
  for (const path of [
    "/pdf/api/epub-action",
    "/pdf/api/formula-ocr",
    "/pdf/api/formula-ocr-status",
    "/pdf/api/page-overlay",
  ]) {
    assert.equal(route(path).remoteBook.mode, "required", path);
    assert.deepEqual(
      route(path).remoteBook.requiredMethods,
      route(path).methods,
      path,
    );
  }

  assert.match(
    gateway,
    /let mayOmitUnboundLocalIdentity = !identityRequired/,
    "required routes and required methods must never enter the omission path",
  );
  assert.match(gateway, /Self\.isStrictLocalBookIdentity\(/);
  assert.match(gateway, /Self\.removeJSONPointer\(/);
  assert.match(
    gateway,
    /\^localbook:\[a-f0-9\]\{64\}\$/,
    "the legacy opaque local identity remains narrowly recognized",
  );
  assert.match(
    gateway,
    /\^\(\?:localbook:\)\?localbook-\[a-f0-9\]\{64\}\$/,
    "only the current strict localbook token shape may be omitted",
  );
  assert.match(
    gateway,
    /guard transform == \.exact else \{ return false \}/,
    "composite and malformed identities must continue to fail closed",
  );
});

test("native sync-batch splits only the old bounded outbox surface by declared owner", () => {
  const gate = runtime.indexOf("assertNoNativePDFMutationJournal().then");
  const dispatch = runtime.indexOf("url.pathname === '/pdf/api/sync-batch'");
  assert.ok(gate >= 0 && dispatch > gate, "persistent PDF recovery must gate the split batch");
  assert.match(runtime, /function nativeSyncBatchTarget\(op\)/);
  assert.match(runtime, /\^\\\/pdf\\\/api\\\/entity\\\//);
  assert.match(runtime, /declaredNativeInterface\(target\.url\.pathname, target\.method\)/);
  assert.match(runtime, /route\.owner === 'local'\s*\? localFetch\(target\.url\.href, nestedInit, true, writerLease\)\s*:\s*nativePiFetch/);
  assert.match(runtime, /results\[index\] = result/);
  assert.match(runtime, /ownerNamespace:\s*lease\.namespace/);
  assert.match(runtime, /generation:\s*lease\.generation/);
  assert.match(runtime, /X-BW-Mutation-Id/);
  assert.doesNotMatch(
    runtime.slice(dispatch, dispatch + 180),
    /nativePiFetch\(input, init, route\)/,
    "the opaque outer batch must not be sent to Pi",
  );
});
