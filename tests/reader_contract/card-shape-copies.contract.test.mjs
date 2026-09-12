// 卡片 data 形状有三份，钉住它们必须一致。
//
// 2026-09-13 盘点时数出来的三处：
//   ① `reader_card` 工具描述里的散文（weather={lo,hi,cond,loc?,…}）—— 模型
//      构造参数时读的就是它；
//   ② `ReaderCapabilities/cards.md` 的按需指南；
//   ③ `ReaderRealtimeOutput.cs` 里的校验器 —— **真正执法的那份**。
//
// 而 inputSchema 里 `data` 只写了 `{"type":"object"}` 加一句"见指南"——
// 也就是说，唯一能机器强制的地方恰恰是空的，三份全靠人肉同步。
//
// ⚠ 为什么这个工具**不该**像 reader_context_snapshot 那样把说明搬进载荷：
//    那一招的前提是"说明只有拿到返回值才用得上"。这里正相反 —— `data` 必须
//    在**调用前**就构造对，说明搬到返回值里等于永远迟到一步。
//
// 三份现在是一致的（本测试就是为了让它保持一致）。漂移的表现不是静默：
// 校验器会拒，模型拿到报错。但在语音里那就是白白废掉一轮对话。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");
const DIR = "extensions/bw-reader-webext/windows/ComputerVoiceAudio/";
const MCP = read(DIR + "ReaderContextMcpServer.cs");
const OUTPUT = read(DIR + "ReaderRealtimeOutput.cs");
const GUIDE = read(DIR + "ReaderCapabilities/cards.md");

/// 描述是逐段拼接的 C# 字面量，先还原成一整串再解析。
function toolProse(constant) {
  const at = MCP.indexOf(`["name"] = ${constant},`);
  assert.notStrictEqual(at, -1, `找不到工具 ${constant}`);
  const end = MCP.indexOf('["inputSchema"]', at);
  return MCP.slice(at, end).replace(/"\s*\+\s*"/g, "");
}

/// 从散文里读 `kind={a,b,c?}` —— 带 ? 的是可选。
function proseShapes() {
  const prose = toolProse("CardToolName");
  const shapes = {};
  for (const m of prose.matchAll(/(weather|news|images|videos|fact|general)=\{([^}]*)\}/g)) {
    const fields = m[2]
      .replace(/items:\s*\[\s*\{?/, "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    shapes[m[1]] = {
      required: fields.filter((f) => !f.endsWith("?")),
      optional: fields.filter((f) => f.endsWith("?")).map((f) => f.slice(0, -1)),
    };
  }
  return shapes;
}

/// 从校验器里读 `case "kind": … ["a","b"], ["c","d"]`。
function validatorShapes() {
  const shapes = {};
  for (const kind of ["weather", "news", "images", "videos"]) {
    const at = OUTPUT.indexOf(`case "${kind}":`);
    assert.notStrictEqual(at, -1, `校验器里找不到 ${kind}`);
    const body = OUTPUT.slice(at, at + 420);
    const lists = [...body.matchAll(/\[((?:"[a-z]+"(?:,\s*)?)+)\]/g)].map((m) =>
      [...m[1].matchAll(/"([a-z]+)"/g)].map((x) => x[1]),
    );
    assert.ok(lists.length >= 2, `${kind} 的必填/可选两张表没读到`);
    shapes[kind] = { required: lists[0], optional: lists[1] };
  }
  return shapes;
}

test("散文里的 data 形状与校验器逐字一致", () => {
  const prose = proseShapes();
  const rules = validatorShapes();
  for (const [kind, rule] of Object.entries(rules)) {
    assert.ok(prose[kind], `描述里没写 ${kind} 的形状`);
    assert.deepEqual(
      prose[kind].required.sort(),
      rule.required.slice().sort(),
      `${kind} 必填字段对不上：描述 ${prose[kind].required} vs 校验器 ${rule.required}`,
    );
    assert.deepEqual(
      prose[kind].optional.sort(),
      rule.optional.slice().sort(),
      `${kind} 可选字段对不上：描述 ${prose[kind].optional} vs 校验器 ${rule.optional}`,
    );
  }
});

test("按需指南里也是同一套字段", () => {
  // 指南是给"要写复杂卡片时再翻"的那次用的。它跟描述说的必须是同一件事，
  // 否则翻了指南反而比不翻更错。
  const rules = validatorShapes();
  for (const [kind, rule] of Object.entries(rules)) {
    const at = GUIDE.indexOf("### `" + kind + "`");
    assert.notStrictEqual(at, -1, `指南里没有 ${kind}`);
    const body = GUIDE.slice(at, at + 500);
    for (const field of [...rule.required, ...rule.optional]) {
      assert.ok(
        body.includes(`"${field}"`),
        `指南的 ${kind} 少了字段 ${field}`,
      );
    }
  }
});

test("形状没有进 schema——这是已知的，别当它已经修好", () => {
  // ⚠ 这条是**现状备忘**，不是通过就万事大吉：`data` 在 schema 里仍然只是
  //   一个开放对象，唯一能机器强制形状的地方是空的。真要根治，应当由校验器
  //   生成 schema 的 oneOf，让三份变成一份。在那之前，上面两条是仅有的护栏。
  const at = MCP.indexOf('["name"] = CardToolName,');
  // 窗口从 inputSchema 自己起算：描述一变长，按 name 起算的固定窗口就会滑空。
  const schemaAt = MCP.indexOf('["inputSchema"]', at);
  const schema = MCP.slice(schemaAt, schemaAt + 400);
  assert.match(schema, /BuildTypedCardArgumentsSchema\(\)/,
    "schema 由这个函数生成；换了就要重看这条备忘");
  const builderAt = MCP.indexOf("JsonObject BuildTypedCardArgumentsSchema(");
  assert.notStrictEqual(builderAt, -1, "找不到 schema 生成函数");
  assert.doesNotMatch(
    MCP.slice(builderAt, builderAt + 4000),
    /"lo"|"precip"|"thumb"/,
    "如果形状真的进了 schema，这条备忘就该删掉、并让校验器与 schema 同源");
});
