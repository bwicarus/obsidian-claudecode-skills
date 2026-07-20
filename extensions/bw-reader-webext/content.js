// 页面内选区工具条(Shadow DOM 隔离,不污染宿主页样式)。iOS Safari 不支持扩展 contextMenus,
// 所以用固定底部工具条,而非右键菜单。只在用户主动查词时才发送选区+附近段落,不自动上传整页。
(() => {
  if (window.top !== window) return;   // 不进 iframe

  let snapshot = null;
  let locked = false;   // 面板交互中不因 selectionchange 抢关
  let timer = null;

  const host = document.createElement("div");
  host.id = "bw-reader-extension";
  document.documentElement.appendChild(host);
  const root = host.attachShadow({ mode: "open" });

  root.innerHTML = `
    <style>
      #panel { position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%);
        z-index: 2147483647; width: min(560px, calc(100vw - 24px)); padding: 10px;
        border: 1px solid rgba(120,130,150,.35); border-radius: 14px;
        background: rgba(24,27,34,.97); color: #f0f3f8; box-shadow: 0 10px 35px rgba(0,0,0,.4);
        font: 14px/1.45 system-ui, -apple-system, "Noto Sans CJK SC", sans-serif; }
      #panel[hidden] { display: none; }
      #top { display: flex; gap: 6px; align-items: center; }
      #selection { flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: #cfe6ff; }
      button { border: 0; border-radius: 9px; padding: 7px 11px; background: #4f7cff; color: white;
        font: inherit; cursor: pointer; -webkit-tap-highlight-color: transparent; }
      button.sec { background: #2f3745; }
      #close { background: #4b5563; }
      #result { margin-top: 8px; max-height: 240px; overflow: auto; white-space: pre-wrap;
        overflow-wrap: anywhere; font-size: 13px; line-height: 1.5; }
      #result:empty { display: none; }
    </style>
    <section id="panel" hidden>
      <div id="top">
        <span id="selection"></span>
        <button id="lookup">查词</button>
        <button id="translate" class="sec">翻译</button>
        <button id="explain" class="sec">解释</button>
        <button id="close">✕</button>
      </div>
      <div id="result"></div>
    </section>
  `;

  const panel = root.querySelector("#panel");
  const selectionLabel = root.querySelector("#selection");
  const result = root.querySelector("#result");

  function languageHint() {
    const lang = String(document.documentElement.lang || "").toLowerCase();
    if (lang.startsWith("ja")) return "ja";
    if (lang.startsWith("en")) return "en";
    return "";
  }

  function captureSelection() {
    if (locked) return;
    const active = document.activeElement;
    if (active?.matches?.("input, textarea") || active?.isContentEditable) { panel.hidden = true; return; }

    const selection = window.getSelection();
    const text = String(selection?.toString() || "").trim();
    if (!text || !selection.rangeCount) { panel.hidden = true; return; }

    let element = selection.anchorNode;
    if (element?.nodeType === Node.TEXT_NODE) element = element.parentElement;
    const block = element?.closest?.("p, li, td, blockquote, h1, h2, h3, article, section");

    const url = new URL(location.href); url.hash = "";
    let rect = null;
    try { const rr = selection.getRangeAt(0).getBoundingClientRect();
          rect = { left: rr.left, top: rr.top, right: rr.right, bottom: rr.bottom }; } catch (_) {}
    snapshot = {
      rect,
      text: text.slice(0, 5000),
      context: String(block?.innerText || "").slice(0, 1200),
      title: document.title.slice(0, 300),
      url: url.href,
      lang: languageHint()
    };
    selectionLabel.textContent = snapshot.text.slice(0, 80);
    result.textContent = "";
    panel.hidden = false;
  }

  function formatDictionary(d) {
    // dict-quick 真实字段(pdf_reader.py:5323):英语 word/lemma/phonetic/translation/definition/audio_us;
    // 日语 lemma/reading/accent/pos/translation/definition/examples
    const lines = [
      d.word || d.lemma || "",
      d.reading ? `读音：${d.reading}` : "",
      d.phonetic ? `音标：${d.phonetic}` : "",
      d.pos ? `词性：${d.pos}` : "",
      d.translation || "",
      d.definition || ""
    ];
    if (Array.isArray(d.examples) && d.examples.length) {
      lines.push("例：" + d.examples.slice(0, 2).map((e) => (e.text || e)).join("  /  "));
    }
    return lines.filter(Boolean).join("\n");
  }

  async function send(type, render) {
    if (!snapshot) return;
    result.textContent = "查询中……";
    try {
      const resp = await chrome.runtime.sendMessage({ type, payload: snapshot });
      if (!resp?.ok) throw new Error(resp?.error || "查询失败");
      result.textContent = render(resp.data);   // 用 textContent,服务端内容永不成为页面代码
    } catch (e) {
      result.textContent = e instanceof Error ? e.message : String(e);
    }
  }

  document.addEventListener("selectionchange", () => {
    clearTimeout(timer);
    timer = setTimeout(captureSelection, 220);
  }, { passive: true });

  panel.addEventListener("pointerdown", () => { locked = true; });

  // ── 里程碑3:接阅读器同款组件(rc-wordpop 查词框 / rc-result 结果模态,SSE 经 facade 桥)──
  const EP = { translate: "/pdf/api/translate", explain: "/pdf/api/explain" };
  const resOpts = () => ({ kind: "note", aiParams: () => ({}) });
  const langsOf = () => (snapshot && snapshot.lang) ? [snapshot.lang] : [];
  root.querySelector("#lookup").addEventListener("click", () => {
    if (window.RC && RC.wordpop && snapshot) {
      panel.hidden = true;
      RC.wordpop.show({
        word: snapshot.text, rect: snapshot.rect, ctx: snapshot.context,
        file: "web:" + snapshot.url, langs: langsOf(),
        onFallback: (w) => { if (RC.result) RC.result.aiCall(EP.translate, { text: w, target_lang: "中文" }, "🌐 翻译", resOpts()); }
      });
      return;
    }
    send("LOOKUP", formatDictionary);   // 组件缺席兜底:旧 background 通道
  });
  root.querySelector("#translate").addEventListener("click", () => {
    if (window.RC && RC.result && snapshot) {
      panel.hidden = true;
      RC.result.aiCall(EP.translate, { text: snapshot.text, target_lang: "中文" }, "🌐 翻译", resOpts());
      return;
    }
    send("TRANSLATE", (d) => d.translation || d.result || d.zh || JSON.stringify(d));
  });
  root.querySelector("#explain").addEventListener("click", () => {
    if (window.RC && RC.result && snapshot) {
      panel.hidden = true;
      RC.result.aiCall(EP.explain, { text: snapshot.text, context: snapshot.context }, "💡 AI 解释", resOpts());
      return;
    }
    send("EXPLAIN", (d) => d.explanation || d.text || d.result || JSON.stringify(d));
  });
  // ── 点词直查(阅读器同款体验;改编自 html-reader.js clickWord 路径)──
  //    只在纯文本上触发:链接/按钮/输入框/可编辑区/扩展自身一律放行,不抢网页交互。
  let _lastDictTs = 0;
  const caretFromPoint = (x, y) => {
    if (document.caretRangeFromPoint) { const r = document.caretRangeFromPoint(x, y); return r ? { node: r.startContainer, offset: r.startOffset } : null; }
    if (document.caretPositionFromPoint) { const q = document.caretPositionFromPoint(x, y); return q ? { node: q.offsetNode, offset: q.offset } : null; }
    return null;
  };
  const wordAt = (node, off) => {
    const sTxt = node.nodeValue || ""; if (!sTxt) return null;
    const isW = (c) => /[A-Za-z0-9'’\-]/.test(c) || /[぀-ヿ㐀-鿿가-힯一-鿿]/.test(c);
    let i = off; if (i >= sTxt.length) i = sTxt.length - 1; if (i < 0) return null;
    if (!isW(sTxt[i])) { if (i > 0 && isW(sTxt[i - 1])) i--; else return null; }
    let lo = i, hi = i + 1;
    while (lo > 0 && isW(sTxt[lo - 1])) lo--;
    while (hi < sTxt.length && isW(sTxt[hi])) hi++;
    return { node, start: lo, end: hi, text: sTxt.slice(lo, hi) };
  };
  document.addEventListener("click", (e) => {
    if (!(window.RC && RC.wordpop)) return;
    const path = e.composedPath ? e.composedPath() : [];
    if (path.includes(host)) return;                       // 扩展自身 UI
    const tgt = e.target;
    if (tgt && tgt.closest && tgt.closest('a,button,input,textarea,select,label,[contenteditable="true"],[role="button"],video,audio')) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;                   // 拖选走工具条,不抢
    const now = Date.now(); if (now - _lastDictTs < 500) return; _lastDictTs = now;
    const pos = caretFromPoint(e.clientX, e.clientY);
    if (!pos || !pos.node || pos.node.nodeType !== 3) return;
    const w = wordAt(pos.node, pos.offset);
    if (!w || !w.text) return;
    const isEn = /^[A-Za-z][A-Za-z'’\-]*$/.test(w.text), isJa = /[぀-ヿ]/.test(w.text) || /[㐀-鿿]/.test(w.text);
    if (!isEn && !isJa) return;
    const rng = document.createRange();
    try { rng.setStart(w.node, w.start); rng.setEnd(w.node, w.end); } catch (_) { return; }
    const rr = rng.getBoundingClientRect();
    const blk = w.node.parentElement && w.node.parentElement.closest ? w.node.parentElement.closest("p,li,td,blockquote,h1,h2,h3,h4,div") : null;
    const pctx = (blk ? (blk.textContent || "") : "").trim().slice(0, 1200);
    RC.wordpop.show({
      word: w.text, rect: { left: rr.left, top: rr.top, right: rr.right, bottom: rr.bottom },
      ctx: pctx, file: "web:" + location.href.split("#")[0], langs: languageHint() ? [languageHint()] : [],
      onFallback: (wd) => { if (RC.result) RC.result.aiCall(EP.translate, { text: wd, target_lang: "中文" }, "🌐 翻译", resOpts()); }
    });
  }, true);

  root.querySelector("#close").addEventListener("click", () => {
    locked = false; snapshot = null; panel.hidden = true;
    window.getSelection()?.removeAllRanges();
  });
})();
