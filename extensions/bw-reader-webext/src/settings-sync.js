// 扩展设置是跨网站的本地权威；每个网页 isolated world 的 localStorage 只是共享
// RC 组件的兼容镜像。进入书籍 PWA 时，再把 PWA PreferenceStore 中缺失的值并入
// 扩展，并把扩展已有值回写给 PWA。这样普通网页不依赖 PWA，同一设置也只有一份。
(() => {
  'use strict';
  if (window.__bwSettingsSync) return;
  window.__bwSettingsSync = 2;

  const KEY = 'bwReaderExtensionPreferencesV2';
  const extensionStore = window.__bwExtensionStore;
  if (!extensionStore) return;
  const CONTRACT = 'preference-store/1';
  const TO_PAGE = 'extension-to-page';
  const TO_EXTENSION = 'page-to-extension';
  const PREFIX = /^(eph-|eph2-|rc-note-|rc-ink-|bw-set-|set-|pdf-set-)/;
  const MAX_VALUE_BYTES = 64 * 1024;
  const MAX_KEYS = 256;
  const originalSetItem = Storage.prototype.setItem;
  const originalRemoveItem = Storage.prototype.removeItem;
  const encoder = new TextEncoder();
  const pendingWrites = new Map();
  const pendingRequests = new Map();
  let requestSequence = 0;
  let writeTimer = 0;
  let applyingMirror = false;
  let pwaAllowedKeys = null;

  const validKey = (key) => (
    typeof key === 'string' &&
    key.length > 0 &&
    key.length <= 160 &&
    PREFIX.test(key)
  );
  const validValue = (value) => (
    value == null ||
    (
      typeof value === 'string' &&
      encoder.encode(value).byteLength <= MAX_VALUE_BYTES
    )
  );
  const normalizeValues = (value) => {
    const out = {};
    const source = (
      value &&
      typeof value === 'object' &&
      !Array.isArray(value)
    ) ? value : {};
    for (const [key, raw] of Object.entries(source).slice(0, MAX_KEYS)) {
      if (!validKey(key) || !validValue(raw)) continue;
      out[key] = raw == null ? null : String(raw);
    }
    return out;
  };
  const normalizeRecord = (value) => ({
    schema: 2,
    values: normalizeValues(value?.schema === 2 ? value.values : {}),
    updatedAt: Math.max(0, Number(value?.updatedAt || 0))
  });
  const setState = (state, detail = '') => {
    const root = document.documentElement;
    if (!root) return;
    root.dataset.bwReaderPreferenceSync = state;
    if (detail) root.dataset.bwReaderPreferenceError = String(detail).slice(0, 240);
    else delete root.dataset.bwReaderPreferenceError;
  };
  const applyValues = (values) => {
    applyingMirror = true;
    try {
      for (const [key, value] of Object.entries(normalizeValues(values))) {
        try {
          if (value == null) originalRemoveItem.call(localStorage, key);
          else originalSetItem.call(localStorage, key, value);
        } catch (_) {}
      }
    } finally {
      applyingMirror = false;
    }
  };
  const pwaRequest = (type, payload, timeoutMs = 2500) => {
    const requestId = 'extension-preference-' +
      Date.now().toString(36) + '-' + (++requestSequence).toString(36);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pendingRequests.delete(requestId);
        reject(new Error('PWA PreferenceStore 未就绪'));
      }, timeoutMs);
      pendingRequests.set(requestId, { type, resolve, reject, timer });
      window.postMessage({
        __bwReaderPreference: CONTRACT,
        direction: TO_PAGE,
        type,
        requestId,
        payload: payload == null ? null : payload
      }, location.origin);
    });
  };
  const patchPwa = async (changes) => {
    if (!pwaAllowedKeys || !changes.length) return;
    const allowed = changes
      .filter(([key]) => pwaAllowedKeys.has(key))
      .slice(0, 64)
      .map(([legacyKey, value]) => ({ legacyKey, value }));
    if (!allowed.length) return;
    await pwaRequest('PATCH', { changes: allowed }, 4000);
  };
  const flush = async () => {
    writeTimer = 0;
    if (!pendingWrites.size) return;
    const changes = Array.from(pendingWrites.entries()).slice(0, MAX_KEYS);
    for (const [key, sent] of changes) {
      if (pendingWrites.get(key) === sent) pendingWrites.delete(key);
    }
    try {
      const record = normalizeRecord(await extensionStore.get(KEY));
      for (const [key, value] of changes) record.values[key] = value;
      record.updatedAt = Date.now();
      await extensionStore.set(KEY, record);
      setState('ready');
      try { await patchPwa(changes); } catch (error) {
        // 扩展本地写入已成功；PWA 镜像失败不会回滚跨站权威值。
        setState('ready-local', error?.message || error);
      }
    } catch (error) {
      for (const [key, value] of changes) pendingWrites.set(key, value);
      setState('retrying', error?.message || error);
      if (!writeTimer) writeTimer = setTimeout(flush, 1000);
    }
  };
  const queueWrite = (key, value) => {
    if (!validKey(key) || !validValue(value)) return;
    pendingWrites.set(key, value == null ? null : String(value));
    clearTimeout(writeTimer);
    writeTimer = setTimeout(flush, 120);
  };

  Storage.prototype.setItem = function (key, value) {
    originalSetItem.apply(this, arguments);
    try {
      if (this === localStorage && !applyingMirror) {
        queueWrite(String(key), String(value));
      }
    } catch (_) {}
  };
  Storage.prototype.removeItem = function (key) {
    originalRemoveItem.apply(this, arguments);
    try {
      if (this === localStorage && !applyingMirror) {
        queueWrite(String(key), null);
      }
    } catch (_) {}
  };

  window.addEventListener('message', (event) => {
    if (
      event.source !== window ||
      event.origin !== location.origin ||
      !event.data ||
      event.data.__bwReaderPreference !== CONTRACT ||
      event.data.direction !== TO_EXTENSION
    ) return;
    const message = event.data;
    const pending = pendingRequests.get(String(message.requestId || ''));
    if (!pending) return;
    if (
      (pending.type === 'HELLO' && message.type !== 'READY') ||
      (pending.type === 'PATCH' && message.type !== 'RESULT')
    ) return;
    clearTimeout(pending.timer);
    pendingRequests.delete(message.requestId);
    const payload = message.payload || {};
    if (payload.ok === false || payload.error || payload.code) {
      const error = new Error(String(payload.error || 'PreferenceStore 拒绝请求'));
      error.code = String(payload.code || 'BW_PREFERENCE_STORE');
      pending.reject(error);
    } else {
      pending.resolve(payload);
    }
  });

  const mergePwa = async (extensionRecord) => {
    if (!window.__bwPwaBridge) return extensionRecord;
    const payload = await pwaRequest('HELLO', null, 4000);
    const allowed = Array.isArray(payload.allowedKeys)
      ? payload.allowedKeys.map(String).filter(validKey)
      : [];
    pwaAllowedKeys = new Set(allowed);
    const pwaValues = normalizeValues(payload.values || {});
    const missing = {};
    for (const [key, value] of Object.entries(pwaValues)) {
      if (
        pwaAllowedKeys.has(key) &&
        !Object.prototype.hasOwnProperty.call(extensionRecord.values, key)
      ) {
        extensionRecord.values[key] = value;
        missing[key] = value;
      }
    }
    if (Object.keys(missing).length) {
      extensionRecord.updatedAt = Date.now();
      await extensionStore.set(KEY, extensionRecord);
    }
    await patchPwa(Object.entries(extensionRecord.values));
    return extensionRecord;
  };

  setState('connecting');
  extensionStore.get(KEY).then(async (stored) => {
    let record = normalizeRecord(stored);
    applyValues(record.values);             // 扩展跨站值始终先落地。
    try {
      record = await mergePwa(record);       // PWA 只补缺失值，不能覆盖扩展已有值。
      applyValues(record.values);
    } catch (error) {
      if (window.__bwPwaBridge) setState('ready-local', error?.message || error);
    }
    if (!document.documentElement.dataset.bwReaderPreferenceError) {
      setState('ready');
    }
  }).catch((error) => {
    setState('failed', error?.message || error);
  });
})();
