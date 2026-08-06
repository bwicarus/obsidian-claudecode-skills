import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const CONTENT = readFileSync(
  new URL("extensions/bw-reader-webext/content.js", ROOT),
  "utf8",
);

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
});

test("web context follows the current viewport and keeps whole-article fallback", () => {
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
    /function visibleTextSlice\(value, rects, firstVisible, lastVisible\)[\s\S]*value\.slice\(start, end\)/,
  );
  assert.match(
    viewport,
    /if \(!visibleText\.trim\(\)\) return null/,
  );
  assert.match(viewport, /viewKey:[\s\S]*viewportRevision[\s\S]*scrollTop/);

  const snapshot = CONTENT.slice(snapshotStart, reportStart);
  assert.match(
    snapshot,
    /var viewport = viewportArticleText\(root\);[\s\S]*if \(viewport\)[\s\S]*text = viewport\.text[\s\S]*else \{[\s\S]*text = articleText\(root\)/,
  );
  assert.match(snapshot, /viewKey: viewKey/);
  assert.match(
    CONTENT,
    /signature = snap\.url \+ "\|" \+ snap\.title \+ "\|" \+ snap\.viewKey/,
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
