import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 网页版 reader_page_text。两件事必须同时成立，缺一都是**静默失败**：
//   ① C# 表面闸放行 kind==="web"（不放行 → BuildQueryRequest 回 null，请求发不出去）
//   ② 页面侧 query 表有网页实现（没有 → 回 "unsupported"）
// 这两处离得很远，改了一处会以为改好了 —— 2026-08-19 那类事故的原样重演。
const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

const QUERY_CS = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderQuery.cs",
);
const INBOUND = read("_server_deploy/static/pdf/rc-computer-voice.js");
const VENDOR = read("extensions/bw-reader-webext/vendor/rc-computer-voice.js");
const MANIFEST = JSON.parse(read("extensions/bw-reader-webext/manifest.json"));
const PAGETEXT_SRC = read("extensions/bw-reader-webext/src/web-pagetext.js");
const TEXTLAYER_SRC = read("extensions/bw-reader-webext/src/web-textlayer.js");

test("① C# 表面闸放行 web，且 search 仍然不放行", () => {
  const branch = QUERY_CS.slice(
    QUERY_CS.indexOf('"page-text" ='),
    QUERY_CS.indexOf('"page-text" =') + 260,
  );
  assert.match(
    branch, /kind is "pdf" or "epub" or "web"/,
    'page-text 必须放行 web，否则请求在 C# 侧就被丢掉',
  );
  // search 放行了也只会回 unsupported（网页没有全文索引）——
  // 放行一个注定失败的能力比不放行更糟：AI 会反复重试。
  const searchBranch = QUERY_CS.slice(
    QUERY_CS.indexOf('"search" ='),
    QUERY_CS.indexOf('"search" =') + 120,
  );
  assert.doesNotMatch(
    searchBranch, /"web"/,
    "search 不该放行 web —— 网页没有全文索引，放行只会让 AI 反复重试",
  );
});

test("② 页面侧 query 表有网页回退（源与扩展副本都要有）", () => {
  for (const [name, src] of [["源", INBOUND], ["扩展副本", VENDOR]]) {
    const at = src.indexOf('"page-text": function');
    assert.ok(at > 0, `${name} 里找不到 page-text handler`);
    // ⚠ 窗口要够宽：handler 每次加分支都会变长，窗口太窄会让断言
    //   莫名其妙地红，而看起来像功能坏了。
    const body = src.slice(at, at + 1200);
    // ⚠ 只断言"标识符出现过"是不够的：把调用那行改成 `if (false)` 之后
    //   变量声明还在，断言照样通过 —— 变异验证抓到的正是这一点。
    //   要钉住的是**回退真的被调用**。
    assert.match(
      body, /__bwWebPageText/,
      `${name} 的 page-text 必须有网页回退，否则网页上只会回 unsupported`,
    );
    assert.match(
      body, /web\.read\(/,
      `${name} 的网页回退必须真的被调用，不能只声明不用`,
    );
    // ⚠ 早退检查：原实现是 `if (typeof target !== "function") return null;`
    //   —— 保留它的话网页分支永远走不到。
    assert.doesNotMatch(
      body, /typeof target !== "function"\) return null/,
      `${name} 里那句提前 return 必须去掉，否则网页分支是死代码`,
    );
  }
});

test("③ 三个网页文件都挂进了 content_scripts，且顺序正确", () => {
  const js = MANIFEST.content_scripts[1].js;
  for (const f of ["src/web-textlayer.js", "src/web-bind.js", "src/web-pagetext.js"]) {
    assert.ok(js.includes(f), `${f} 没挂进 manifest —— 文件写了但根本不加载`);
  }
  assert.ok(
    js.indexOf("src/web-textlayer.js") < js.indexOf("src/web-bind.js"),
    "字符层必须排在使用它的层之前",
  );
  assert.ok(
    js.indexOf("src/web-textlayer.js") < js.indexOf("src/web-adapter.js"),
    "adapter 取锚要用字符层，必须排在它之后",
  );
});

// ── 行为测试：把真代码跑起来 ─────────────────────────────────────
//
// ⚠ 桩必须**有结构**（nodeType / parentElement / 祖先链），否则像 2026-08-23
//   那次一样：桩全绿，真浏览器里整类情况失效。桩仍不能替代真机，
//   真机覆盖见 extensions/bw-reader-webext/test_web_bind_local.py。
function buildDom(blocks) {
  const body = {
    nodeType: 1, tagName: "BODY", childNodes: [],
    closest: () => null, contains: () => true,
  };
  const nodes = [];
  const elements = [];
  blocks.forEach((b) => {
    // wrapper 让"嵌套块只取最内层"这条规则有真覆盖
    const outer = b.wrap
      ? { nodeType: 1, tagName: b.wrap.toUpperCase(), childNodes: [], parentElement: body,
          closest: () => null, contains: () => true, __inner: true }
      : null;
    const el = {
      nodeType: 1,
      tagName: (b.tag || "P").toUpperCase(),
      childNodes: [],
      parentElement: outer || body,
      closest(sel) {
        if (b.chrome && sel.includes("role=navigation")) {
          return { tagName: b.chrome.toUpperCase(), getAttribute: () => null };
        }
        return null;
      },
      contains: () => true,
      __selfSel: b.tag || "p",
    };
    const t = { nodeType: 3, nodeValue: b.text, parentElement: el };
    el.childNodes.push(t);
    if (outer) { outer.childNodes.push(el); elements.push(outer); }
    elements.push(el);
    nodes.push(t);
  });
  return { body, nodes, elements };
}

function loadStack(blocks, opts = {}) {
  const { body, nodes, elements } = buildDom(blocks);
  const doc = {
    body,
    documentElement: body,
    head: { appendChild() {} },
    createTreeWalker(_r, _w, filter) {
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
        setStart(n, o) { r.startContainer = n; r.startOffset = o; },
        setEnd(n, o) { r.endContainer = n; r.endOffset = o; },
        collapse() {}, comparePoint: () => 0,
      };
      return r;
    },
    // 选择器匹配：只回答"这个元素算不算块"
    querySelectorAll(sel) {
      const wanted = sel.split(",").map((s) => s.trim().toLowerCase());
      return elements.filter((e) => wanted.includes(e.tagName.toLowerCase()));
    },
  };
  // querySelector 用于 hasInner 判定：wrapper 里有内层块
  elements.forEach((e) => {
    e.querySelector = (sel) => {
      const wanted = sel.split(",").map((s) => s.trim().toLowerCase());
      const kid = (e.childNodes || []).find(
        (c) => c.nodeType === 1 && wanted.includes(c.tagName.toLowerCase()),
      );
      return kid || null;
    };
  });

  const win = {};
  const NodeFilter = { SHOW_TEXT: 4, FILTER_REJECT: 2, FILTER_ACCEPT: 1 };
  new Function("window", "document", "NodeFilter", "MutationObserver", TEXTLAYER_SRC)(
    win, doc, NodeFilter, undefined,
  );
  if (opts.articleRoot !== false) win.__bwArticleRoot = () => body;
  new Function("window", "document", "NodeFilter", "MutationObserver", PAGETEXT_SRC)(
    win, doc, NodeFilter, undefined,
  );
  return { api: win.__bwWebPageText, win, doc, elements };
}

const BLOCKS = [
  { tag: "h1", text: "热力学导论" },
  { tag: "p", text: "第一定律说的是能量守恒。" },
  { tag: "p", text: "首页 关于 联系", chrome: "nav" },
  { tag: "p", text: "第二定律说的是熵增。" },
];

test("④ 输出带 [NN] 编号和区域标签，导航块被标出来", () => {
  const { api } = loadStack(BLOCKS);
  const out = api.read({});
  assert.equal(out.ok, true);
  const lines = out.text.split("\n");
  assert.equal(lines.length, 4);
  assert.match(lines[0], /^\[01\] 正文 # 热力学导论/, "标题要有 # 前缀和编号");
  assert.match(lines[1], /^\[02\] 正文 第一定律/);
  assert.match(
    lines[2], /^\[03\] 导航 /,
    "导航块必须被标成导航 —— 这正是用户要的'为 ai 区分正文、边栏等块区'",
  );
  assert.match(lines[3], /^\[04\] 正文 第二定律/);
  assert.equal(out.degraded, false, "认得出块结构时不该标降级");
});

test("⑤ segments 是字符层真坐标，不是 Markdown 里的位置", () => {
  const { api, win } = loadStack(BLOCKS);
  const out = api.read({});
  const snap = win.__bwWebTextLayer.snapshot();
  const seg = out.segments.find((s) => s.text.includes("能量守恒"));
  assert.ok(seg, "应该有一条覆盖'能量守恒'的 segment");
  assert.equal(
    snap.text.slice(seg.from, seg.to), "第一定律说的是能量守恒。",
    "from/to 必须能在字符层里切出原文 —— 这是 bind 能不能钉准的唯一判据",
  );
  // ⚠ 反向确认：拿 Markdown 的位置去切必然切错。这条正是 ANCHOR_MAP 的教训。
  assert.notEqual(
    out.text.slice(seg.from, seg.to), "第一定律说的是能量守恒。",
    "如果 Markdown 位置恰好也对，这个测试就失去了意义（说明两套坐标没分开）",
  );
});

test("⑥ segments 带 block 号和 region，能跟正文里的 [NN] 对上", () => {
  const { api } = loadStack(BLOCKS);
  const out = api.read({});
  const nav = out.segments.find((s) => s.region === "导航");
  assert.ok(nav, "导航块也要给 segment —— AI 需要知道它存在才能主动跳过");
  assert.equal(nav.block, 3, "block 号必须与正文里的 [03] 一致");
});

test("⑦ 带 rev，且与字符层一致", () => {
  const { api, win } = loadStack(BLOCKS);
  const out = api.read({});
  assert.equal(out.rev, win.__bwWebTextLayer.snapshot().rev,
    "rev 必须来自同一份字符层，否则 AI 回传的 bind.rev 永远对不上");
  assert.equal(out.page, 1, "网页整篇即一页");
});

test("⑧ 空页面明确说不可用，且标为可重试", () => {
  const { api } = loadStack([]);
  const out = api.read({});
  assert.equal(out.ok, false);
  assert.equal(out.retryable, true,
    "页面还没渲染完是暂时的，必须让调用方知道可以再试");
});

test("⑨ 截断要说出来", () => {
  const many = [];
  for (let i = 0; i < 260; i += 1) {
    many.push({ tag: "p", text: "这是一个足够长的段落用来把总量顶过上限。" + i });
  }
  const { api } = loadStack(many);
  const out = api.read({});
  assert.equal(
    out.truncated, true,
    "截断必须明说 —— 否则 AI 会把'只有这些'当成'全部就这些'然后据此下结论",
  );
});

// ── 2026-08-23 审计抓到的 high ────────────────────────────────────
test("⑩ ⚠ 正文写在 div 里的站点必须认得出块，而不是返回'成功且空'", () => {
  // x.com 的推文是 <div data-testid="tweetText">，多数 React/SPA 与 Gmail 同理。
  // 原实现的 BLOCK_SEL 没有 div，一个块都选不出来，却仍返回 ok:true + 空 text
  // + truncated:false —— 等于告诉 AI「整页就这些」。
  const divPage = [
    { tag: "div", text: "这是一条推文的正文内容。" },
    { tag: "div", text: "这是第二条推文。" },
  ];
  const { api } = loadStack(divPage);
  const out = api.read({});
  assert.equal(out.ok, true);
  assert.ok(out.text.length > 0, "div 站点绝不能返回空 text");
  assert.match(out.text, /这是一条推文的正文内容/);
  assert.equal(out.segments.length, 2, "两个 div 应该各出一条 segment");
});

test("⑪ 嵌套块只取最内层，同一段文字不重复出现", () => {
  // 加了 div 之后最大的风险是每层包裹都出一块，同一段文字出现多次，
  // 而且区间互相包含，AI 无从选择。
  const nested = [{ tag: "p", text: "被包在 div 里的段落。", wrap: "div" }];
  const { api } = loadStack(nested);
  const out = api.read({});
  assert.equal(out.segments.length, 1, "外层 div 不该再出一块");
  assert.match(out.text, /^\[01\] .*被包在 div 里的段落。$/);
});

test("⑫ ⚠ 字符层非空却认不出块时，给降级视图并明说，绝不返回'成功且空'", () => {
  // 认不出块的极端情况（自定义元素、shadow 化布局…）。
  // 宁可给一份没有块结构的平铺文本，也不能让 AI 以为这页是空的。
  const exotic = [{ tag: "custom-thing", text: "这段文字确实存在于页面上。" }];
  const { api } = loadStack(exotic);
  const out = api.read({});
  assert.equal(out.ok, true);
  assert.equal(out.degraded, true, "降级必须明说");
  assert.match(
    out.text, /这段文字确实存在于页面上/,
    "降级视图必须包含真实内容 —— 空 text 会让 AI 断言这页没内容",
  );
  assert.equal(out.segments.length, 1);
  assert.equal(out.segments[0].from, 0);
});
