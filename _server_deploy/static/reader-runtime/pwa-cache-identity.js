/* pwa-cache-identity.js — 私有 PDF CacheStorage 的可信 client 绑定触发器。
 *
 * 页面永远不发送 acct-v1 namespace；它只能请求 Service Worker 重新向服务器
 * no-store 身份端点核验。这样旧 HTML 壳、同源脚本或代理页都不能自报他人账户。
 */
(function () {
  'use strict';
  var root = typeof globalThis !== 'undefined' ? globalThis : window;
  if (root.BWReaderPrivateCache) return;

  var REBIND = 'BW_PDF_CACHE_REBIND';
  var IDENTITY_REQUEST = 'BW_PDF_CACHE_IDENTITY_REQUEST';
  var CLEAR = 'BW_PDF_CACHE_CLEAR_PRIVATE';
  var DELETE_BOOK = 'BW_PDF_CACHE_DELETE_BOOK';
  var TRUSTED_PATHS = {
    '/pdf/': true,
    '/pdf/view': true,
    '/pdf/epub/view': true,
    '/pdf/html/view': true,
    '/pdf/web/live': true,
    '/pdf/fav/open': true
  };
  var rebinding = false;

  function isTrustedReaderPage() {
    return !!TRUSTED_PATHS[String(location.pathname || '')];
  }

  function workerFromRegistration(registration) {
    return registration && (
      registration.active || registration.waiting || registration.installing
    );
  }

  function postRebind(worker) {
    if (!isTrustedReaderPage() || !worker || rebinding) return false;
    rebinding = true;
    try {
      worker.postMessage({ type: REBIND });
    } catch (_) {
      rebinding = false;
      return false;
    }
    setTimeout(function () { rebinding = false; }, 500);
    return true;
  }

  function registration() {
    if (!('serviceWorker' in navigator)) return Promise.resolve(null);
    return navigator.serviceWorker.register('/pdf/sw.js', { scope: '/pdf/' })
      .catch(function () {
        return navigator.serviceWorker.getRegistration('/pdf/');
      });
  }

  function requestRebind() {
    if (!isTrustedReaderPage()) return Promise.resolve(false);
    return registration().then(function (reg) {
      return postRebind(
        navigator.serviceWorker.controller || workerFromRegistration(reg)
      );
    }).catch(function () { return false; });
  }

  function clear(scope) {
    if (!('serviceWorker' in navigator)) return Promise.resolve(false);
    return navigator.serviceWorker.getRegistration('/pdf/').then(function (reg) {
      var worker = workerFromRegistration(reg);
      if (!worker) return false;
      return new Promise(function (resolve) {
        var channel = typeof MessageChannel === 'function'
          ? new MessageChannel()
          : null;
        var done = false;
        var finish = function (ok) {
          if (done) return;
          done = true;
          resolve(!!ok);
        };
        if (channel) {
          channel.port1.onmessage = function (event) {
            finish(event.data && event.data.ok);
          };
        }
        try {
          worker.postMessage(
            { type: CLEAR, scope: scope === 'current' ? 'current' : 'all' },
            channel ? [channel.port2] : []
          );
        } catch (_) {
          finish(false);
          return;
        }
        setTimeout(function () { finish(!channel); }, 800);
      });
    }).catch(function () { return false; });
  }

  function deleteBook(file) {
    file = String(file || '').trim();
    if (!file || !('serviceWorker' in navigator)) return Promise.resolve(false);
    return navigator.serviceWorker.getRegistration('/pdf/').then(function (reg) {
      var worker = workerFromRegistration(reg);
      if (!worker) return false;
      return new Promise(function (resolve) {
        var channel = typeof MessageChannel === 'function'
          ? new MessageChannel()
          : null;
        var done = false;
        function finish(ok) {
          if (done) return;
          done = true;
          resolve(!!ok);
        }
        if (channel) {
          channel.port1.onmessage = function (event) {
            finish(event.data && event.data.ok);
          };
        }
        try {
          worker.postMessage(
            { type: DELETE_BOOK, file: file },
            channel ? [channel.port2] : []
          );
        } catch (_) {
          finish(false);
          return;
        }
        setTimeout(function () { finish(!channel); }, 1200);
      });
    }).catch(function () { return false; });
  }

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', function (event) {
      if (!event.data || event.data.type !== IDENTITY_REQUEST) return;
      // 只回“请服务器重新核验”，绝不回 window.__USER__ 或 namespace。
      postRebind(event.source || navigator.serviceWorker.controller);
    });
    navigator.serviceWorker.addEventListener('controllerchange', requestRebind);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', requestRebind, { once: true });
  } else {
    requestRebind();
  }
  window.addEventListener('pageshow', requestRebind);

  root.BWReaderPrivateCache = {
    rebind: requestRebind,
    clearCurrent: function () { return clear('current'); },
    clearAll: function () { return clear('all'); },
    deleteBook: deleteBook
  };
})();
