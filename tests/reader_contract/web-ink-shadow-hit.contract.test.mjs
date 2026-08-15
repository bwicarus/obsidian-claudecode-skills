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

test("主 Shadow 宿主是 0×0,不铺任何全视口覆盖平面", () => {
  // 这条测试上一版钉的是反面(全视口 fixed + pointer-events:none + 子层
  // auto)——那套结构在 iPad 真机上让顶栏与侧栏上方按钮整片不可点,只剩
  // 按钮下边缘一线能命中,0.2.109/0.2.110 两轮修法均被真机否证后整体还原。
  // 现在钉住还原后的形状:宿主 0×0、不占布局、没有 pointer-events 反转,
  // fixed 子元素各自逃逸,能被点到的只有真实控件自己的矩形。
  assert.match(
    FACADE_SOURCE,
    /host\.style\.cssText = "position:absolute;top:0;left:0;width:0;height:0;z-index:2147483647;"/,
  );
  // 全视口宿主不得回潮 —— 谁要再引入,必须先拿真机证据推翻这一条。
  assert.doesNotMatch(
    FACADE_SOURCE,
    /host\.style\.cssText = "[^"]*(100vw|inset:0|pointer-events)/,
  );
  // root 不给内联样式:0 尺寸静态容器,不构成覆盖平面。
  assert.doesNotMatch(FACADE_SOURCE, /root\.style\.cssText = "[^"]+"/);
});
