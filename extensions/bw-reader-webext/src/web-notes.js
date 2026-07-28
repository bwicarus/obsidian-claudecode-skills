// 普通网页便签：复用阅读器 rc-stickynote 的唯一 UI/手势实现；
// 数据只经扩展 document-notes scoped repository，网页锚只在此边界封装/解封。
(() => {
  'use strict';
  if (
    window.__bwPwaProviderOnly ||
    window.__bwPwaBridge ||
    !window.RC?.stickynote?.init ||
    !window.__bwWebPins ||
    window.__bwWebNotes
  ) return;

  const RC = window.RC;
  const pinRoot = window.__bwPinRoot;
  const pins = window.__bwWebPins;
  const repository = window.__bwDocumentNotes;
  if (!pinRoot) return;

  const toast = (message) => {
    try { RC.toast?.(message); } catch (_) {}
  };
  const noteCss = document.createElement('style');
  noteCss.textContent = '#bw-pin-root>.rc-note{pointer-events:auto}';
  window.__bwHead?.appendChild(noteCss);
  window.__bwPinHead?.appendChild(noteCss.cloneNode(true));

  let currentIdentity = null;
  let identityRun = 0;
  let raf = 0;
  let identityTimer = 0;
  let observedUrl = location.href.split('#')[0];
  let refreshSeries = 0;

  const identityKey = (identity) => {
    if (!identity) return '';
    let scope = '';
    try { scope = JSON.stringify(identity.scope ?? null); } catch (_) {
      scope = String(identity.scope ?? '');
    }
    return String(identity.documentId || '') + '\n' + scope;
  };

  const envelopeFor = (rawAnchor, documentId) => {
    if (!rawAnchor) return null;
    return {
      documentId,
      kind: 'web-dom',
      revision: 1,
      data: rawAnchor
    };
  };

  const rawAnchorOf = (anchor, documentId) => {
    if (
      !anchor ||
      anchor.documentId !== documentId ||
      anchor.kind !== 'web-dom' ||
      Number(anchor.revision) !== 1 ||
      !anchor.data
    ) return null;
    return anchor.data;
  };

  function initForIdentity(identity) {
    const documentId = String(identity?.documentId || '');
    if (!documentId) throw new Error('网页便签身份缺少 documentId');
    const mount = (anchor) => {
      const rawAnchor = rawAnchorOf(anchor, documentId);
      if (!rawAnchor) return null;
      const resolved = pins.resolveAnchor(rawAnchor);
      return resolved ? {
        el: pinRoot,
        left: scrollX + resolved.x,
        top: scrollY + resolved.y
      } : null;
    };
    const anchorFromPoint = (x, y) => (
      envelopeFor(pins.anchorAt(x, y), documentId)
    );
    RC.stickynote.init({
      documentId,
      repository,
      disablePortal: true,
      mount,
      anchorFromPoint,
      toast
    });
  }

  async function refreshIdentity(force = false, quiet = false) {
    const run = ++identityRun;
    if (!repository || typeof repository.identity !== 'function') {
      toast('✗ 网页便签本地仓库未就绪');
      return false;
    }
    try {
      // 必须先完成 READY/identity 握手，再允许 rc-stickynote list/create。
      const identity = await repository.identity();
      if (run !== identityRun) return false;
      if (!identity?.documentId) throw new Error('identity 响应无效');
      if (!force && identityKey(identity) === identityKey(currentIdentity)) return true;
      if (currentIdentity) RC.stickynote.removeAll();
      currentIdentity = identity;
      initForIdentity(identity);
      return true;
    } catch (error) {
      if (run === identityRun && !quiet) {
        toast('✗ 网页便签初始化失败：' + String(error?.message || error));
      }
      return false;
    }
  }

  async function boundedRefresh() {
    const series = ++refreshSeries;
    const waits = [0, 80, 220, 500, 1000];
    for (let index = 0; index < waits.length; index++) {
      if (waits[index]) {
        await new Promise((resolve) => setTimeout(resolve, waits[index]));
      }
      if (series !== refreshSeries) return false;
      if (await refreshIdentity(false, true)) return true;
    }
    if (series === refreshSeries) toast('✗ 网页便签身份暂不可用，请稍后重试');
    return false;
  }

  function invalidateAndRefresh() {
    // sender/account/navigation 一失效，旧 document UI 立即不可写；不等待下一次
    // identity READY，避免 transient SPA sender lag 把操作送进上一页。
    identityRun += 1;
    currentIdentity = null;
    try { RC.stickynote.removeAll(); } catch (_) {}
    return boundedRefresh();
  }

  function reposition() {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      try { RC.stickynote.repositionAll(); } catch (_) {}
    });
  }

  function scheduleIdentityCheck() {
    clearTimeout(identityTimer);
    identityTimer = setTimeout(() => {
      identityTimer = 0;
      checkIdentityNow();
    }, 40);
  }
  function checkIdentityNow() {
    const nextUrl = location.href.split('#')[0];
    if (nextUrl === observedUrl) return;
    observedUrl = nextUrl;
    invalidateAndRefresh();
  }

  // SPA 路由可能在不 reload 的情况下改变 documentId。history 入口立即检查，
  // DOM observer 兜底处理框架自行改写 URL 的时序；旧 init 的 generation/subscription
  // 由 rc-stickynote.removeAll/init 明确 teardown。
  for (const name of ['pushState', 'replaceState']) {
    const native = history[name];
    if (typeof native !== 'function' || native.__bwNotesWrapped) continue;
    const wrapped = function (...args) {
      const result = native.apply(this, args);
      checkIdentityNow();
      return result;
    };
    wrapped.__bwNotesWrapped = true;
    history[name] = wrapped;
  }
  addEventListener('popstate', checkIdentityNow, { passive: true });
  addEventListener('resize', reposition, { passive: true });
  if (repository && typeof repository.onInvalidate === 'function') {
    try { repository.onInvalidate(() => {
      observedUrl = location.href.split('#')[0];
      invalidateAndRefresh();
    }); } catch (error) {
      toast('✗ 网页便签失效监听失败：' + String(error?.message || error));
    }
  }
  try {
    new MutationObserver(() => {
      reposition();
      scheduleIdentityCheck();
    }).observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['style', 'class', 'hidden', 'open']
    });
  } catch (_) {}

  const ready = refreshIdentity(true);
  window.__bwWebNotes = {
    ready,
    create: async () => {
      if (!currentIdentity && !(await boundedRefresh())) return false;
      return RC.stickynote.createAtCenter();
    },
    reposition,
    refreshIdentity: () => boundedRefresh()
  };
  RC.actions.bind(
    'note.create',
    () => window.__bwWebNotes.create(),
    { owner: 'web-extension', runtime: 'extension', storage: 'document-notes' }
  );
})();
