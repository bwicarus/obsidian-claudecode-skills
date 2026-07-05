/* epub2-highlight.js — epub.js 版 EPUB 阅读器:高亮(P3)。自包含 IIFE,等 window.__epub ready。
 *
 * 为什么独立成模块:高亮的「锚」是这套阅读器最该交给成熟库的部分。手搓 HTML 版(epub-html.js)用
 *   section+offset 偏移锚(自己维护 TreeWalker / _countable / 装饰节点排除),epub.js 版直接用 epub.js 原生
 *   **CFI**(contents.cfiFromRange(range))——库自己保证跨章/重渲后还能定位,**离屏章节重渲会自动重画**,
 *   不用我们管 DOM。渲染走 rendition.annotations.add('highlight', cfiRange, ...)(SVG rect 画进 iframe),
 *   删走 rendition.annotations.remove(cfiRange,'highlight'),点高亮走 annotations 的点击 cb(+ markClicked 兜底)。
 *
 * 复用 / 对照:
 *   · 编辑浮层 + 列表 = 共享层 RC.highlight(rc-highlight.js):openEditor(4 色板 + 备注 + 删除 + 只读预览)、
 *     renderList(色点/文字/备注/跳转/删除)。调法逐字照搬 epub-html.js 的 openHlEditor / loadHlPane,锚换成 CFI。
 *   · CFI 高亮的 annotations 渲染/删/点 = 照搬 epub-ai.js 旧实现(renderHl/unrenderHl/markClicked),
 *     只把编辑浮层从它手搓的 #ep-hlpop 换成共享层 RC.highlight。
 *   · 对照 PDF reader.src/17-highlight.js(saveHighlight / openPickColor 记最近色 / popover 预览块)。
 *
 * 选区从哪来:epub2.js 维护 cur = {text, cfi, ctx, rect}(多路兜底捕获:mouseup/touchend/selectionchange/轮询,
 *   iOS 上比 R.on('selected') 可靠)。本模块优先读 epub2.js 暴露的 window.__epub.curSel()(同一份快照,
 *   避免 iOS 上点工具栏按钮时原生选区已被收起的竞态);拿不到再从 live selection 兜底重算 CFI。
 */
(function () {
  'use strict';
  function ready(fn) { if (window.__epub && window.__epub.rendition) fn(); else setTimeout(function () { ready(fn); }, 120); }
  ready(init);

  function init() {
    var R = window.__epub.rendition, CFG = window.__epub.cfg || {};
    var FREL = CFG.fileRel || '';
    var $ = function (id) { return document.getElementById(id); };
    function toast(m) { if (window.RC && RC.toast) RC.toast(m); }
    // 调法/签名照搬 epub-html.js / epub-ai.js 的 reqJson(method,url,body,ok,err)
    function reqJson(method, url, body, ok, err) {
      var o = { method: method, headers: {} };
      if (body) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
      fetch(url, o).then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.ok) ok(d); else err((d && d.error) || '失败');
      }).catch(function (e) { err((e && e.message) || '网络错误'); });
    }
    // 色板:跟 epub-html.js 一致,读共享层 RC.settings.hlColors(设置面板可改),兜底 PDF 默认四色
    function hlColors() { return (window.RC && RC.settings && RC.settings.hlColors) ? RC.settings.hlColors() : ['#fff59d', '#a7f3d0', '#a3d4ff', '#fda4af']; }

    var _hls = {};   // id -> highlight {id, cfi, text, color, note, sentence, time}

    // ════════════════════════════════════════════════════════════════════════
    // 渲染 —— epub.js annotations(CFI 锚 → SVG rect 画进 iframe;离屏章节重渲库会自动重画)。
    //   照搬 epub-ai.js renderHl/unrenderHl:半透明 fill(字透出来不被实色盖死),点击 cb → 编辑浮层。
    // ════════════════════════════════════════════════════════════════════════
    function renderHl(h) {
      _hls[h.id] = h;
      try {
        var hasColor = !!(h.color && String(h.color).trim());
        // has-note 视觉指示:判定 note||body||sentence(逐字对照 PDF renderHighlightsOnPage 的 hasNote)
        var hasNote = !!(((h.note || '').trim()) || ((h.body || '').trim()) || ((h.sentence || '').trim()));
        // no-color 虚框:透明填充 + 虚线描边。epub.js(marks-pane)把 styles 作为属性挂到高亮 <g>,
        //   子 <rect> 继承 fill/stroke/stroke-dasharray → 每行得到一个虚线框。
        //   逐字对照 PDF .hl-saved.no-color:border:1px dashed rgba(150,170,200,.55)。
        var styles = hasColor
          ? { 'fill': h.color, 'fill-opacity': '0.40' }
          // pointer-events:all → 透明填充的矩形整块也可点(否则只有虚线描边可点,很难命中开编辑浮层)
          : { 'fill': 'transparent', 'fill-opacity': '0', 'stroke': 'rgba(150,170,200,.55)', 'stroke-width': '1', 'stroke-dasharray': '4 2', 'pointer-events': 'all' };
        // has-note + 有色:加整圈描边近似 PDF .hl-saved.has-note 的底部内描边(SVG <g> 子 rect 继承 stroke,无法只画底边)
        if (hasColor && hasNote) { styles['stroke'] = 'rgba(0,0,0,.35)'; styles['stroke-width'] = '1'; }
        // data → data-* 属性(data-id 命中;data-title 存提示文字 = PDF div.title。
        //   注:epub.js SVG 注解无原生 hover tooltip,只能把文字存进 data-title 属性,无法触发浏览器气泡)
        var data = { id: h.id };
        var tip = (h.note || h.body || h.sentence || h.text || '');
        if (tip) data.title = String(tip).slice(0, 200);
        R.annotations.add('highlight', h.cfi, data, function () { openHlEditor(h); }, '', styles);
      } catch (e) { /* CFI 失效/章节结构变 → 跳过该条,不阻塞其它 */ }
    }
    function unrenderHl(h) { try { R.annotations.remove(h.cfi, 'highlight'); } catch (e) {} }

    // ── 选区快照:优先 epub2.js 的 cur(window.__epub.curSel),兜底 live selection 重算 CFI ──
    function curSel() {
      try { if (window.__epub && typeof window.__epub.curSel === 'function') { var c = window.__epub.curSel(); if (c && (c.cfi || c.text)) return c; } } catch (e) {}
      return liveSel();
    }
    function liveSel() {
      try {
        var cs = R.getContents() || [];
        for (var i = 0; i < cs.length; i++) {
          var c = cs[i], s = c.window.getSelection();
          var t = s && !s.isCollapsed ? (s.toString() || '').trim() : '';
          if (!t) continue;
          var rng = s.getRangeAt(0);
          var cfi = ''; try { if (c.cfiFromRange) cfi = c.cfiFromRange(rng); } catch (e2) {}
          var node = s.anchorNode, blk = node ? (node.nodeType === 3 ? node.parentElement : node) : null;
          blk = blk && blk.closest ? blk.closest('p,li,td,blockquote,div,section,h1,h2,h3,h4') : null;
          return { text: t, cfi: cfi, ctx: (blk ? (blk.textContent || '') : '').trim().slice(0, 1200) };
        }
      } catch (e) {}
      return { text: '', cfi: '', ctx: '' };
    }
    function clearNativeSel() {
      try { (R.getContents() || []).forEach(function (c) { var s = c.window.getSelection(); if (s) s.removeAllRanges(); }); } catch (e) {}
      var bar = $('ep-sel'); if (bar) bar.classList.remove('open');
    }

    // ── 建高亮:POST(cfi/text/color/sentence)→ annotations.add → 记最近色(照搬 PDF onPickColor / epub-html saveHl)──
    function makeHl(sel, color, okToast) {
      if (!sel || !sel.cfi) { toast('无法定位选区'); return; }
      color = color || localStorage.getItem('eph-hl-color') || hlColors()[0];
      try { localStorage.setItem('eph-hl-color', color); } catch (e) {}
      reqJson('POST', '/pdf/api/epub-highlights',
        { file: FREL, cfi: sel.cfi, text: sel.text || '', color: color, sentence: sel.ctx || '', kind: 'note' },
        function (d) { renderHl(d.highlight); toast(okToast || '已高亮'); clearNativeSel(); },
        function (er) { toast('高亮失败:' + er); });
    }

    // ── 4 色板:互斥激活 + 「先点色后选字」(逐字照搬 PDF 17-highlight.js renderHlPicker / onPickColor;
    //   DOM 结构=手搓版 epub-html.js #ep-hl-pick + .swatch[data-c],激活态持久化 eph-hl-active)──
    //   _activeHlColor:当前激活色(互斥单选);空 = 没激活 → 点工具栏色板先存色再等选字
    var _activeHlColor = localStorage.getItem('eph-hl-active') || '';
    function renderHlPicker() {
      var box = $('ep-hl-pick'); if (!box) return;
      var html = '<span class="lbl">🖌</span>';   // 🖌 lbl(逐字照搬手搓版 renderHlPicker)
      hlColors().forEach(function (c) { html += '<i class="swatch' + (c === _activeHlColor ? ' active' : '') + '" data-c="' + c + '" style="background:' + c + '"></i>'; });
      box.innerHTML = html;
    }
    function onPickColor(col) {
      // 互斥激活:再点同色 = 取消激活;点其他色 = 切到该色(同时立刻标记)。逐字照搬 PDF onPickColor
      if (col === _activeHlColor) {
        _activeHlColor = '';
        try { localStorage.setItem('eph-hl-active', ''); } catch (e) {}
        renderHlPicker();
        return;
      }
      _activeHlColor = col;
      try { localStorage.setItem('eph-hl-active', col); } catch (e) {}
      renderHlPicker();
      var sel = curSel();
      if (!sel || !sel.cfi) { toast('已选定颜色（先选中文字）'); return; }
      makeHl(sel, col, '已标记 🖌');
    }

    // ── 选区工具栏接「🖍 高亮」act + 色板(点色走 onPickColor 互斥激活流) ──
    //   自包含:本模块在 #ep-sel 上挂自己的 click 监听,与 epub2.js 的 selBar handler **并存**
    //   (epub2.js 对 act='highlight' 无分支 → 它只会 hideSel 后 no-op,不冲突)。
    var selBar = $('ep-sel');
    if (selBar) selBar.addEventListener('click', function (e) {
      var sw = e.target.closest('#ep-hl-pick .swatch');   // 模板色板:点某色 → PDF onPickColor 互斥激活流
      if (sw) { onPickColor(sw.dataset.c); return; }
      var b = e.target.closest('button'); if (!b) return;
      if (b.dataset.act === 'highlight') makeHl(curSel(), localStorage.getItem('eph-hl-color') || hlColors()[0]);
    });

    // ════════════════════════════════════════════════════════════════════════
    // CRUD —— 删/改(改色:annotations 不支持原地改样式 → unrender + render;照搬 epub-ai.js patchHl)。
    // ════════════════════════════════════════════════════════════════════════
    function delHl(h) {
      if (!confirm('删除这条高亮？')) return;   // 逐字照搬 PDF _hlDelete 的删除确认
      reqJson('DELETE', '/pdf/api/epub-highlights?file=' + encodeURIComponent(FREL) + '&id=' + encodeURIComponent(h.id), null,
        function () { unrenderHl(h); delete _hls[h.id]; toast('已删除'); }, function () {});
    }
    function patchHl(h, f, okToast) {
      reqJson('PATCH', '/pdf/api/epub-highlights', Object.assign({ file: FREL, id: h.id }, f), function (d) {
        // 改色(含改成无色虚框 color:'')→ 用返回值比较;变了就 unrender+render 重画 + toast(逐字对照 PDF _hlUpdate「已保存」)
        if ('color' in f && d.highlight.color !== h.color) { unrenderHl(h); h.color = d.highlight.color; renderHl(h); toast(okToast || '已保存'); }
        if ('note' in f) h.note = d.highlight.note;   // note 改后的「已保存」由 rc-highlight 编辑层保存按钮负责,避免双 toast
      }, function () {});
    }

    // ── 编辑浮层:复用共享层 RC.highlight.openEditor(改色/备注/删除 + 只读预览原文/所在句)。
    //   不传 anchorEl/placeBelow → RC.highlight 走「屏幕水平居中 + 上 22%」定位(=epub-ai 旧 #ep-hlpop 行为;
    //   高亮 mark 在 epub.js 的 iframe 内,父文档没有可锚 DOM,故不做 placeBelow / 再点同锚关)。──
    function openHlEditor(h) {
      if (!(window.RC && RC.highlight)) { toast('编辑层未就绪'); return; }
      RC.highlight.openEditor({
        colors: hlColors(), current: h.color, note: h.note || '',
        preview: h.text || '', sentence: h.sentence || '',
        // c==='' 表示「再点当前色取消颜色」:照搬 PDF —— hasNote 判定 note||body||sentence;
        //   无备注则直接删该高亮,有备注则保留为无色虚框 + toast「已取消颜色（备注保留）」
        onColor: function (c) {
          if (c === '') {
            var hasNote = ((h.note || '').trim()) || ((h.body || '').trim()) || ((h.sentence || '').trim());
            if (!hasNote) { delHl(h); if (window.RC && RC.highlight) RC.highlight.closeEditor(); }
            else patchHl(h, { color: '' }, '已取消颜色（备注保留）');
            return;
          }
          patchHl(h, { color: c });
        },
        onNote: function (t) { patchHl(h, { note: t }); },
        onDelete: function () { delHl(h); }
      });
    }
    // 点高亮兜底:epub.js 内部点 mark → markClicked(cfiRange)。按 cfi 找回 h 开编辑浮层
    //   (annotations.add 的 cb 是主路径;某些 epub.js 版本 cb 不稳时由此补)。
    try { R.on('markClicked', function (cfi) { var h = Object.keys(_hls).map(function (k) { return _hls[k]; }).filter(function (x) { return x.cfi === cfi; })[0]; if (h) openHlEditor(h); }); } catch (e) {}

    // ════════════════════════════════════════════════════════════════════════
    // 开书加载已存高亮 —— GET → 每条 annotations.add(epub.js 在对应章节渲染时自动画上,离屏不画也没关系)。
    // ════════════════════════════════════════════════════════════════════════
    function loadHls() {
      // no-store:撤销/改高亮后必须拿最新,否则命中浏览器缓存看不到变化(逐字对照 PDF loadAllHighlights)
      fetch('/pdf/api/epub-highlights?file=' + encodeURIComponent(FREL), { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
        (d.highlights || []).forEach(function (h) { if (h.cfi && !_hls[h.id]) renderHl(h); });   // 只认 cfi 锚(偏移锚是手搓版的,这里不渲)
      }).catch(function () {});
    }

    // ── 抽屉「高亮」pane:列表(点跳 R.display(cfi))。渲进 #ep-side-hl,用 RC.highlight.renderList ──
    //   由 epub2.js 的 RC.sidedrawer onTab('hl') 调 window.epubHl.loadPane(),也自挂 hl tab 点击兜底。
    function loadPane() {
      var box = $('ep-side-hl'); if (!box) return;
      box.innerHTML = '<div class="ep-empty"><span class="ep-spin"></span> 加载…</div>';
      fetch('/pdf/api/epub-highlights?file=' + encodeURIComponent(FREL), { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {   // no-store:删/改后拿最新
        var hs = (d && d.highlights || []).filter(function (h) { return h.cfi; });
        hs.forEach(function (h) { _hls[h.id] = h; });
        if (!(window.RC && RC.highlight)) { box.innerHTML = '<div class="ep-empty">编辑层未就绪</div>'; return; }
        RC.highlight.renderList(box, hs, {
          reverse: true,
          emptyHtml: '还没有高亮。<br>选中文字 → 底部「🖍 高亮」',
          onJump: function (h) { if (window.RC && RC.sidedrawer) RC.sidedrawer.close(); try { R.display(h.cfi); } catch (e) {} },
          onDelete: function (h) { delHl(h); }
        });
      }).catch(function () { box.innerHTML = '<div class="ep-empty">加载失败</div>'; });
    }

    // 对外:供 epub2.js onTab('hl') 调;自挂 hl tab 点击作兜底(抽屉记忆 hl 时也会经 onTab 触发)
    // 助手 AI 高亮持久化用(POST 进 sidecar,不 renderHl 避免与助手内存注解重影;刷新后由 loadHls 渲染)
    function persistHl(cfi, text, color, sentence) {
      if (!cfi) return;
      try { reqJson('POST', '/pdf/api/epub-highlights', { file: FREL, cfi: cfi, text: text || '', color: color || (hlColors()[0]), sentence: sentence || '', kind: 'note' }, function () {}, function () {}); } catch (e) {}
    }
    // AI 结果框/字典框「🖌 标记」用:把选区/词 + AI 正文(独立字段 body)+ 类型(kind)存成高亮(CFI 锚)。带 renderHl 立即可见。
    //   逐字对照 PDF markFromResult:body=AI 正文(非用户备注 note),颜色优先级 激活色 → 最近色 → 首色。
    function markFromSel(sel, body, kind, color) {
      if (!sel || !sel.cfi) { toast('无法定位选区,请重新选中后再标记'); return; }
      color = color || _activeHlColor || localStorage.getItem('eph-hl-color') || hlColors()[0];
      reqJson('POST', '/pdf/api/epub-highlights',
        { file: FREL, cfi: sel.cfi, text: sel.text || '', color: color, sentence: sel.ctx || sel.sentence || '', body: body || '', kind: kind || 'note' },
        function (d) { renderHl(d.highlight); toast('已加标记 🖌'); },
        function (er) { toast('高亮失败:' + er); });
    }
    window.epubHl = { loadPane: loadPane, reload: loadHls, persist: persistHl, markFromSel: markFromSel, renderPicker: renderHlPicker };
    document.addEventListener('click', function (e) {
      var t = e.target.closest && e.target.closest('#ep-side-tabs .ep-side-tab[data-pane="hl"]');
      if (t) loadPane();
    }, false);

    renderHlPicker();   // 应用激活态外框 + 设置面板自定义色(逐字对照 PDF 初始化 renderHlPicker)
    loadHls();
  }
})();
