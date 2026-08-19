import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");

const READER = read("_server_deploy/static/pdf/reader.js");
const PDF_CSS = read("_server_deploy/static/pdf/pdf-styles.css");
const TEMPLATE = read("_server_deploy/templates/pdf_reader.html");

// 2026-08-19：`.fig-badge` / `.fig-hit` / `.fig-hl` / `.fig-hl-sel` 在 PDF 阅读器里
// **一条样式都没有**。reader.js 照常创建它们、照常写 `style.left/top`，但没有
// `position:absolute` —— 那些坐标全是死的，徽标退化成一堆没定位没形状的块散在页面上。
//
// 同名规则只存在于 epub-styles.css，而 pdf_reader.html 不加载那一份。
// 也就是说：**样式写在了另一个阅读器的样式表里，谁都没发现**。
//
// 用户实测时把这些灰块当成了绑定卡的收起态，反过来让"卡片没钉上"这件事更难查。
//
// 这条测试钉的是一个通用约束：**JS 用 left/top 定位的元素，CSS 必须给它 position**。
// 少一个 position，坐标就是死的，而页面上还是会出现东西 —— 是"看起来在工作"的那种坏。

/// reader.js 里 `className = 'xxx'` + 随后写 style.left 的那些类。
const POSITIONED_BY_JS = ["fig-badge", "fig-hit"];
/// 绝对定位的叠加层元素（由 _paintSelHls / 描述浮层创建）。
const OVERLAY_CLASSES = ["fig-hl", "fig-hl-sel", "fig-pop"];

/// 取某个类选择器的规则体。只认 `.cls{` 与 `.cls,`/`.cls:` 起头的规则，
/// 避免把 `.fig-layer` 这种前缀相同的算进来。
const ruleFor = (css, cls) => {
  const re = new RegExp(`\\.${cls}(?=[\\s,{:])[^{]*\\{([^}]*)\\}`, "g");
  let body = "";
  let m;
  while ((m = re.exec(css))) body += m[1] + ";";
  return body;
};

test("PDF 阅读器确实会创建这些插图层元素", () => {
  // 前提失守时这条先红：如果哪天不再创建了，下面几条就该删掉而不是继续维护
  for (const cls of [...POSITIONED_BY_JS, ...OVERLAY_CLASSES]) {
    assert.ok(
      READER.includes(`'${cls}'`),
      `reader.js 不再创建 .${cls} —— 那么 pdf-styles.css 里对应的规则也该清掉`,
    );
  }
});

test("JS 写了 left/top 的元素，CSS 必须给它 position —— 否则坐标是死的", () => {
  for (const cls of POSITIONED_BY_JS) {
    const body = ruleFor(PDF_CSS, cls);
    assert.ok(body, `pdf-styles.css 缺少 .${cls} 的规则`);
    assert.match(
      body,
      /position\s*:\s*absolute/,
      `.${cls} 没有 position:absolute —— reader.js 写的 left/top 不会生效，` +
        `元素会退化成无定位的块散在页面上（2026-08-19 实际发生过）`,
    );
  }
});

test("叠加层元素也必须有定位，且命中层不能有可见外观", () => {
  for (const cls of OVERLAY_CLASSES) {
    const body = ruleFor(PDF_CSS, cls);
    assert.ok(body, `pdf-styles.css 缺少 .${cls} 的规则`);
    assert.match(body, /position\s*:\s*(absolute|fixed)/, `.${cls} 缺少定位`);
  }
  // fig-hit 盖在图上只为接事件；给它可见外观就是在图上糊一块脏东西
  assert.match(
    ruleFor(PDF_CSS, "fig-hit"),
    /background\s*:\s*transparent/,
    ".fig-hit 必须透明 —— 它只接事件，不该被看见",
  );
});

test("样式必须在 PDF 自己加载的表里，不能靠另一个阅读器的样式表", () => {
  // 病根就是这个：规则写在 epub-styles.css 里，而 PDF 根本不加载它。
  assert.match(TEMPLATE, /pdf-styles\.css/);
  assert.ok(
    !TEMPLATE.includes("epub-styles.css"),
    "pdf_reader.html 不加载 epub-styles.css —— 所以 PDF 用到的类必须自己有一份",
  );
});
