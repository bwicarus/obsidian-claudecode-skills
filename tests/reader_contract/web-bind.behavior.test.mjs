import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// web-bind.js 此前**零测试覆盖** —— 2026-08-23 的审计在里面抓到三个 high。
// 这份补上契约层的钉子；真机行为见
// extensions/bw-reader-webext/test_web_bind_local.py。
const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

const BIND = read("extensions/bw-reader-webext/src/web-bind.js");
const HIGHLIGHTS = read("extensions/bw-reader-webext/src/web-highlights.js");
const STICKY = read("extensions/bw-reader-webext/vendor/rc-stickynote.js");
const VOICECALL = read("extensions/bw-reader-webext/vendor/rc-voicecall.js");

test("⚠ ::highlight 规则注进主文档 head，不能进影子树", () => {
  // 抓到的 high：规则原来跟角标样式共用一个 style，而那个 style 挂在
  // window.__bwHead —— #bw-reader-host 影子树里的节点。被高亮的文本在主文档树，
  // 影子树的规则匹配不到，于是一个像素都不画，而所有返回值都说成功。
  const fn = BIND.slice(BIND.indexOf("function ensureHlCss"), BIND.indexOf("function pinRoot"));
  assert.match(
    fn, /document\.head \|\| document\.documentElement/,
    "::highlight 的 style 必须挂进主文档 head",
  );
  assert.doesNotMatch(
    fn, /__bwHead/,
    "::highlight 的 style 绝不能挂进 __bwHead（那是影子树）",
  );
  // 反向：角标样式**必须**留在 __bwHead —— 它在 pinRoot 里，靠 syncPinStyles 镜像
  const badgeCss = BIND.slice(BIND.indexOf("function ensureCss"), BIND.indexOf("function ensureHlCss"));
  assert.match(
    badgeCss, /window\.__bwHead/,
    "角标样式在 pinRoot 内，必须走 __bwHead 才能被 syncPinStyles 镜像过去",
  );
  // 与已在真机跑过的参考实现一致
  assert.match(
    HIGHLIGHTS, /document\.head\.appendChild\(css\)/,
    "参考实现 web-highlights.js 也是注进 document.head 的",
  );
});

test("⚠ __pageBindRemove 必须存在 —— rc-stickynote 三处撤销点靠它", () => {
  // 抓到的 high：这个函数原本不存在。三处调用都写成
  // `if (_b && window.__pageBindRemove)`，函数不在就**整句跳过、不报错**，
  // 于是便签删了、角标和 Highlight 永远留在页面上，序号还全部错位。
  const callSites = STICKY.match(/window\.__pageBindRemove/g) || [];
  assert.ok(
    callSites.length >= 3,
    `rc-stickynote 应有 3 处撤销点，实得 ${callSites.length}`,
  );
  assert.match(
    BIND, /window\.__pageBindRemove\s*=\s*function/,
    "网页侧必须实现 __pageBindRemove，否则撤销静默跳过",
  );
  const impl = BIND.slice(BIND.indexOf("window.__pageBindRemove"), BIND.indexOf("window.__pageBindPersist"));
  assert.match(impl, /clearMark\(/, "撤销必须真的清掉标记");
  assert.match(impl, /renumber\(/, "撤销后必须重编号，否则序号错位");
});

test("⚠ 撤销与创建必须用同一套 key 推导", () => {
  // key 推导若两处各写一遍，迟早分叉：创建用 'u'+uid、撤销用区间 key，
  // 表现是「删了但角标还在」，而且没有任何报错。
  const shared = (BIND.match(/markKey\(/g) || []).length;
  assert.ok(
    shared >= 3,
    `创建/撤销/清理都应调同一个 markKey，实得 ${shared} 处调用`,
  );
});

test("⚠ 不得用 surroundContents 之类往正文流插节点的画法", () => {
  // 插一个 <mark> 就会改变字符层，于是所有已存在的锚当场偏移 ——
  // 而且偏移的是**别的卡**，查起来像随机错位。
  // ⚠ 只看代码行：源码注释里就写着「绝不退回 surroundContents」，
  //   直接对全文断言会被自己的注释匹配上（这个坑之前踩过一次）。
  const code = BIND.split("\n")
    .filter((l) => !/^\s*(\/\/|\/\*|\*)/.test(l))
    .join("\n");
  assert.doesNotMatch(
    code, /surroundContents/,
    "绝不能退回 surroundContents —— 它会把字符层整体推移",
  );
  assert.doesNotMatch(
    code, /insertAdjacentHTML|appendChild\(\s*mark/,
    "标记不得插入正文流",
  );
});

test("::highlight 规则按颜色分桶，不随标记数累积", () => {
  // 原来是每个标记一条规则、textContent += 追加、从不回收；
  // 加上便签侧由 MutationObserver 每次 DOM 变动重挂一遍，样式表无界膨胀，
  // 追加代价随表长二次增长（实测 0.04ms → 4.19ms/次）。
  assert.match(BIND, /hlByColor/, "应按颜色分桶复用 Highlight 对象");
  const paint = BIND.slice(BIND.indexOf("function highlightFor"), BIND.indexOf("function paintRange"));
  assert.match(paint, /if \(hlByColor\[name\]\) return hlByColor\[name\]/,
    "同色必须复用，规则只写一次");
  assert.match(BIND, /function unpaintRange/, "必须能把 range 从 Highlight 里删掉");
});

test("角标重定位：先读完所有 rect 再统一写，避免布局抖动", () => {
  // 原来是"读一个 → 写一个 → 再读"，每次写都让下一次读强制同步布局。
  // 实测 9.3ms/角标/帧，2 张卡就爆帧预算。
  const fn = BIND.slice(BIND.indexOf("function reposition"), BIND.indexOf("window.addEventListener('scroll'"));
  const readAt = fn.indexOf("rects[i] = measure");
  const writeAt = fn.indexOf("applyBadgePos(list[i].badge");
  assert.ok(readAt > 0 && writeAt > readAt,
    "必须先把所有 rect 读完（一个循环），再统一写样式（另一个循环）");
});

test("⚠ 角标也要跟随 DOM 变化，不能只靠 scroll/resize", () => {
  // 译页是异步分批到达的，每段插入译文都把后面内容整体下移，
  // 期间既没有 scroll 也没有 resize —— 角标会停在旧坐标上指向别的段落。
  assert.match(
    BIND, /TL\.onChange\(reposition\)/,
    "必须订阅字符层的 DOM 变化",
  );
});

test("resolve 只构建一次索引", () => {
  // 原来 snapshot() + locate() 是两遍遍历加两遍哈希（实测 ~220ms/卡）。
  const fn = BIND.slice(BIND.indexOf("function resolve(bind)"), BIND.indexOf("function markKey"));
  assert.equal(
    (fn.match(/TL\.build\(\)/g) || []).length, 1,
    "resolve 里只能构建一次索引",
  );
  assert.match(fn, /TL\.locate\(bind, idx\)/, "locate 必须复用同一份索引");
  assert.doesNotMatch(fn, /TL\.snapshot\(\)/, "别再额外走一遍 snapshot");
});

test("失败原因分类与 rc-stickynote 的两类处置对得上", () => {
  // rc-stickynote 把 why 分成「暂时性 → 继续藏着等重试」和
  // 「这页就是不成 → 立刻显示回来」。产出一个永远不会好转却被当成暂时性的 why，
  // 会让卡片**永久隐身**。
  const transient = STICKY.match(/why === 'page-not-rendered' \|\| res\.why === 'no-char-layer'/);
  assert.ok(transient, "rc-stickynote 的暂时性判据应是这两个值");
  const whys = [...BIND.matchAll(/why: '([a-z-]+)'/g)].map((m) => m[1]);
  const transientSet = new Set(["page-not-rendered", "no-char-layer"]);
  for (const w of whys) {
    if (transientSet.has(w)) {
      assert.equal(
        w, "no-char-layer",
        "网页上唯一合理的暂时性 why 是 no-char-layer（页面还没渲染完）",
      );
    }
  }
  assert.ok(whys.includes("no-char-layer"), "字符层为空时应报暂时性");
  assert.ok(whys.includes("range-unresolved"), "解不出区间应报永久性，让卡片显示回来");
});

test("__pageBindPersist 的返回形状与 rc-voicecall 的消费一致", () => {
  const at = VOICECALL.indexOf("__pageBindPersist");
  assert.ok(at > 0);
  const consume = VOICECALL.slice(at, at + 700);
  assert.match(consume, /_pr\s*&&\s*_pr\.ok === true/, "调用方按 ok===true 判成功");
  const impl = BIND.slice(BIND.indexOf("window.__pageBindPersist"), BIND.indexOf("function currentSelection"));
  assert.match(impl, /ok: true/, "成功分支必须给 ok:true");
  assert.match(impl, /why: 'persistence-unavailable'/, "共享层缺席时要说清原因");
  assert.match(
    impl, /result\.ok !== true/,
    "只有便签仓真的提交后才算成功 —— 绝不先画标记再谎报 bound",
  );
});
