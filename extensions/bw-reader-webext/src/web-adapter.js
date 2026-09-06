// web-adapter.js — 扩展版 WebAdapter:把「用户正在看的真实网页」接入共享控制层 window.RC。
//
// 照搬对象(测绘定稿):html-reader.js 的 HtmlAdapter——三个宿主里最贴近"活 DOM"的实现
// (选区=window.getSelection、锚=字符偏移、popup fixed、_host.asst 最精简可跑子集)。
// 差异只有三类,均已标注:
//   [宿主] 内容根从 #html-content 改为宿主页 document.body;可见文本用 getBoundingClientRect(任意页无固定滚动容器)
//   [挂载] mountPanel/mountTabs 指向扩展 Shadow DOM 里的 #ep-side / #ep-side-tabs(经 window.__bwShadow)
//   [身份] file = 'web:'+URL(与阅读器网页模式同构,后端按此识别网页语境)
// 端点保持相对路径——facade.js 的 fetch 门面统一重写到 Pi + Bearer,这里零关心跨源。
(function () {
  "use strict";
  // 受信任 PWA 由 pwa-adapter 接管；不能先装 WebAdapter，否则助手会读取 iframe 外壳而非 PDF 上下文。
  if (window.__bwPwaProviderOnly || window.__bwPwaBridge) return;
  var RC = window.RC;
  if (!RC || !RC.use) return;
  var shadow = window.__bwShadow;
  var host = document.getElementById("bw-reader-host");
  var bwFetch = window.__bwReaderFetch || window.fetch.bind(window);

  function _frel() {
    try { var u = new URL(location.href); u.hash = ""; return "web:" + u.href; } catch (e) { return "web:" + location.href; }
  }
  var FREL = _frel();

  function toast(m) { try { RC.toast(m); } catch (e) {} }

  // ── 网页语言检测(逐字照搬 html-reader.js _docLangs,内容根换 body)──
  var _langsCache = null;
  function _docLangs() {
    if (_langsCache) return _langsCache;
    try {
      var t = (document.body.innerText || "").slice(0, 4000);
      var kana = (t.match(/[぀-ヿ]/g) || []).length, han = (t.match(/[㐀-鿿]/g) || []).length,
          lat = (t.match(/[A-Za-z]/g) || []).length, out = [];
      if (kana > 20) out.push("ja");
      if (lat > Math.max(han, kana) * 2 && lat > 200) out.push("en");
      _langsCache = out;
    } catch (e) { _langsCache = []; }
    return _langsCache;
  }
  // ── 视口内正文(html-reader _visibleText 的活 DOM 版:viewport rect 判可见)──
  function _visibleText() {
    try {
      var out = [], nodes = document.body.querySelectorAll("p,li,h1,h2,h3,h4,blockquote,td");
      var vh = window.innerHeight || 800;
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        if (host && host.contains(el)) continue;
        var r = el.getBoundingClientRect();
        if (r.bottom < 0 || r.top > vh || !r.height) continue;
        out.push(el.innerText || "");
        if (out.join("").length > 1600) break;
      }
      return out.join("\n").slice(0, 1600);
    } catch (e) { return ""; }
  }
  // ── 选区捕获(照搬 captureFromSelection;容器=整页,排除扩展自身 UI)──
  function captureFromSelection() {
    try {
      var sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return null;
      var rng = sel.getRangeAt(0);
      if (shadow && shadow.contains(rng.commonAncestorContainer)) return null;   // 扩展自身 UI 的选区不算
      var txt = (sel.toString() || "").trim();
      if (!txt) return null;
      var rect = rng.getBoundingClientRect();
      var blk = rng.startContainer.nodeType === 3 ? rng.startContainer.parentElement : rng.startContainer;
      blk = blk && blk.closest ? blk.closest("p,li,td,blockquote,div,section,h1,h2,h3,h4") : null;
      var ctx = (blk ? (blk.textContent || "") : "").trim().slice(0, 1200);
      return RC.contract.selection({
        text: txt, context: ctx, ctx: ctx,
        // 里程碑 3 已落地:网页字符层由 web-textlayer.js 提供,锚就是 page-chars
        // (page 恒为 1)。**不新增 bind kind** —— 那份白名单有 17 份副本。
        // 取不到时仍然给 null,绝不编一个 —— 假锚会把卡钉到无关的位置上。
        anchor: (function () {
          try {
            return window.__bwWebTextLayer
              ? window.__bwWebTextLayer.rangeToBind(rng) : null;
          } catch (e) { return null; }
        })(),
        rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom }
      });
    } catch (e) { return null; }
  }
  function clearNativeSel() { try { var s = window.getSelection(); if (s) s.removeAllRanges(); } catch (e) {} }

  // ── 通用网络端点是已确认等价的共享契约；fetch 门面仍负责扩展跨源。──
  var EP = RC.contract.endpoints();
  function _visualSurface() {
    try {
      var snap = window.__bwWebInk && window.__bwWebInk.exportSnapshot
        ? window.__bwWebInk.exportSnapshot() : null;
      if (!snap) return null;
      return {
        element: document.body,
        width: snap.width,
        height: snap.height,
        viewport: {
          x: window.scrollX, y: window.scrollY,
          width: window.innerWidth, height: window.innerHeight
        },
        strokes: snap.strokes || []
      };
    } catch (e) { return null; }
  }

  var WebAdapter = {
    kind: "web",
    config: RC.contract.adapterConfig('web'),
    getEndpoints: function () { return EP; },
    fileInfo: function () { return { file: FREL, langs: _docLangs() }; },
    captureSelection: function () { return captureFromSelection(); },
    clearSelection: function () { clearNativeSel(); },
    // 网页锚现在解析得出真实 Range —— 滚过去并短暂选中,让用户看见落点。
    jumpToAnchor: function (anchor) {
      try {
        var hit = window.__bwWebTextLayer && window.__bwWebTextLayer.locate(anchor);
        if (!hit || !hit.range) return false;
        var r = hit.range.getBoundingClientRect();
        window.scrollTo({
          top: window.scrollY + r.top - window.innerHeight * 0.3,
          behavior: 'smooth'
        });
        var sel = window.getSelection();
        if (sel) { sel.removeAllRanges(); sel.addRange(hit.range); }
        return true;
      } catch (e) { return false; }
    },
    currentChapterText: function () { try { return (document.body.innerText || "").slice(0, 8000); } catch (e) { return ""; } },
    getContext: function (opts) {   // 照搬 HtmlAdapter.getContext,加 url(网页身份)
      opts = opts || {}; var sel = opts.selection || {};
      if (!sel.sel) {
        try {
          var c0 = captureFromSelection();
          if (window.__focusSel && window.__focusSel.text) sel = { sel: window.__focusSel.text, sent: "" };
          else if (c0 && c0.text) sel = { sel: c0.text, sent: c0.context || "" };
        } catch (e) {}
      }
      var surface = _visualSurface(), ink = surface ? surface.strokes : [];
      return {
        page_type: "web",
        file: FREL, file_rel: FREL, book: document.title, book_name: document.title,
        url: location.href,
        langs: _docLangs(),
        visible_text: _visibleText(),
        page: 1, pages: [1], total: 1, total_pages: 1,
        current_section_idx: 0, total_sections: 1,
        ink: ink.slice(0, 60), has_ink: ink.length > 0,
        want_viewshot: ink.length > 0,
        selection: sel.sel || "", selection_sentence: sel.sent || "",
        selection_anchor: sel.anchor || undefined
      };
    },
    getVisualSurface: function () { return _visualSurface(); },
    captureShot: function () {
      return window.RC && RC.captureInkRegion
        ? RC.captureInkRegion() : Promise.resolve(null);
    },
    currentLocation: function () { return { unit: "page", index: 0, total: 1 }; },
    collectFigures: function () { return []; },
    _host: {
      asst: {   // 照搬 html-reader.js 的最精简可跑袋;挂载点改 shadow,html 专属项 no-op
        md: function (t) { return (RC.md) ? RC.md(t) : String(t || ""); },
        toast: function (m) { toast(m); },
        fmtTime: function (ms) { try { var s2 = Math.round((Date.now() - (ms || 0)) / 1000); return s2 < 60 ? (s2 + "秒前") : (s2 < 3600 ? (Math.round(s2 / 60) + "分钟前") : (Math.round(s2 / 3600) + "小时前")); } catch (e) { return ""; } },
        fileRel: function () { return FREL; },
        pdfNumPages: function () { return 1; },
        locCount: function () { return 1; },
        dispPage: function (p) { return p; }, pdfFromDisp: function (d) { return d; },
        goTo: function () {},
        goToInBook: function (file, page) {
          try {
            window.open(
              'https://bwicarus-2.taile44d0c.ts.net/pdf/view?file=' +
                encodeURIComponent(String(file || '')) +
                '&page=' + Math.max(1, parseInt(page, 10) || 1),
              '_blank'
            );
          } catch (e) {}
        },
        changePage: function () {}, fitWidth: function () {}, zoomBy: function () {}, toggleTranslate: function () {},
        openDrawer: function () { try { RC.sidedrawer.open("asst"); } catch (e) {} },
        switchTab: function (n) { try { RC.sidedrawer.open(n); } catch (e) {} },
        asstOpen: function () { try { return !!(shadow && shadow.querySelector('.ep-side-pane[data-pane="asst"].active')); } catch (e) { return false; } },
        voiceContext: function () { return WebAdapter.getContext(); },
        setFocusSel: function (t) { try { window.__focusSel = t ? { text: t } : null; } catch (e) {} },
        focusSel: function () { return window.__focusSel || null; },
        clearFigFocus: function () {}, figThumb: function () {},
        locLabel: function () { return ""; }, locNoun: function () { return "页"; },
        noteAttached: function () { return []; }, clearNoteAttached: function () {}, renderNoteChips: function () {},
        notesReload: function () {}, noteInject: function () { return false; },
        reloadHighlights: function () {}, loadAllHighlights: function () {},
        renderHighlightsOnPage: function () {}, showHlPicker: function () {},
        assistEdit: function () {},
        renderPhraseHl: function () {}, removePhraseHighlight: function () {},
        activePhraseHl: function () { return null; }, setActivePhraseHl: function () {},
        charsRangeToText: function () { return ""; }, charRangeToPtRects: function () { return []; },
        flashSelOnPage: function () {},
        noteNearText: function () { return ""; },
        jumpToCtx: function () { try { RC.sidedrawer.close(); } catch (e) {} },
        prewarm: function (off) { try { bwFetch("/api/assistant/prewarm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(off ? { off: 1 } : {}) }); } catch (e) {} },
        getPaidNoted: function () { return !!window.__paidNoted; }, setPaidNoted: function (v) { window.__paidNoted = v; },
        showAction: function () { return null; }, queueAction: function () {}, taskAction: function () {},
        // [挂载] rc-assistant.mountPdfSidebar 的容器钩子 → 扩展 Shadow DOM 里的抽屉
        mountPanel: function () { return shadow ? shadow.getElementById("ep-side") : null; },
        mountTabs: function () { return shadow ? shadow.getElementById("ep-side-tabs") : null; }
      }
    }
  };

  RC.use(WebAdapter);
  window.__webAdapter = WebAdapter;
})();
