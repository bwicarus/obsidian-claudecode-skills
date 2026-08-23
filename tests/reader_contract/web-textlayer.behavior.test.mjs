import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 网页字符层是"卡片能不能钉到网页上"的地基：AI 拿到的下标和钉卡时解析的
// 下标必须是同一套坐标。所以这里**不做正则断言**，而是把真代码跑起来。
//
// ⚠ 2026-08-23 的教训：这个桩的第一版**连 nodeType 都没设**，于是
//   "Range 的边界容器可能是元素节点"这一整类情况零覆盖，桩全绿而真浏览器里
//   三击选段直接失效。桩越不像真 DOM，它给的信心就越假 ——
//   见 references/silent-failure-lessons.md。现在的桩带 nodeType、
//   父子关系和 comparePoint，元素容器路径有真覆盖。
//   即便如此，桩仍**不能替代真机**：真机覆盖见
//   extensions/bw-reader-webext/test_web_bind_local.py。
const SRC = readFileSync(
  new URL("../../extensions/bw-reader-webext/src/web-textlayer.js", import.meta.url),
  "utf8",
);

// ── 最小但**有结构**的 DOM 桩 ──────────────────────────────────
// 每个 chunk 挂在一个块元素下，块元素挂在 body 下，从而能表达
// "边界落在块元素上、offset 是子节点序号" 这种真实形状。
function makeDom(chunks) {
  const body = { nodeType: 1, tagName: "BODY", childNodes: [], closest: () => null };
  const nodes = [];
  chunks.forEach((c) => {
    const el = {
      nodeType: 1,
      tagName: (c.tag || "P").toUpperCase(),
      childNodes: [],
      closest(sel) {
        // 只需回答"祖先里有没有被排除的东西"
        if (c.excludedBy && sel.includes(c.excludedBy)) return { tagName: "X" };
        return null;
      },
    };
    const t = {
      nodeType: 3,
      nodeValue: c.text,
      parentElement: el,
      __block: el,
    };
    el.childNodes.push(t);
    el.parentElement = body;
    body.childNodes.push(el);
    nodes.push(t);
  });

  // 文档序：块 i 的文本节点排在块 i+1 之前
  const orderOf = (node) => {
    const i = nodes.indexOf(node);
    return i < 0 ? Number.POSITIVE_INFINITY : i;
  };

  const doc = {
    body,
    documentElement: body,
    createTreeWalker(_root, _what, filter) {
      let i = -1;
      return {
        nextNode() {
          for (;;) {
            i += 1;
            if (i >= nodes.length) return null;
            if (filter.acceptNode(nodes[i]) === 1) return nodes[i];
          }
        },
      };
    },
    createRange() {
      const r = {
        setStart(node, off) { r.startContainer = node; r.startOffset = off; },
        setEnd(node, off) { r.endContainer = node; r.endOffset = off; },
        collapse() {},
        // 真实语义：返回 (node,offset) 这个点相对本 range（此处已折叠）的位置
        comparePoint(node) {
          const boundary = r.startContainer;
          const bOff = r.startOffset;
          let bIndex;
          if (boundary.nodeType === 3) bIndex = orderOf(boundary);
          else if (boundary === body) bIndex = bOff;           // body 的第 N 个块
          else bIndex = orderOf(boundary.childNodes[0]) + (bOff > 0 ? 1 : 0);
          const nIndex = orderOf(node);
          if (nIndex < bIndex) return -1;
          if (nIndex > bIndex) return 1;
          return boundary.nodeType === 3 && bOff > 0 ? -1 : 0;
        },
      };
      return r;
    },
  };
  return { doc, nodes, body };
}

function load(chunks) {
  const { doc, nodes, body } = makeDom(chunks);
  const win = {};
  const NodeFilter = { SHOW_TEXT: 4, FILTER_REJECT: 2, FILTER_ACCEPT: 1 };
  const fn = new Function(
    "window", "document", "NodeFilter", "MutationObserver",
    SRC + "\nreturn window.__bwWebTextLayer;",
  );
  const api = fn(win, doc, NodeFilter, undefined);
  return { api, nodes, body, doc };
}

const PAGE = [
  { text: "导航 首页 关于" },
  { text: "热力学第一定律说的是能量守恒。" },
  { text: "边栏广告：点击这里" },
  { text: "第二段又提到能量守恒这个词。" },
  { text: "隐藏脚本内容", excludedBy: "script" },
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

test("排除名单挡住扩展自己插进正文流的东西（rt / 译文 / 占位）", () => {
  // ⚠ 这三条是 2026-08-23 审计抓到的 high：振假名把读音、译页把译文
  //   插进**同一个块内部**，不排除的话已存在的锚当场全错。
  const withNoise = [
    { text: "日本" },
    { text: "にほん", excludedBy: "rt" },          // 振假名读音
    { text: "語" },
    { text: "This is Japanese.", excludedBy: "data-rc-tr" },  // 译文
    { text: "翻译中…", excludedBy: "data-rc-ph" },            // 占位
  ];
  const snap = load(withNoise).api.snapshot();
  assert.equal(
    snap.text, "日本語",
    "读音/译文/占位都不算正文字符 —— 混进来会让已钉的卡按文本也找不回来",
  );
});

test("⚠ 反向：原文被收起（.rc-tr-src）时仍必须留在字符层里", () => {
  // replace 样式下原文被裹进 .rc-tr-src.rc-tr-src-hidden **收起但不删**。
  // 排除它会让所有锚朝另一个方向全错 —— 判据是"是不是页面原本的正文"，
  // 不是"现在看不看得见"。
  const SRC_TEXT = readFileSync(
    new URL("../../extensions/bw-reader-webext/src/web-textlayer.js", import.meta.url),
    "utf8",
  );
  const sel = SRC_TEXT.slice(
    SRC_TEXT.indexOf("var EXCLUDE_SEL"),
    SRC_TEXT.indexOf("function excluded"),
  );
  assert.doesNotMatch(
    sel, /rc-tr-src/,
    ".rc-tr-src 是原文本身，绝不能排除",
  );
  assert.match(sel, /\[data-rc-tr\]/, "译文必须排除");
  assert.match(sel, /\brt\b/, "注音读音必须排除");
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
  const { api, nodes } = load(PAGE);
  const node = nodes[1];
  const local = node.nodeValue.indexOf("能量守恒");
  const bind = api.rangeToBind({
    startContainer: node, startOffset: local,
    endContainer: node, endOffset: local + 4,
  });
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

// ── 元素容器：真浏览器里最常见的那一类选区 ──────────────────────
test("⚠ 三击选段：endContainer 是元素时也要给出锚，不能返回 null", () => {
  // 实测（真 Chrome）：三击段落 → startContainer=TEXT, endContainer=<P>。
  // 老实现在这里返回 null，「锁定元素」对最常见的手势直接失效且不出声。
  const { api, nodes } = load(PAGE);
  const node = nodes[1];
  const block = node.parentElement;
  const bind = api.rangeToBind({
    startContainer: node, startOffset: 0,
    endContainer: block, endOffset: 1,      // 块的第 1 个子节点之后 = 段末
  });
  assert.ok(bind, "三击选段必须能折出锚 —— 这是最常见的'选中这一段'手势");
  const snap = api.snapshot();
  assert.equal(
    snap.text.slice(bind.from, bind.to), "热力学第一定律说的是能量守恒。",
    "元素边界必须换算成该块末尾的字符下标",
  );
});

test("⚠ 全选：startContainer=<body>、offset=0 也要成立", () => {
  const { api, body, nodes } = load(PAGE);
  const last = nodes[3];
  const bind = api.rangeToBind({
    startContainer: body, startOffset: 0,
    endContainer: last, endOffset: last.nodeValue.length,
  });
  assert.ok(bind, "Ctrl+A 必须能折出锚");
  assert.equal(bind.from, 0, "body 的 offset 0 = 全文开头");
  const snap = api.snapshot();
  assert.equal(snap.text.slice(bind.from, bind.to), snap.text);
});

test("⚠ 元素容器绝不能把子节点序号当字符偏移", () => {
  // 老实现若拿到 i>=0 而容器是元素，会做 starts[i] + offset ——
  // 把"第几个子节点"当成"第几个字符"，得到一个看起来合法却完全错位的区间。
  const { api, nodes } = load(PAGE);
  const block = nodes[3].parentElement;
  const bind = api.rangeToBind({
    startContainer: block, startOffset: 0,
    endContainer: block, endOffset: 1,
  });
  assert.ok(bind);
  const snap = api.snapshot();
  assert.equal(
    snap.text.slice(bind.from, bind.to), "第二段又提到能量守恒这个词。",
    "整块选中必须精确覆盖该块，多一个字少一个字都说明换算错了",
  );
});

test("解析①：rev 对得上走精确下标", () => {
  const { api, nodes } = load(PAGE);
  const node = nodes[1];
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
  const { api, nodes } = load(PAGE);
  const node = nodes[3];             // 第二处「能量守恒」
  const local = node.nodeValue.indexOf("能量守恒");
  const bind = api.rangeToBind({
    startContainer: node, startOffset: local,
    endContainer: node, endOffset: local + 4,
  });

  const shifted = [{ text: "【插入的横幅】" }, ...PAGE];
  const after = load(shifted);
  const got = after.api.locate(bind);

  // 2026-08-23 起 how 的取值改成能区分四条路：
  //   exact / by-block / by-text / by-text-block-missed
  assert.equal(got.how, "by-text", "rev 对不上必须退回重找，而不是用旧下标");
  assert.equal(
    got.range.startContainer, after.nodes[4],
    "同一个词重复出现时，必须用原下标挑最接近的那一处 —— 挑错就等于把卡钉到别的段落",
  );
});

test("解析③：文本彻底消失时返回 null，不猜一个位置", () => {
  const { api, nodes } = load(PAGE);
  const node = nodes[1];
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
  const { api, nodes } = load(PAGE);
  const node = nodes[1];
  const local = node.nodeValue.indexOf("能量守恒");
  const bind = api.rangeToBind({
    startContainer: node, startOffset: local,
    endContainer: node, endOffset: local + 4,
  });
  const forged = { ...bind, from: 0, to: 4 };
  const got = api.locate(forged);
  assert.notEqual(got && got.how, "exact",
    "rev 一致也要核对文本；碰撞的代价是钉到完全无关的位置");
});

test("locate 可复用外部索引，不重复构建", () => {
  // resolve() 一张卡原本要走两遍 build + 两遍 revision（实测 ~220ms/卡）。
  const { api, nodes } = load(PAGE);
  const node = nodes[1];
  const bind = api.rangeToBind({
    startContainer: node, startOffset: 0,
    endContainer: node, endOffset: 4,
  });
  const idx = api.build();
  const got = api.locate(bind, idx);
  assert.ok(got && got.range, "传入预建索引时必须照常工作");
  assert.equal(api.build(), idx, "DOM 没变时 build() 必须复用同一份索引");
});

test("非 page-chars 锚一律不认", () => {
  const { api } = load(PAGE);
  assert.equal(api.locate({ kind: "upage-block", upage: "x", bid: "y" }), null);
  assert.equal(api.locate(null), null);
});

// ── (块, 块内文字) 寻址 —— 用户 2026-08-23 定的方式 ──────────────
const DUP = [
  { text: "第一段提到能量守恒。" },
  { text: "第二段也提到能量守恒。" },
  { text: "第三段还是能量守恒。" },
];

test("⚠ 块把重复消歧：同一句话出现三次，块号决定命中哪一处", () => {
  const { api, nodes } = load(DUP);
  const snap = api.snapshot();
  // 第二块的区间
  const b2From = snap.text.indexOf("第二段");
  const b2To = b2From + DUP[1].text.length;
  const got = api.locate(
    { kind: "page-chars", page: 1, text: "能量守恒" },
    null,
    { from: b2From, to: b2To },
  );
  assert.ok(got, "块内应当命中");
  assert.equal(got.how, "by-block", "命中块内时必须报 by-block");
  assert.equal(
    got.range.startContainer, nodes[1],
    "必须落在第二块里 —— 这正是块寻址存在的理由",
  );
});

test("⚠ 块对不上时退回全页，但**必须出声**", () => {
  const { api } = load(DUP);
  const got = api.locate(
    { kind: "page-chars", page: 1, text: "能量守恒" },
    null,
    { from: 9990, to: 9999 },          // 一个不存在的块区间
  );
  assert.ok(got, "退回全页仍应命中");
  assert.equal(
    got.how, "by-text-block-missed",
    "带了块号却没在块里找到 —— 这必须能看出来。静默降级是这条链上最难查的形态",
  );
});

test("没带块号时按文本找，报 by-text", () => {
  const { api } = load(DUP);
  const got = api.locate({ kind: "page-chars", page: 1, text: "能量守恒" });
  assert.equal(got.how, "by-text");
});

test("text-only 锚（无 from/to）不会被当成精确锚", () => {
  const { api } = load(DUP);
  const got = api.locate({ kind: "page-chars", page: 1, text: "能量守恒" });
  assert.notEqual(
    got.how, "exact",
    "没有 from/to 就不可能是精确的；报成 exact 会掩盖真实的定位质量",
  );
});
