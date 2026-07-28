import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ContextSelection = require(
  "../../_server_deploy/static/reader-runtime/context-selection-registry.js",
);

function card(registry, cid, text = "整张卡片") {
  return registry.select({
    id: `card:${cid}`,
    kind: "card",
    label: "学习卡片",
    text,
    source: { cid },
  });
}

function part(registry, cid, partId, text) {
  return registry.select({
    id: `card:${cid}/part:${partId}`,
    kind: "card-part",
    label: `段落 ${partId}`,
    text,
    parentId: `card:${cid}`,
    source: { cid, partId },
  });
}

test("同 cid 的多个视图共用一个语义 id，不产生重复上下文", () => {
  const registry = ContextSelection.createRegistry();
  card(registry, "card_001", "第一次渲染");
  registry.upsert({
    id: "card:card_001",
    kind: "card",
    label: "学习卡片",
    text: "第二个 DOM 实例看到的同一张卡",
  });

  assert.equal(registry.isSelected("card:card_001"), true);
  assert.deepEqual(
    registry.snapshot().items.map((item) => item.id),
    ["card:card_001"],
  );
  assert.equal(
    registry.snapshot().items[0].text,
    "第二个 DOM 实例看到的同一张卡",
  );
});

test("选中整卡后内部段落从有效上下文移除，取消整卡后段落恢复", () => {
  const registry = ContextSelection.createRegistry();
  part(registry, "card_002", "answer-1", "内部回答一");
  part(registry, "card_002", "answer-2", "内部回答二");
  assert.deepEqual(
    registry.snapshot().items.map((item) => item.id),
    ["card:card_002/part:answer-1", "card:card_002/part:answer-2"],
  );

  card(registry, "card_002");
  assert.deepEqual(
    registry.snapshot().items.map((item) => item.id),
    ["card:card_002"],
    "整卡是最大选中节点，内部两段不得重复发送",
  );
  assert.equal(
    registry.isSelected("card:card_002/part:answer-1"),
    true,
    "内部原始选中态保留，避免无意义清空用户选择",
  );
  assert.equal(
    registry.isEffective("card:card_002/part:answer-1"),
    false,
  );

  registry.deselect("card:card_002");
  assert.deepEqual(
    registry.snapshot().items.map((item) => item.id),
    ["card:card_002/part:answer-1", "card:card_002/part:answer-2"],
  );
});

test("covers 可覆盖显式成员和成员的后代", () => {
  const registry = ContextSelection.createRegistry();
  registry.select({
    id: "turn:42",
    kind: "turn",
    label: "整轮回答",
    text: "完整问答",
    covers: ["tool-result:42"],
  });
  registry.select({
    id: "tool-result:42",
    kind: "tool-result",
    label: "工具结果",
    text: "工具结果全文",
  });
  registry.select({
    id: "tool-result:42/part:1",
    kind: "paragraph",
    label: "工具结果段落",
    text: "局部",
    parentId: "tool-result:42",
  });

  assert.deepEqual(
    registry.snapshot().items.map((item) => item.id),
    ["turn:42"],
  );
});

test("相同语义集合无论登记和选择顺序如何都稳定序列化", () => {
  const left = ContextSelection.createRegistry();
  const right = ContextSelection.createRegistry();
  left.select({
    id: "b",
    kind: "paragraph",
    label: "B",
    text: "beta",
    source: { z: 2, a: 1 },
    meta: { second: true, first: true },
  });
  left.select({
    id: "a",
    kind: "paragraph",
    label: "A",
    text: "alpha",
  });

  right.select({
    id: "a",
    text: "alpha",
    label: "A",
    kind: "paragraph",
  });
  right.select({
    id: "b",
    text: "beta",
    label: "B",
    kind: "paragraph",
    meta: { first: true, second: true },
    source: { a: 1, z: 2 },
  });

  assert.equal(left.serialize(), right.serialize());
  assert.deepEqual(
    left.snapshot().items.map((item) => item.id),
    ["a", "b"],
  );
  assert.equal("version" in left.snapshot(), false);
  assert.equal("updatedAt" in left.snapshot(), false);
});

test("重复 select 不推进本地版本，选择快照只作为动态请求尾部", () => {
  const registry = ContextSelection.createRegistry();
  const staticToolPrefix = Object.freeze({
    system: "固定系统提示",
    tools: Object.freeze(["read", "create_card"]),
  });
  const staticBefore = JSON.stringify(staticToolPrefix);

  registry.select({ id: "card:stable", label: "卡片", text: "A" });
  const afterFirstSelect = registry.version();
  registry.select("card:stable");
  assert.equal(registry.version(), afterFirstSelect);

  const request = {
    static: staticToolPrefix,
    user: "帮我复习",
    contextTail: registry.snapshot(),
  };
  assert.equal(JSON.stringify(request.static), staticBefore);
  assert.equal(request.contextTail.items[0].id, "card:stable");
});

test("快照预算、旧 pinned 映射和同名标签保持确定性", () => {
  const registry = ContextSelection.createRegistry();
  registry.select({ id: "a", label: "同名", text: "123456" });
  registry.select({ id: "b", label: "同名", text: "abcdef" });
  registry.select({ id: "c", label: "第三项", text: "ignored" });

  const legacy = registry.toLegacy({ limit: 2, maxText: 4 });
  assert.deepEqual(legacy.labels, ["同名", "同名·2"]);
  assert.deepEqual(legacy.map, { "同名": "1234", "同名·2": "abcd" });
  assert.equal(
    legacy.serialized,
    registry.serialize({ limit: 2, maxText: 4 }),
  );
});

test("非法 parent 自环和循环 JSON 元数据被显式拒绝", () => {
  const registry = ContextSelection.createRegistry();
  assert.throws(
    () => registry.upsert({ id: "self", parentId: "self", text: "x" }),
    (error) => error.code === "BW_CONTEXT_SELECTION_INVALID",
  );
  const circular = {};
  circular.self = circular;
  assert.throws(
    () => registry.upsert({ id: "cycle", text: "x", meta: circular }),
    (error) => error.code === "BW_CONTEXT_SELECTION_INVALID",
  );
});
