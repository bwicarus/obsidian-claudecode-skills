// 助手画完高亮后，界面要出现那张卡片条；其余情况一概不出现。
//
// 迁移前这条反馈由 Pi 的 client_actions 带回：runActions → _assistEdit → hlcard
// （原文｜↗跳转｜↩撤销⇄↪重做）。App 本地化之后写入不再经 Pi，反馈也就没了，
// 用户看到的是"撤销和取消高亮的按钮都不见了"——其实按钮所在的那张条没出现。
//
// 四条边界，每一条都对应一种"看起来像成功"的情形：
//   · 只有本地确实落库才显示（不是"发出去了"就显示）
//   · 失败与未知都不显示（宁可没有卡片，也不能给一张指不回任何高亮的）
//   · 手动划线不显示（基线如此；一并覆盖的话每划一次线都多一张卡，那是新增噪声）
//   · 同一条高亮不重复（CAS 冲突会让写入重试，重试不该再刷一张）
import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const RUNTIME = readFileSync(
  new URL("_server_deploy/static/pdf/native-local-runtime.js", ROOT),
  "utf8",
);

function balanced(source, start) {
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  assert.fail("括号未闭合");
}

// 把触发器连同它的去重集合放进沙箱，注入一个记录调用的 _assistEdit。
function makeAnnouncer({ dispPage } = {}) {
  const start = RUNTIME.indexOf("var announcedAssistantHighlights = new Set();");
  assert.notEqual(start, -1, "找不到去重集合");
  const fnStart = RUNTIME.indexOf("function announceAssistantHighlight(", start);
  assert.notEqual(fnStart, -1, "找不到触发器");
  const calls = [];
  const context = {
    Set,
    Number,
    String,
    root: {
      _assistEdit: (payload) => { calls.push(payload); },
      ...(dispPage ? { _dispPage: dispPage } : {}),
    },
    announce: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `var announcedAssistantHighlights = new Set();
     ${balanced(RUNTIME, fnStart)}
     announce = announceAssistantHighlight;`,
    context,
  );
  return { announce: context.announce, calls };
}

const BODY = {
  file: "book.pdf", id: "c_1111111111111111", page: 12,
  text: "quoted span", color: "yellow",
};

test("助手高亮落库成功后出现卡片条，字段供渲染器与撤销使用", () => {
  const { announce, calls } = makeAnnouncer();
  announce(BODY, { ok: true, id: "c_1111111111111111" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].type, "highlight");
  assert.equal(calls[0].file, "book.pdf");
  // 沙箱对象来自另一个 realm，deepEqual 会因原型不同而失败；只比内容。
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].items)), [{
    id: "c_1111111111111111",
    text: "quoted span",
    color: "yellow",
    pdf_page: 12,
    disp_page: 12,
  }], "渲染器要 text/color/页码，撤销要 id");
});

test("印刷页码可用时按印刷页显示", () => {
  const { announce, calls } = makeAnnouncer({ dispPage: (page) => page - 4 });
  announce(BODY, { ok: true, id: "c_1111111111111111" });
  assert.equal(calls[0].items[0].pdf_page, 12);
  assert.equal(calls[0].items[0].disp_page, 8, "显示用印刷页，跳转仍用 PDF 页");
});

test("写入未确认成功时不显示", () => {
  const { announce, calls } = makeAnnouncer();
  announce(BODY, { ok: false, error: "conflict" });
  announce(BODY, null);
  announce(BODY, undefined);
  assert.equal(
    calls.length, 0,
    "宁可没有卡片，也不能给一张指不回任何高亮的",
  );
});

test("拿不到高亮 id 时不显示", () => {
  const { announce, calls } = makeAnnouncer();
  announce({ ...BODY, id: "" }, { ok: true });
  assert.equal(calls.length, 0, "没有 id 的卡片条既跳不回也撤不掉");
});

test("同一条高亮只报一次", () => {
  const { announce, calls } = makeAnnouncer();
  const saved = { ok: true, id: "c_1111111111111111" };
  announce(BODY, saved);
  announce(BODY, saved);
  announce(BODY, saved);
  assert.equal(calls.length, 1, "CAS 冲突会重试写入，重试不该再刷一张卡");
});

test("渲染器缺席时静默跳过，不影响已完成的写入", () => {
  const start = RUNTIME.indexOf("function announceAssistantHighlight(");
  const context = { Set, Number, String, root: {}, announce: null };
  context.globalThis = context;
  vm.runInNewContext(
    `var announcedAssistantHighlights = new Set();
     ${balanced(RUNTIME, start)}
     announce = announceAssistantHighlight;`,
    context,
  );
  assert.doesNotThrow(() => context.announce(BODY, { ok: true, id: "c_1" }));
});

test("只挂在助手入口，手动划线不经过它", () => {
  // 助手入口：savePDFHighlight → assistant-exact-highlight → independent=true
  const assistant = RUNTIME.slice(
    RUNTIME.indexOf("withNativePDFWriter('assistant-exact-highlight'"),
    RUNTIME.indexOf("withNativePDFWriter('assistant-exact-highlight'") + 600,
  );
  assert.match(
    assistant,
    /announceAssistantHighlight\(body, saved\)/,
    "助手路径必须在落库成功后补发卡片条",
  );
  // 手动路径：/pdf/api/highlights 的普通写入，independent=false
  const manual = RUNTIME.slice(
    RUNTIME.indexOf("persistLocalPDFHighlight(body, code, false)") - 400,
    RUNTIME.indexOf("persistLocalPDFHighlight(body, code, false)") + 200,
  );
  assert.doesNotMatch(
    manual,
    /announceAssistantHighlight/,
    "手动划线不得产生卡片条：基线如此，否则每划一次线都多一张",
  );
});
