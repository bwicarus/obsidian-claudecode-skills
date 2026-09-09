// 读了却没放行 —— 和放行了却没搬字段一样，表现都是「明明改齐了却不生效」。
//
// 2026-09-08 给制卡加「归属二选一」时，rc-computer-voice.js 的两道入站闸都加了
// `normalizeKjCardTrack(value.track)` 的**读取**，却没把 `track` 加进
// `exactObject` 的**放行表**。而 exactObject 是全等校验：任何带 track 的请求
// 直接以「含未知字段 track」被拒。于是 rc-flashcard 每次导出都撞
// BW_COMPUTER_VOICE_DIRECT_SCHEMA，而那个码不在「可安全重试」名单里，
// 卡片被标成 'unknown' 并显示「结果未知，已阻止重复发送」——
// 一张卡就此永久发不出去，且看起来像是电脑那边的问题。
//
// 这份测试不检查某个具体字段，而是检查这条**规则**：闸里读到的每个字段，
// 都必须在同一道闸的放行表里。加新字段时忘了哪一半都会在这里停下。
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => fs.readFileSync(new URL(path, ROOT), "utf8");

//: 同一道闸的两份副本（源 + 扩展自带）。vendor 由 build.py 生成，
//: 但它是**签入仓库**的产物，所以两份都必须自洽。
const COPIES = [
  "_server_deploy/static/pdf/rc-computer-voice.js",
  "extensions/bw-reader-webext/vendor/rc-computer-voice.js",
];

/** 取 exactObject( 开始的那个调用的全部实参文本（按括号配平）。 */
function exactObjectCall(body, from) {
  const start = body.indexOf("exactObject(", from);
  assert.ok(start >= 0, "这段里找不到 exactObject 调用");
  let depth = 0;
  for (let i = start + "exactObject".length; i < body.length; i += 1) {
    if (body[i] === "(") depth += 1;
    else if (body[i] === ")") {
      depth -= 1;
      if (depth === 0) return body.slice(start, i + 1);
    }
  }
  throw new Error("exactObject 调用括号不配平");
}

/** 放行表 = 调用里出现的全部字符串字面量（去掉最后那个 label）。 */
function allowedKeys(call) {
  const literals = [...call.matchAll(/"([^"\\]+)"/g)].map((m) => m[1]);
  assert.ok(literals.length > 1, "放行表为空");
  return new Set(literals.slice(0, -1));
}

/** 闸里实际读到的字段：`<name>.<ident>` 与 hasOwnProperty.call(<name>, "x")。 */
function readKeys(body, receiver) {
  const keys = new Set();
  const dotted = new RegExp("\\b" + receiver + "\\.([A-Za-z_$][\\w$]*)", "g");
  for (const match of body.matchAll(dotted)) keys.add(match[1]);
  const owned = new RegExp(
    "hasOwnProperty\\.call\\(\\s*" + receiver + "\\s*,\\s*\"([^\"]+)\"",
    "g",
  );
  for (const match of body.matchAll(owned)) keys.add(match[1]);
  return keys;
}

/** 入库闸：normalizeLocalAnkiAddRequest 的函数体。 */
function localAnkiAddGate(source) {
  const head = "function normalizeLocalAnkiAddRequest(value) {";
  const start = source.indexOf(head);
  assert.ok(start >= 0, "找不到 normalizeLocalAnkiAddRequest");
  const end = source.indexOf("\n  function ", start + head.length);
  assert.ok(end > start, "找不到 normalizeLocalAnkiAddRequest 的结尾");
  return source.slice(start, end);
}

/** 草稿闸：anki-draft 那条分支。 */
function ankiDraftGate(source) {
  const head = '} else if (kind === "anki-draft") {';
  const start = source.indexOf(head);
  assert.ok(start >= 0, "找不到 anki-draft 分支");
  const end = source.indexOf('} else if (kind === "client-action") {', start);
  assert.ok(end > start, "找不到 anki-draft 分支的结尾");
  return source.slice(start, end);
}

const GATES = [
  { name: "本机 Anki 入库请求", slice: localAnkiAddGate, receiver: "value" },
  { name: "Reader Anki 草稿输出", slice: ankiDraftGate, receiver: "p" },
];

for (const copy of COPIES) {
  for (const gate of GATES) {
    test(`${copy} 的「${gate.name}」闸放行了它读到的每个字段`, () => {
      const body = gate.slice(read(copy));
      const allowed = allowedKeys(exactObjectCall(body, 0));
      const missing = [...readKeys(body, gate.receiver)].filter(
        (key) => !allowed.has(key),
      );
      assert.deepEqual(
        missing,
        [],
        `这些字段被读取但没放行：${missing.join(", ")}；` +
          `exactObject 是全等校验，带这些字段的请求会被整条拒掉`,
      );
    });
  }

  test(`${copy} 两道闸都认 track（归属二选一的可选字段）`, () => {
    const source = read(copy);
    for (const gate of GATES) {
      const body = gate.slice(source);
      assert.ok(
        allowedKeys(exactObjectCall(body, 0)).has("track"),
        `${gate.name} 没放行 track`,
      );
      // 可选而非必需：桥先装、App 后出构建，旧版不发 track 也要过。
      assert.match(body, /normalizeKjCardTrack\(/);
    }
  });
}

test("rc-flashcard 两条身份路都带归属 —— 与入站闸成对", () => {
  const flash = read("_server_deploy/static/pdf/rc-flashcard.js");
  const send = flash.indexOf("RC.computerVoice.addLocalAnkiCard(ctx.draft");
  assert.ok(send >= 0, "找不到导出调用");
  const call = flash.slice(send, flash.indexOf("));", send));
  // 草稿路：字面写出 nodeIds / track。
  assert.match(call, /nodeIds:\s*kjNodes/);
  assert.match(call, /track:\s*kjTrack/);
  // 实体路：归属经 entityAnkiRequest 传下去。
  assert.match(call, /entityAnkiRequest\(/);
  const build = flash.indexOf("function entityAnkiRequest(");
  assert.ok(build >= 0, "找不到 entityAnkiRequest");
  const body = flash.slice(build, flash.indexOf("\n  function ", build));
  assert.match(body, /nodeIds:\s*kjNodes/);
  assert.match(body, /track:\s*kjTrack/);
  assert.match(body, /entityId:\s*entityId/);
  assert.match(body, /cards:\s*bridgeCards\(cards\)/);
});

test("桥只收 type/front/back —— 导出前必须剥掉 deck/tags/reason", () => {
  const flash = read("_server_deploy/static/pdf/rc-flashcard.js");
  const build = flash.indexOf("function bridgeCard(card) {");
  assert.ok(build >= 0, "找不到 bridgeCard");
  const body = flash.slice(build, flash.indexOf("\n  function ", build));
  // repositoryCard 会带上 deck/tags/reason（仓库要），而 C# 侧是 RequireExact，
  // 多一个字段整条拒。所以这一层必须只留卡面本身。
  assert.match(body, /type: 'cloze'/);
  assert.match(body, /type: 'basic'/);
  for (const extra of ["deck", "tags", "reason"]) {
    assert.doesNotMatch(
      body,
      new RegExp(extra + ":"),
      `bridgeCard 不该把 ${extra} 送到桥`,
    );
  }
  const anki = read(
    "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderLocalAnki.cs",
  );
  assert.match(anki, /RequireExact\(value, "type", "front", "back"\)/);
});

test("自动补送遍历卡库而不是挂载中的容器", () => {
  const flash = read("_server_deploy/static/pdf/rc-flashcard.js");
  const start = flash.indexOf("function retryFailedComputerExports(");
  assert.ok(start >= 0);
  const body = flash.slice(start, flash.indexOf("\n  // 挂钩全放在 try 里", start));
  // 遍历必须从仓库快照出发：只看 _groups 的话，书关掉的卡永远轮不到，
  // 而那正是最需要补送的一批。
  assert.match(body, /repo\.snapshot\(\)/);
  assert.match(body, /exportRecordCard\(record, index\)/);
  // 仍然只补 failed；unknown 要人来决定。
  assert.match(body, /receipt\.status !== 'failed'/);
});

test("没挂载的补送只写回执，不写整份 exactState", () => {
  const flash = read("_server_deploy/static/pdf/rc-flashcard.js");
  const start = flash.indexOf("function exportRecordCard(record, index) {");
  assert.ok(start >= 0);
  const body = flash.slice(start, flash.indexOf("\n  function ", start + 10));
  // 手上只有从仓库读回来的残缺卡对象；写整份 exactState 会抹掉 _st/_next。
  assert.match(body, /return Promise\.resolve\(false\);/);
  assert.doesNotMatch(body, /_stateSync/);
});

// ── 归属字段必须在整条链上同名同在 ────────────────────────────────────
// 2026-09-08 加「归属二选一」时 kjTrack 只加了写入端，没加卡库的字段声明。
// 而 allowedFields 遇到未声明字段是**抛错**不是忽略 —— 于是每一张带轨道的卡
// 在 saveConfirmedCard 就 BW_CARD_REPOSITORY_INPUT，用户看到的是
// 「本地卡库保存失败」，跟归属看不出任何关系。
const REPO_COPIES = [
  "_server_deploy/static/reader-runtime/card-repository.js",
  "extensions/bw-reader-webext/vendor/reader-runtime-card-repository.js",
];

for (const copy of REPO_COPIES) {
  test(`${copy} 声明了归属的两个字段`, () => {
    const source = read(copy);
    const fields = source.slice(
      source.indexOf("var SOURCE_FIELDS = {"),
      source.indexOf("var REVIEW_FIELDS = {"),
    );
    for (const field of ["kjNodes", "kjTrack"]) {
      assert.match(
        fields,
        new RegExp(field + ":\\s*true"),
        `SOURCE_FIELDS 少了 ${field}；allowedFields 会因此抛错`,
      );
      assert.match(
        fields,
        new RegExp(field + ":\\s*\\d+"),
        `SOURCE_LIMITS 少了 ${field} 的长度上限`,
      );
    }
  });
}

test("写入端与卡库对归属字段的叫法一致", () => {
  const voicecall = read("_server_deploy/static/pdf/rc-voicecall.js");
  // 写入端确实往 source 里放这两个字段 —— 卡库那边才必须声明它们。
  assert.match(voicecall, /source\.kjTrack = String\(payload\.track\)/);
  assert.match(voicecall, /source\.kjNodes = /);
});

test("MCP 的 source schema 允许修补归属", () => {
  const mcp = read(
    "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs",
  );
  const start = mcp.indexOf("private static JsonObject BuildLearningCardSourceSchema()");
  assert.ok(start >= 0);
  const body = mcp.slice(start, mcp.indexOf("\n    private static JsonObject LearningCardSourceTextSchema", start));
  // 归属 2026-09-06 才成为硬要求；之前的卡没有归属就进不了 Anki、
  // 永远评不了分。不能改就只能删掉重做，连复习历史一起丢。
  assert.match(body, /\["kjTrack"\] = new JsonObject/);
  assert.match(body, /\["kjNodes"\] = new JsonObject/);
  // 取值表与校验用的是同一份，避免又多一处手工同步的副本。
  assert.match(body, /KjCardTracks\.All/);
});
