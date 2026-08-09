import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const WEB = read("ios/BWReader/App/ReaderWebView.swift");
const EPUB = read("_server_deploy/static/pdf/epub-html.js");

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  assert.fail(`unterminated function ${name}`);
}

test("native reader presents JavaScript alert, confirm and prompt dialogs", () => {
  assert.match(WEB, /runJavaScriptAlertPanelWithMessage/);
  assert.match(WEB, /runJavaScriptConfirmPanelWithMessage/);
  assert.match(WEB, /runJavaScriptTextInputPanelWithPrompt/);
  assert.match(WEB, /webView\.window\?\.rootViewController/);
  assert.match(WEB, /UIAlertAction\(title: "取消", style: \.cancel/);
  assert.match(WEB, /UIAlertAction\(title: "确定", style: \.destructive/);
  assert.match(WEB, /completionHandler\(false\)/);
  assert.match(WEB, /completionHandler\(nil\)/);
});

test("EPUB visible decoration scans only the current viewport neighbourhood", () => {
  const measured = [];
  const secEls = Array.from({ length: 1000 }, (_, index) => ({
    index,
    getBoundingClientRect() {
      measured.push(index);
      const top = (index - 500) * 400;
      return { top, bottom: top + 400 };
    },
  }));
  const context = {
    window: { innerHeight: 800 },
    secEls,
    loaded: Array(1000).fill(true),
    _curTopIdx: 500,
  };
  const source = functionSource(EPUB, "_visibleLoadedSecs");
  vm.runInNewContext(`${source}; this.result = _visibleLoadedSecs();`, context);

  assert.deepEqual(
    Array.from(context.result, (section) => section.index),
    [498, 499, 500, 501, 502, 503],
  );
  assert.ok(
    measured.length < 20,
    `expected a bounded neighbourhood scan, measured ${measured.length} sections`,
  );
});

test("EPUB scroll decoration is a no-op when every decoration is disabled", () => {
  const schedule = functionSource(EPUB, "_decoSchedule");
  assert.match(
    schedule,
    /if \(\(!_vocabOn\(\) \|\| !_vocabMap\) && !_deco\.ruby && !_deco\.pagetr\) return;/,
  );
  assert.ok(
    schedule.indexOf("return;") < schedule.indexOf("setTimeout"),
    "the disabled-state guard must run before a timer is allocated",
  );
});
