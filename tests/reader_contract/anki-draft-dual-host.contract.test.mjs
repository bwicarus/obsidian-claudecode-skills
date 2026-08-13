// 语音制卡草稿要在浮层与对话流两处可见，而它们必须是同一个实体。
//
// 侧栏打开时浮层容器被让位，草稿就此完全不可见、也不进对话历史，用户只知道"卡没了"。
// 原设计是同 gid 双宿主：两处显示同一张卡。
//
// 但"补一个宿主"很容易补成"复制一份"：turnCard 此前一律自造
// assistant-turn:<tid>:<seq> 作为 repositorySource 并把 entityRegistered 写死为
// false，于是同一份草稿在仓库里成了两个实体，编辑与确认各走各的。所以这里断言的
// 不是"两处都有卡"，而是"两处指向同一个实体"。
import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { DataStore } from "./helpers.mjs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const require = createRequire(import.meta.url);
const CardRepository = require(
  "../../_server_deploy/static/reader-runtime/card-repository.js",
);
// 沙箱里的对象来自另一个 realm，deepEqual 会因原型不同而失败；这里只关心内容。
const plain = (value) => JSON.parse(JSON.stringify(value));
const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const TURNCARD = read("_server_deploy/static/pdf/rc-turncard.js");

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

const SOURCE = {
  kind: "reader-anki-draft",
  sourceId: "reader-draft:d_1",
  draftId: "d_1",
  tool: "reader_anki_draft",
};
const LOCAL = { draftId: "d_1", sourceInstanceId: "inst_1" };
const CARDS = [{ type: "basic", front: "Q", back: "A" }];

// 浮层登记之后，把同一张草稿镜像进对话流。
function runMirror({ currentTid = "turn_7" } = {}) {
  const start = VOICECALL.indexOf("function _mirrorDraftIntoTurnFlow(");
  assert.notEqual(start, -1, "找不到镜像函数");
  const parts = [];
  const context = {
    RC: {
      turnCard: {
        current: () => currentTid,
        idle: () => {},
        addPart: (tid, part) => { parts.push({ tid, part }); },
      },
    },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(VOICECALL, start)}
     out = _mirrorDraftIntoTurnFlow(${JSON.stringify(CARDS)}, "card_abc123",
       ${JSON.stringify(SOURCE)}, ${JSON.stringify(LOCAL)});`,
    context,
  );
  return { ok: context.out, parts };
}

function runToolSource(gid, payload = {}, tool = "make_anki") {
  const start = VOICECALL.indexOf("function _toolCardRepositorySource(");
  assert.notEqual(start, -1, "找不到普通工具卡来源函数");
  const context = { out: null };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(VOICECALL, start)}
     out = _toolCardRepositorySource(${JSON.stringify(gid)},
       ${JSON.stringify(payload)}, ${JSON.stringify(tool)});`,
    context,
  );
  return plain(context.out);
}

test("草稿镜像进对话流时带着与浮层完全相同的身份", () => {
  const { ok, parts } = runMirror();
  assert.equal(ok, true);
  assert.equal(parts.length, 1, "只应产生一个额外宿主，不能更多");
  const { part } = parts[0];
  assert.equal(part.gid, "card_abc123", "gid 必须与浮层一致");
  assert.deepEqual(plain(part.repositorySource), SOURCE, "source 必须原样带过去");
  assert.deepEqual(plain(part.localDraft), LOCAL);
  // entityRegistered 是旧 Pi entity registry 的兼容标志，不代表本地已 registerDraft。
  // 避免第二个实体靠的是身份一致（同 gid/cards/source 的再次登记幂等），
  // 不是靠跳过登记 —— 把它翻成 true 会让这张卡被误认为已在 Pi 注册过。
  assert.equal(part.entityRegistered, false, "Pi 兼容标志不得被借用来表达本地登记状态");
});

test("普通 make_anki 的两个宿主真实重放同一草稿并可保存", async () => {
  const gid = "card_abc123";
  const payload = { text: "ordinary generated material" };
  const floatSource = runToolSource(gid, payload);
  const inflowSource = runToolSource(gid, payload);

  assert.ok(floatSource.draftId, "普通工具卡也必须有稳定非空 draftId");
  assert.equal(inflowSource.draftId, floatSource.draftId);
  assert.deepEqual(inflowSource, floatSource, "两个宿主必须得到完全相同的 source");
  assert.equal(floatSource.legacy.piEntityRegistered, false);
  for (const key of ["documentId", "location", "anchor", "selection"]) {
    assert.equal(key in floatSource, false, `普通制卡不得伪造页面 ${key}`);
  }

  const store = DataStore.createDataStore({
    backend: DataStore.createMemoryBackend(),
    deviceId: "ordinary-tool-dual-host",
    causalCollections: ["card-entities", "card-states"],
  });
  const repository = CardRepository.createCardRepository({
    store,
    clock: () => 1_800_000_000_000,
  });
  const first = await repository.registerDraft({
    id: gid,
    cards: CARDS,
    source: floatSource,
  }, {
    mutationId: "ordinary-tool-float",
    requireDraftIdForReplay: true,
  });
  const replayed = await repository.registerDraft({
    id: gid,
    cards: CARDS,
    source: inflowSource,
  }, {
    mutationId: "ordinary-tool-inflow",
    requireDraftIdForReplay: true,
  });

  assert.deepEqual(replayed, first, "第二宿主应幂等复用，不得触发 source conflict");
  assert.equal((await store.list("card-entities")).length, 1);
  assert.equal((await store.list("card-states")).length, 1);

  const confirmed = await repository.saveConfirmedCard({
    id: gid,
    cardIndex: 0,
    card: CARDS[0],
  }, { mutationId: "ordinary-tool-confirm" });
  assert.equal(confirmed.states["0"].phase, "confirmed");
  assert.deepEqual(confirmed.states["0"].projections.anki, {},
    "Reader 本地保存不得冒充外部 Anki 投影");
  assert.equal(confirmed.source.legacy.piEntityRegistered, false,
    "本地双宿主登记不得改写旧 Pi entityRegistered 语义");
  assert.equal((await repository.snapshot()).length, 1, "保存后仍只有一个实体");
});

test("始终使用确定性轮次，不读 current() 也不绑陈旧的语音轮次", () => {
  // current() 会被 loadHistory 的 renderTurn 改写，读它就可能挂到别人的轮次上。
  const { parts } = runMirror({ currentTid: "turn_polluted_by_history" });
  assert.equal(parts.length, 1, "仍然只有一个额外宿主，不会多出第三处");
  assert.equal(
    parts[0].tid,
    "reader-draft:card_abc123",
    "即便 current() 有值也必须忽略：确定性 tid 才不会挂到别人的轮次上",
  );
});

// 对话流一侧：拿到上游身份就必须沿用，不能自造。
function runTurnCardCards(part) {
  const anchor = TURNCARD.indexOf("} else if (p.kind === 'cards') {");
  assert.notEqual(anchor, -1, "找不到 cards 分支");
  const branch = TURNCARD.slice(anchor, TURNCARD.indexOf("} else if (p.kind === 'meta')", anchor));
  const calls = [];
  const context = {
    window: { RC: {} },
    RC: {
      flashcard: {
        presentDraft: (cards, gid, options) => {
          calls.push({ cards, gid, options });
          return Promise.resolve(true);
        },
      },
    },
    d: { textContent: "" },
    t: { tid: "turn_7", parts: [] },
    p: part,
    Promise,
    _localCardGid: (seed) => "card_" + "f".repeat(6),
  };
  context.window.RC = context.RC;
  context.globalThis = context;
  // 分支体外层是 if/else if 链，这里补一个可执行的外壳。
  // branch 本身以 "} else if" 开头，外壳要留一个未闭合的 if 给它接上。
  // 截取止于下一个 "} else if"，而那个 "}" 正是闭合本分支的，所以要补回来。
  vm.runInNewContext(`(function () { if (false) { ${branch} } })();`, context);
  return calls;
}

test("对话流沿用上游身份，不自造 assistant-turn source", () => {
  const calls = runTurnCardCards({
    kind: "cards", cards: CARDS, draft: true, gid: "card_abc123",
    entityRegistered: false, repositorySource: SOURCE, localDraft: LOCAL,
  });
  assert.equal(calls.length, 1);
  const { gid, options } = calls[0];
  assert.equal(gid, "card_abc123", "两宿主同 gid");
  assert.deepEqual(plain(options.repositorySource), SOURCE, "必须沿用上游 source");
  assert.equal(options.entityRegistered, false, "Pi 兼容标志原样沿用，不在这里翻真");
  assert.doesNotMatch(
    JSON.stringify(options.repositorySource),
    /assistant-turn/,
    "不得生成 assistant-turn:* 这个第二身份",
  );
});

test("没有上游身份的旧路径保持原行为，不回归", () => {
  const calls = runTurnCardCards({
    kind: "cards", cards: CARDS, draft: true, gid: "card_abc123",
  });
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].options.entityRegistered,
    false,
    "没有上游登记时仍需本地登记一次",
  );
  assert.match(
    JSON.stringify(calls[0].options.repositorySource),
    /assistant-turn/,
    "旧路径沿用原来的自造 source，行为不变",
  );
});

test("generic 草稿的 source 不带书页锚点", () => {
  const { parts } = runMirror();
  const source = parts[0].part.repositorySource;
  for (const key of ["documentId", "page", "anchor", "location"]) {
    assert.equal(
      Object.hasOwn(source, key),
      false,
      `generic 草稿不该携带 ${key}：它不绑定具体书页`,
    );
  }
});
