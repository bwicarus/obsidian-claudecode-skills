// 普通网页的振假名/英文音标与逐段译页。
// PDF 页面仍把这两项交给 PWA：它掌握精确字符框；这里仅处理可重排的真实 DOM。
(function () {
  'use strict';
  if (window.__bwPwaProviderOnly || window.__bwPwaBridge || window.__bwWebDecorations) return;

  var host = window.__bwReaderHost;
  var bwFetch = window.__bwReaderFetch || window.fetch.bind(window);
  var rubyOn = false, translateOn = false, applyingRuby = false, applyingTranslate = false;
  var scheduleTimer = 0;
  var BLOCK_SEL = 'p,li,blockquote,h1,h2,h3,h4,td,figcaption';

  function toast(msg) { try { window.RC && RC.toast(msg); } catch (_) {} }
  function fileRel() {
    try { var u = new URL(location.href); u.hash = ''; return 'web:' + u.href; }
    catch (_) { return 'web:' + location.href; }
  }
  function visible(el, margin) {
    if (!el || !el.isConnected || (host && host.contains(el))) return false;
    var r = el.getBoundingClientRect(), vh = innerHeight || 800;
    if (!r.width || !r.height || r.bottom < -margin || r.top > vh + margin) return false;
    var s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden';
  }
  function ignored(node) {
    var p = node && node.parentElement;
    return !p || !!p.closest('script,style,noscript,textarea,input,select,option,button,code,pre,svg,math,ruby,[contenteditable="true"],[data-bw-web-translation]');
  }
  function blocks() {
    return Array.prototype.slice.call(document.querySelectorAll(BLOCK_SEL)).filter(function (el) {
      return visible(el, 700) && !el.querySelector(BLOCK_SEL);
    }).slice(0, 100);
  }
  function textNodes(el) {
    var out = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT), n;
    while ((n = w.nextNode())) {
      if (ignored(n)) continue;
      var s = n.nodeValue || '';
      if (s.trim() && /[㐀-鿿一-鿿A-Za-z]/.test(s)) out.push(n);
      if (out.length >= 160) break;
    }
    return out;
  }
  function wrapTokens(node, original, toks) {
    if (!node.isConnected || node.nodeValue !== original) return;
    var sorted = (toks || []).filter(function (t) {
      return t && t.reading && Number(t.end) > Number(t.start);
    }).sort(function (a, b) { return Number(b.start) - Number(a.start); });
    sorted.forEach(function (t) {
      try {
        var s = Math.max(0, Number(t.start) || 0), e = Math.min(node.nodeValue.length, Number(t.end) || 0);
        if (e <= s) return;
        var selected = node.splitText(s); selected.splitText(e - s);
        var ruby = document.createElement('ruby'); ruby.dataset.bwWebRuby = '1';
        var rt = document.createElement('rt'); rt.textContent = String(t.reading || '');
        selected.parentNode.insertBefore(ruby, selected); ruby.appendChild(selected); ruby.appendChild(rt);
      } catch (_) {}
    });
  }
  function clearRuby() {
    document.querySelectorAll('ruby[data-bw-web-ruby="1"]').forEach(function (ruby) {
      var p = ruby.parentNode; if (!p) return;
      Array.prototype.slice.call(ruby.childNodes).forEach(function (c) {
        if (!(c.nodeType === 1 && c.tagName === 'RT')) p.insertBefore(c, ruby);
      });
      p.removeChild(ruby); p.normalize();
    });
    document.querySelectorAll('[data-bw-web-ruby-done]').forEach(function (el) { delete el.dataset.bwWebRubyDone; });
  }
  async function applyRuby() {
    if (!rubyOn || applyingRuby) return;
    var bs = blocks().filter(function (b) { return b.dataset.bwWebRubyDone !== '1'; });
    var entries = [];
    bs.forEach(function (b) {
      if (entries.length >= 120) return;
      var nodes = textNodes(b).slice(0, 120 - entries.length);
      if (!nodes.length) return;
      b.dataset.bwWebRubyDone = '1';
      nodes.forEach(function (n) { entries.push({ node: n, text: n.nodeValue || '', block: b }); });
    });
    if (!entries.length) return;
    applyingRuby = true;
    try {
      var r = await bwFetch('/pdf/api/epub-furigana', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: fileRel(), texts: entries.map(function (x) { return x.text; }) })
      });
      var d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
      if (!rubyOn) return;
      var items = d.items || [];
      entries.forEach(function (x, i) { wrapTokens(x.node, x.text, (items[i] && items[i].tokens) || []); });
    } catch (e) {
      entries.forEach(function (x) { delete x.block.dataset.bwWebRubyDone; });
      toast('振假名加载失败：' + ((e && e.message) || e));
    } finally { applyingRuby = false; }
  }
  function clearTranslate() {
    if (window.__rcTr && window.__rcTr.state && window.__rcTr.state.on) {
      window.__rcTr.stop();
      return;
    }
    document.querySelectorAll('[data-bw-web-translation="1"]').forEach(function (el) { el.remove(); });
    document.querySelectorAll('[data-bw-web-tr-done]').forEach(function (el) { delete el.dataset.bwWebTrDone; });
  }
  async function applyTranslate() {
    if (!translateOn || applyingTranslate) return;
    var bs = blocks().filter(function (b) { return b.dataset.bwWebTrDone !== '1' && (b.innerText || '').trim(); }).slice(0, 40);
    if (!bs.length) return;
    bs.forEach(function (b) { b.dataset.bwWebTrDone = '1'; });
    applyingTranslate = true;
    try {
      var texts = bs.map(function (b) { return (b.innerText || '').trim().slice(0, 4000); });
      var r = await bwFetch('/pdf/api/epub-translate-section', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: fileRel(), texts: texts })
      });
      var d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
      if (!translateOn) return;
      (d.translations || []).forEach(function (zh, i) {
        var b = bs[i]; if (!b || !b.isConnected || !String(zh || '').trim()) return;
        var div = document.createElement('div');
        div.dataset.bwWebTranslation = '1'; div.textContent = String(zh).trim();
        div.style.cssText = 'margin:.28em 0 .75em;padding:.42em .65em;border-left:3px solid #60a5fa;background:rgba(30,58,95,.09);color:inherit;font:inherit;line-height:1.65;opacity:.88';
        b.insertAdjacentElement('afterend', div);
      });
    } catch (e) {
      bs.forEach(function (b) { delete b.dataset.bwWebTrDone; });
      toast('译页加载失败：' + ((e && e.message) || e));
    } finally { applyingTranslate = false; }
  }
  function refresh() {
    clearTimeout(scheduleTimer);
    scheduleTimer = setTimeout(function () {
      if (rubyOn) applyRuby();
      if (translateOn && !window.__rcTr) applyTranslate();
    }, 180);
  }
  function toggleRuby() {
    rubyOn = !rubyOn;
    if (rubyOn && translateOn) { translateOn = false; clearTranslate(); }
    if (rubyOn) { toast('振假名 / 英文音标 已开'); applyRuby(); } else clearRuby();
    return { ruby: rubyOn, translate: translateOn };
  }
  function toggleTranslate() {
    translateOn = !translateOn;
    if (translateOn && rubyOn) { rubyOn = false; clearRuby(); }
    if (window.__rcTr) {
      if (translateOn) { toast('沉浸式翻译已开：滚到哪译到哪'); window.__rcTr.start(); }
      else window.__rcTr.stop();
    } else if (translateOn) { toast('逐段译页已开，正在翻译当前视野…'); applyTranslate(); }
    else clearTranslate();
    return { ruby: rubyOn, translate: translateOn };
  }

  addEventListener('scroll', refresh, { passive: true });
  addEventListener('resize', refresh, { passive: true });
  new MutationObserver(refresh).observe(document.documentElement, { childList: true, subtree: true });
  window.__bwWebDecorations = { toggleRuby: toggleRuby, toggleTranslate: toggleTranslate, refresh: refresh, state: function () { return { ruby: rubyOn, translate: translateOn }; } };
  if (window.RC && RC.actions) {
    RC.actions.bind('reading.ruby.toggle', function () { return toggleRuby(); }, { owner: 'web-extension', runtime: 'extension', storage: 'runtime' });
    RC.actions.bind('translation.page.toggle', function () { return toggleTranslate(); }, { owner: 'web-extension', runtime: 'extension', storage: 'runtime' });
  }
})();
