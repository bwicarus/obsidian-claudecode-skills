import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const INK_SOURCE = fs.readFileSync(
  "extensions/bw-reader-webext/src/web-ink.js",
  "utf8",
);
const FACADE_SOURCE = fs.readFileSync(
  "extensions/bw-reader-webext/src/facade.js",
  "utf8",
);

function loadIgnore(tools) {
  const start = INK_SOURCE.indexOf("  const INTERACTIVE_SELECTOR=");
  const end = INK_SOURCE.indexOf("  const preventSel=", start);
  assert.ok(start >= 0 && end > start, "web ink interactive-path helper missing");
  return vm.runInNewContext(
    `(function (tools) {\n${INK_SOURCE.slice(start, end)}\nreturn _ignore;\n})(tools)`,
    { tools },
  );
}

function elementThatMatches(selector) {
  return {
    matches(candidate) {
      return candidate.includes(selector);
    },
  };
}

test("Apple Pencil 事件按 composedPath 放行 Shadow 内按钮", () => {
  const tools = {};
  const button = elementThatMatches("button");
  const host = { closest: () => null };
  const ignore = loadIgnore(tools);

  assert.equal(
    ignore({ target: host, composedPath: () => [button, {}, host] }),
    true,
  );
  assert.equal(
    ignore({ target: host, composedPath: () => [{}, host] }),
    false,
  );
});

test("stylus touch 在扩展控件上不得被全局滚动拦截器 preventDefault", () => {
  assert.match(
    INK_SOURCE,
    /const _blk=e=>\{if\(_ignore\(e\)\)return;for\(const t of e\.touches\|\|\[\]\)/,
  );
});

test("主 Shadow 宿主保持显式全视口且空白区域继续穿透", () => {
  assert.match(
    FACADE_SOURCE,
    /host\.style\.cssText = "position:fixed;inset:0;width:100vw;height:100vh;z-index:2147483647;pointer-events:none;"/,
  );
  assert.match(
    FACADE_SOURCE,
    /root\.style\.cssText = "position:fixed;inset:0;width:100%;height:100%;pointer-events:none;"/,
  );
  assert.doesNotMatch(FACADE_SOURCE, /host\.style\.cssText = "[^"]*pointer-events:auto/);
});
