// document_start：只在真正的书籍 PWA 中建立 DocumentHost 桥。
// 扩展默认接管共享 UI；PWA 保留书籍渲染、精确锚点与书籍专属数据。
(() => {
  'use strict';
  if (window.top !== window || window.__bwPwaProviderMarker) return;
  window.__bwPwaProviderMarker = true;

  const version = chrome.runtime.getManifest().version;
  const trustedEntries = new Map([
    ['/pdf/view', { app: 'pdf', route: 'pdf' }],
    ['/pdf/epub/view', { app: 'epub', route: 'epub' }],
    ['/pdf/html/view', { app: 'html', route: 'html' }],
    ['/pdf/fav/open', { app: 'epub', route: 'favorite' }],
  ]);
  const expectedIdentity = trustedEntries.get(location.pathname);
  if (!expectedIdentity) return;
  const expectedHostKind = expectedIdentity.route;
  const validPageIdentity = () => {
    const app = document.querySelector('meta[name="bw-reader-app"]')
      ?.getAttribute('content');
    const route = document.querySelector('meta[name="bw-reader-route"]')
      ?.getAttribute('content');
    return app === expectedIdentity.app && route === expectedIdentity.route;
  };

  {
    const PROTOCOL = 'bw-reader-services/1';
    const TO_EXTENSION = 'page-to-extension';
    const TO_PAGE = 'extension-to-page';
    const OPS = new Set([
      'get', 'list', 'put', 'remove', 'batch',
      'changes', 'applyChanges', 'status',
      // 页面只能观察脱敏状态或请求唯一 owner 执行一次正常 journal
      // drain；不能解除冲突暂停，也不能由同页 postMessage 选择赢家。
      'syncStatus', 'syncNow'
    ]);
    let port = null;
    let reconnectTimer = 0;
    let reconnectAttempt = 0;
    let namespace = '';
    let providerTicket = '';
    let pendingHello = null;
    let sentHelloId = '';
    let active = false;
    let retryBlocked = false;
    const RECONNECT_BASE_MS = 500;
    const RECONNECT_MAX_MS = 30000;
    const PERMANENT_ERRORS = new Set([
      'BW_PROVIDER_AUTH',
      'BW_PROVIDER_AUTH_EXPIRED',
      'BW_PROVIDER_NAMESPACE',
      'BW_PROVIDER_PAGE',
      'BW_PROVIDER_ORIGIN',
      'BW_PROVIDER_PROTOCOL',
      'BW_PROVIDER_UNSUPPORTED',
    ]);
    const OWNER_CLAIM_KEYS = [
      'contract',
      'deviceFamilyId',
      'documentLifetime',
      'hostContract',
      'hostKind',
      'markerObserved',
      'pwaDirectOwner',
      'pwaServerOwner',
      'registryDigest',
      'runtimeContract',
      'syncChangeContract',
      'syncContract',
    ];
    const checkedOwnerClaim = (value) => {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
      if (
        JSON.stringify(Object.keys(value).sort()) !==
          JSON.stringify(OWNER_CLAIM_KEYS) ||
        value.contract !== 'pwa-extension-owner-claim/1' ||
        !/^pwa-install-v1-[a-f0-9]{32}$/.test(
          String(value.deviceFamilyId || '')
        ) ||
        value.runtimeContract !== 'pwa-runtime/1' ||
        value.hostContract !== 'document-host/1' ||
        value.hostKind !== expectedHostKind ||
        value.markerObserved !== true ||
        value.documentLifetime !== true ||
        value.pwaServerOwner !== 'paused' ||
        value.pwaDirectOwner !== 'paused' ||
        value.syncContract !== 'sync-v3' ||
        value.syncChangeContract !== 'record-parent-state/1' ||
        !/^sync-v3:record-parent-state\/1\|[A-Za-z0-9._:-]+(?:\|[A-Za-z0-9._:-]+)*$/
          .test(String(value.registryDigest || ''))
      ) return null;
      return {
        contract: value.contract,
        deviceFamilyId: String(value.deviceFamilyId),
        runtimeContract: value.runtimeContract,
        hostContract: value.hostContract,
        hostKind: value.hostKind,
        markerObserved: true,
        documentLifetime: true,
        pwaServerOwner: value.pwaServerOwner,
        pwaDirectOwner: value.pwaDirectOwner,
        syncContract: value.syncContract,
        syncChangeContract: value.syncChangeContract,
        registryDigest: String(value.registryDigest),
      };
    };
    const post = (type, payload, id) => {
      window.postMessage({
        protocol: PROTOCOL,
        direction: TO_PAGE,
        type,
        id: id || null,
        payload: payload == null ? null : payload
      }, location.origin);
    };
    const clearReconnect = () => {
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = 0;
    };
    const permanentError = (payload) => {
      const code = String(payload?.code || '');
      return payload?.retryable === false || PERMANENT_ERRORS.has(code);
    };
    const scheduleReconnect = () => {
      if (!active || retryBlocked || port || reconnectTimer) return;
      reconnectAttempt += 1;
      const delay = Math.min(
        RECONNECT_MAX_MS,
        RECONNECT_BASE_MS * Math.pow(2, Math.max(0, reconnectAttempt - 1))
      );
      reconnectTimer = setTimeout(() => {
        reconnectTimer = 0;
        connect();
      }, delay);
    };
    const sendPendingHello = () => {
      if (!port || !pendingHello || sentHelloId === pendingHello.id) return;
      const currentPort = port;
      try {
        currentPort.postMessage({
          protocol: PROTOCOL,
          direction: TO_EXTENSION,
          type: 'HELLO',
          id: pendingHello.id || null,
          payload: pendingHello.payload
        });
        sentHelloId = pendingHello.id;
      } catch (_) {
        if (port !== currentPort) return;
        port = null;
        sentHelloId = '';
        pendingHello = null;
        post('DISCONNECTED', { reason: 'extension-provider-port-send-failed' });
        try { currentPort.disconnect(); } catch (_) {}
        scheduleReconnect();
      }
    };
    const connect = () => {
      if (!active || retryBlocked || port) return;
      clearReconnect();
      try {
        const currentPort = chrome.runtime.connect({ name: 'bw-reader-provider' });
        port = currentPort;
        sentHelloId = '';
        currentPort.onMessage.addListener((message) => {
          if (port !== currentPort) return;
          if (!message || message.protocol !== PROTOCOL) return;
          reconnectAttempt = 0;
          if (message.type === 'READY') {
            if (message.id && sentHelloId && message.id !== sentHelloId) return;
            const responseId = message.id || pendingHello?.id || sentHelloId;
            retryBlocked = false;
            if (!pendingHello || !message.id || message.id === pendingHello.id) pendingHello = null;
            post('READY', message.payload || { version }, responseId);
          } else if (message.type === 'ERROR') {
            if (message.id && sentHelloId && message.id !== sentHelloId) return;
            const payload = message.payload || {
              code: 'BW_PROVIDER_ERROR',
              error: '扩展服务握手失败'
            };
            const permanent = permanentError(payload);
            if (!pendingHello || !message.id || message.id === pendingHello.id) pendingHello = null;
            if (permanent) {
              retryBlocked = true;
              clearReconnect();
            }
            post('ERROR', {
              ...payload,
              retryable: permanent ? false : payload.retryable
            }, message.id || sentHelloId);
          } else if (message.type === 'RESULT') {
            post('RESULT', message.payload || { ok: false, error: '空响应' }, message.id);
          } else if (message.type === 'CHANGE') {
            post('CHANGE', message.payload || {});
          }
        });
        currentPort.onDisconnect.addListener(() => {
          if (port !== currentPort) return;
          void chrome.runtime.lastError;
          port = null;
          sentHelloId = '';
          pendingHello = null;
          post('DISCONNECTED', { reason: 'extension-provider-port-disconnected' });
          scheduleReconnect();
        });
        sendPendingHello();
      } catch (_) {
        port = null;
        sentHelloId = '';
        scheduleReconnect();
      }
    };
    connect();

    window.addEventListener('message', (event) => {
      if (event.source !== window || event.origin !== location.origin) return;
      const message = event.data;
      if (!message || message.protocol !== PROTOCOL || message.direction !== TO_EXTENSION) return;
      if (message.type === 'HELLO') {
        const requested = String(
          message.namespace || message.payload?.namespace || ''
        ).trim();
        const requestedTicket = String(
          message.ticket || message.payload?.ticket || ''
        ).trim();
        if (!/^acct-v1-[a-f0-9]{64}$/.test(requested)) {
          retryBlocked = true;
          clearReconnect();
          post('ERROR', {
            code: 'BW_PROVIDER_NAMESPACE',
            error: '用户命名空间无效',
            retryable: false
          }, message.id);
          return;
        }
        if (!/^pvt-v2-[0-9]{10,12}-[a-f0-9]{32}-[a-f0-9]{64}$/.test(requestedTicket)) {
          retryBlocked = true;
          clearReconnect();
          post('ERROR', {
            code: 'BW_PROVIDER_AUTH',
            error: '扩展 Vault 授权证明无效',
            retryable: false
          }, message.id);
          return;
        }
        if (namespace && namespace !== requested) {
          retryBlocked = true;
          clearReconnect();
          post('ERROR', {
            code: 'BW_PROVIDER_NAMESPACE',
            error: '同一页面不能切换用户命名空间',
            retryable: false
          }, message.id);
          return;
        }
        namespace = requested;
        providerTicket = requestedTicket;
        retryBlocked = false;
        const ownerClaim = checkedOwnerClaim(
          message.payload?.syncOwnerClaim
        );
        pendingHello = {
          id: String(message.id || ''),
          payload: {
            namespace,
            ticket: providerTicket,
            page: location.pathname,
            ...(ownerClaim ? { syncOwnerClaim: ownerClaim } : {}),
          }
        };
        if (!port && !reconnectTimer) connect();
        else sendPendingHello();
        return;
      }
      if (message.type !== 'CALL' || !message.id) return;
      const operation = String(message.payload?.operation || '');
      if (!namespace || !OPS.has(operation) || !port) {
        const code = !namespace
          ? 'BW_PROVIDER_NAMESPACE'
          : (!OPS.has(operation) ? 'BW_PROVIDER_OPERATION' : 'BW_PROVIDER_UNAVAILABLE');
        post('RESULT', {
          ok: false,
          code,
          error: !namespace
            ? '扩展服务尚未初始化用户命名空间'
            : (!OPS.has(operation) ? '不允许的数据操作' : '扩展服务当前不可用')
        }, message.id);
        return;
      }
      port.postMessage({
        protocol: PROTOCOL,
        direction: TO_EXTENSION,
        type: 'CALL',
        id: message.id,
        payload: { operation, args: message.payload?.args || {} }
      });
    });

    const activate = () => {
      if (!validPageIdentity()) return;
      active = true;
      const documentRoot = document.documentElement;
      if (!documentRoot) return;
      documentRoot.dataset.bwReaderExtensionProvider = version;
      connect();
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', activate, { once: true });
    } else {
      activate();
    }
  }

  /* 正式接管入口：完整 UI 已由 manifest 静态注入，不再依赖 ?ui=legacy 或后台动态载入。 */
  const activateTakeover = () => {
    const documentRoot = document.documentElement;
    if (!documentRoot || !validPageIdentity()) return;
    if (window.__bwPwaBridge) return;
    const PROTOCOL = 'bw-reader-pwa/1';
    documentRoot.dataset.bwReaderExtension = version;

    let seq = 0;
    const pending = new Map();
    const listeners = new Map();
    let heartbeatTimer = 0;
    const emit = (type, payload) => {
      const set = listeners.get(type);
      if (set) for (const fn of set) { try { fn(payload); } catch (_) {} }
    };
    const send = (type, payload, id) => {
      window.postMessage({ protocol: PROTOCOL, direction: 'to-page', type, payload: payload || null, id: id || null }, location.origin);
    };

    const bridge = window.__bwPwaBridge = {
      protocol: PROTOCOL,
      ready: false,
      state: null,
      selection: null,
      on(type, fn) {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type).add(fn);
        return () => listeners.get(type)?.delete(fn);
      },
      request(type, payload, timeoutMs = 10000) {
        const id = 'e' + Date.now().toString(36) + (++seq).toString(36);
        return new Promise((resolve, reject) => {
          const timer = setTimeout(() => { pending.delete(id); reject(new Error('阅读器桥接超时')); }, timeoutMs);
          pending.set(id, { resolve, reject, timer });
          send(type, payload, id);
        });
      },
      local(action, payload) { return bridge.request('LOCAL_ACTION', { action, payload: payload || {} }, 60000); },
      context() { return bridge.request('GET_CONTEXT', null, 15000); },
      clearSelection() { return bridge.request('CLEAR_SELECTION', null, 10000); },
      async takeover() {
        if (bridge.takenOver) return true;
        await bridge.request('TAKEOVER', {
          version,
          uiOwner: 'extension'
        }, 10000);
        bridge.takenOver = true;
        if (!heartbeatTimer) {
          heartbeatTimer = setInterval(() => {
            send('HEARTBEAT', { version, uiOwner: 'extension' });
          }, 5000);
        }
        return true;
      },
      release() {
        if (heartbeatTimer) clearInterval(heartbeatTimer);
        heartbeatTimer = 0;
        if (bridge.takenOver) {
          send('GOODBYE', { version, uiOwner: 'extension' });
        }
        bridge.takenOver = false;
      },
      takenOver: false,
    };

    window.addEventListener('message', (e) => {
      if (e.source !== window || e.origin !== location.origin) return;
      const m = e.data;
      if (!m || m.protocol !== PROTOCOL || m.direction !== 'to-extension') return;
      // 页面桥是排在体积较大的书籍 renderer 模块之后加载的。document_start
      // 发出的第一条 HELLO 可能先于页面桥监听器；HOST_READY 是页面桥安装完毕
      // 后的确定性信号，收到后补发一次幂等 HELLO，不能靠加载时序碰运气。
      if (m.type === 'HOST_READY') {
        emit('HOST_READY', m.payload || null);
        if (!bridge.ready) send('HELLO', { version, uiOwner: 'extension' });
        return;
      }
      if (m.type === 'READY') {
        bridge.ready = true; bridge.state = m.payload || null;
        bridge.selection = m.payload?.selection || null;
        emit('READY', bridge.state);
        document.dispatchEvent(new CustomEvent('bw:pwa-ready'));
        return;
      }
      if (m.type === 'SELECTION') {
        bridge.selection = m.payload || null;
        emit('SELECTION', bridge.selection);
        return;
      }
      if (m.type === 'ACTION') { bridge.lastAction = m.payload || null; emit('ACTION', bridge.lastAction); return; }
      if (m.type === 'LOCATION') {
        if (bridge.state) bridge.state.currentLocation = m.payload || null;
        emit('LOCATION', m.payload || null);
        return;
      }
      if (m.type === 'RESULT' && m.id) {
        const p = pending.get(m.id); if (!p) return;
        clearTimeout(p.timer); pending.delete(m.id);
        if (m.payload?.ok) p.resolve(m.payload.result);
        else p.reject(new Error(m.payload?.error || '阅读器命令失败'));
      }
    });
    send('HELLO', { version, uiOwner: 'extension' });
    addEventListener('pagehide', () => {
      bridge.release();
    }, { once: true });
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', activateTakeover, { once: true });
  } else {
    activateTakeover();
  }
})();
