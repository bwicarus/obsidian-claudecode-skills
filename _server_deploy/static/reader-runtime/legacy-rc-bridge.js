/* legacy-rc-bridge.js — 把现有书籍/Web RC.adapter 暴露成 DocumentHost。
 *
 * reader-host/1 与 RC.use() 原样保留；新消费者可读 RC.documentHost.current()。
 * 每次读取都按当前 adapter 的真实方法重建能力，晚绑定 PDF host 也不会被旧快照掩盖。
 */
(function () {
  'use strict';
  var root = typeof globalThis !== 'undefined' ? globalThis : window;
  var RC = root.RC;
  var api = root.BWReaderRuntime && root.BWReaderRuntime.documentHost;
  if (!RC || !RC.use || !api || RC.documentHost) return;

  function kindOf(adapter) {
    try {
      var route = document.querySelector('meta[name="bw-reader-route"]');
      var app = document.querySelector('meta[name="bw-reader-app"]');
      route = route ? String(route.getAttribute('content') || '') : '';
      app = app ? String(app.getAttribute('content') || '') : '';
      if (route === 'favorite') return 'favorite';
      if (app === 'html') return 'html';
    } catch (_) {}
    var raw = String(adapter && adapter.kind || '');
    if (raw.indexOf('pdf') >= 0) return 'pdf';
    if (raw.indexOf('epub') >= 0) return 'epub';
    if (raw.indexOf('html') >= 0) return 'html';
    return 'web';
  }

  function documentIdOf(adapter, kind) {
    try {
      var info = adapter && adapter.fileInfo && adapter.fileInfo();
      if (info && info.file) return String(info.file);
    } catch (_) {}
    try {
      if (root.__PDF_CFG && root.__PDF_CFG.file_rel) return String(root.__PDF_CFG.file_rel);
      if (root.HTML_CFG && root.HTML_CFG.fileRel) return String(root.HTML_CFG.fileRel);
      if (root.EPUB_CFG && root.EPUB_CFG.fileRel) return String(root.EPUB_CFG.fileRel);
      if (root.__WEB_CFG && root.__WEB_CFG.url) return 'web:' + String(root.__WEB_CFG.url);
    } catch (_) {}
    try { return kind + ':' + String(location.href).split('#')[0]; }
    catch (_) { return kind + ':unknown'; }
  }

  function current() {
    var adapter = RC.adapter();
    if (!adapter || !Object.keys(adapter).length) return null;
    var kind = kindOf(adapter);
    return api.createLegacyDocumentHost(adapter, {
      kind: kind,
      documentId: documentIdOf(adapter, kind)
    });
  }

  RC.documentHost = {
    contract: api.CONTRACT,
    current: current,
    audit: function () {
      var host = current();
      return host ? host.audit() : {
        contract: api.CONTRACT,
        valid: false,
        kind: 'unknown',
        documentId: '',
        errors: ['当前尚无 RC adapter']
      };
    },
    capability: function (name) {
      var host = current();
      return host ? host.capability(name) : {
        status: 'pending',
        owner: 'pwa',
        reason: '当前尚无阅读宿主'
      };
    }
  };

  var originalUse = RC.use;
  RC.use = function (adapter) {
    var result = originalUse.call(RC, adapter);
    try {
      setTimeout(function () {
        var host = current();
        if (!host || !document || !document.dispatchEvent) return;
        document.dispatchEvent(new CustomEvent('bw:document-host-ready', {
          detail: {
            contract: host.contract,
            kind: host.kind,
            documentId: host.documentId,
            capabilities: host.capabilities
          }
        }));
      }, 0);
    } catch (_) {}
    return result;
  };

  // rc-core 可能在本桥之前已经登记过 adapter（例如热加载/测试页），补发一次只读能力事件。
  try {
    if (RC.adapter && Object.keys(RC.adapter() || {}).length) {
      setTimeout(function () {
        var host = current();
        if (host && document && document.dispatchEvent) {
          document.dispatchEvent(new CustomEvent('bw:document-host-ready', {
            detail: { contract: host.contract, kind: host.kind, documentId: host.documentId, capabilities: host.capabilities }
          }));
        }
      }, 0);
    }
  } catch (_) {}
})();
