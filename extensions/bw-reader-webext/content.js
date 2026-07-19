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
    snapshot = {
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

  root.querySelector("#lookup").addEventListener("click", () => send("LOOKUP", formatDictionary));
  root.querySelector("#translate").addEventListener("click", () =>
    send("TRANSLATE", (d) => d.translation || d.result || d.zh || JSON.stringify(d)));
  root.querySelector("#explain").addEventListener("click", () =>
    send("EXPLAIN", (d) => d.explanation || d.text || d.result || JSON.stringify(d)));
  root.querySelector("#close").addEventListener("click", () => {
    locked = false; snapshot = null; panel.hidden = true;
    window.getSelection()?.removeAllRanges();
  });
})();
