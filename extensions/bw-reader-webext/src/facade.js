// facade.js — Shadow DOM 宿主 + document 门面(必须在 vendor/rc-*.js 之前加载)。
//
// 设计:rc-*.js 共享层(阅读器原文件)**逐字零改动**搬进扩展;build.py 把每份源码包一层
//   ;(function(document){ <原文> })(window.__bwReaderDoc);
// 用参数遮蔽全局 document,把它们的 getElementById/head/body/documentElement/事件监听
// 全部重定向进扩展自己的 Shadow DOM——源文件不 fork、不 drift,随时从阅读器重新拉最新版。
//
// 对应测绘结论(webext-reader-chrome-portspec):
//   · rc-* 的 DOM 根引用是 verbatim 复用的最大障碍 → 用门面一次解决,不逐文件 sed;
//   · 事件监听的 e.target 在 shadow 边界会 retarget 到 host → 包一层 composedPath()[0] 代理,
//     否则「点外关闭」判定把 shadow 内点击误判为框外(rc-sidedrawer 设置弹层就踩这个)。
(() => {
  "use strict";
  if (window.__bwPwaProviderOnly) return;
  if (window.__bwReaderDoc) return;   // 幂等
  const ORIGIN = "https://bwicarus.taile44d0c.ts.net";

  const localStoreCall = (type, key, value) => new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ type, key, value }, (response) => {
      const runtimeError = chrome.runtime.lastError;
      if (runtimeError || !response?.ok) {
        reject(new Error(runtimeError?.message || response?.error || '扩展本地数据不可用'));
      } else {
        resolve(response.data);
      }
    });
  });
  window.__bwExtensionStore = Object.freeze({
    get: (key) => localStoreCall('BW_LOCAL_STORAGE_GET', key),
    set: (key, value) => localStoreCall('BW_LOCAL_STORAGE_SET', key, value),
    remove: (key) => localStoreCall('BW_LOCAL_STORAGE_REMOVE', key)
  });

  const nativeBridgeEncoder = new TextEncoder();
  const nativeBridgeTrimUtf8 = (value, maximumBytes) => {
    const text = String(value || '');
    if (nativeBridgeEncoder.encode(text).byteLength <= maximumBytes) return text;
    let result = '';
    let used = 0;
    for (const character of text) {
      const bytes = nativeBridgeEncoder.encode(character).byteLength;
      if (used + bytes > maximumBytes) break;
      result += character;
      used += bytes;
    }
    return result;
  };
  const nativeBridgeRevision = (value) => {
    const text = String(value || '');
    let first = 0x811c9dc5;
    let second = 0x9e3779b9;
    for (let index = 0; index < text.length; index += 1) {
      const code = text.charCodeAt(index);
      first = Math.imul(first ^ code, 0x01000193) >>> 0;
      second = Math.imul(second ^ (code + index), 0x85ebca6b) >>> 0;
    }
    return first.toString(16).padStart(8, '0') +
      second.toString(16).padStart(8, '0');
  };
  const nativeBridgeWebContext = () => {
    let value = {};
    try {
      value = (window.RC && RC.adapter && RC.adapter().getContext()) || {};
    } catch (_) {}
    const url = nativeBridgeTrimUtf8(window.location.href, 2048);
    const title = nativeBridgeTrimUtf8(
      value.book_name || value.book || value.title || document.title,
      1024
    );
    const visibleText = nativeBridgeTrimUtf8(
      value.visible_text || value.text || '',
      32768
    );
    const selection = nativeBridgeTrimUtf8(
      value.selection || window.getSelection?.().toString() || '',
      4096
    );
    return {
      url,
      title,
      visibleText,
      selection,
      revision: nativeBridgeRevision(
        [url, title, visibleText, selection].join('\u001f')
      )
    };
  };

  const nativeComputerVoiceBridge = (() => {
    const CONTRACT = 'bw-reader-native/1';
    const ACTIONS = new Set([
      'capabilities',
      'voice.status',
      'voice.toggle',
      'voice.context'
    ]);
    const APP_KINDS = new Set(['codex-desktop', 'chatgpt-classic']);
    const encoder = new TextEncoder();
    let available = false;
    let launchScheme = '';
    let supportedAppKinds = new Set();
    let latestState = {
      phase: 'unavailable',
      active: false,
      busy: false,
      sessionId: null,
      appKind: null,
      updatedAt: ''
    };
    let pollTimer = null;
    let statusInFlight = null;
    let toggleInFlight = null;
    let contextInFlight = null;
    let lastContextRevision = '';
    let contextRefreshTimer = null;

    const requestId = () => {
      const bytes = new Uint8Array(18);
      crypto.getRandomValues(bytes);
      return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
    };
    const trimUtf8 = (value, maximumBytes) => {
      const text = String(value || '');
      if (encoder.encode(text).byteLength <= maximumBytes) return text;
      let result = '';
      let used = 0;
      for (const character of text) {
        const bytes = encoder.encode(character).byteLength;
        if (used + bytes > maximumBytes) break;
        result += character;
        used += bytes;
      }
      return result;
    };
    const call = (action, details) => new Promise((resolve, reject) => {
      if (!ACTIONS.has(action)) {
        reject(Object.assign(new Error('BWReader App 请求无效'), {
          code: 'BW_NATIVE_APP_REQUEST_INVALID'
        }));
        return;
      }
      chrome.runtime.sendMessage(Object.assign({
        type: 'BW_NATIVE_APP_REQUEST',
        action,
        requestId: details?.requestId || requestId()
      }, details || {}), (response) => {
        const runtimeError = chrome.runtime.lastError;
        const data = response?.data;
        if (runtimeError || !response?.ok || !data?.ok) {
          reject(Object.assign(new Error(
            runtimeError?.message ||
            data?.error ||
            response?.error ||
            'BWReader App 暂时不可用'
          ), {
            code: data?.code || response?.code || 'BW_NATIVE_APP_UNAVAILABLE'
          }));
          return;
        }
        if (data.contract !== CONTRACT || data.action !== action) {
          reject(Object.assign(new Error('BWReader App 响应无效'), {
            code: 'BW_NATIVE_APP_RESPONSE_INVALID'
          }));
          return;
        }
        resolve(data);
      });
    });
    const titleFor = (state) => {
      if (state.active === true) return '电脑客户端语音已连接';
      if (state.busy === true) return '正在交给 BWReader App 处理电脑语音…';
      if (state.phase === 'failed') return '电脑客户端语音启动失败';
      return '在 BWReader App 中启动电脑客户端语音';
    };
    const publishState = (state) => {
      const value = state && typeof state === 'object' ? state : {};
      latestState = {
        phase: String(value.phase || 'idle'),
        active: value.active === true,
        busy: value.busy === true,
        sessionId: typeof value.sessionId === 'string' ? value.sessionId : null,
        appKind: APP_KINDS.has(value.appKind) ? value.appKind : null,
        updatedAt: String(value.updatedAt || '')
      };
      window.__BW_NATIVE_COMPUTER_VOICE_STATE__ = {
        active: latestState.active,
        busy: latestState.busy,
        sessionId: latestState.sessionId,
        appKind: latestState.appKind,
        phase: latestState.phase,
        title: titleFor(latestState)
      };
      window.dispatchEvent(new CustomEvent(
        'bw-native-computer-voice-state',
        { detail: window.__BW_NATIVE_COMPUTER_VOICE_STATE__ }
      ));
      scheduleStatusPoll();
    };
    const publishCapability = () => {
      window.dispatchEvent(new CustomEvent(
        'bw-native-computer-voice-capability',
        { detail: { available, appKinds: Array.from(supportedAppKinds) } }
      ));
    };
    const scheduleStatusPoll = () => {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      if (!available || document.visibilityState === 'hidden') return;
      if (!latestState.busy && !latestState.active) return;
      pollTimer = setTimeout(() => {
        pollTimer = null;
        refreshStatus().catch(() => {});
      }, latestState.busy ? 700 : 4000);
    };
    const pushContextIfChanged = () => {
      if (!available || (!latestState.active && !latestState.busy)) {
        return Promise.resolve(false);
      }
      if (contextInFlight) return contextInFlight;
      const webContext = nativeBridgeWebContext();
      if (webContext.revision === lastContextRevision) {
        return Promise.resolve(false);
      }
      contextInFlight = call('voice.context', { webContext }).then((value) => {
        lastContextRevision = webContext.revision;
        if (value.state) publishState(value.state);
        return true;
      }).finally(() => {
        contextInFlight = null;
      });
      return contextInFlight;
    };
    const scheduleContextRefresh = () => {
      if (!available || (!latestState.active && !latestState.busy)) return;
      if (contextRefreshTimer) clearTimeout(contextRefreshTimer);
      contextRefreshTimer = setTimeout(() => {
        contextRefreshTimer = null;
        pushContextIfChanged().catch(() => {});
      }, 250);
    };
    const refreshStatus = () => {
      if (!available) {
        return Promise.reject(Object.assign(new Error('BWReader App 原生桥不可用'), {
          code: 'BW_NATIVE_APP_NOT_SUPPORTED'
        }));
      }
      if (statusInFlight) return statusInFlight;
      statusInFlight = call('voice.status').then((value) => {
        publishState(value.state);
        pushContextIfChanged().catch(() => {});
        return latestState;
      }).finally(() => {
        statusInFlight = null;
      });
      return statusInFlight;
    };
    const initialize = () => call('capabilities').then((value) => {
      const actions = new Set(value.actions || []);
      const appKinds = new Set(value.appKinds || []);
      if (
        !actions.has('voice.status') ||
        !actions.has('voice.toggle') ||
        !actions.has('voice.context') ||
        value.launchScheme !== 'bwreader' ||
        !appKinds.has('codex-desktop')
      ) {
        throw Object.assign(new Error('BWReader App 电脑语音能力不完整'), {
          code: 'BW_NATIVE_APP_CAPABILITY_MISSING'
        });
      }
      available = true;
      launchScheme = value.launchScheme;
      supportedAppKinds = appKinds;
      // This is only the optional containing-App command bridge.  Do not set
      // __BW_NATIVE_COMPUTER_VOICE__: that flag is reserved for the App's own
      // WKWebView, where Swift truly owns microphone/audio/WSS.  Setting it in
      // ordinary Safari pages disables their trusted computer-button gesture
      // and forces the old App deep-link path.
      window.__BW_NATIVE_APP_COMPUTER_VOICE__ = true;
      publishCapability();
      return refreshStatus().catch(() => latestState);
    }).catch((error) => {
      available = false;
      launchScheme = '';
      supportedAppKinds = new Set();
      window.__BW_NATIVE_APP_COMPUTER_VOICE__ = false;
      publishCapability();
      throw error;
    });
    const toggle = (appKind) => {
      const target = appKind === 'chatgpt-classic'
        ? 'chatgpt-classic'
        : 'codex-desktop';
      if (!available || !supportedAppKinds.has(target) || launchScheme !== 'bwreader') {
        return Promise.reject(Object.assign(new Error('请先安装或更新 BWReader App'), {
          code: 'BW_NATIVE_APP_NOT_SUPPORTED'
        }));
      }
      if (toggleInFlight) return toggleInFlight;
      const id = requestId();
      const webContext = nativeBridgeWebContext();
      const shouldLaunch = !latestState.active && !latestState.busy;
      publishState(Object.assign({}, latestState, {
        phase: latestState.active ? 'stopping' : 'launching',
        busy: true,
        appKind: target,
        updatedAt: new Date().toISOString()
      }));
      toggleInFlight = call('voice.toggle', {
        requestId: id,
        appKind: target,
        webContext
      }).then((value) => {
        lastContextRevision = webContext.revision;
        publishState(value.state);
        return value;
      }).catch((error) => {
        publishState({
          phase: 'failed',
          active: false,
          busy: false,
          sessionId: null,
          appKind: target,
          updatedAt: new Date().toISOString()
        });
        throw error;
      }).finally(() => {
        toggleInFlight = null;
      });

      // Keep the custom-scheme navigation in the original trusted click.  The
      // native handler writes the same one-time request id into the App Group;
      // the App waits briefly if URL delivery wins that race.
      if (shouldLaunch) {
        const launchURL = `${launchScheme}://native-voice?requestId=${encodeURIComponent(id)}`;
        try { window.location.assign(launchURL); } catch (_) {}
      }
      return toggleInFlight;
    };

    window.addEventListener('pageshow', () => {
      if (available) refreshStatus().catch(() => {});
    });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && available) {
        refreshStatus().catch(() => {});
      }
    });
    document.addEventListener('selectionchange', () => {
      scheduleContextRefresh();
    });
    window.addEventListener('scroll', scheduleContextRefresh, { passive: true });
    window.addEventListener('hashchange', scheduleContextRefresh);
    window.addEventListener('popstate', scheduleContextRefresh);
    setTimeout(() => initialize().catch(() => {}), 0);
    return Object.freeze({
      available: () => available,
      state: () => Object.assign({}, latestState),
      refreshStatus,
      pushContextIfChanged,
      toggle
    });
  })();
  window.__bwNativeComputerVoiceExtensionBridge = nativeComputerVoiceBridge;

  const nativeAppDataBridge = (() => {
    const CONTRACT = 'bw-reader-native/1';
    const ACTIONS = new Set([
      'notes.status', 'notes.list', 'notes.read', 'notes.create'
    ]);
    const safeId = /^[A-Za-z0-9_-]{8,96}$/;
    const utf8Length = (value) => new TextEncoder().encode(String(value || '')).byteLength;
    const requestId = () => {
      const bytes = new Uint8Array(18);
      crypto.getRandomValues(bytes);
      return Array.from(
        bytes,
        (value) => value.toString(16).padStart(2, '0')
      ).join('');
    };
    const call = (action, details) => new Promise((resolve, reject) => {
      if (!ACTIONS.has(action)) {
        reject(Object.assign(new Error('BWReader App 数据请求无效'), {
          code: 'BW_NATIVE_APP_REQUEST_INVALID'
        }));
        return;
      }
      chrome.runtime.sendMessage(Object.assign({
        type: 'BW_NATIVE_APP_REQUEST',
        action,
        requestId: requestId()
      }, details || {}), (response) => {
        const runtimeError = chrome.runtime.lastError;
        const data = response?.data;
        if (runtimeError || !response?.ok || !data?.ok) {
          reject(Object.assign(new Error(
            runtimeError?.message ||
            data?.error ||
            response?.error ||
            'BWReader App 本机数据暂时不可用'
          ), {
            code: data?.code || response?.code || 'BW_NATIVE_APP_UNAVAILABLE'
          }));
          return;
        }
        if (data.contract !== CONTRACT || data.action !== action) {
          reject(Object.assign(new Error('BWReader App 数据响应无效'), {
            code: 'BW_NATIVE_APP_RESPONSE_INVALID'
          }));
          return;
        }
        resolve(data);
      });
    });
    return Object.freeze({
      status: () => call('notes.status'),
      listNotes: () => call('notes.list'),
      readNote: (noteId) => {
        const value = String(noteId || '');
        if (!safeId.test(value)) {
          return Promise.reject(Object.assign(new Error('本机笔记编号无效'), {
            code: 'BW_NATIVE_NOTE_ID_INVALID'
          }));
        }
        return call('notes.read', { noteId: value });
      },
      createNote: (details) => {
        const value = details && typeof details === 'object' && !Array.isArray(details)
          ? details
          : {};
        const keys = Object.keys(value);
        const allowed = new Set(['name', 'text', 'file', 'page']);
        const name = typeof value.name === 'string' ? value.name.trim() : '';
        const text = typeof value.text === 'string' ? value.text.trim() : '';
        const file = value.file == null ? '' : value.file;
        const page = value.page == null ? 0 : value.page;
        if (
          keys.some((key) => !allowed.has(key)) ||
          !name || !text ||
          utf8Length(name) > 512 || utf8Length(text) > 262144 ||
          typeof file !== 'string' || utf8Length(file) > 8192 ||
          !Number.isSafeInteger(page) || page < 0 || page > 10000000
        ) {
          return Promise.reject(Object.assign(new Error('本机笔记内容无效'), {
            code: 'BW_NATIVE_NOTE_CREATE_INVALID'
          }));
        }
        return call('notes.create', { name, text, file, page });
      }
    });
  })();
  window.__bwNativeAppDataBridge = nativeAppDataBridge;

  function createNativeLocalNotesFetchInterceptor(environment) {
    const origin = String(environment.origin || '');
    const runtime = environment.runtime;
    const bridge = environment.bridge;
    const URLCtor = environment.URL;
    const ResponseCtor = environment.Response;
    if (typeof URLCtor !== 'function' || typeof ResponseCtor !== 'function') {
      return async function unavailableNativeNotesInterceptor() { return null; };
    }
    let safariExtension = false;
    try {
      safariExtension = new URLCtor(String(runtime.getURL(''))).protocol ===
        'safari-web-extension:';
    } catch (_) {}

    function invalid(message) {
      return Object.assign(new TypeError(message), {
        code: 'BW_NATIVE_NOTE_CREATE_INVALID'
      });
    }

    return async function intercept(url, init) {
      if (!safariExtension) return null;
      let target;
      try { target = new URLCtor(String(url)); }
      catch (_) { return null; }
      const method = String(init?.method || 'GET').toUpperCase();
      if (
        target.origin !== origin ||
        target.pathname !== '/pdf/api/to-note' ||
        method !== 'POST'
      ) {
        return null;
      }
      if (typeof init?.body !== 'string') {
        throw invalid('本机笔记请求正文必须是 JSON');
      }
      let body;
      try { body = JSON.parse(init.body); }
      catch (_) { throw invalid('本机笔记请求正文不是有效 JSON'); }
      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        throw invalid('本机笔记请求正文无效');
      }
      const result = await bridge.createNote(body);
      if (result?.handled === false && result.disposition === 'pi') {
        return null;
      }
      if (
        result?.handled !== true ||
        (result.disposition !== 'queued' && result.disposition !== 'committed') ||
        (result.disposition === 'queued' && (
          typeof result.plannedFileName !== 'string' || !result.plannedFileName
        )) ||
        (result.disposition === 'committed' && (
          typeof result.notePath !== 'string' || !result.notePath
        )) ||
        typeof result.obsidianURL !== 'string'
      ) {
        throw Object.assign(new Error('BWReader App 本机笔记响应无效'), {
          code: 'BW_NATIVE_APP_RESPONSE_INVALID'
        });
      }
      const displayPath = result.disposition === 'queued'
        ? result.plannedFileName
        : result.notePath;
      return new ResponseCtor(JSON.stringify({
        ok: true,
        // note_path stays for the existing Reader success UI; when queued it
        // is explicitly only the planned name and may gain a suffix on write.
        note_path: displayPath,
        planned_note_path: result.disposition === 'queued' ? displayPath : '',
        obsidian_url: result.obsidianURL,
        local_disposition: result.disposition,
        pending_export: result.disposition === 'queued'
      }), {
        status: 200,
        headers: { 'content-type': 'application/json; charset=utf-8' }
      });
    };
  }

  const nativeLocalNotesFetchInterceptor = createNativeLocalNotesFetchInterceptor({
    origin: ORIGIN,
    runtime: chrome.runtime,
    bridge: nativeAppDataBridge,
    URL: globalThis.URL,
    Response: globalThis.Response
  });

  // iOS may reclaim the Safari extension background even while a long-lived
  // Port and WSS are active.  The shared Reader button used to call through
  // that worker, so a click could disappear before Windows saw a connection.
  //
  // Keep the button in exactly the same Reader/sidebar position, but place one
  // tiny extension-owned foreground document over it.  The trusted click,
  // microphone, playback and WSS then all live under safari-web-extension://
  // without opening BWReader or a separate call tab.  The frame is moved, not
  // recreated, when the UI switches between the sidebar and top-bar buttons;
  // an active call therefore survives that visual change.


  // Keep the button in exactly the same Reader/sidebar position, but place one
  // tiny extension-owned foreground document over it.  The trusted click,
  // microphone, playback and WSS then all live under safari-web-extension://
  // without opening BWReader or a separate call tab.  The frame is moved, not
  // recreated, when the UI switches between the sidebar and top-bar buttons;
  // an active call therefore survives that visual change.
  const inlineComputerVoiceSurface = (() => {
    const runtime = window.chrome && window.chrome.runtime;
    if (!runtime || typeof runtime.getURL !== 'function') return null;
    let extensionRoot = '';
    try { extensionRoot = String(runtime.getURL('')); } catch (_) {}
    // Any extension scheme, not Safari's alone: the check exists to confirm this
    // is an extension context at all, and hard-coding one browser's prefix also
    // made the whole surface unreachable in Chrome, where it can be debugged.
    if (!/^(safari-web-extension|chrome-extension|moz-extension):\/\//.test(extensionRoot)) {
      return null;
    }

    const CONTRACT = 'bw-extension-computer-voice-frame/1';
    const frameLog = [];
    // Guards against leaving the user with no control at all. The frame is an
    // extension document and load failure is close to unthinkable, but "close
    // to" is not a state worth shipping a dead button for.
    let retired = false;
    let loaded = false;
    const frame = document.createElement('iframe');
    // call.html, not the separate inline document: this one already carries the
    // context link and the page-following that the user just verified, while
    // inline-computer-voice.js has neither. One document in two forms beats two
    // documents drifting apart.
    frame.src = runtime.getURL('call.html?compact=1');
    frame.title = '电脑客户端桥接';
    frame.setAttribute('allow', 'microphone; autoplay');
    frame.setAttribute('aria-label', '电脑客户端桥接');
    frame.setAttribute('scrolling', 'no');
    const important = (name, value) => frame.style.setProperty(name, value, 'important');
    important('position', 'fixed');
    important('left', '-100px');
    important('top', '-100px');
    important('width', '42px');
    important('height', '42px');
    important('border', '0');
    important('border-radius', '12px');
    important('background', 'transparent');
    important('z-index', '2147483646');
    important('opacity', '0');
    important('pointer-events', 'none');

    let ready = false;
    let lastState = 'idle';

    const visibleButton = () => {
      // Looked up inside the shadow root, not the page document.
      //
      // The extension's whole UI lives in a shadow tree, and vendor code only
      // reaches it because build.py hands those files a Proxy in place of
      // `document`. This file is not wrapped that way, so plain
      // document.getElementById never found the button -- position() therefore
      // saw "no button", kept the frame hidden, and every press fell through to
      // opening a tab. That is why the embedded form appeared to do nothing.
      //
      // Read at call time: the shadow root is created further down this file,
      // long after this closure is built.
      const scope = window.__bwShadow || document;
      const candidates = [
        scope.getElementById
          ? scope.getElementById('asst-computer')
          : scope.querySelector('#asst-computer'),
        scope.getElementById
          ? scope.getElementById('vc-top-computer')
          : scope.querySelector('#vc-top-computer')
      ];
      for (const button of candidates) {
        if (!button || !button.isConnected) continue;
        const rect = button.getBoundingClientRect();
        const style = window.getComputedStyle(button);
        if (
          rect.width >= 20 && rect.height >= 20 &&
          rect.bottom > 0 && rect.right > 0 &&
          rect.top < window.innerHeight && rect.left < window.innerWidth &&
          style.display !== 'none' &&
          // visibility and opacity are not checked: once the frame takes over,
          // this button is hidden by us, and a hidden button still has to be
          // measurable or the frame would lose the place it stands in.
          true
        ) return { button, rect };
      }
      return null;
    };

    const position = () => {
      const found = visibleButton();
      // Shown as soon as there is a place for it, rather than after the frame
      // reports itself ready.
      //
      // Waiting for ready was the whole failure: a frame that could not finish
      // its checks stayed transparent and click-through, so every press landed
      // on the button underneath and opened a tab -- while looking, from the
      // outside, exactly as if nothing had been built at all. Nor could it say
      // why, since a frame nobody can press cannot be asked.
      //
      // Standing in for the button rather than covering it also settles the
      // layering question for good: the original is hidden, so there is no
      // second control left to compete for the press.
      if (!found || retired) {
        important('opacity', '0');
        important('pointer-events', 'none');
        important('left', '-100px');
        important('top', '-100px');
        return;
      }
      if (found.button.style.visibility !== 'hidden') {
        found.button.style.setProperty('visibility', 'hidden', 'important');
        found.button.setAttribute('data-bw-frame-standin', '1');
      }
      const rect = found.rect;
      important('left', `${Math.round(rect.left)}px`);
      important('top', `${Math.round(rect.top)}px`);
      important('width', `${Math.round(rect.width)}px`);
      important('height', `${Math.round(rect.height)}px`);
      important('border-radius', window.getComputedStyle(found.button).borderRadius || '12px');
      important('opacity', '1');
      important('pointer-events', 'auto');
      found.button.classList.toggle('on', lastState === 'active');
      found.button.classList.toggle('connecting', lastState === 'connecting');
    };

    window.addEventListener('message', (event) => {
      if (event.source !== frame.contentWindow) return;
      const data = event.data;
      if (!data || data.contract !== CONTRACT || typeof data.type !== 'string') return;
      if (data.type === 'ready') {
        ready = !!(data.value && data.value.ok === true);
        position();
        return;
      }
      // Kept because the frame's own detail panel is hidden in compact form.
      // A failure that says only "connection failed" costs another build to
      // diagnose; the lines leading up to it usually name the cause outright.
      if (data.type === 'log') {
        const line = data.value && data.value.line;
        if (line) {
          frameLog.push(String(line));
          if (frameLog.length > 6) frameLog.shift();
          try { console.log('[bw-voice-frame]', line); } catch (_) {}
        }
        return;
      }
      if (data.type !== 'state' || !data.value) return;
      lastState = String(data.value.state || 'idle');
      const message = String(data.value.message || '');
      const found = visibleButton();
      if (found && message) {
        found.button.title = message;
        found.button.setAttribute('aria-label', message);
      }
      if (lastState === 'failed') {
        const full = [message].concat(frameLog).filter(Boolean).join(String.fromCharCode(10));
        if (full) { try { window.RC && RC.toast && RC.toast(full); } catch (_) {} }
      }
      position();
    });

    // Placed inside the shadow tree, alongside the button it covers.
    //
    // On the page document it lost every click: the shadow host carries
    // z-index 2147483647 -- the maximum -- and the frame could only reach
    // 2147483646, so the host sat on top and every press went to the original
    // button. Raising the frame is impossible; joining the same tree is not.
    //
    // Moved rather than placed once, because this code runs long before the
    // shadow root is created further down this file. Attaching to the page
    // document first and relocating on the first sighting keeps both orders
    // valid; the Reader's own pages have no shadow and simply stay put.
    const attach = () => {
      const target = window.__bwShadow;
      if (target && frame.parentNode !== target) {
        target.appendChild(frame);
        return true;
      }
      return false;
    };
    frame.addEventListener('load', () => { loaded = true; });
    // Retires the frame if it never loads, restoring the original button so the
    // user is not left pressing a blank square. Generous, because a cold
    // extension page on a busy tab can take a moment.
    window.setTimeout(() => {
      if (loaded) return;
      retired = true;
      const found = visibleButton();
      if (found) found.button.style.removeProperty('visibility');
      position();
    }, 8000);

    (document.documentElement || document.body).appendChild(frame);
    if (!attach()) {
      const relocate = window.setInterval(() => {
        if (attach()) window.clearInterval(relocate);
      }, 200);
      window.addEventListener('pagehide', () => window.clearInterval(relocate), { once: true });
    }
    window.addEventListener('resize', position, { passive: true });
    window.addEventListener('scroll', position, { passive: true, capture: true });
    const timer = window.setInterval(position, 250);
    window.addEventListener('pagehide', () => {
      retired = true;
      window.clearInterval(timer);
    }, { once: true });
    // Exposed so the shared layer can tell whether this surface took the click.
    // When it is not ready the frame stays invisible and click-through, the
    // press lands on the original button, and the module opens a tab instead --
    // the fallback needs no coordination beyond knowing which happened.
    return Object.freeze({
      position,
      isReady: function () { return ready; },
      // Isolated-world closure capability: content.js can claim only the exact
      // frame created above. A host page can clone DOM attributes or insert a
      // competing web-accessible call.html, but it cannot manufacture this
      // object identity or replace the closed-over reference.
      frameForClaim: function () {
        return !retired && frame.isConnected ? frame : null;
      },
    });
  })();
  window.__bwInlineComputerVoiceSurface = inlineComputerVoiceSurface;

  const nativeAgentVoiceBridge = (() => {
    const CONTRACT = 'bw-reader-native/1';
    const ACTIONS = new Set([
      'capabilities',
      'agent.status',
      'agent.toggle',
      'agent.events',
      'agent.command'
    ]);
    let available = false;
    let launchScheme = '';
    let cursor = 0;
    let pollTimer = null;
    let pollInFlight = null;
    let commandTail = Promise.resolve();
    let latestState = {
      phase: 'unavailable',
      active: false,
      busy: false,
      speaking: false
    };

    const requestId = () => {
      const bytes = new Uint8Array(18);
      crypto.getRandomValues(bytes);
      return Array.from(bytes, (value) =>
        value.toString(16).padStart(2, '0')
      ).join('');
    };
    const call = (action, details) => new Promise((resolve, reject) => {
      if (!ACTIONS.has(action)) {
        reject(Object.assign(new Error('BWReader App 原生语音请求无效'), {
          code: 'BW_NATIVE_APP_REQUEST_INVALID'
        }));
        return;
      }
      chrome.runtime.sendMessage(Object.assign({
        type: 'BW_NATIVE_APP_REQUEST',
        action,
        requestId: details?.requestId || requestId()
      }, details || {}), (response) => {
        const runtimeError = chrome.runtime.lastError;
        const data = response?.data;
        if (runtimeError || !response?.ok || !data?.ok) {
          reject(Object.assign(new Error(
            runtimeError?.message || data?.error || response?.error ||
            'BWReader App 原生语音暂时不可用'
          ), {
            code: data?.code || response?.code || 'BW_NATIVE_APP_UNAVAILABLE'
          }));
          return;
        }
        if (data.contract !== CONTRACT || data.action !== action) {
          reject(Object.assign(new Error('BWReader App 原生语音响应无效'), {
            code: 'BW_NATIVE_APP_RESPONSE_INVALID'
          }));
          return;
        }
        resolve(data);
      });
    });
    const dispatch = (event, payload) => {
      window.dispatchEvent(new CustomEvent('bw-native-agent-voice-event', {
        detail: { event, payload: payload || {} }
      }));
    };
    const publishState = (state) => {
      const value = state && typeof state === 'object' ? state : {};
      latestState = {
        phase: String(value.phase || 'idle'),
        active: value.active === true,
        busy: value.busy === true,
        speaking: value.speaking === true,
        detail: String(value.detail || '')
      };
      dispatch('state', Object.assign({}, latestState));
      schedulePoll();
    };
    const pollEvents = () => {
      if (!available || pollInFlight) return pollInFlight || Promise.resolve();
      pollInFlight = call('agent.events', { after: cursor }).then((value) => {
        for (const item of value.events || []) {
          cursor = Math.max(cursor, Number(item.sequence || 0));
          dispatch(item.event, item.payload || {});
          if (item.event === 'state') publishState(item.payload || {});
        }
        cursor = Math.max(cursor, Number(value.cursor || 0));
      }).finally(() => {
        pollInFlight = null;
        schedulePoll();
      });
      return pollInFlight;
    };
    const schedulePoll = () => {
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = null;
      if (!available || document.visibilityState === 'hidden') return;
      const delay = latestState.active || latestState.busy ? 350 : 2000;
      pollTimer = setTimeout(() => {
        pollTimer = null;
        pollEvents().catch(() => {});
      }, delay);
    };
    const refreshStatus = () => call('agent.status').then((value) => {
      publishState(value.state);
      return Object.assign({}, latestState);
    });
    const toggle = (command, context) => {
      const operation = command === 'stop' ? 'stop' : 'start';
      const id = requestId();
      const coldStart = operation === 'start' &&
        !latestState.active && !latestState.busy;
      publishState(Object.assign({}, latestState, {
        phase: operation === 'start' ? 'launching' : 'stopping',
        busy: true
      }));
      const details = { requestId: id, command: operation };
      if (operation === 'start') {
        details.webContext = context || nativeBridgeWebContext();
      }
      const result = call('agent.toggle', details).then((value) => {
        publishState(value.state);
        pollEvents().catch(() => {});
        return value;
      }).catch((error) => {
        publishState({
          phase: 'failed', active: false, busy: false, speaking: false,
          detail: error.message
        });
        throw error;
      });
      if (coldStart && launchScheme === 'bwreader') {
        try {
          window.location.assign(
            `${launchScheme}://native-agent?requestId=${encodeURIComponent(id)}`
          );
        } catch (_) {}
      }
      return result;
    };
    const command = (name, body) => {
      const details = { command: name };
      if (name === 'speak') {
        details.text = nativeBridgeTrimUtf8(body?.text, 32768);
        details.mood = nativeBridgeTrimUtf8(body?.mood, 256);
      }
      const operation = commandTail.then(() => call('agent.command', details));
      commandTail = operation.catch(() => {});
      return operation.then((value) => {
        if (value.state) publishState(value.state);
        return value;
      });
    };
    const post = (body) => {
      const action = String(body?.action || '');
      if (action === 'start') return toggle('start', nativeBridgeWebContext());
      if (action === 'stop') return toggle('stop');
      if (action === 'speak' || action === 'speak_done' || action === 'cancel') {
        return command(action, body);
      }
      return Promise.reject(Object.assign(new Error('原生语音命令无效'), {
        code: 'BW_NATIVE_AGENT_COMMAND_INVALID'
      }));
    };
    const installCompatibilityShim = () => {
      let webkit = window.webkit;
      if (!webkit || typeof webkit !== 'object') {
        webkit = {};
        try { window.webkit = webkit; } catch (_) { return false; }
      }
      let handlers = webkit.messageHandlers;
      if (!handlers || typeof handlers !== 'object') {
        handlers = {};
        try { webkit.messageHandlers = handlers; } catch (_) { return false; }
      }
      if (!handlers.bwNativeAgentVoice) {
        try {
          handlers.bwNativeAgentVoice = Object.freeze({
            postMessage(body) { post(body).catch(() => {}); }
          });
        } catch (_) { return false; }
      }
      window.__BW_NATIVE_AGENT_VOICE__ = true;
      window.dispatchEvent(new CustomEvent(
        'bw-native-agent-voice-capability',
        { detail: { available: true } }
      ));
      return true;
    };
    const initialize = () => call('capabilities').then((value) => {
      const actions = new Set(value.actions || []);
      if ([
        'agent.status', 'agent.toggle', 'agent.events', 'agent.command'
      ].some((action) => !actions.has(action)) || value.launchScheme !== 'bwreader') {
        throw Object.assign(new Error('BWReader App 原生 Realtime 能力不完整'), {
          code: 'BW_NATIVE_APP_CAPABILITY_MISSING'
        });
      }
      available = true;
      launchScheme = value.launchScheme;
      // Event files survive extension/background suspension.  Establish the
      // current cursor before exposing the bridge so a newly loaded page never
      // replays ASR/TTS events from an older conversation.
      return call('agent.events', { after: 0 }).then((history) => {
        cursor = Math.max(cursor, Number(history.cursor || 0));
        installCompatibilityShim();
        return refreshStatus();
      }).catch((error) => {
        available = false;
        throw error;
      });
    }).catch((error) => {
      available = false;
      window.__BW_NATIVE_AGENT_VOICE__ = false;
      throw error;
    });

    window.addEventListener('pageshow', () => {
      if (available) refreshStatus().then(() => pollEvents()).catch(() => {});
    });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && available) {
        refreshStatus().then(() => pollEvents()).catch(() => {});
      }
    });
    setTimeout(() => initialize().catch(() => {}), 0);
    return Object.freeze({
      available: () => available,
      state: () => Object.assign({}, latestState),
      refreshStatus,
      pollEvents,
      post
    });
  })();
  window.__bwNativeAgentVoiceExtensionBridge = nativeAgentVoiceBridge;
  const pageCardPresentationCall = (type, cid, value) => new Promise(
    (resolve, reject) => {
      chrome.runtime.sendMessage({ type, cid, value }, (response) => {
        const runtimeError = chrome.runtime.lastError;
        if (runtimeError || !response?.ok) {
          reject(new Error(
            runtimeError?.message ||
            response?.error ||
            '页面卡片大小无法保存'
          ));
        } else {
          resolve(response.data);
        }
      });
    }
  );
  // 页面 placement 的设备级呈现仓库。按 cid 的原子合并只在后台完成，
  // 内容脚本不能 get→merge→set 整张 map，也不依赖 PWA 账户是否已建立。
  window.__bwPageCardPresentation = Object.freeze({
    get: (cid) => pageCardPresentationCall(
      'BW_PAGE_CARD_PRESENTATION_GET',
      cid
    ),
    set: (cid, value) => pageCardPresentationCall(
      'BW_PAGE_CARD_PRESENTATION_SET',
      cid,
      value
    )
  });
  const VOCABULARY_STATE_PROTOCOL = 'bw-vocabulary-state/1';
  const vocabularyStateTransport = (() => {
    let port = null;
    let ready = null;
    let readyResolve = null;
    let readyReject = null;
    let scope = '';
    let sequence = 0;
    let reconnectTimer = null;
    let used = false;
    const pending = new Map();
    const changeListeners = new Set();
    const reconnectListeners = new Set();
    const invalidationListeners = new Set();
    const transportError = (message, code) => Object.assign(
      new Error(String(message || '扩展词汇状态仓库不可用')),
      { code: String(code || 'BW_VOCABULARY_STATE_TRANSPORT') }
    );
    const rejectPending = (error) => {
      for (const request of pending.values()) request.reject(error);
      pending.clear();
    };
    const scheduleReconnect = () => {
      if (reconnectTimer || !used) return;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect().catch(() => scheduleReconnect());
      }, 250);
    };
    const disconnect = (error, reconnect) => {
      const oldPort = port;
      port = null;
      if (scope) {
        for (const listener of [...invalidationListeners]) {
          try { listener({ scope, error }); } catch (_) {}
        }
      }
      const rejectReady = readyReject;
      readyReject = null;
      readyResolve = null;
      ready = null;
      if (rejectReady) rejectReady(error);
      rejectPending(error);
      try { oldPort?.disconnect(); } catch (_) {}
      if (reconnect) scheduleReconnect();
    };
    const connect = () => {
      used = true;
      if (ready) return ready;
      ready = new Promise((resolve, reject) => {
        readyResolve = resolve;
        readyReject = reject;
      });
      const candidate = chrome.runtime.connect({ name: 'bw-vocabulary-state' });
      port = candidate;
      candidate.onMessage.addListener((message) => {
        if (!message || message.protocol !== VOCABULARY_STATE_PROTOCOL) return;
        if (message.type === 'READY') {
          const nextScope = String(message.scope || '');
          if (!/^vstate-scope-v1-[a-f0-9]{64}$/.test(nextScope)) {
            disconnect(transportError(
              '扩展返回了无效的词汇账户作用域',
              'BW_VOCABULARY_STATE_SCOPE'
            ), false);
            return;
          }
          const previousScope = scope;
          scope = nextScope;
          const resolveReady = readyResolve;
          readyResolve = null;
          readyReject = null;
          if (resolveReady) resolveReady({ scope });
          if (previousScope) {
            for (const listener of [...reconnectListeners]) {
              try {
                listener({
                  scope,
                  previousScope,
                  changed: previousScope !== scope
                });
              } catch (_) {}
            }
          }
          return;
        }
        if (message.type === 'CHANGE') {
          for (const listener of [...changeListeners]) {
            try { listener(message.record); } catch (_) {}
          }
          return;
        }
        if (message.type === 'INVALIDATED' || message.type === 'ERROR') {
          disconnect(transportError(message.error || message.reason, message.code), true);
          return;
        }
        if (message.type !== 'RESULT' || !message.id) return;
        const request = pending.get(String(message.id));
        if (!request) return;
        pending.delete(String(message.id));
        if (message.ok) request.resolve(message.data);
        else request.reject(transportError(message.error, message.code));
      });
      candidate.onDisconnect.addListener(() => {
        if (port !== candidate) return;
        const runtimeError = chrome.runtime.lastError;
        disconnect(transportError(
          runtimeError?.message || '扩展词汇状态连接已断开'
        ), true);
      });
      return ready;
    };
    const call = (operation, payload) => connect().then(() => new Promise(
      (resolve, reject) => {
        if (!port) {
          reject(transportError('扩展词汇状态连接已断开'));
          return;
        }
        const id = `vstate-call-${++sequence}`;
        pending.set(id, { resolve, reject });
        try {
          port.postMessage({
            protocol: VOCABULARY_STATE_PROTOCOL,
            type: 'CALL',
            id,
            operation,
            payload: payload || {}
          });
        } catch (error) {
          pending.delete(id);
          reject(transportError(error?.message));
        }
      }
    ));
    return Object.freeze({
      identity: () => connect().then((value) => value.scope),
      list: (query) => call('LIST', { query: query || {} }),
      put: (record, mutationId) => call('PUT', { record, mutationId }),
      subscribe(listener) {
        if (typeof listener !== 'function') return () => {};
        changeListeners.add(listener);
        connect().catch(() => {});
        return () => changeListeners.delete(listener);
      },
      onReconnect(listener) {
        if (typeof listener !== 'function') return () => {};
        reconnectListeners.add(listener);
        connect().catch(() => {});
        return () => reconnectListeners.delete(listener);
      },
      onInvalidate(listener) {
        if (typeof listener !== 'function') return () => {};
        invalidationListeners.add(listener);
        return () => invalidationListeners.delete(listener);
      }
    });
  })();
  window.__bwVocabularyStateTransport = vocabularyStateTransport;

  // ── 网页便签本地仓库门面 ──────────────────────────────────────────────
  // documentId 是安全边界，不由网页组件猜测或提交：background 根据顶层
  // sender 计算后只在 READY 中返回。共享组件仍使用统一 anchor envelope；
  // create/patch 在这里核对 envelope 的 documentId 后，仅从传输副本剥掉它，
  // 再交给 background 重新注入可信身份。
  function createDocumentNotesTransport(environment) {
    const targetWindow = environment.window;
    const runtime = environment.chrome.runtime;
    const protocol = "bw-document-notes/1";
    const portName = "bw-document-notes";
    const maxReadyMismatchRetries = 4;
    const maxAutoReconnects = 8;
    const requestTimeoutMs = 15_000;
    const readyTimeoutMs = 8_000;
    const pending = new Map();
    const changeListeners = new Set();
    const invalidationListeners = new Set();
    let port = null;
    let readyPromise = null;
    let readyResolve = null;
    let readyReject = null;
    let readyTimer = null;
    let readyIdentity = null;
    let generation = 1;
    let sequence = 0;
    let reconnectTimer = null;
    let reconnectAttempts = 0;
    let used = false;

    const error = (message, code, details) => Object.assign(
      new Error(String(message || "扩展文档便签仓库不可用")),
      {
        code: String(code || "BW_DOCUMENT_NOTES_TRANSPORT"),
        details: details || null
      }
    );
    const isPlainObject = (value) => !!(
      value &&
      Object.prototype.toString.call(value) === "[object Object]"
    );
    const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
    const cloneValue = (value) => {
      if (typeof targetWindow.structuredClone === "function") {
        return targetWindow.structuredClone(value);
      }
      return JSON.parse(JSON.stringify(value));
    };
    const rejectIdentityFields = (value, label, allowDocumentId) => {
      if (!value || typeof value !== "object") return;
      for (const key of ["documentId", "url", "pageUrl"]) {
        if (allowDocumentId && key === "documentId") continue;
        if (own(value, key)) {
          throw error(
            `${label} 不能提供 ${key}`,
            "BW_DOCUMENT_NOTES_IDENTITY"
          );
        }
      }
    };
    const canonicalDocumentId = () => {
      let url;
      try { url = new URL(String(targetWindow.location.href || "")); }
      catch (_) { url = null; }
      if (
        !url ||
        !["http:", "https:"].includes(url.protocol) ||
        url.username ||
        url.password
      ) {
        throw error(
          "当前页面不能使用网页便签",
          "BW_DOCUMENT_NOTES_PAGE"
        );
      }
      url.hash = "";
      return "web:" + url.href;
    };
    let expectedDocumentId = canonicalDocumentId();

    const clearReadyTimer = () => {
      if (readyTimer == null) return;
      targetWindow.clearTimeout(readyTimer);
      readyTimer = null;
    };
    const rejectPending = (cause) => {
      for (const request of pending.values()) {
        targetWindow.clearTimeout(request.timer);
        let rejection = cause;
        if (request.mutationId) {
          rejection = error(
            cause.message,
            cause.code,
            Object.assign({}, cause.details || {}, {
              outcomeUnknown: true,
              mutationId: request.mutationId
            })
          );
        }
        request.reject(rejection);
      }
      pending.clear();
    };
    const notifyInvalidation = (reason, cause, previousIdentity) => {
      const event = Object.freeze({
        reason: String(reason || cause?.code || "document-context-stale"),
        error: cause,
        previousIdentity: previousIdentity
          ? Object.freeze({
              documentId: String(previousIdentity.documentId || ""),
              scope: String(previousIdentity.scope || "")
            })
          : null
      });
      for (const listener of [...invalidationListeners]) {
        try { listener(event); } catch (_) {}
      }
    };
    const clearReady = (cause) => {
      clearReadyTimer();
      const reject = readyReject;
      readyPromise = null;
      readyResolve = null;
      readyReject = null;
      readyIdentity = null;
      if (reject) reject(cause);
    };
    const closePort = () => {
      const oldPort = port;
      port = null;
      try { oldPort?.disconnect(); } catch (_) {}
    };
    const shouldReconnect = () => (
      used || changeListeners.size > 0 || invalidationListeners.size > 0
    );
    const scheduleReconnect = (shortDelay) => {
      if (
        reconnectTimer != null ||
        !shouldReconnect() ||
        reconnectAttempts >= maxAutoReconnects
      ) return;
      const attempt = reconnectAttempts++;
      const delay = shortDelay
        ? 80
        : Math.min(5_000, 150 * (2 ** attempt));
      const scheduledGeneration = generation;
      reconnectTimer = targetWindow.setTimeout(() => {
        reconnectTimer = null;
        if (scheduledGeneration !== generation) {
          scheduleReconnect(true);
          return;
        }
        connectWithRetry(scheduledGeneration, false).catch(() => {
          if (scheduledGeneration === generation) scheduleReconnect(false);
        });
      }, delay);
    };
    const invalidate = (reason, cause, reconnect, previousIdentityOverride) => {
      const previousIdentity = previousIdentityOverride || readyIdentity || {
        documentId: expectedDocumentId,
        scope: ""
      };
      const staleCause = cause?.code === "BW_DOCUMENT_NOTES_STALE"
        ? cause
        : error(
            cause?.message || "网页便签上下文已经失效",
            "BW_DOCUMENT_NOTES_STALE",
            Object.assign({}, cause?.details || {}, {
              reason: String(reason || ""),
              sourceCode: String(cause?.code || "")
            })
          );
      generation += 1;
      clearReady(staleCause);
      rejectPending(staleCause);
      closePort();
      if (reconnectTimer != null) {
        targetWindow.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      notifyInvalidation(reason, cause, previousIdentity);
      if (reconnect) scheduleReconnect(true);
    };
    const rejectHandshake = (candidate, cause) => {
      if (port !== candidate) return;
      port = null;
      clearReady(cause);
      try { candidate.disconnect(); } catch (_) {}
    };
    const startConnection = (capturedGeneration) => {
      if (capturedGeneration !== generation) {
        return Promise.reject(error(
          "网页便签调用属于已失效页面",
          "BW_DOCUMENT_NOTES_STALE"
        ));
      }
      if (readyPromise) return readyPromise;
      let candidate;
      try {
        candidate = runtime.connect({ name: portName });
      } catch (cause) {
        return Promise.reject(error(
          cause?.message || "无法建立扩展网页便签连接",
          "BW_DOCUMENT_NOTES_DISCONNECTED"
        ));
      }
      readyPromise = new Promise((resolve, reject) => {
        readyResolve = resolve;
        readyReject = reject;
      });
      port = candidate;
      const candidateGeneration = capturedGeneration;
      readyTimer = targetWindow.setTimeout(() => {
        if (port !== candidate || candidateGeneration !== generation) return;
        rejectHandshake(candidate, error(
          "等待网页便签仓库 READY 超时",
          "BW_DOCUMENT_NOTES_READY_TIMEOUT"
        ));
      }, readyTimeoutMs);
      candidate.onMessage.addListener((message) => {
        if (
          port !== candidate ||
          candidateGeneration !== generation ||
          !message ||
          message.protocol !== protocol
        ) return;
        if (message.type === "READY") {
          let currentExpected;
          try { currentExpected = canonicalDocumentId(); }
          catch (cause) {
            invalidate("page-unavailable", cause, false);
            return;
          }
          const documentId = String(message.documentId || "");
          const scope = String(message.scope || "");
          if (currentExpected !== expectedDocumentId) {
            checkLocation("ready-location-changed");
            return;
          }
          if (documentId !== expectedDocumentId) {
            rejectHandshake(candidate, error(
              "扩展返回的文档身份与当前页面不一致",
              "BW_DOCUMENT_NOTES_READY_MISMATCH",
              { expectedDocumentId, documentId }
            ));
            return;
          }
          if (!/^document-notes-scope-v1-[a-f0-9]{64}$/.test(scope)) {
            rejectHandshake(candidate, error(
              "扩展返回了无效的便签账户作用域",
              "BW_DOCUMENT_NOTES_SCOPE"
            ));
            return;
          }
          clearReadyTimer();
          readyIdentity = Object.freeze({ documentId, scope });
          reconnectAttempts = 0;
          const resolve = readyResolve;
          readyResolve = null;
          readyReject = null;
          if (resolve) resolve(readyIdentity);
          return;
        }
        if (message.type === "CHANGE") {
          if (!readyIdentity) return;
          const event = message.data;
          // 不变换 operation/cursor/rev；只校验 CHANGE 仍属于 READY 绑定的文档。
          if (
            !event ||
            !event.note ||
            event.note.documentId !== readyIdentity.documentId
          ) {
            invalidate(
              "change-document-mismatch",
              error(
                "便签变更属于其它文档",
                "BW_DOCUMENT_NOTES_CHANGE_IDENTITY"
              ),
              true
            );
            return;
          }
          for (const listener of [...changeListeners]) {
            try { listener(event); } catch (_) {}
          }
          return;
        }
        if (
          message.type === "ERROR" &&
          !readyIdentity &&
          [
            "BW_DOCUMENT_NOTES_SENDER",
            "BW_DOCUMENT_NOTES_STALE"
          ].includes(String(message.code || ""))
        ) {
          // Chromium 在 SPA pushState 后可能先更新 content location，再稍晚更新
          // sender.tab.url。此时 background 正确拒绝旧身份；把它视为一次 READY
          // mismatch，在同一页面 generation 内短暂重试，不能把 mutation 重发。
          rejectHandshake(candidate, error(
            message.error || "后台文档身份尚未更新",
            "BW_DOCUMENT_NOTES_READY_MISMATCH",
            { backgroundCode: String(message.code || "") }
          ));
          return;
        }
        if (message.type === "INVALIDATED" || message.type === "ERROR") {
          invalidate(
            message.reason || message.type.toLowerCase(),
            error(
              message.error || message.reason,
              message.code,
              message.details
            ),
            true
          );
          return;
        }
        if (message.type !== "RESULT" || !message.id) return;
        const id = String(message.id);
        const request = pending.get(id);
        if (!request) return;
        pending.delete(id);
        targetWindow.clearTimeout(request.timer);
        if (request.generation !== generation) {
          request.reject(error(
            "网页便签结果属于已失效页面",
            "BW_DOCUMENT_NOTES_STALE"
          ));
        } else if (message.ok) {
          request.resolve(message.data);
        } else {
          request.reject(error(message.error, message.code, message.details));
        }
      });
      candidate.onDisconnect.addListener(() => {
        if (port !== candidate || candidateGeneration !== generation) return;
        const runtimeError = runtime.lastError;
        invalidate(
          "port-disconnected",
          error(
            runtimeError?.message || "扩展网页便签连接已断开",
            "BW_DOCUMENT_NOTES_DISCONNECTED"
          ),
          true
        );
      });
      return readyPromise;
    };
    async function connectWithRetry(capturedGeneration, explicit) {
      used = true;
      if (explicit) {
        reconnectAttempts = 0;
        if (reconnectTimer != null) {
          targetWindow.clearTimeout(reconnectTimer);
          reconnectTimer = null;
        }
      }
      let mismatch = null;
      for (let attempt = 0; attempt < maxReadyMismatchRetries; attempt += 1) {
        // content script lives in an isolated world. A SPA's main-world
        // history.pushState/replaceState does not have to pass through the
        // wrappers installed below, so every actual connection attempt must
        // re-read the live Location rather than trusting those wrappers.
        checkLocation("connect");
        if (capturedGeneration !== generation) {
          throw error(
            "网页便签调用属于已失效页面",
            "BW_DOCUMENT_NOTES_STALE"
          );
        }
        try {
          return await startConnection(capturedGeneration);
        } catch (cause) {
          mismatch = cause;
          if (
            cause?.code !== "BW_DOCUMENT_NOTES_READY_MISMATCH" ||
            attempt + 1 >= maxReadyMismatchRetries
          ) throw cause;
          await new Promise((resolve) => {
            targetWindow.setTimeout(resolve, 80 * (attempt + 1));
          });
        }
      }
      throw mismatch || error(
        "网页便签连接重试次数已用尽",
        "BW_DOCUMENT_NOTES_RECONNECT_LIMIT"
      );
    }
    const call = async (operation, makePayload) => {
      checkLocation("api-call-before-generation");
      const capturedGeneration = generation;
      checkLocation("api-call-before-connect");
      if (capturedGeneration !== generation) {
        throw error(
          "网页便签调用属于已失效页面",
          "BW_DOCUMENT_NOTES_STALE"
        );
      }
      const identity = await connectWithRetry(capturedGeneration, true);
      checkLocation("api-call-after-connect");
      if (capturedGeneration !== generation || identity !== readyIdentity) {
        throw error(
          "网页便签调用属于已失效页面",
          "BW_DOCUMENT_NOTES_STALE"
        );
      }
      const payload = makePayload ? makePayload(identity) : {};
      // Payload cloning/validation can execute page-provided accessors. Check
      // once more immediately before selecting and writing to the port so a
      // navigation during payload preparation can never leak a CALL to the
      // old document connection.
      checkLocation("api-call-before-send");
      if (capturedGeneration !== generation || identity !== readyIdentity) {
        throw error(
          "网页便签调用属于已失效页面",
          "BW_DOCUMENT_NOTES_STALE"
        );
      }
      const activePort = port;
      if (!activePort) {
        throw error(
          "扩展网页便签连接已断开",
          "BW_DOCUMENT_NOTES_DISCONNECTED"
        );
      }
      return new Promise((resolve, reject) => {
        const id = `document-notes-call-${capturedGeneration}-${++sequence}`;
        const mutationId = String(payload?.options?.mutationId || "");
        const timer = targetWindow.setTimeout(() => {
          const request = pending.get(id);
          if (!request) return;
          pending.delete(id);
          request.reject(error(
            "等待网页便签操作结果超时",
            "BW_DOCUMENT_NOTES_RESULT_TIMEOUT",
            mutationId ? { outcomeUnknown: true, mutationId } : null
          ));
        }, requestTimeoutMs);
        pending.set(id, {
          generation: capturedGeneration,
          mutationId,
          resolve,
          reject,
          timer
        });
        try {
          activePort.postMessage({
            protocol,
            type: "CALL",
            id,
            operation,
            payload
          });
        } catch (cause) {
          pending.delete(id);
          targetWindow.clearTimeout(timer);
          reject(error(cause?.message, "BW_DOCUMENT_NOTES_DISCONNECTED"));
        }
      });
    };
    const prepareAnchor = (anchor, identity, label) => {
      if (!isPlainObject(anchor)) {
        throw error(`${label} 必须是普通对象`, "BW_DOCUMENT_NOTES_PAYLOAD");
      }
      rejectIdentityFields(anchor, label, true);
      if (
        !own(anchor, "documentId") ||
        String(anchor.documentId || "") !== identity.documentId
      ) {
        throw error(
          `${label}.documentId 与当前 READY 文档不一致`,
          "BW_DOCUMENT_NOTES_IDENTITY"
        );
      }
      const copy = cloneValue(anchor);
      delete copy.documentId;
      return copy;
    };
    const prepareCreate = (input, options, identity) => {
      if (!isPlainObject(input)) {
        throw error("create.input 必须是普通对象", "BW_DOCUMENT_NOTES_PAYLOAD");
      }
      rejectIdentityFields(input, "create.input", true);
      if (
        !own(input, "documentId") ||
        String(input.documentId || "") !== identity.documentId
      ) {
        throw error(
          "create.input.documentId 与当前 READY 文档不一致",
          "BW_DOCUMENT_NOTES_IDENTITY"
        );
      }
      rejectIdentityFields(options, "create.options", false);
      const copy = cloneValue(input);
      delete copy.documentId;
      copy.anchor = prepareAnchor(input.anchor, identity, "create.input.anchor");
      return { input: copy, options: cloneValue(options || {}) };
    };
    const preparePatch = (noteId, changes, options, identity) => {
      if (!isPlainObject(changes)) {
        throw error("patch.changes 必须是普通对象", "BW_DOCUMENT_NOTES_PAYLOAD");
      }
      rejectIdentityFields(changes, "patch.changes", false);
      rejectIdentityFields(options, "patch.options", false);
      const copy = cloneValue(changes);
      if (own(changes, "anchor")) {
        copy.anchor = prepareAnchor(
          changes.anchor,
          identity,
          "patch.changes.anchor"
        );
      }
      return {
        noteId: String(noteId || ""),
        changes: copy,
        options: cloneValue(options || {})
      };
    };
    const checkLocation = (reason) => {
      let nextDocumentId;
      try { nextDocumentId = canonicalDocumentId(); }
      catch (cause) {
        invalidate(reason, cause, false);
        return;
      }
      if (nextDocumentId === expectedDocumentId) return;
      const previousDocumentId = expectedDocumentId;
      expectedDocumentId = nextDocumentId;
      reconnectAttempts = 0;
      invalidate(
        reason,
        error(
          "网页文档身份已经变化",
          "BW_DOCUMENT_NOTES_STALE",
          { previousDocumentId, documentId: nextDocumentId }
        ),
        true,
        {
          documentId: previousDocumentId,
          scope: readyIdentity?.scope || ""
        }
      );
    };
    for (const method of ["pushState", "replaceState"]) {
      const original = targetWindow.history?.[method];
      if (typeof original !== "function") continue;
      try {
        targetWindow.history[method] = function (...args) {
          const result = Reflect.apply(original, this, args);
          checkLocation(`history.${method}`);
          return result;
        };
      } catch (_) {}
    }
    targetWindow.addEventListener("popstate", () => {
      checkLocation("popstate");
    });
    targetWindow.addEventListener("hashchange", () => {
      checkLocation("hashchange");
    });
    try {
      targetWindow.navigation?.addEventListener(
        "currententrychange",
        () => checkLocation("navigation.currententrychange")
      );
    } catch (_) {}
    // Content scripts and the page run in different JS worlds. On browsers
    // without Navigation API, a main-world pushState that does not mutate the
    // DOM cannot reach the wrappers or observers in this world. Keep a
    // deliberately low-frequency identity fence after the repository is used;
    // API calls still perform their synchronous checks before every send.
    const locationPollMs = Math.max(
      250,
      Number(environment.locationPollMs) || 1000
    );
    const locationPollTimer = targetWindow.setInterval?.(() => {
      if (used) checkLocation("location-poll");
    }, locationPollMs);
    // Node contract tests use real timers; do not keep their process alive.
    locationPollTimer?.unref?.();

    const api = {
      identity: () => {
        checkLocation("identity-before-generation");
        const capturedGeneration = generation;
        checkLocation("identity-before-connect");
        if (capturedGeneration !== generation) {
          return Promise.reject(error(
            "网页便签调用属于已失效页面",
            "BW_DOCUMENT_NOTES_STALE"
          ));
        }
        return connectWithRetry(capturedGeneration, true).then((identity) => {
          checkLocation("identity-after-connect");
          if (
            capturedGeneration !== generation ||
            identity !== readyIdentity
          ) {
            throw error(
              "网页便签调用属于已失效页面",
              "BW_DOCUMENT_NOTES_STALE"
            );
          }
          return {
            documentId: identity.documentId,
            scope: identity.scope
          };
        });
      },
      newId: () => call("NEW_ID", () => ({})),
      list: (query) => call("LIST", () => {
        rejectIdentityFields(query, "list.query", false);
        return { query: cloneValue(query || {}) };
      }),
      get: (noteId, query) => call("GET", () => {
        rejectIdentityFields(query, "get.query", false);
        return {
          noteId: String(noteId || ""),
          query: cloneValue(query || {})
        };
      }),
      create: (input, options) => call(
        "CREATE",
        (identity) => prepareCreate(input, options, identity)
      ),
      patch: (noteId, changes, options) => call(
        "PATCH",
        (identity) => preparePatch(noteId, changes, options, identity)
      ),
      remove: (noteId, options) => call("REMOVE", () => {
        rejectIdentityFields(options, "remove.options", false);
        return {
          noteId: String(noteId || ""),
          options: cloneValue(options || {})
        };
      }),
      subscribe(listener) {
        if (typeof listener !== "function") return () => {};
        changeListeners.add(listener);
        checkLocation("subscribe");
        const capturedGeneration = generation;
        connectWithRetry(capturedGeneration, true).catch(() => {
          if (capturedGeneration === generation) scheduleReconnect(false);
        });
        return () => changeListeners.delete(listener);
      },
      onInvalidate(listener) {
        if (typeof listener !== "function") return () => {};
        invalidationListeners.add(listener);
        checkLocation("onInvalidate");
        const capturedGeneration = generation;
        connectWithRetry(capturedGeneration, true).catch(() => {
          if (capturedGeneration === generation) scheduleReconnect(false);
        });
        return () => invalidationListeners.delete(listener);
      }
    };
    return Object.freeze(api);
  }
  window.__bwDocumentNotes = createDocumentNotesTransport({
    window,
    chrome
  });

  window.__bwTranslationCacheGet = (cacheNamespace) => new Promise(
    (resolve, reject) => {
      chrome.runtime.sendMessage({
        type: 'BW_TRANSLATION_CACHE_GET',
        cacheNamespace
      }, (response) => {
        const runtimeError = chrome.runtime.lastError;
        if (runtimeError || !response?.ok) {
          reject(new Error(
            runtimeError?.message ||
            response?.error ||
            '扩展译文缓存不可用'
          ));
        } else {
          resolve(response.data || { items: {}, cacheNamespace });
        }
      });
    }
  );

  // ── Shadow 宿主:全视口但自身穿透；真正的扩展控件在 #bw-root 直系层恢复命中。
  // 旧 0×0 宿主让 fixed 侧栏大多可点，却会令底部收藏面板在 Chromium 命中测试中时有时无。
  const host = document.createElement("div");
  host.id = "bw-reader-host";
  host.style.cssText = "position:fixed;inset:0;width:auto;height:auto;z-index:2147483647;pointer-events:none;";
  document.documentElement.appendChild(host);
  const shadow = host.attachShadow({ mode: "open" });

  // 样式落点(充当 document.head 角色)。样式放 shadow 内任意位置都作用于整棵 shadow 树。
  const headEl = document.createElement("div");
  headEl.id = "bw-head";
  shadow.appendChild(headEl);

  // 根容器(充当 document.body / documentElement 双角色):
  //   · rc-sidedrawer 往 body 挂抽屉/把手、往 body toggle .ep-side-open 类 → 落这里;
  //   · rc-sidedrawer 往 documentElement 设 --gp-blur CSS 变量 → 落这里(变量继承到抽屉)。
  const root = document.createElement("div");
  root.id = "bw-root";
  root.style.cssText = "position:fixed;inset:0;pointer-events:none;";
  shadow.appendChild(root);

  // 网页卡片专用的文档坐标层：宿主页滚动时由浏览器原生带着走，不再在 fixed UI 层里逐帧追位置。
  // 单独 shadow 保持样式隔离；共享层后续注入的 style 会镜像进来。
  const pinHost = document.createElement("div");
  pinHost.id = "bw-reader-pins";
  pinHost.style.cssText = "position:absolute;left:0;top:0;width:100%;height:0;z-index:2147483646;pointer-events:none;overflow:visible;";
  document.documentElement.appendChild(pinHost);
  const pinShadow = pinHost.attachShadow({ mode: "open" });
  const pinHead = document.createElement("div"); pinHead.id = "bw-pin-head"; pinShadow.appendChild(pinHead);
  const pinRoot = document.createElement("div"); pinRoot.id = "bw-pin-root";
  pinRoot.style.cssText = "position:absolute;left:0;top:0;width:100%;height:0;overflow:visible;pointer-events:none;";
  pinShadow.appendChild(pinRoot);
  let pinStyleRaf = 0;
  const syncPinStyles = () => {
    pinStyleRaf = 0; pinHead.replaceChildren();
    headEl.querySelectorAll("style").forEach((s) => pinHead.appendChild(s.cloneNode(true)));
  };
  new MutationObserver(() => { if (!pinStyleRaf) pinStyleRaf = requestAnimationFrame(syncPinStyles); })
    .observe(headEl, { childList: true, subtree: true, characterData: true });

  // rc-result 是阅读器共享逻辑，按设计复用宿主提供的固定 DOM 骨架；普通网页没有模板，
  // 因此在 vendor 加载前补齐同一组 id（样式仍由 rc-result 原件注入）。
  const resultChrome = document.createElement("div");
  resultChrome.id = "bw-result-chrome";
  resultChrome.innerHTML = `
    <div id="result-mask"><div id="result-modal">
      <h3 id="result-title">结果</h3><div class="src" id="result-src"></div><div class="content" id="result-content"></div>
      <div id="result-followup"><input id="result-followup-input" placeholder="继续追问…（基于上面的解释）"><button data-bw-call="_followupAsk">追问</button></div>
      <div class="actions"><div id="vocab-actions"></div><button id="result-mark-btn" data-bw-call="markFromResult">🖌 标记</button><button id="result-anki-btn" data-bw-call="ankiFromResult">🎴 制 Anki</button><button data-bw-call="closeResult">关闭</button></div>
    </div></div>
    <div id="draft-badge" title="已选段落"><div style="display:flex;flex-direction:column;align-items:center;line-height:1.1"><span class="icon">📋</span><span class="count" id="draft-count">0</span></div></div>
    <div id="draft-mask"><div id="draft-modal"><h3><span>📋 已选段落 <span id="draft-modal-count"></span></span><button class="clear-all" data-bw-call="clearAllDrafts">全部清空</button></h3><div id="draft-list">…</div><div id="draft-actions"><button class="primary" data-bw-call="createNoteFromDrafts">📝 创建笔记</button><button data-bw-call="createAnkiFromDrafts">🎴 创建 Anki</button><button data-bw-call="createBothFromDrafts">📚 笔记 + Anki</button></div></div></div>`;
  root.appendChild(resultChrome);
  resultChrome.addEventListener("click", (e) => {
    if (e.target === resultChrome.querySelector("#result-mask")) { try { window.closeResult && window.closeResult(); } catch (_) {} return; }
    if (e.target === resultChrome.querySelector("#draft-mask")) { try { window.closeDraftModal && window.closeDraftModal(); } catch (_) {} return; }
    if (e.target.closest && e.target.closest("#draft-badge")) { try { window.openDraftModal && window.openDraftModal(); } catch (_) {} return; }
    const b = e.target.closest && e.target.closest("[data-bw-call]");
    if (b) { const fn = window[b.dataset.bwCall]; if (typeof fn === "function") { e.preventDefault(); fn(); } }
  });
  resultChrome.querySelector("#result-followup-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); try { window._followupAsk && window._followupAsk(); } catch (_) {} }
  });

  // ── 事件包装:e.target 换成 composedPath()[0](shadow 内真实目标),其余透传 ──
  const wrapHandler = (fn) => {
    const wrapped = (e) => {
      const proxied = new Proxy(e, {
        get(t, p) {
          if (p === "target") {
            try { const cp = e.composedPath(); if (cp && cp.length) return cp[0]; } catch (_) {}
            return e.target;
          }
          const v = t[p];
          return typeof v === "function" ? v.bind(t) : v;
        }
      });
      return fn.call(document, proxied);
    };
    return wrapped;
  };
  // 原始 fn 可能同时绑定 pointerup/pointercancel。必须按 type+capture 分开保存；只按 fn 保存会让
  // 后一次注册覆盖前一次，旧 pointerup 永远移不掉，之后每次点击都会重复执行上一次放置。
  const handlerMap = new WeakMap();   // fn → Map("type|capture" → wrapped)
  const handlerKey = (type, opts) => type + "|" + ((opts === true || (opts && opts.capture)) ? "1" : "0");
  const mappedHandler = (fn, type, opts, create) => {
    let m = handlerMap.get(fn);
    if (!m && create) { m = new Map(); handlerMap.set(fn, m); }
    if (!m) return null;
    const k = handlerKey(type, opts); let w = m.get(k);
    if (!w && create) { w = wrapHandler(fn); m.set(k, w); }
    return w || null;
  };

  // ── document 门面:重定向 DOM 根,其余(createElement/createTextNode/…)透传真 document ──
  window.__bwReaderDoc = new Proxy(document, {
    get(t, p) {
      if (p === "getElementById") return (id) => shadow.getElementById(id);
      if (p === "querySelector") return (s) => shadow.querySelector(s);
      if (p === "querySelectorAll") return (s) => shadow.querySelectorAll(s);
      if (p === "head") return headEl;
      if (p === "body") return root;
      if (p === "documentElement") return root;
      if (p === "addEventListener") {
        return (type, fn, opts) => {
          const w = mappedHandler(fn, type, opts, true);
          document.addEventListener(type, w, opts);
        };
      }
      if (p === "removeEventListener") {
        return (type, fn, opts) => document.removeEventListener(type, mappedHandler(fn, type, opts, false) || fn, opts);
      }
      const v = t[p];
      return typeof v === "function" ? v.bind(t) : v;
    },
    set(t, p, v) { t[p] = v; return true; }
  });

  // ── fetch 门面:vendor 包装把 rc-* 的 fetch 一并遮蔽成这个 ──
  // rc-* 全部用相对路径(/pdf/api/*、/api/assistant/*…)→ 重写到 Pi ORIGIN;
  // 跨源 + Bearer + SSE 统一走 background 长连 port(content script 的 fetch 受宿主页 CORS 限制,
  // background 有 host_permissions 才能带 Bearer 直连)。流式响应用 ReadableStream 原样重建,
  // rc-assistant 的 getReader() 打字机 / rid 续传语义不变。非本服务的绝对 URL(如词典音频)走原生 fetch。
  // 共享阅读器语音层仍按“同源 /voice-rt”组织；普通网页的同源是宿主网站，
  // 因此由宿主适配层只提供一次服务地址解析，rc-voicecall 继续保持唯一实现。
  // PWA 未提供这个 hook 时仍使用它自己的 location.host。
  window.__bwReaderWsUrl = (path) => {
    const url = new URL(String(path || "/voice-rt"), ORIGIN);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return url.href;
  };
  const BW_WS_CONNECTING = 0;
  const BW_WS_OPEN = 1;
  const BW_WS_CLOSING = 2;
  const BW_WS_CLOSED = 3;
  const MAX_BRIDGED_WS_FRAME_BYTES = 8 * 1024 * 1024;
  const MAX_BRIDGED_WS_FRAME_B64_CHARS =
    4 * Math.ceil(MAX_BRIDGED_WS_FRAME_BYTES / 3);
  const bytesToBase64 = (bytes) => {
    const parts = [];
    for (let i = 0; i < bytes.length; i += 0x8000) {
      parts.push(String.fromCharCode.apply(
        null,
        bytes.subarray(i, i + 0x8000)
      ));
    }
    return btoa(parts.join(""));
  };
  const base64ToBuffer = (value, declaredBytes) => {
    const encoded = String(value || "");
    const declared = Number(declaredBytes);
    if (
      !Number.isSafeInteger(declared) ||
      declared < 0 ||
      declared > MAX_BRIDGED_WS_FRAME_BYTES ||
      encoded.length > MAX_BRIDGED_WS_FRAME_B64_CHARS ||
      encoded.length % 4 !== 0 ||
      !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)
    ) {
      throw new TypeError("invalid BW WebSocket binary frame");
    }
    const raw = atob(encoded);
    if (raw.length !== declared) {
      throw new TypeError("BW WebSocket frame size mismatch");
    }
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes.buffer;
  };
  const bridgedWsEvent = (type, details) => {
    try {
      if (type === "message") {
        return new MessageEvent(type, { data: details?.data });
      }
      if (type === "close") {
        return new CloseEvent(type, {
          code: Number(details?.code || 0),
          reason: String(details?.reason || ""),
          wasClean: !!details?.wasClean
        });
      }
      return new Event(type);
    } catch (_) {
      return Object.assign({ type }, details || {});
    }
  };
  // 普通网页的 CSP 会把 content script 直接发出的跨站 WebSocket 一并拦掉。
  // 这里仅实现浏览器 WebSocket 的最小同接口；真正连接在 background 中建立。
  // 共享 rc-voicecall.js 只看这个接口，所以 PWA/扩展仍共用同一份侧栏和语音状态机。
  window.__bwReaderOpenWebSocket = (path) => {
    const port = chrome.runtime.connect({ name: "bw-ws" });
    const listeners = new Map();
    let readyState = BW_WS_CONNECTING;
    let binaryType = "blob";
    let terminal = false;
    // Chrome 114+ 只“打开”长连 port 不足以维持 MV3 worker；20 秒一条小消息同时
    // 让 port 与 Chrome 116+ 的后台 WebSocket 生命周期保持活跃。
    const heartbeat = setInterval(() => {
      if (terminal) return;
      try { port.postMessage({ type: "ping" }); } catch (_) {}
    }, 20_000);
    const socket = {
      onopen: null,
      onmessage: null,
      onerror: null,
      onclose: null,
      get url() { return window.__bwReaderWsUrl(path); },
      get protocol() { return ""; },
      get extensions() { return ""; },
      get bufferedAmount() { return 0; },
      get readyState() { return readyState; },
      get binaryType() { return binaryType; },
      set binaryType(value) {
        if (value === "arraybuffer" || value === "blob") binaryType = value;
      },
      addEventListener(type, listener) {
        if (typeof listener !== "function" && !listener?.handleEvent) return;
        const key = String(type || "");
        if (!listeners.has(key)) listeners.set(key, new Set());
        listeners.get(key).add(listener);
      },
      removeEventListener(type, listener) {
        listeners.get(String(type || ""))?.delete(listener);
      },
      send(data) {
        if (readyState !== BW_WS_OPEN) {
          throw new DOMException(
            "WebSocket is not open",
            "InvalidStateError"
          );
        }
        if (typeof data === "string") {
          port.postMessage({ type: "send", data });
          return;
        }
        let bytes = null;
        if (data instanceof ArrayBuffer) {
          bytes = new Uint8Array(data);
        } else if (ArrayBuffer.isView(data)) {
          bytes = new Uint8Array(
            data.buffer,
            data.byteOffset,
            data.byteLength
          );
        }
        if (!bytes) {
          throw new TypeError("BW WebSocket bridge only accepts text or binary buffers");
        }
        if (bytes.byteLength > MAX_BRIDGED_WS_FRAME_BYTES) {
          throw new TypeError("BW WebSocket frame exceeds 8 MiB");
        }
        port.postMessage({
          type: "send",
          binary: true,
          bytes: bytes.byteLength,
          b64: bytesToBase64(bytes)
        });
      },
      close(code, reason) {
        if (readyState === BW_WS_CLOSED || readyState === BW_WS_CLOSING) return;
        readyState = BW_WS_CLOSING;
        try {
          port.postMessage({
            type: "close",
            code: code == null ? undefined : Number(code),
            reason: reason == null ? "" : String(reason)
          });
        } catch (_) {}
      }
    };
    Object.defineProperties(socket, {
      CONNECTING: { value: BW_WS_CONNECTING },
      OPEN: { value: BW_WS_OPEN },
      CLOSING: { value: BW_WS_CLOSING },
      CLOSED: { value: BW_WS_CLOSED }
    });
    const dispatch = (type, details) => {
      const event = bridgedWsEvent(type, details);
      const property = socket["on" + type];
      if (typeof property === "function") {
        try { property.call(socket, event); } catch (error) {
          queueMicrotask(() => { throw error; });
        }
      }
      for (const listener of listeners.get(type) || []) {
        try {
          if (typeof listener === "function") listener.call(socket, event);
          else listener.handleEvent(event);
        } catch (error) {
          queueMicrotask(() => { throw error; });
        }
      }
    };
    port.onMessage.addListener((message) => {
      if (!message || terminal) return;
      if (message.type === "open") {
        if (readyState !== BW_WS_CONNECTING) return;
        readyState = BW_WS_OPEN;
        dispatch("open");
        return;
      }
      if (message.type === "message") {
        if (readyState !== BW_WS_OPEN) return;
        let data = message.data;
        if (message.binary) {
          try {
            const buffer = base64ToBuffer(message.b64, message.bytes);
            data = binaryType === "arraybuffer"
              ? buffer
              : new Blob([buffer]);
          } catch (_) {
            dispatch("error");
            return;
          }
        }
        dispatch("message", { data });
        return;
      }
      if (message.type === "error") {
        dispatch("error", { error: String(message.error || "") });
        return;
      }
      if (message.type === "close") {
        terminal = true;
        clearInterval(heartbeat);
        readyState = BW_WS_CLOSED;
        dispatch("close", {
          code: Number(message.code || 0),
          reason: String(message.reason || ""),
          wasClean: !!message.wasClean
        });
        try { port.disconnect(); } catch (_) {}
      }
    });
    port.onDisconnect.addListener(() => {
      if (terminal) return;
      terminal = true;
      clearInterval(heartbeat);
      readyState = BW_WS_CLOSED;
      dispatch("close", {
        code: 1006,
        reason: "BW WebSocket bridge disconnected",
        wasClean: false
      });
    });
    port.postMessage({ type: "open", path: String(path || "/voice-rt") });
    return socket;
  };
  const nativeWindowOpen = window.open.bind(window);
  window.open = (url, target, features) => {
    const value = String(url || "");
    return nativeWindowOpen(
      /^\/(?:pdf\/|skilltree\/|api\/)/.test(value) ? ORIGIN + value : url,
      target,
      features
    );
  };
  let _port = null, _seq = 0;
  const _pending = new Map();
  function ensurePort() {
    if (_port) return _port;
    _port = chrome.runtime.connect({ name: "bw-fetch" });
    _port.onMessage.addListener((m) => {
      const p = _pending.get(m.id);
      if (!p) return;
      if (m.type === "head") p.head(m);
      else if (m.type === "chunk") p.chunk(m.b64);
      else if (m.type === "done") { p.done(); _pending.delete(m.id); }
      else if (m.type === "error") { p.error(m.error); _pending.delete(m.id); }
    });
    _port.onDisconnect.addListener(() => {
      _port = null;
      for (const p of _pending.values()) p.error("bridge disconnected");
      _pending.clear();
    });
    return _port;
  }
  // Chrome extension Port 使用 JSON 序列化，Blob/ArrayBuffer 不能像原生 fetch 那样
  // 直接穿过隔离世界。语音片段上限与服务端保持一致；编码后的请求仍由 background
  // 按“固定来源 + 固定路由 + 固定方法”再次校验，网页本身拿不到账户令牌。
  const MAX_BRIDGED_BINARY_BODY_BYTES = 8 * 1024 * 1024;
  async function encodeBridgedBody(body) {
    if (body == null) return {};
    if (typeof body === "string") return { body };
    let buffer = null;
    if (typeof Blob !== "undefined" && body instanceof Blob) {
      if (body.size > MAX_BRIDGED_BINARY_BODY_BYTES) {
        throw new TypeError("binary request body exceeds 8 MiB");
      }
      buffer = await body.arrayBuffer();
    } else if (body instanceof ArrayBuffer) {
      buffer = body;
    } else if (ArrayBuffer.isView(body)) {
      buffer = body.buffer.slice(
        body.byteOffset,
        body.byteOffset + body.byteLength
      );
    } else {
      throw new TypeError("unsupported request body");
    }
    const bytes = new Uint8Array(buffer);
    if (!bytes.byteLength || bytes.byteLength > MAX_BRIDGED_BINARY_BODY_BYTES) {
      throw new TypeError("binary request body must be 1 byte to 8 MiB");
    }
    const parts = [];
    for (let i = 0; i < bytes.length; i += 0x8000) {
      parts.push(String.fromCharCode.apply(
        null,
        bytes.subarray(i, i + 0x8000)
      ));
    }
    return {
      bodyB64: btoa(parts.join("")),
      bodyBytes: bytes.byteLength
    };
  }
  window.__bwReaderFetch = async function (url, init) {
    init = init || {};
    let u = String(url);
    if (u.startsWith("/")) u = ORIGIN + u;
    if (!u.startsWith(ORIGIN + "/")) return fetch(url, init);   // 外站资源走原生
    if (init.signal?.aborted) throw new DOMException("aborted", "AbortError");
    const nativeNoteResponse = await nativeLocalNotesFetchInterceptor(u, init);
    if (nativeNoteResponse) return nativeNoteResponse;
    const encodedBody = await encodeBridgedBody(init.body);
    if (init.signal?.aborted) throw new DOMException("aborted", "AbortError");
    const id = ++_seq;
    return new Promise((resolve, reject) => {
      let ctrl = null;
      const stream = new ReadableStream({ start(c) { ctrl = c; } });
      let settled = false;
      _pending.set(id, {
        head(m) {
          settled = true;
          resolve(new Response(stream, { status: m.status, statusText: m.statusText || "", headers: m.headers || {} }));
        },
        chunk(b64) {
          try {
            const bin = atob(b64), arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            ctrl.enqueue(arr);
          } catch (_) {}
        },
        done() { try { ctrl.close(); } catch (_) {} },
        error(e) {
          try { ctrl.error(new Error(e)); } catch (_) {}
          if (!settled) reject(new TypeError(e));
        }
      });
      const headers = {};
      try { new Headers(init.headers || {}).forEach((v, k) => { headers[k] = v; }); } catch (_) {}
      ensurePort().postMessage({
        id, url: u,
        init: Object.assign({
          method: init.method || "GET",
          headers
        }, encodedBody)
      });
      if (init.signal) {
        init.signal.addEventListener("abort", () => {
          try { ensurePort().postMessage({ abort: id }); } catch (_) {}
          const p = _pending.get(id);
          if (p) { p.error("aborted"); _pending.delete(id); }
        });
      }
    });
  };
  // 真实网页 DOM 中运行的沉浸翻译引擎也走同一条 Pi/background 网络桥。
  window.__rcRawFetch = window.__bwReaderFetch;
  window.__bwServerUrl = (path) => {
    const value = String(path || "");
    return value.startsWith("/") ? ORIGIN + value : value;
  };

  // 共享组件来自阅读器源码，内部仍会生成 /skilltree、/pdf/view 等根相对链接。
  // 扩展在第三方网站运行时必须统一指回 BW，而不是误开 wikipedia.org/pdf/...。
  const rewriteServerAnchor = (event) => {
    const anchor = event.composedPath?.().find(
      (node) => node?.tagName === "A" && node.getAttribute
    );
    if (!anchor) return;
    const href = String(anchor.getAttribute("href") || "");
    if (!/^\/(?:pdf\/|skilltree\/|api\/)/.test(href)) return;
    anchor.href = ORIGIN + href;
  };
  shadow.addEventListener("click", rewriteServerAnchor, true);
  pinShadow.addEventListener("click", rewriteServerAnchor, true);

  // 私有编号图片必须经过 background 的 Bearer fetch，不能只把 src 改成绝对 URL。
  // MutationObserver 同时覆盖 innerHTML、新节点和后续 src 变化；相同资源复用 object URL。
  const privateImagePath = /^\/pdf\/api\/(?:asset\/[^/?#]+|toolshot\/[^/?#]+|img-proxy)(?:[?#]|$)/;
  const privateImageCache = new Map();
  const privateImageUrls = new Set();
  const privateImageFetchPath = (path) => {
    if (!/^\/pdf\/api\/asset\/[^/?#]+(?:[?#]|$)/.test(path)) return path;
    try {
      const url = new URL(path, ORIGIN);
      url.searchParams.set("proxy", "1");
      return url.pathname + url.search + url.hash;
    } catch (_) {
      return path;
    }
  };
  const loadPrivateImage = (path) => {
    const requestPath = privateImageFetchPath(path);
    if (privateImageCache.has(requestPath)) return privateImageCache.get(requestPath);
    const operation = window.__bwReaderFetch(requestPath).then(async (response) => {
      if (!response.ok) throw new Error("private image HTTP " + response.status);
      const type = String(response.headers.get("content-type") || "");
      if (type && !type.startsWith("image/") && type !== "application/octet-stream") {
        throw new Error("private image returned " + type);
      }
      const url = URL.createObjectURL(await response.blob());
      privateImageUrls.add(url);
      return url;
    }).catch((error) => {
      privateImageCache.delete(requestPath);
      throw error;
    });
    privateImageCache.set(requestPath, operation);
    return operation;
  };
  const upgradePrivateImage = (image) => {
    if (!image || image.tagName !== "IMG") return;
    const raw = String(image.getAttribute("src") || "");
    if (!privateImagePath.test(raw) || image.dataset.bwPrivateImageLoading === raw) return;
    image.dataset.bwPrivateImageLoading = raw;
    loadPrivateImage(raw).then((url) => {
      delete image.dataset.bwPrivateImageLoading;
      image.dataset.bwPrivateImageSource = raw;
      image.dataset.bwPrivateImageUrl = url;
      if (image.isConnected && image.getAttribute("src") === raw) {
        image.setAttribute("src", url);
      }
    }).catch(() => {
      if (image.dataset.bwPrivateImageLoading === raw) {
        delete image.dataset.bwPrivateImageLoading;
        image.dataset.bwPrivateImageError = "1";
      }
    });
  };
  const scanPrivateImages = (node) => {
    if (!node || node.nodeType !== 1) return;
    if (node.tagName === "IMG") upgradePrivateImage(node);
    node.querySelectorAll?.("img[src]").forEach(upgradePrivateImage);
  };
  const privateImageObserver = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "attributes") upgradePrivateImage(record.target);
      else record.addedNodes.forEach(scanPrivateImages);
    }
  });
  privateImageObserver.observe(root, {
    subtree: true, childList: true, attributes: true, attributeFilter: ["src"]
  });
  privateImageObserver.observe(pinRoot, {
    subtree: true, childList: true, attributes: true, attributeFilter: ["src"]
  });
  addEventListener("pagehide", () => {
    privateImageObserver.disconnect();
    for (const url of privateImageUrls) URL.revokeObjectURL(url);
    privateImageUrls.clear();
    privateImageCache.clear();
  }, { once: true });

  // ── MathJax 配置(必须在 vendor/mathjax-full.js 之前定义;tex 配置逐字来自 pdf_reader.html:15)──
  // 差异仅一处:startup.typeset:false —— 扩展跑在任意网页上,绝不能自动排版宿主页正文
  // (宿主页里的 "$5" 之类会被误当公式);我们只经 RC.typeset(el) 对自己 shadow 里的节点手动排版。
  window.MathJax = {
    tex: { inlineMath: [["$", "$"], ["\\(", "\\)"]], displayMath: [["$$", "$$"], ["\\[", "\\]"]] },
    options: { skipHtmlTags: ["script", "noscript", "style", "textarea", "pre", "code"] },
    startup: { typeset: false }
  };

  // shell.js / 调试用句柄
  window.__bwShadow = shadow;
  window.__bwRoot = root;
  window.__bwHead = headEl;
  window.__bwReaderHost = host;
  window.__bwPinHost = pinHost;
  window.__bwPinShadow = pinShadow;
  window.__bwPinHead = pinHead;
  window.__bwPinRoot = pinRoot;
})();
