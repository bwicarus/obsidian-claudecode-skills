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
