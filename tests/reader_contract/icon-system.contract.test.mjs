// SF Symbols 图标桥（用户 2026-09-20：「不能直接使用苹果提供的页面元素容器么」）。
//
// 界面上原本有 357 个 emoji 当图标用 —— 这是「整体还是像网页」最大的单一来源。
// 现在图标是一个 <span>，靠 CSS mask + currentColor 上色：
//   · App 内 → mask 指向环回服务，由 UIImage(systemName:) 渲染，**是真的 SF Symbols**
//   · 扩展/桌面 → 回落到内置手绘 SVG（同样 1.8 描边、圆头圆角）
// 两边同一份 CSS、同一个调用，不分叉。
//
// ⚠ 这条测试**真的执行** rc-ui.js 并检查生成出来的 CSS —— 不是比对源码文本。
//   155 处 emoji 替换全指望这份 CSS 是对的；只钉源码写法的话，一个拼错的
//   mask 属性名能让所有图标一起变成空白方块而测试全绿。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SRC = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-ui.js", import.meta.url), "utf8");

function run(nativeBasePath) {
  const styles = [];
  const mk = () => ({ id: "", textContent: "", setAttribute() {}, appendChild() {} });
  const ctx = {
    window: {},
    document: {
      getElementById: () => null,
      createElement: () => { const e = mk(); styles.push(e); return e; },
      head: { appendChild() {} },
      addEventListener() {},
    },
  };
  if (nativeBasePath) ctx.window.__BW_NATIVE_LOCAL_BASE_PATH__ = nativeBasePath;
  ctx.window.document = ctx.document;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  const ui = ctx.window.RC.ui;
  ui.inject();
  return { ui, css: styles.map((s) => s.textContent).join(" ") };
}

test("每个图标都生成 mask 规则，未知名字不吐坏标记", () => {
  const { ui, css } = run(null);
  const names = ui.iconNames();
  assert.ok(names.length >= 20, "图标集不该缩水");
  for (const n of names) {
    assert.ok(css.includes(".rc-i-" + n + "{"), n + " 缺 mask 规则");
  }
  assert.equal(
    (css.match(/-webkit-mask-image:/g) || []).length, names.length,
    "每个图标恰好一条 -webkit-mask-image");
  assert.match(ui.icon("trash"), /class="rc-i rc-i-trash"/);
  // 未知图标必须返回空串：返回半个 span 会在页面上留一个看不见的空洞。
  assert.equal(ui.icon("definitely-not-an-icon"), "");
});

test("图标靠 currentColor 上色，所以跟着按钮文字变色", () => {
  const { css } = run(null);
  assert.match(css, /\.rc-i\{[^}]*background-color:currentColor/,
    "用 mask + currentColor，而不是把颜色烤进图里");
  assert.match(css, /\.rc-i\{[^}]*mask-size:contain/);
});

test("没问过 App 之前一律用回落，问过之后只切它认得的", () => {
  // ⚠ 这条是 2026-09-20 补的防线：某个符号在当前 iOS 版本上不存在时，取图 404，
  //   而 CSS mask 拿不到图就是**空白方块** —— 没有回落可言。所以不能乐观地
  //   「有 App 就全指过去」，必须先问一次 names=…，只对它认得的切。
  const token = "a".repeat(64);
  const { css } = run("/r/" + token);
  assert.doesNotMatch(css, /native-api\/sf-symbol\?name=/,
    "还没问过就指向 SF = 未知符号会变空白方块");
  assert.match(css, /rc-i-trash\{[^}]*data:image\/svg/,
    "问过之前必须先用内置 SVG");
});

test("探测只在 App 内发起，且一次把所有符号名问完", () => {
  const SRC2 = readFileSync(
    new URL("../../_server_deploy/static/pdf/rc-ui.js", import.meta.url), "utf8");
  assert.match(SRC2, /\?names=/, "用 names= 批量问，而不是每个图标各探一次");
  assert.match(SRC2, /if \(!sf \|\| _sfHave\) return;/,
    "没有 App 不发请求（扩展里不该去碰 App 的环回地址），问过一次不再问");
});

test("环回路径必须是合法能力路径，否则一律回落", () => {
  // 伪造/半截的路径不能被当成 App —— 那会让图标指向一个打不开的地址，
  // 表现是整片图标空白，而不是回落。
  for (const bad of ["/r/short", "/r/" + "z".repeat(64), "http://evil/x", ""]) {
    const { css } = run(bad);
    assert.doesNotMatch(css, /native-api\/sf-symbol/, "不该认: " + bad);
  }
});
