// 书籍用户状态的三方合并 —— 这些是**真的跑**的单元测试，不是文本断言。
//
// 为什么非要有合并：apply-atomically 是整域权威覆盖，两台设备各改各的时，
// 后写的那次要么被拒、要么把对方整域盖掉。跨设备同步（CKSyncEngine 把冲突的
// 服务端记录交回来）必须先在条目层面合完再写。
import assert from "node:assert/strict";
import test from "node:test";

const { mergeDomain, mergeCollection, mergeStrokeMap, mergePosition } =
  await import("../../_server_deploy/static/reader-runtime/user-state-merge.js")
    .then((m) => m.default ?? m);

const hl = (id, time, extra = {}) => ({ id, time, color: "#fff59d", ...extra });

test("只有一边动过时，听动过的那边", () => {
  const base = [hl("a", 1)];
  const mine = [hl("a", 1)];
  const theirs = [hl("a", 1), hl("b", 2)];
  assert.deepEqual(mergeCollection(base, mine, theirs).map((h) => h.id), ["a", "b"]);
  assert.deepEqual(mergeCollection(base, theirs, mine).map((h) => h.id), ["a", "b"]);
});

test("两边各加各的，都要在", () => {
  const base = [];
  const mine = [hl("a", 1)];
  const theirs = [hl("b", 2)];
  const out = mergeCollection(base, mine, theirs).map((h) => h.id).sort();
  assert.deepEqual(out, ["a", "b"]);
});

test("删除不是缺席：墓碑要压住另一边那条还活着的记录", () => {
  // ⚠ 把删除当"缺席"处理的话，另一台设备上那条还在的记录会把它复活 ——
  // 用户删掉的高亮过一会儿自己回来了。
  const base = [hl("a", 1)];
  const mine = [{ id: "a", deleted: true, time: 5 }];
  const theirs = [hl("a", 1)];
  const out = mergeCollection(base, mine, theirs);
  assert.equal(out.length, 1);
  assert.equal(out[0].deleted, true);
});

test("同一条两边都改：按版本决胜", () => {
  const base = [hl("a", 1)];
  const mine = [hl("a", 2, { color: "#aaa" })];
  const theirs = [hl("a", 3, { color: "#bbb" })];
  assert.equal(mergeCollection(base, mine, theirs)[0].color, "#bbb");
  assert.equal(mergeCollection(base, theirs, mine)[0].color, "#bbb");
});

test("平手时保留本地 —— 眼前的东西不该在同步后突然变样", () => {
  const base = [hl("a", 1)];
  const mine = [hl("a", 2, { color: "#aaa" })];
  const theirs = [hl("a", 2, { color: "#bbb" })];
  assert.equal(mergeCollection(base, mine, theirs)[0].color, "#aaa");
});

test("两边都删了就不复活", () => {
  const base = [hl("a", 1), hl("b", 1)];
  assert.deepEqual(mergeCollection(base, [hl("a", 1)], [hl("a", 1)]).map((h) => h.id), ["a"]);
});

test("便签按 rev 决胜", () => {
  const base = [{ id: "n1", rev: 1, text: "x" }];
  const mine = [{ id: "n1", rev: 2, text: "我改的" }];
  const theirs = [{ id: "n1", rev: 5, text: "它改的" }];
  assert.equal(mergeCollection(base, mine, theirs)[0].text, "它改的");
});

test("笔迹同一面两边都画过 → 取并集", () => {
  // 笔画没有 id，丢掉任何一边都等于把用户画过的东西弄没了。
  const base = { "page:1": [{ p: [[0, 0]] }] };
  const mine = { "page:1": [{ p: [[0, 0]] }, { p: [[1, 1]] }] };
  const theirs = { "page:1": [{ p: [[0, 0]] }, { p: [[2, 2]] }] };
  const out = mergeStrokeMap(base, mine, theirs);
  assert.equal(out["page:1"].length, 3);
});

test("笔迹只有一边动过时不做并集（另一边可能是擦掉了）", () => {
  const base = { "page:1": [{ p: [[0, 0]] }, { p: [[1, 1]] }] };
  const mine = { "page:1": [{ p: [[0, 0]] }] };          // 擦掉一笔
  const theirs = { "page:1": [{ p: [[0, 0]] }, { p: [[1, 1]] }] };
  assert.equal(mergeStrokeMap(base, mine, theirs)["page:1"].length, 1);
});

test("阅读位置按时间取新，没时间戳保留本地", () => {
  assert.equal(mergePosition({ page: 1, ts: 1 }, { page: 2, ts: 2 }, { page: 9, ts: 9 }).page, 9);
  assert.equal(mergePosition({ page: 1, ts: 1 }, { page: 2, ts: 9 }, { page: 9, ts: 2 }).page, 2);
  // 没有时间戳时硬塞远端，表现是"打开书跳到我没读过的地方"。
  assert.equal(mergePosition({ page: 1 }, { page: 2 }, { page: 9 }).page, 2);
});

test("mergeDomain 认得每个域的形状", () => {
  const base = { pdf: [hl("a", 1)], epub: [] };
  const mine = { pdf: [hl("a", 1), hl("m", 2)], epub: [] };
  const theirs = { pdf: [hl("a", 1), hl("t", 3)], epub: [] };
  const merged = mergeDomain("highlights", base, mine, theirs);
  assert.equal(merged.value.pdf.length, 3);
  assert.equal(merged.changed, true);

  const ink = mergeDomain("ink", { pdf: {}, epub: {} },
                          { pdf: { "page:1": [{ p: [[0, 0]] }] }, epub: {} },
                          { pdf: { "page:2": [{ p: [[1, 1]] }] }, epub: {} });
  assert.deepEqual(Object.keys(ink.value.pdf).sort(), ["page:1", "page:2"]);

  // 认不出来的域不猜：保留本地并标 unknown，让调用方决定。
  const unknown = mergeDomain("something-new", {}, { a: 1 }, { a: 2 });
  assert.equal(unknown.unknown, true);
  assert.deepEqual(unknown.value, { a: 1 });
});

test("两边一样时 changed=false —— 不必白写一次", () => {
  const value = [hl("a", 1)];
  const merged = mergeDomain("notes", value, value, value);
  assert.equal(merged.changed, false);
});

test("「一边删、一边改」是刻意选的：改赢，删的那条会回来", () => {
  // 便签/插入页的删除是**从数组里拿掉**（不像高亮有墓碑），所以没法知道删除
  // 与编辑谁更晚。两害相权：宁可让一条被删的便签回来（再删一次就行），
  // 也不要把另一台设备上刚写的内容弄丢。
  // ⚠ 这条行为是被测出来的，不是没想过 —— 哪天要改成"删赢"，改这里并连同
  //    上面那段理由一起改掉。
  const base = [{ id: "n1", rev: 1, text: "x" }];
  const mine = [];                                       // 我这边删了
  const theirs = [{ id: "n1", rev: 4, text: "它改的" }];  // 它那边改了
  const out = mergeCollection(base, mine, theirs);
  assert.equal(out.length, 1);
  assert.equal(out[0].text, "它改的");
});

test("一边删、一边没动 → 删得掉", () => {
  const base = [{ id: "n1", rev: 1, text: "x" }];
  const out = mergeCollection(base, [], [{ id: "n1", rev: 1, text: "x" }]);
  assert.equal(out.length, 0);
});
