import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 网页字符层是"卡片能不能钉到网页上"的地基：AI 拿到的下标和钉卡时解析的
// 下标必须是同一套坐标。所以这里**不做正则断言**，而是把真代码跑起来。
//
// 用最小 DOM 桩而不是 jsdom：本仓没有 node_modules，而且要验的逻辑
//（下标换算、revision、重找消歧）不依赖真实排版。
const SRC = readFileSync(
  new URL("../../extensions/bw-reader-webext/src/web-textlayer.js", import.meta.url),
  "utf8",
);

function makeDom(chunks) {
  // chunks: [{text, excluded?}]
  const nodes = chunks.map((c) => ({
    nodeValue: c.text,
    __excluded: !!c.excluded,
    parentElement: { closest: () => (c.excluded ? {} : null) },
  }));
  const ranges = [];
  return {
    nodes,
    ranges,
    document: {
      body: {},
      documentElement: {},
      createTreeWalker(_root, _what, filter) {
        let i = -1;
        return {
          nextNode() {
            for (;;) {
              i += 1;
              if (i >= nodes.length) return null;
              const verdict = filter.acceptNode(nodes[i]);
              if (verdict === 1) return nodes[i];
            }
          },
        };
      },
      createRange() {
        const r = {
          setStart(node, off) { r.startContainer = node; r.startOffset = off; },
          setEnd(node, off) { r.endContainer = node; r.endOffset = off; },
        };
        ranges.push(r);
        return r;
      },
    },
  };
}

function load(chunks) {
  const dom = makeDom(chunks);
  const win = {};
  const sandbox = {
    window: win,
    document: dom.document,
    NodeFilter: { SHOW_TEXT: 4, FILTER_REJECT: 2, FILTER_ACCEPT: 1 },
  };
  const fn = new Function(
    "window", "document", "NodeFilter",
    SRC + "\nreturn window.__bwWebTextLayer;",
  );
  const api = fn(sandbox.window, sandbox.document, sandbox.NodeFilter);
  return { api, dom };
}

const PAGE = [
  { text: "导航 首页 关于" , excluded: false },
  { text: "热力学第一定律说的是能量守恒。" },
  { text: "边栏广告：点击这里" },
  { text: "第二段又提到能量守恒这个词。" },
  { text: "隐藏脚本内容", excluded: true },
];

test("字符层：整页统一坐标，被排除的节点不占下标", () => {
  const { api } = load(PAGE);
  const snap = api.snapshot();
  assert.equal(
    snap.text,
    "导航 首页 关于热力学第一定律说的是能量守恒。边栏广告：点击这里第二段又提到能量守恒这个词。",
    "文本必须是全页拼接（含边栏），且不含被排除节点",
  );
  assert.ok(!snap.text.includes("隐藏脚本"), "script/隐藏节点不得进入字符层");
  assert.equal(snap.length, snap.text.length);
});

test("revision：内容变了就变，内容不变就稳定", () => {
  const a = load(PAGE).api.revision();
  const b = load(PAGE).api.revision();
  assert.equal(a, b, "同样内容必须得到同样 revision，否则每次都退回重找");

  const changed = PAGE.slice();
  changed[1] = { text: "热力学第一定律说的是能量守恒!" };
  const c = load(changed).api.revision();
  assert.notEqual(a, c, "内容变了 revision 必须变，否则旧下标会被当成有效");
});

test("选区 → 锚：page 恒为 1，下标是全页坐标", () => {
  const { api, dom } = load(PAGE);
  // 选中第 2 个文本节点里的「能量守恒」
  const node = dom.nodes[1];
  const local = node.nodeValue.indexOf("能量守恒");
  const range = {
    startContainer: node, startOffset: local,
    endContainer: node, endOffset: local + 4,
  };
  const bind = api.rangeToBind(range);
  assert.equal(bind.kind, "page-chars");
  assert.equal(bind.page, 1, "网页没有分页，page 恒为 1（合法且诚实）");
  assert.equal(bind.text, "能量守恒");
  const snap = api.snapshot();
  assert.equal(
    snap.text.slice(bind.from, bind.to), "能量守恒",
    "from/to 必须是**全页**坐标，不是节点内坐标",
  );
  assert.ok(bind.rev, "必须带 rev，否则 DOM 变了也察觉不到");
});

test("解析①：rev 对得上走精确下标", () => {
  const { api, dom } = load(PAGE);
  const node = dom.nodes[1];
  const local = node.nodeValue.indexOf("能量守恒");
  const bind = api.rangeToBind({
    startContainer: node, startOffset: local,
    endContainer: node, endOffset: local + 4,
  });
  const got = api.locate(bind);
  assert.equal(got.how, "exact", "revision 没变时必须走精确路径");
  assert.equal(got.range.startContainer, node);
  assert.equal(got.range.startOffset, local);
});

test("解析②：DOM 变了退回按文本重找，并用原下标消歧", () => {
  const { api, dom } = load(PAGE);
  const node = dom.nodes[3];             // 第二处「能量守恒」
  const local = node.nodeValue.indexOf("能量守恒");
  const bind = api.rangeToBind({
    startContainer: node, startOffset: local,
    endContainer: node, endOffset: local + 4,
  });

  // 页面变了（顶部插入一段），rev 失效
  const shifted = [{ text: "【插入的横幅】" }, ...PAGE];
  const after = load(shifted);
  const got = after.api.locate(bind);

  assert.equal(got.how, "refound", "rev 对不上必须退回重找，而不是用旧下标");
  // ⚠ 关键：页内有**两处**「能量守恒」，必须挑回原来那一处（第二处）
  assert.equal(
    got.range.startContainer, after.dom.nodes[4],
    "同一个词重复出现时，必须用原下标挑最接近的那一处 —— 挑错就等于把卡钉到别的段落",
  );
});

test("解析③：文本彻底消失时返回 null，不猜一个位置", () => {
  const { api, dom } = load(PAGE);
  const node = dom.nodes[1];
  const local = node.nodeValue.indexOf("能量守恒");
  const bind = api.rangeToBind({
    startContainer: node, startOffset: local,
    endContainer: node, endOffset: local + 4,
  });
  const gone = load([{ text: "整页换成了别的内容" }]);
  assert.equal(
    gone.api.locate(bind), null,
    "找不到就返回 null → 上层退回浮层；绝不在页面上定出一个荒唐的位置",
  );
});

test("解析④：rev 相同但文本对不上时不盲信下标", () => {
  const { api, dom } = load(PAGE);
  const node = dom.nodes[1];
  const local = node.nodeValue.indexOf("能量守恒");
  const bind = api.rangeToBind({
    startContainer: node, startOffset: local,
    endContainer: node, endOffset: local + 4,
  });
  // 伪造：rev 保持有效，但把 from/to 指到别处（模拟哈希碰撞）
  const forged = { ...bind, from: 0, to: 4 };
  const got = api.locate(forged);
  assert.notEqual(got && got.how, "exact",
    "rev 一致也要核对文本；碰撞的代价是钉到完全无关的位置");
});

test("非 page-chars 锚一律不认", () => {
  const { api } = load(PAGE);
  assert.equal(api.locate({ kind: "upage-block", upage: "x", bid: "y" }), null);
  assert.equal(api.locate(null), null);
});
