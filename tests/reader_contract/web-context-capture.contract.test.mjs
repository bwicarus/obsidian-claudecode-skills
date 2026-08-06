import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";

const ROOT = new URL("../../", import.meta.url);
const CONTENT = readFileSync(
  new URL("extensions/bw-reader-webext/content.js", ROOT),
  "utf8",
);

test("source instance id is a real 128-bit base64url value", () => {
  const start = CONTENT.indexOf("function createSourceInstanceId()");
  const end = CONTENT.indexOf("var sourceInstanceId = createSourceInstanceId()", start);
  assert.ok(start >= 0 && end > start);
  const factory = new Function(
    "window",
    "Uint8Array",
    "Math",
    `${CONTENT.slice(start, end)}; return createSourceInstanceId;`,
  );
  const create = factory(
    {
      crypto: webcrypto,
      btoa: (value) => Buffer.from(value, "binary").toString("base64"),
    },
    Uint8Array,
    Math,
  );
  const id = create();
  assert.match(id, /^[A-Za-z0-9_-]{22}$/);
  const base64 = id.replace(/-/g, "+").replace(/_/g, "/") + "==";
  assert.equal(Buffer.from(base64, "base64").byteLength, 16);
});

test("web context only reports the focused page even on forced refresh", () => {
  const reportStart = CONTENT.indexOf("function report(force)");
  const reportEnd = CONTENT.indexOf("function schedule(force)", reportStart);
  assert.ok(reportStart >= 0 && reportEnd > reportStart);
  const report = CONTENT.slice(reportStart, reportEnd);
  assert.match(report, /if \(!focused\) \{/);
  assert.doesNotMatch(report, /!focused\s*&&\s*!force/);
});

test("focused web context has a bounded heartbeat that cannot bypass focus", () => {
  assert.match(CONTENT, /var ACTIVE_CONTEXT_HEARTBEAT_MS = 60000;/);
  assert.match(
    CONTENT,
    /window\.setInterval\(function \(\) \{[\s\S]*if \(contextSyncEnabled\) schedule\(true\);[\s\S]*ACTIVE_CONTEXT_HEARTBEAT_MS/,
  );
});

test("article extraction consumes live text nodes once and excludes hidden ancestors", () => {
  const extractStart = CONTENT.indexOf("function articleText(root)");
  const extractEnd = CONTENT.indexOf("function snapshot()", extractStart);
  assert.ok(extractStart >= 0 && extractEnd > extractStart);
  const extract = CONTENT.slice(extractStart, extractEnd);
  assert.match(extract, /NodeFilter\.SHOW_TEXT/);
  assert.doesNotMatch(extract, /NodeFilter\.SHOW_ELEMENT/);
  assert.doesNotMatch(extract, /cloneNode/);
  assert.match(extract, /el\.matches\(ARTICLE_DROP\)/);
  assert.match(extract, /style\.display !== "none"/);
  assert.match(extract, /if \(ok && el\.parentElement\) ok = rendered\(el\.parentElement\)/);
  assert.match(extract, /if \(node\) articleTextTraversalTruncated = true/);
});

test("web context follows the current viewport and keeps a marked reading-region fallback", () => {
  const viewportStart = CONTENT.indexOf("function viewportArticleText(root)");
  const snapshotStart = CONTENT.indexOf("function snapshot()", viewportStart);
  const reportStart = CONTENT.indexOf("function report(force)", snapshotStart);
  assert.ok(viewportStart >= 0 && snapshotStart > viewportStart && reportStart > snapshotStart);

  const viewport = CONTENT.slice(viewportStart, snapshotStart);
  assert.match(viewport, /document\.createRange\(\)/);
  assert.match(viewport, /range\.getClientRects\(\)/);
  assert.match(
    viewport,
    /function clippingRect\(el\)[\s\S]*style\.overflowX[\s\S]*style\.overflowY/,
  );
  assert.match(
    viewport,
    /function textSliceForLines\(value, rects, firstLine, endLine\)[\s\S]*value\.slice\(start, end\)/,
  );
  assert.match(
    viewport,
    /if \(!visibleText\.trim\(\)\) return null/,
  );
  assert.match(viewport, /viewKey:[\s\S]*viewportRevision[\s\S]*scrollTop/);

  const snapshot = CONTENT.slice(snapshotStart, reportStart);
  assert.match(
    snapshot,
    /fullText = whole;[\s\S]*var viewport = viewportArticleText\(root\);[\s\S]*visibleText = viewport\.visibleText[\s\S]*beforeText = viewport\.beforeText[\s\S]*afterText = viewport\.afterText/,
  );
  assert.match(snapshot, /visibleText = root === body \? fullText : articleText\(root\)/);
  assert.match(snapshot, /viewKey: viewKey/);
  assert.match(
    CONTENT,
    /signature = snap\.url \+ "\|" \+ snap\.title \+ "\|" \+ snap\.viewKey/,
  );
});

test("web context separates canonical full document from marked viewport context", () => {
  assert.match(CONTENT, /activationRevision: activationRevision/);
  assert.match(CONTENT, /contentRevision: contentRevision/);
  assert.match(CONTENT, /boundRelayDocumentText[\s\S]*256 \* 1024[\s\S]*768 \* 1024/);
  assert.match(CONTENT, /BW_READER_CONTEXT_POST/);
  assert.doesNotMatch(CONTENT, /bwActivePageContextV1/);
  assert.match(CONTENT, /function canonicalDocumentKey\(\)[\s\S]*parsed\.hash = ""/);
  assert.match(CONTENT, /function createSourceInstanceId\(\)[\s\S]*new Uint8Array\(16\)/);
  assert.match(
    CONTENT,
    /viewportPayload = \{[\s\S]*beforeText:[\s\S]*visibleText:[\s\S]*afterText:[\s\S]*selectionState:/,
  );
  const viewportStart = CONTENT.indexOf("var viewportPayload = {");
  const viewportEnd = CONTENT.indexOf("return {", viewportStart);
  assert.doesNotMatch(
    CONTENT.slice(viewportStart, viewportEnd),
    /selectionRegions/,
    "selection region metadata must not change reader-viewport/1",
  );
  assert.match(
    CONTENT,
    /document: \{[\s\S]*sourceInstanceId:[\s\S]*documentKey: canonicalDocumentKey\(\)[\s\S]*text: fullText/,
  );
  assert.match(CONTENT, /truncated: fullTextTruncated/);
  assert.match(
    CONTENT,
    /var whole = String\(body\.innerText \|\| ""\);[\s\S]*fullText = whole/,
  );
  assert.match(
    CONTENT,
    /text: visibleText,[\s\S]*selectionRegions: selectionRegions,[\s\S]*viewport: viewportPayload/,
  );
  assert.match(
    CONTENT,
    /function prepareRelaySnapshot\(snap\)[\s\S]*Object\.assign\(\{\}, snap,[\s\S]*document:/,
  );
  assert.doesNotMatch(CONTENT, /var legacyPage = \{|bw-page-context\/1/);
  assert.match(
    CONTENT,
    /window\.addEventListener\("rc:inkchange"[\s\S]*schedule\(true\)[\s\S]*window\.addEventListener\("bw:browser-control-refresh"[\s\S]*lastBrowserControlCorrelation = requestId[\s\S]*report\(true\)/,
  );
  assert.match(
    CONTENT,
    /viewportPayload\.controlCorrelation = lastBrowserControlCorrelation/,
    "the post-control viewport must carry the exact browser request correlation",
  );
});

test("root and inner scrollers share the bounded viewport refresh path", () => {
  assert.match(
    CONTENT,
    /function noteViewportScroll\(\) \{[\s\S]*viewportRevision \+= 1;[\s\S]*schedule\(false\)/,
  );
  assert.match(
    CONTENT,
    /window\.addEventListener\("scroll", noteViewportScroll, \{ passive: true \}\)/,
  );
  assert.match(
    CONTENT,
    /document\.addEventListener\("scroll", function \(event\)[\s\S]*noteViewportScroll\(\);[\s\S]*\{ capture: true, passive: true \}/,
  );
  assert.match(CONTENT, /var THROTTLE_MS = 1500;/);
});

test("A to B to A foreground activation resends corpus even without hidden", () => {
  assert.match(CONTENT, /var activationRevision = 1/);
  assert.match(CONTENT, /\["pageshow", "focus", "resume"\]/);
  assert.match(CONTENT, /noteForegroundActivation[\s\S]*activationRevision \+= 1/);
  assert.match(CONTENT, /document: \{[\s\S]*activationRevision: activationRevision/);
  assert.match(CONTENT, /prepareRelaySnapshot[\s\S]*activationRevision: snap\.document\.activationRevision/);
});
