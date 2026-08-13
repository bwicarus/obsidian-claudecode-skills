/* native-local-runtime.js — App-owned local Reader bootstrap.
 *
 * The WKWebView is a renderer, not a PWA owner. Files stay behind the native
 * loopback capability path; JavaScript receives only an opaque localBookID.
 * Local document state is authoritative in IndexedDB and network-only tools
 * use the bounded native Pi gateway.
 */
(function (root) {
  'use strict';

  var CONTRACT = 'native-local-runtime/1';
  var BOOTSTRAP_CONTRACT = 'reader-native-sync-bootstrap/1';
  var SYNC_REQUEST_CONTRACT = 'reader-pi-sync-request/1';
  var USER_STATE_REQUEST_CONTRACT = 'reader-book-user-state-web-request/1';
  var USER_STATE_RESPONSE_CONTRACT = 'reader-book-user-state-web-response/1';
  var USER_STATE_IMPORT_CONTRACT = 'reader-book-user-state-import/1';
  var USER_STATE_RECEIPT_CONTRACT = 'reader-book-user-state-import-receipt/1';
  var NATIVE_PDF_ASSISTANT_STATE_CONTRACT = 'reader-native-pdf-assistant-state/1';
  var NATIVE_EPUB_ASSISTANT_STATE_CONTRACT = 'reader-native-epub-assistant-state/1';
  var NATIVE_EPUB_ACTION_CONTRACT = 'reader-native-epub-action/1';
  var NATIVE_INTERFACE_CONTRACT = 'reader-native-interface-manifest/2';
  var NATIVE_INTERFACE_OWNERS = new Set(['local', 'pi', 'native']);
  var NATIVE_INTERFACE_MATCHES = new Set(['exact', 'segment']);
  var NATIVE_INTERFACE_STATUSES = new Set(['supported', 'degraded', 'pending']);
  var NATIVE_INTERFACE_METHODS = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']);
  var NATIVE_INTERFACE_SURFACES = new Set(['pdf', 'epub']);
  var DICTIONARY_FALLBACK_CACHE_VERSION = 'reader-jp-zh/1';
  var DICTIONARY_FALLBACK_CACHE_KIND = 'dictionary-fallback-cache';
  var DICTIONARY_FALLBACK_CACHE_LIMIT = 128;
  var DICTIONARY_FALLBACK_CACHE_TTL_MS = 365 * 24 * 60 * 60 * 1000;
  var NATIVE_REMOTE_BOOK_MODES = new Set(['required', 'conditional']);
  var NATIVE_REMOTE_BOOK_SCOPES = new Set(['current', 'catalog']);
  var NATIVE_REMOTE_BOOK_POINTERS = new Set([
    '/file', '/context/file', '/context/file_rel', '/ctx/file_rel',
    '/item/file', '/remove_item/file'
  ]);
  var NATIVE_REMOTE_BOOK_TRANSFORMS = new Set([
    'exact', 'prefix-before-delimiter'
  ]);
  var USER_STATE_DOMAINS = Object.freeze([
    'reading-position', 'highlights', 'ink', 'closed-regions',
    'notes', 'user-pages', 'card-placements', 'entity-references'
  ]);
  var runtimeRoot = root.BWReaderRuntime = root.BWReaderRuntime || {};
  if (runtimeRoot.nativeLocalRuntime) return;

  function RuntimeError(message, code, details) {
    this.name = 'NativeLocalRuntimeError';
    this.code = code || 'BW_NATIVE_LOCAL_RUNTIME';
    this.message = message;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, RuntimeError);
  }
  RuntimeError.prototype = Object.create(Error.prototype);
  RuntimeError.prototype.constructor = RuntimeError;

  function nativeInterfacePageSurface() {
    var path = '';
    try { path = new URL(root.location.href).pathname; } catch (_) {}
    if (path.endsWith('/shells/pdf.html')) return 'pdf';
    if (path.endsWith('/shells/epub.html')) return 'epub';
    throw new RuntimeError(
      '本机 Reader 页面类型无法识别', 'BW_NATIVE_INTERFACE_SURFACE'
    );
  }

  function nativeInterfaceManifestFromRoot() {
    var manifest = root.__BW_NATIVE_INTERFACE_MANIFEST__;
    if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest) ||
        manifest.contract !== NATIVE_INTERFACE_CONTRACT ||
        !Array.isArray(manifest.routes) || !manifest.routes.length ||
        !Array.isArray(manifest.scanIgnores) ||
        Object.keys(manifest).sort().join(',') !== 'contract,routes,scanIgnores') {
      throw new RuntimeError(
        '原生 Reader 接口清单缺失或合同无效',
        'BW_NATIVE_INTERFACE_MANIFEST'
      );
    }
    var identities = new Set();
    function validRemoteBookPolicy(policy, route) {
      if (policy === null) return true;
      if (!policy || typeof policy !== 'object' || Array.isArray(policy) ||
          route.owner !== 'pi' || Object.keys(policy).sort().join(',') !==
          'continuation,identities,mode,requiredMethods,scope' ||
          !NATIVE_REMOTE_BOOK_MODES.has(policy.mode) ||
          !NATIVE_REMOTE_BOOK_SCOPES.has(policy.scope) ||
          !Array.isArray(policy.requiredMethods) ||
          policy.requiredMethods.some(function (method) {
            return route.methods.indexOf(method) < 0;
          }) || new Set(policy.requiredMethods).size !== policy.requiredMethods.length ||
          !Array.isArray(policy.identities) || !policy.identities.length) {
        return false;
      }
      var canonicalRequired = route.methods.filter(function (method) {
        return policy.requiredMethods.indexOf(method) >= 0;
      });
      if ((policy.mode === 'required') !==
          (policy.requiredMethods.length === route.methods.length) ||
          JSON.stringify(policy.requiredMethods) !==
            JSON.stringify(canonicalRequired)) {
        return false;
      }
      var identityMethods = new Set();
      var validIdentities = policy.identities.every(function (item) {
        if (!item || typeof item !== 'object' || Array.isArray(item) ||
            Object.keys(item).sort().join(',') !==
            'location,methods,pointer,transform' ||
            !Array.isArray(item.methods) || !item.methods.length ||
            item.methods.some(function (method) {
              return route.methods.indexOf(method) < 0;
            }) || new Set(item.methods).size !== item.methods.length ||
            (item.location !== 'query' && item.location !== 'json') ||
            !NATIVE_REMOTE_BOOK_POINTERS.has(item.pointer) ||
            (item.location === 'query' && item.pointer !== '/file') ||
            !NATIVE_REMOTE_BOOK_TRANSFORMS.has(item.transform)) {
          return false;
        }
        item.methods.forEach(function (method) { identityMethods.add(method); });
        return true;
      });
      if (!validIdentities || policy.requiredMethods.some(function (method) {
        return !identityMethods.has(method);
      })) return false;
      return policy.continuation === null || (
        policy.continuation && typeof policy.continuation === 'object' &&
        !Array.isArray(policy.continuation) &&
        Object.keys(policy.continuation).sort().join(',') ===
          'fromPointer,kind,pointer' &&
        policy.continuation.kind === 'rid' &&
        policy.continuation.pointer === '/rid' &&
        policy.continuation.fromPointer === '/from'
      );
    }
    manifest.routes.forEach(function (route) {
      var validObject = route && typeof route === 'object' && !Array.isArray(route);
      var path = validObject ? route.path : '';
      var identity = validObject ? String(route.match) + ':' + String(path) : '';
      var canonical = typeof path === 'string' &&
        /^\/(?:pdf\/api|api\/assistant)\/(?:[A-Za-z0-9._~:@%+*-]+\/)*[A-Za-z0-9._~:@%+*-]*$/.test(path) &&
        path !== '/pdf/api/' && path !== '/api/assistant/' &&
        path.indexOf('//') < 0 && path.indexOf('\\') < 0;
      var routeKeys = validObject ? Object.keys(route).sort().join(',') : '';
      if (!validObject || routeKeys !==
          'description,match,methods,owner,path,remoteBook,status,surfaces' ||
          !canonical || !NATIVE_INTERFACE_MATCHES.has(route.match) ||
          (route.match === 'segment') !== path.endsWith('/') ||
          !NATIVE_INTERFACE_OWNERS.has(route.owner) ||
          !NATIVE_INTERFACE_STATUSES.has(route.status) ||
          !Array.isArray(route.methods) || !route.methods.length ||
          route.methods.some(function (method) { return !NATIVE_INTERFACE_METHODS.has(method); }) ||
          new Set(route.methods).size !== route.methods.length ||
          !Array.isArray(route.surfaces) || !route.surfaces.length ||
          route.surfaces.some(function (surface) { return !NATIVE_INTERFACE_SURFACES.has(surface); }) ||
          new Set(route.surfaces).size !== route.surfaces.length ||
          !validRemoteBookPolicy(route.remoteBook, route) ||
          typeof route.description !== 'string' || !route.description.trim() ||
          identities.has(identity)) {
        throw new RuntimeError(
          '原生 Reader 接口清单含无效路由：' + identity,
          'BW_NATIVE_INTERFACE_MANIFEST'
        );
      }
      identities.add(identity);
    });
    return manifest;
  }

  function matchingNativeInterfaceRoute(path) {
    if (!nativeInterfaceManifest) {
      throw new RuntimeError(
        '原生 Reader 接口清单尚未准备好', 'BW_NATIVE_INTERFACE_MANIFEST'
      );
    }
    var matches = nativeInterfaceManifest.routes.filter(function (route) {
      if (route.match === 'exact') return route.path === path;
      return path.indexOf(route.path) === 0 && path.length > route.path.length;
    });
    if (matches.length > 1) {
      throw new RuntimeError(
        '原生 Reader 接口清单路由重叠：' + path,
        'BW_NATIVE_INTERFACE_MANIFEST'
      );
    }
    if (!matches.length) {
      throw new RuntimeError(
        '原生 Reader 接口尚未分类：' + path,
        'BW_NATIVE_INTERFACE_UNCLASSIFIED'
      );
    }
    return matches[0];
  }

  function declaredNativeInterface(path, method) {
    var route = matchingNativeInterfaceRoute(path);
    if (route.methods.indexOf(method) < 0) {
      throw new RuntimeError(
        '原生 Reader 接口不接受 ' + method + '：' + path,
        'BW_NATIVE_INTERFACE_METHOD'
      );
    }
    if (route.surfaces.indexOf(nativeInterfaceSurface) < 0) {
      throw new RuntimeError(
        '原生 Reader 接口不适用于当前文档：' + path,
        'BW_NATIVE_INTERFACE_SURFACE'
      );
    }
    if (route.status !== 'supported') {
      throw new RuntimeError(
        '原生 Reader 接口尚未达到旧版兼容：' + path + '（' + route.status + '）',
        'BW_NATIVE_INTERFACE_NOT_READY',
        { path: path, status: route.status, owner: route.owner }
      );
    }
    return route;
  }

  function nativeInterfaceErrorResponse(error) {
    var code = error && error.code || 'BW_NATIVE_INTERFACE_HANDLER';
    var status = code === 'BW_NATIVE_INTERFACE_METHOD' ? 405 :
      code === 'BW_NATIVE_INTERFACE_SURFACE' ? 404 : 501;
    return jsonResponse({
      ok: false,
      code: code,
      error: String(error && error.message || error)
    }, status);
  }

  function required(name, method) {
    var api = runtimeRoot[name];
    if (!api || typeof api[method] !== 'function') {
      throw new RuntimeError(
        '本机 Reader 缺少 ' + name + '.' + method,
        'BW_LOCAL_RUNTIME_DEPENDENCY',
        { name: name, method: method }
      );
    }
    return api;
  }

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function nowSeconds() { return Math.floor(Date.now() / 1000); }
  function randomHex(bytes) {
    var out = new Uint8Array(bytes);
    if (!root.crypto || typeof root.crypto.getRandomValues !== 'function') {
      throw new RuntimeError('无法生成本机记录编号', 'BW_LOCAL_RANDOM_UNAVAILABLE');
    }
    root.crypto.getRandomValues(out);
    return Array.prototype.map.call(out, function (value) {
      return value.toString(16).padStart(2, '0');
    }).join('');
  }
  function stableDeviceId() {
    var key = 'bw-native-local-device-id-v1';
    var current = '';
    try { current = String(root.localStorage.getItem(key) || ''); } catch (_) {}
    if (/^pwa-install-v1-[a-f0-9]{32}$/.test(current)) return current;
    // Preserve the random identity generated by early local-runtime builds,
    // but move it into the server-compatible device-family namespace.
    var legacy = /^native-([a-f0-9]{32})$/.exec(current);
    current = 'pwa-install-v1-' + (legacy ? legacy[1] : randomHex(16));
    try { root.localStorage.setItem(key, current); } catch (_) {}
    return current;
  }
  function stablePreferenceNamespace() {
    var key = 'bw-native-local-preference-namespace-v1';
    var current = '';
    try { current = String(root.localStorage.getItem(key) || ''); } catch (_) {}
    if (/^acct-v1-[a-f0-9]{64}$/.test(current)) return current;
    current = 'acct-v1-' + randomHex(32);
    try { root.localStorage.setItem(key, current); } catch (_) {}
    return current;
  }
  function createLocalPreferenceContext(namespace) {
    var contextId = 'native-local-preferences-v1';
    var generation = 1;
    var active = true;
    function normalizeNamespace(value) {
      value = String(value || '');
      if (!/^acct-v1-[a-f0-9]{64}$/.test(value)) {
        throw new RuntimeError('本机设置命名空间无效', 'BW_LOCAL_PREFERENCE_NAMESPACE');
      }
      return value;
    }
    function lease() {
      if (!active) {
        throw new RuntimeError('本机设置租约已失效', 'BW_LOCAL_PREFERENCE_STALE');
      }
      return Object.freeze({
        contract: 'account-context-lease/1',
        contextId: contextId,
        namespace: namespace,
        generation: generation
      });
    }
    function isCurrent(candidate) {
      return !!(active && candidate &&
        candidate.contract === 'account-context-lease/1' &&
        candidate.contextId === contextId &&
        candidate.namespace === namespace &&
        Number(candidate.generation) === generation);
    }
    function assertCurrent(candidate) {
      if (!isCurrent(candidate)) {
        throw new RuntimeError('本机设置租约已变化', 'BW_LOCAL_PREFERENCE_STALE');
      }
      return Object.freeze({
        contract: 'account-context/1', contextId: contextId,
        active: true, namespace: namespace, generation: generation
      });
    }
    function deactivate() {
      active = false;
      generation += 1;
    }
    return Object.freeze({
      CONTRACT: 'account-context/1',
      normalizeNamespace: normalizeNamespace,
      lease: lease,
      isCurrent: isCurrent,
      assertCurrent: assertCurrent,
      deactivate: deactivate
    });
  }
  function opaqueBookId() {
    var id = String(root.__BW_NATIVE_LOCAL_BOOK_ID__ || '');
    if (!/^localbook-[A-Za-z0-9_-]{8,160}$/.test(id) && id !== 'localbook-welcome') {
      throw new RuntimeError('本机书籍编号无效', 'BW_LOCAL_BOOK_ID');
    }
    return id;
  }
  function localBasePath() {
    var value = String(root.__BW_NATIVE_LOCAL_BASE_PATH__ || '');
    if (!/^\/r\/[a-f0-9]{64}$/.test(value)) {
      throw new RuntimeError('本机会话路径无效', 'BW_LOCAL_CAPABILITY_PATH');
    }
    return value;
  }

  var deviceId = stableDeviceId();
  var preferenceNamespace = stablePreferenceNamespace();
  var preferenceContext = createLocalPreferenceContext(preferenceNamespace);
  var preferenceLease = preferenceContext.lease();
  var bookId = opaqueBookId();
  var basePath = localBasePath();
  var stores = null;
  var router = null;
  var preferences = null;
  var syncControl = null;
  var nativeInterfaceManifest = null;
  var nativeInterfaceSurface = null;
  var bootState = 'starting';
  var bootError = null;
  var originalFetch = root.fetch.bind(root);
  var originalSendBeacon = root.navigator && root.navigator.sendBeacon
    ? root.navigator.sendBeacon.bind(root.navigator) : null;
  var epubPromise = null;
  var maximumEPUBTextBytes = 8 * 1024 * 1024;
  var maximumEPUBTextTotalBytes = 128 * 1024 * 1024;
  var maximumEPUBResourceBytes = 32 * 1024 * 1024;
  var maximumEPUBResourceTotalBytes = 128 * 1024 * 1024;
  var maximumEPUBResourceCount = 128;
  var LOCAL_CONTEXT_SYNC_KEY = 'bw-native-context-sync-preference-v1';
  var LOCAL_CONTEXT_SYNC_CONTRACT = 'reader-native-context-sync-preference/1';
  var LOCAL_CONTEXT_SYNC_MODES = new Set(['legacy-inject', 'snapshot-mcp']);
  var OUTGOING_CONTEXT_CONTRACT = 'reader-outgoing-context/1';
  var OUTGOING_JOURNAL_CONTRACT = 'reader-native-outgoing-journal/1';
  var OUTGOING_DRAWING_CONTRACT = 'reader-native-outgoing-drawing/1';
  var OUTGOING_EVENT_TYPES = new Set([
    'page.context', 'focus', 'drawing', 'command', 'command-failed'
  ]);
  var OUTGOING_FOCUS_KINDS = new Set([
    'text', 'image', 'card', 'drawing', 'region'
  ]);
  var OUTGOING_JOURNAL_KEEP = 2000;
  var OUTGOING_JOURNAL_LIMIT = 500;
  var OUTGOING_JOURNAL_MAX_WAIT_S = 25;
  var OUTGOING_ACTIVE_FRESH_S = 180;
  var OUTGOING_FOCUS_FRESH_S = 300;
  var OUTGOING_DRAWING_FRESH_S = 120;
  var OUTGOING_DRAWING_STABLE_S = 1;
  var OUTGOING_BODY_MAX_BYTES = 64 * 1024;
  var OUTGOING_REF_MAX_BYTES = 32 * 1024;
  var OUTGOING_QUERY_MAX_CHARS = 8192;
  var outgoingMutationQueue = Promise.resolve();
  var blobURLs = [];
  var epubSharedResourceBudget = null;
  var epubActualTextBytes = 0;
  var epubTextQueue = Promise.resolve();
  var epubTextByPath = Object.create(null);
  var localStateMutationQueues = Object.create(null);
  var NATIVE_PDF_MUTATION_REQUEST_CONTRACT =
    'reader-native-pdf-mutation-request/1';
  var NATIVE_PDF_MUTATION_RESPONSE_CONTRACT =
    'reader-native-pdf-mutation-response/1';
  var NATIVE_PDF_MUTATION_JOURNAL_CONTRACT =
    'reader-native-pdf-mutation-web-journal/1';
  var NATIVE_PDF_MUTATION_JOURNAL_KIND = 'pdf-mutation-journal';
  var NATIVE_PDF_MUTATION_JOB_PREFIX = 'npj_';
  var nativePDFMutationJobs = Object.create(null);
  var activeNativePDFMutationJob = null;
  var nativePDFWriterGeneration = 0;
  var nativePDFWriterAccepting = true;
  var nativePDFActiveWriters = 0;
  var nativePDFWriterDrainWaiters = [];
  var EXACT_HIGHLIGHT_IDB_TIMEOUT_MS = 4000;
  // Pi 镜像的等待上限。超过它就按 metadata_pending 返回：本地事实已经落定，
  // 不该由一次远端往返决定用户看到的结果。
  var NATIVE_EPUB_METADATA_TIMEOUT_MS = 8000;
  var PDF_MUTATION_DOCUMENT_KINDS = Object.freeze([
    'reading-position', 'document-highlights', 'ink',
    'document-notes-legacy', 'user-pages',
    'card-placements', 'entity-references'
  ]);
  var PDF_MUTATION_WRITE_PATHS = new Set([
    '/pdf/api/reading-pos', '/pdf/api/highlights', '/pdf/api/ink',
    '/pdf/api/notes', '/pdf/api/userpages', '/pdf/api/ocr-selection',
    '/pdf/api/reocr-page', '/pdf/api/reocr-page/clear',
  ]);
  // Exhaustive policy for the 19 authoritative Pi PAGE_ANCHOR_MIGRATIONS
  // domains plus its separately migrated render-cache domain. App-owned data
  // is migrated locally. Pi-owned data remains bound to the old immutable
  // digest; the native Pi gateway deliberately drops that binding after the
  // PDF digest changes, preserving the data while refusing stale page writes
  // until an explicit upload/sync performs Pi-side reconciliation.
  var PDF_MUTATION_PAGE_ANCHOR_DOMAINS = Object.freeze([
    ['pdf-highlights', 'local-migrate'],
    ['reader-notes', 'local-migrate'],
    ['reader-positions', 'local-migrate'],
    ['pdf-ink', 'local-migrate'],
    ['reader-favorites', 'pi-preserve-rebind'],
    ['reader-userpages', 'local-migrate'],
    ['pdf-tr-sentences', 'pi-preserve-rebind'],
    ['pdf-char-offset', 'pi-preserve-rebind'],
    ['pdf-toc-range', 'pi-preserve-rebind'],
    ['sentence-cards', 'pi-preserve-rebind'],
    ['vocab-exposure', 'pi-preserve-rebind'],
    ['pdf-figures', 'pi-preserve-rebind'],
    ['pdf-ocr-fix', 'native-ocr-migrate'],
    ['pdf-page-ocr', 'native-ocr-migrate'],
    ['ocr-checkpoints', 'native-preserve-reprocess'],
    ['vocab-lookups', 'pi-preserve-rebind'],
    ['assistant-convo', 'pi-preserve-rebind'],
    ['attention-dwell', 'pi-preserve-rebind'],
    ['attention-db', 'pi-preserve-rebuild'],
    ['render-caches', 'pi-preserve-rebuild']
  ]);

  function acquireNativePDFWriterLease(label) {
    if (nativeInterfaceSurface !== 'pdf') return null;
    if (!nativePDFWriterAccepting || activeNativePDFMutationJob) {
      throw outgoingRequestError(
        'PDF 正在改页；完成后请重试写入',
        'BW_NATIVE_PDF_MUTATION_BUSY', 409
      );
    }
    var lease = {
      bookId: bookId,
      generation: nativePDFWriterGeneration,
      id: 'npw_' + randomHex(12),
      label: String(label || 'write'),
      released: false
    };
    nativePDFActiveWriters += 1;
    return lease;
  }

  function assertNativePDFWriterLease(lease) {
    if (nativeInterfaceSurface !== 'pdf') return;
    if (!lease || lease.released || lease.bookId !== bookId ||
        lease.generation !== nativePDFWriterGeneration) {
      throw outgoingRequestError(
        'PDF 写入租约已失效；改页后请重试',
        'BW_NATIVE_PDF_WRITER_STALE', 409
      );
    }
  }

  function releaseNativePDFWriterLease(lease) {
    if (!lease || lease.released) return;
    lease.released = true;
    nativePDFActiveWriters = Math.max(0, nativePDFActiveWriters - 1);
    if (nativePDFActiveWriters === 0 && nativePDFWriterDrainWaiters.length) {
      var waiters = nativePDFWriterDrainWaiters.splice(0);
      waiters.forEach(function (resolve) { resolve(); });
    }
  }

  function withNativePDFWriter(label, task) {
    var lease;
    return Promise.resolve().then(function () {
      lease = acquireNativePDFWriterLease(label);
      return assertNoNativePDFMutationJournal();
    }).then(function () {
      assertNativePDFWriterLease(lease);
      return task(lease);
    }).then(function (value) {
      assertNativePDFWriterLease(lease);
      releaseNativePDFWriterLease(lease);
      return value;
    }, function (error) {
      releaseNativePDFWriterLease(lease);
      throw error;
    });
  }

  function beginNativePDFWriterBarrier() {
    if (!nativePDFWriterAccepting) {
      return Promise.reject(outgoingRequestError(
        '本书已有改页屏障', 'BW_NATIVE_PDF_MUTATION_BUSY', 409
      ));
    }
    nativePDFWriterAccepting = false;
    var drained = nativePDFActiveWriters === 0
      ? Promise.resolve()
      : new Promise(function (resolve) {
          nativePDFWriterDrainWaiters.push(resolve);
        });
    return drained.then(function () {
      if (nativePDFActiveWriters !== 0) {
        throw new RuntimeError(
          'PDF 写入者未能排空', 'BW_NATIVE_PDF_WRITER_DRAIN'
        );
      }
      nativePDFWriterGeneration += 1;
      return {
        bookId: bookId,
        generation: nativePDFWriterGeneration,
        released: false
      };
    }).catch(function (error) {
      nativePDFWriterAccepting = true;
      throw error;
    });
  }

  function endNativePDFWriterBarrier(barrier) {
    if (!barrier || barrier.released) return;
    barrier.released = true;
    nativePDFWriterAccepting = true;
  }

  function createStores() {
    var indexed = required('indexedDBStore', 'createIndexedDBDataStore');
    var registry = required('dataRegistry', 'syncCollections');
    var causal = registry.syncCollections();
    var prefix = 'bw-reader-native-v1';
    return {
      global: indexed.createIndexedDBDataStore({
        dbName: prefix + '-global', deviceId: deviceId,
        channelName: prefix + '-global-events', causalCollections: causal
      }),
      document: indexed.createIndexedDBDataStore({
        dbName: prefix + '-document', deviceId: deviceId,
        channelName: prefix + '-document-events', causalCollections: []
      }),
      device: indexed.createIndexedDBDataStore({
        dbName: prefix + '-device', deviceId: deviceId,
        channelName: prefix + '-device-events', causalCollections: []
      })
    };
  }

  function createRouter(localStores) {
    var api = required('storageRouter', 'createStorageRouter');
    var registry = required('dataRegistry', 'scopes');
    return api.createStorageRouter({
      globalStore: localStores.global,
      documentStore: localStores.document,
      deviceStore: localStores.device,
      scopes: registry.scopes()
    });
  }

  function attachPreferenceStore() {
    var module = required('preferenceStore', 'createPreferenceStore');
    var registry = required('dataRegistry', 'settingMigrations');
    preferences = module.createPreferenceStore({
      accountContext: preferenceContext,
      dataRegistry: registry,
      storage: root.localStorage,
      lease: preferenceLease,
      eventTarget: root.document || null,
      trustedWindow: false,
      messageBridge: false
    });
    if (!preferences || typeof preferences.attach !== 'function') {
      throw new RuntimeError(
        '本机设置存储合同无效',
        'BW_LOCAL_PREFERENCE_STORE'
      );
    }
    root.__BW_READER_PREFERENCES__ = preferences;
    return Promise.resolve(preferences.attach(router, preferenceLease)).then(function () {
      preferenceContext.assertCurrent(preferenceLease);
      return true;
    });
  }

  function blockingFailure(error) {
    bootState = 'failed';
    bootError = error;
    function mount() {
      if (!root.document || root.document.getElementById('bw-local-runtime-failed')) return;
      var panel = root.document.createElement('div');
      panel.id = 'bw-local-runtime-failed';
      panel.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:#111827;color:#fff;display:grid;place-items:center;padding:28px;font:16px/1.6 -apple-system,sans-serif';
      var content = root.document.createElement('div');
      content.style.cssText = 'max-width:680px;background:#7f1d1d;border:1px solid #ef4444;border-radius:16px;padding:24px;white-space:pre-wrap';
      content.textContent = '本机 Reader 无法安全启动\n' +
        String(error && error.code || 'BW_LOCAL_STORE_UNAVAILABLE') + '\n' +
        String(error && error.message || error);
      panel.appendChild(content);
      (root.document.body || root.document.documentElement).appendChild(panel);
    }
    if (root.document && root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', mount, { once: true });
    } else {
      mount();
    }
    try {
      root.dispatchEvent(new CustomEvent('bw:native-local-runtime-failed', {
        detail: { code: error.code || 'BW_LOCAL_STORE_UNAVAILABLE', message: error.message }
      }));
    } catch (_) {}
  }

  function stateId(kind) { return bookId + ':' + kind; }
  function localFileRef() { return 'localbook:' + bookId; }
  function readState(kind, fallback, queryOptions) {
    return stores.document.get('native-' + kind, stateId(kind), queryOptions).then(function (record) {
      return record && record.value && record.value.payload != null
        ? clone(record.value.payload) : clone(fallback);
    });
  }
  function writeState(kind, payload) {
    if (kind === 'document-notes-legacy') {
      return writeNotesAndIndexes(payload);
    }
    return stores.document.put('native-' + kind, {
      id: stateId(kind), documentId: bookId, payload: clone(payload), updatedAt: Date.now()
    }, { mutationId: 'native-' + kind + '-' + randomHex(12) }).then(function () {
      return clone(payload);
    });
  }

  function serializeLocalStateMutation(scope, kind, task) {
    var key = scope + ':' + kind;
    var previous = localStateMutationQueues[key] || Promise.resolve();
    var result = previous.then(task, task);
    var settled = result.catch(function () {});
    localStateMutationQueues[key] = settled;
    settled.then(function () {
      if (localStateMutationQueues[key] === settled) delete localStateMutationQueues[key];
    });
    return result;
  }

  // queryOptions 让调用方给这次前置读设上界。
  // 写入有界而读取无界时，一次挂住的 readonly 事务会把后面的写入一起堵在门外——
  // 于是"有界的 batch"根本走不到。只有显式给出才生效，其余调用语义不变。
  function storedStateRecord(store, kind, ownerKey, ownerId, fallback, queryOptions) {
    var id = ownerId + ':' + kind;
    return store.get('native-' + kind, id, queryOptions).then(function (record) {
      var payload = validateStoredEnvelope(record, id, ownerKey, ownerId);
      return {
        payload: payload == null ? clone(fallback) : payload,
        rev: record == null ? 0 : Number(record.rev || 0)
      };
    });
  }

  function localStateMutationResult(payload, value) {
    return { payload: payload, value: value };
  }

  function mutateDocumentStateNow(kind, fallback, mutator, batchOptions) {
    var attempts = 0;
    function attempt() {
      attempts += 1;
      var relatedKinds = kind === 'document-notes-legacy'
        ? ['document-notes-legacy', 'card-placements', 'entity-references']
        : [kind];
      return Promise.all(relatedKinds.map(function (relatedKind) {
        // 前置读与随后的 batch 用同一个上界：这条链上任何一环无界，整条就仍会挂死。
        return storedStateRecord(
          stores.document, relatedKind, 'documentId', bookId,
          relatedKind === kind ? fallback : [],
          batchOptions
        );
      })).then(function (records) {
        var outcome = mutator(clone(records[0].payload));
        if (!outcome || !Object.prototype.hasOwnProperty.call(outcome, 'payload')) {
          throw new RuntimeError(
            '本机文档状态修改器响应无效', 'BW_LOCAL_STATE_MUTATION'
          );
        }
        var suffix = randomHex(12);
        var mutations;
        if (kind === 'document-notes-legacy') {
          var notes = Array.isArray(outcome.payload) ? clone(outcome.payload) : [];
          var placements = deriveCardPlacements(notes);
          var references = deriveEntityReferences(placements);
          mutations = [
            stateRecordMutation(kind, notes, suffix + '-notes', records[0].rev),
            stateRecordMutation('card-placements', placements, suffix + '-cards', records[1].rev),
            stateRecordMutation('entity-references', references, suffix + '-entities', records[2].rev)
          ];
        } else {
          mutations = [stateRecordMutation(
            kind, outcome.payload, suffix, records[0].rev
          )];
        }
        return stores.document.batch(mutations, batchOptions).then(function () {
          return clone(outcome.value);
        });
      }).catch(function (error) {
        if (error && error.code === 'BW_DATA_CONFLICT' && attempts < 5) {
          return attempt();
        }
        throw error;
      });
    }
    return attempt();
  }

  // batchOptions 必须一路交到 mutateDocumentStateNow。
  //
  // 这里少一个形参，调用方传的事务上界就被 JS 静默丢掉，与 StorageRouter 早先丢
  // batchOptions 是同一个模式：上下游都对，中间少一个参数，于是保护全程空转。
  function mutateDocumentState(kind, fallback, mutator, batchOptions) {
    return serializeLocalStateMutation('document', kind, function () {
      return mutateDocumentStateNow(kind, fallback, mutator, batchOptions);
    });
  }

  function deviceStateId(kind) { return deviceId + ':' + kind; }
  function validateStoredEnvelope(record, expectedId, expectedOwnerKey, expectedOwner) {
    if (record == null) return null;
    var value = record && record.value;
    if (!exactKeys(value, ['id', expectedOwnerKey, 'payload', 'updatedAt']) ||
        value.id !== expectedId || value[expectedOwnerKey] !== expectedOwner ||
        !Number.isFinite(value.updatedAt) || value.updatedAt < 0) {
      throw new RuntimeError(
        '本机上下文状态记录损坏或含未知字段',
        'BW_LOCAL_OUTGOING_STATE_CORRUPT'
      );
    }
    return clone(value.payload);
  }
  function readDeviceState(kind, fallback) {
    var id = deviceStateId(kind);
    return stores.device.get('native-' + kind, id).then(function (record) {
      var payload = validateStoredEnvelope(record, id, 'deviceId', deviceId);
      return payload == null ? clone(fallback) : payload;
    });
  }
  function deviceStateValue(kind, payload) {
    return {
      id: deviceStateId(kind), deviceId: deviceId,
      payload: clone(payload), updatedAt: Date.now()
    };
  }
  function writeDeviceState(kind, payload) {
    return stores.device.put('native-' + kind, deviceStateValue(kind, payload), {
      mutationId: 'native-' + kind + '-' + randomHex(12)
    }).then(function () { return clone(payload); });
  }
  function deviceStateMutation(kind, payload, suffix, ifRev) {
    return {
      operation: 'put',
      collection: 'native-' + kind,
      value: deviceStateValue(kind, payload),
      options: {
        mutationId: 'native-' + kind + '-' + suffix,
        ifRev: ifRev == null ? undefined : ifRev
      }
    };
  }

  function mutateDeviceState(kind, fallback, mutator) {
    return serializeLocalStateMutation('device', kind, function () {
      var attempts = 0;
      function attempt() {
        attempts += 1;
        return storedStateRecord(
          stores.device, kind, 'deviceId', deviceId, fallback
        ).then(function (record) {
          var outcome = mutator(clone(record.payload));
          if (!outcome || !Object.prototype.hasOwnProperty.call(outcome, 'payload')) {
            throw new RuntimeError(
              '本机设备状态修改器响应无效', 'BW_LOCAL_STATE_MUTATION'
            );
          }
          return stores.device.batch([
            deviceStateMutation(kind, outcome.payload, randomHex(12), record.rev)
          ]).then(function () { return clone(outcome.value); });
        }).catch(function (error) {
          if (error && error.code === 'BW_DATA_CONFLICT' && attempts < 5) {
            return attempt();
          }
          throw error;
        });
      }
      return attempt();
    });
  }

  function dictionaryFallbackText(value, label, maximum, required) {
    if (typeof value !== 'string' || value.length > maximum ||
        (required && !value.trim()) || /\u0000/.test(value)) {
      throw new RuntimeError(
        (label || '本机词义缓存文字') + '无效',
        'BW_READER_DICTIONARY_CACHE_INVALID'
      );
    }
    return value;
  }

  function normalizeDictionaryFallbackRequest(value) {
    if (!exactKeys(value, ['mode', 'term', 'context', 'reading', 'english'])) {
      throw new RuntimeError(
        '本机词义缓存请求字段无效',
        'BW_READER_DICTIONARY_CACHE_INVALID'
      );
    }
    var mode = dictionaryFallbackText(value.mode, '释义模式', 16, true);
    if (mode !== 'meaning' && mode !== 'deep') {
      throw new RuntimeError(
        '本机词义缓存模式无效',
        'BW_READER_DICTIONARY_CACHE_INVALID'
      );
    }
    return {
      mode: mode,
      term: dictionaryFallbackText(value.term, '词或词组', 256, true),
      context: dictionaryFallbackText(value.context, '句境', 1200, false),
      reading: dictionaryFallbackText(value.reading, '读音', 256, false),
      english: dictionaryFallbackText(value.english, '英文参考', 1200, false)
    };
  }

  function dictionaryFallbackKey(request) {
    return JSON.stringify([
      DICTIONARY_FALLBACK_CACHE_VERSION,
      request.mode,
      request.term,
      request.context,
      request.reading,
      request.english
    ]);
  }

  function normalizeDictionaryFallbackResult(value) {
    if (!exactKeys(value, ['language', 'text', 'source'])) {
      throw new RuntimeError(
        '本机词义缓存结果字段无效',
        'BW_READER_DICTIONARY_CACHE_INVALID'
      );
    }
    var language = dictionaryFallbackText(
      value.language, '释义语言', 16, true
    );
    var source = dictionaryFallbackText(
      value.source, '释义来源', 64, true
    );
    if (language !== 'zh-CN' || source !== 'pc-codex-cli') {
      throw new RuntimeError(
        '本机词义缓存结果来源无效',
        'BW_READER_DICTIONARY_CACHE_INVALID'
      );
    }
    return {
      language: language,
      text: dictionaryFallbackText(value.text, '中文释义', 6000, true),
      source: source
    };
  }

  function normalizeDictionaryFallbackCache(value) {
    if (!exactKeys(value, ['version', 'entries']) ||
        value.version !== DICTIONARY_FALLBACK_CACHE_VERSION ||
        !Array.isArray(value.entries) ||
        value.entries.length > DICTIONARY_FALLBACK_CACHE_LIMIT) {
      throw new RuntimeError(
        '本机词义缓存已损坏',
        'BW_READER_DICTIONARY_CACHE_CORRUPT'
      );
    }
    return {
      version: value.version,
      entries: value.entries.map(function (entry) {
        if (!exactKeys(entry, ['key', 'language', 'text', 'source', 'updatedAt']) ||
            typeof entry.key !== 'string' || entry.key.length > 4096 ||
            !Number.isFinite(entry.updatedAt) || entry.updatedAt < 0) {
          throw new RuntimeError(
            '本机词义缓存条目已损坏',
            'BW_READER_DICTIONARY_CACHE_CORRUPT'
          );
        }
        var result = normalizeDictionaryFallbackResult({
          language: entry.language,
          text: entry.text,
          source: entry.source
        });
        return {
          key: entry.key,
          language: result.language,
          text: result.text,
          source: result.source,
          updatedAt: entry.updatedAt
        };
      })
    };
  }

  var dictionaryFallbackCacheAPI = Object.freeze({
    version: DICTIONARY_FALLBACK_CACHE_VERSION,
    get: function (rawRequest) {
      var request = normalizeDictionaryFallbackRequest(rawRequest);
      var key = dictionaryFallbackKey(request);
      return readDeviceState(DICTIONARY_FALLBACK_CACHE_KIND, {
        version: DICTIONARY_FALLBACK_CACHE_VERSION,
        entries: []
      }).then(function (stored) {
        var cache = normalizeDictionaryFallbackCache(stored);
        var now = Date.now();
        var entry = cache.entries.find(function (item) {
          return item.key === key &&
            now - item.updatedAt <= DICTIONARY_FALLBACK_CACHE_TTL_MS;
        });
        return entry ? {
          language: entry.language,
          text: entry.text,
          source: entry.source,
          cached: true
        } : null;
      });
    },
    put: function (rawRequest, rawResult) {
      var request = normalizeDictionaryFallbackRequest(rawRequest);
      var result = normalizeDictionaryFallbackResult(rawResult);
      var key = dictionaryFallbackKey(request);
      return mutateDeviceState(DICTIONARY_FALLBACK_CACHE_KIND, {
        version: DICTIONARY_FALLBACK_CACHE_VERSION,
        entries: []
      }, function (stored) {
        var cache = normalizeDictionaryFallbackCache(stored);
        var now = Date.now();
        var entries = cache.entries.filter(function (item) {
          return item.key !== key &&
            now - item.updatedAt <= DICTIONARY_FALLBACK_CACHE_TTL_MS;
        });
        entries.push({
          key: key,
          language: result.language,
          text: result.text,
          source: result.source,
          updatedAt: now
        });
        entries.sort(function (left, right) {
          return right.updatedAt - left.updatedAt;
        });
        if (entries.length > DICTIONARY_FALLBACK_CACHE_LIMIT) {
          entries.length = DICTIONARY_FALLBACK_CACHE_LIMIT;
        }
        return {
          payload: {
            version: DICTIONARY_FALLBACK_CACHE_VERSION,
            entries: entries
          },
          value: {
            language: result.language,
            text: result.text,
            source: result.source,
            cached: true
          }
        };
      });
    }
  });
  function readDocumentOutgoingState(kind, fallback) {
    var id = stateId(kind);
    return stores.document.get('native-' + kind, id).then(function (record) {
      var payload = validateStoredEnvelope(record, id, 'documentId', bookId);
      return payload == null ? clone(fallback) : payload;
    });
  }
  function writeDocumentOutgoingState(kind, payload) {
    return stores.document.put('native-' + kind, {
      id: stateId(kind), documentId: bookId,
      payload: clone(payload), updatedAt: Date.now()
    }, { mutationId: 'native-' + kind + '-' + randomHex(12) }).then(function () {
      return clone(payload);
    });
  }
  function serializeOutgoingMutation(task) {
    var result = outgoingMutationQueue.then(task, task);
    outgoingMutationQueue = result.catch(function () {});
    return result;
  }

  function stateRecordMutation(kind, payload, mutationSuffix, ifRev) {
    return {
      operation: 'put',
      collection: 'native-' + kind,
      value: {
        id: stateId(kind), documentId: bookId,
        payload: clone(payload), updatedAt: Date.now()
      },
      options: {
        mutationId: 'native-' + kind + '-' + mutationSuffix,
        ifRev: ifRev == null ? undefined : ifRev
      }
    };
  }

  function placementEntityIds(note) {
    var values = [];
    function add(value) {
      value = String(value || '').trim();
      if (value && value.length <= 240 && values.indexOf(value) < 0) values.push(value);
    }
    if (note && note.card) {
      add(note.card.gid); add(note.card.cid); add(note.card.id);
    }
    if (note && note.html) { add(note.html.cid); add(note.html.id); }
    if (note && note.video) { add(note.video.id); }
    return values;
  }

  function deriveCardPlacements(notes) {
    return (Array.isArray(notes) ? notes : []).filter(function (note) {
      return !!(note && typeof note === 'object' &&
        (note.card || note.html || note.video));
    }).map(function (note) {
      var kind = note.card ? 'card' : (note.html ? 'html' : 'video');
      return {
        placementId: String(note.id || ''),
        noteId: String(note.id || ''),
        kind: kind,
        anchor: clone(note.anchor == null ? null : note.anchor),
        w: Number.isFinite(Number(note.w)) ? Number(note.w) : null,
        h: Number.isFinite(Number(note.h)) ? Number(note.h) : null,
        collapsed: !!note.collapsed,
        entityIds: placementEntityIds(note)
      };
    }).filter(function (item) { return !!item.placementId; });
  }

  function deriveEntityReferences(placements) {
    var grouped = Object.create(null);
    (Array.isArray(placements) ? placements : []).forEach(function (placement) {
      (Array.isArray(placement.entityIds) ? placement.entityIds : []).forEach(function (entityId) {
        if (!grouped[entityId]) {
          grouped[entityId] = {
            entityId: entityId, kind: placement.kind, placementIds: []
          };
        }
        if (grouped[entityId].placementIds.indexOf(placement.placementId) < 0) {
          grouped[entityId].placementIds.push(placement.placementId);
        }
      });
    });
    return Object.keys(grouped).sort().map(function (key) {
      grouped[key].placementIds.sort();
      return grouped[key];
    });
  }

  function writeNotesAndIndexes(payload) {
    var notes = Array.isArray(payload) ? clone(payload) : [];
    var placements = deriveCardPlacements(notes);
    var references = deriveEntityReferences(placements);
    var suffix = randomHex(12);
    return stores.document.batch([
      stateRecordMutation('document-notes-legacy', notes, suffix + '-notes'),
      stateRecordMutation('card-placements', placements, suffix + '-cards'),
      stateRecordMutation('entity-references', references, suffix + '-entities')
    ]).then(function () { return clone(notes); });
  }

  var USER_STATE_RECORD_KINDS = Object.freeze([
    'reading-position', 'document-highlights', 'epub-highlights',
    'ink', 'epub-ink', 'document-notes-legacy', 'user-pages',
    'card-placements', 'entity-references', 'user-state-domain-meta'
  ]);
  var USER_STATE_DOMAIN_LIMITS = Object.freeze({
    'reading-position': 64 * 1024,
    'highlights': 6 * 1024 * 1024,
    'ink': 24 * 1024 * 1024,
    'closed-regions': 6 * 1024 * 1024,
    'notes': 10 * 1024 * 1024,
    'user-pages': 12 * 1024 * 1024,
    'card-placements': 3 * 1024 * 1024,
    'entity-references': 3 * 1024 * 1024
  });

  function exactKeys(value, keys) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    var actual = Object.keys(value).sort();
    var expected = keys.slice().sort();
    return actual.length === expected.length && actual.every(function (key, index) {
      return key === expected[index];
    });
  }

  function canonicalJSONValue(value) {
    if (value === null || typeof value === 'string' ||
        typeof value === 'boolean') return value;
    if (typeof value === 'number') {
      if (!Number.isFinite(value)) {
        throw new RuntimeError('书籍附属数据包含非有限数字', 'BW_USER_STATE_INVALID');
      }
      return value;
    }
    if (Array.isArray(value)) return value.map(canonicalJSONValue);
    if (!value || typeof value !== 'object') {
      throw new RuntimeError('书籍附属数据不是 JSON', 'BW_USER_STATE_INVALID');
    }
    var output = {};
    Object.keys(value).sort().forEach(function (key) {
      if (!key) throw new RuntimeError('书籍附属数据字段名无效', 'BW_USER_STATE_INVALID');
      output[key] = canonicalJSONValue(value[key]);
    });
    return output;
  }

  function canonicalJSONString(value) {
    return JSON.stringify(canonicalJSONValue(value));
  }

  function utf8(value) { return new TextEncoder().encode(String(value)); }
  function sha256Hex(value) {
    if (!root.crypto || !root.crypto.subtle ||
        typeof root.crypto.subtle.digest !== 'function') {
      return Promise.reject(new RuntimeError(
        '本机书籍数据摘要不可用', 'BW_USER_STATE_DIGEST_UNAVAILABLE'
      ));
    }
    return root.crypto.subtle.digest('SHA-256', utf8(value)).then(function (buffer) {
      return Array.prototype.map.call(new Uint8Array(buffer), function (byte) {
        return byte.toString(16).padStart(2, '0');
      }).join('');
    });
  }

  function recordPayload(record, fallback) {
    return record && record.value && record.value.payload != null
      ? clone(record.value.payload) : clone(fallback);
  }

  function userStateSnapshotRecords() {
    if (!stores || !stores.document || typeof stores.document.getMany !== 'function') {
      return Promise.reject(new RuntimeError(
        '本机书籍数据快照接口不可用', 'BW_USER_STATE_SNAPSHOT_UNAVAILABLE'
      ));
    }
    return stores.document.getMany(USER_STATE_RECORD_KINDS.map(function (kind) {
      return { collection: 'native-' + kind, id: stateId(kind) };
    })).then(function (records) {
      var output = Object.create(null);
      USER_STATE_RECORD_KINDS.forEach(function (kind, index) {
        output[kind] = records[index] || null;
      });
      return output;
    });
  }

  function filteredStrokeMap(value, regions) {
    var output = {};
    if (!value || typeof value !== 'object' || Array.isArray(value)) return output;
    Object.keys(value).forEach(function (surface) {
      var strokes = Array.isArray(value[surface]) ? value[surface] : [];
      var selected = strokes.filter(function (stroke) {
        return !!(stroke && typeof stroke === 'object' &&
          ((stroke.t === 'region') === regions));
      }).map(clone);
      if (selected.length) output[surface] = selected;
    });
    return output;
  }

  function mergeStrokeMaps(ink, regions) {
    var output = {};
    var keys = new Set(Object.keys(ink || {}).concat(Object.keys(regions || {})));
    keys.forEach(function (surface) {
      var strokes = [];
      (Array.isArray(ink && ink[surface]) ? ink[surface] : []).forEach(function (stroke) {
        if (stroke && stroke.t !== 'region') strokes.push(clone(stroke));
      });
      (Array.isArray(regions && regions[surface]) ? regions[surface] : []).forEach(function (stroke) {
        if (stroke && stroke.t === 'region') strokes.push(clone(stroke));
      });
      if (strokes.length) output[surface] = strokes;
    });
    return output;
  }

  function userStateDomainsFromRecords(records) {
    var notes = recordPayload(records['document-notes-legacy'], []);
    if (!Array.isArray(notes)) notes = [];
    var derivedCards = deriveCardPlacements(notes);
    var cardRecord = records['card-placements'];
    var cards = cardRecord ? recordPayload(cardRecord, []) : derivedCards;
    if (!Array.isArray(cards)) cards = derivedCards;
    var derivedEntities = deriveEntityReferences(cards);
    var entityRecord = records['entity-references'];
    var entities = entityRecord ? recordPayload(entityRecord, []) : derivedEntities;
    if (!Array.isArray(entities)) entities = derivedEntities;
    var pdfInk = recordPayload(records.ink, {});
    var epubInk = recordPayload(records['epub-ink'], {});
    return {
      'reading-position': recordPayload(records['reading-position'], null),
      'highlights': {
        pdf: recordPayload(records['document-highlights'], []),
        epub: recordPayload(records['epub-highlights'], [])
      },
      'ink': {
        pdf: filteredStrokeMap(pdfInk, false),
        epub: filteredStrokeMap(epubInk, false)
      },
      'closed-regions': {
        pdf: filteredStrokeMap(pdfInk, true),
        epub: filteredStrokeMap(epubInk, true)
      },
      'notes': notes,
      'user-pages': recordPayload(records['user-pages'], []),
      'card-placements': cards,
      'entity-references': entities
    };
  }

  function userStateDomainRevision(name, records) {
    var kinds = {
      'reading-position': ['reading-position'],
      'highlights': ['document-highlights', 'epub-highlights'],
      'ink': ['ink', 'epub-ink'],
      'closed-regions': ['ink', 'epub-ink'],
      'notes': ['document-notes-legacy'],
      'user-pages': ['user-pages'],
      'card-placements': ['card-placements', 'document-notes-legacy'],
      'entity-references': ['entity-references', 'document-notes-legacy']
    }[name] || [];
    return kinds.reduce(function (revision, kind) {
      return Math.max(revision, Number(records[kind] && records[kind].rev || 0));
    }, 0);
  }

  function userStateDomainEmpty(name, value) {
    if (name === 'highlights' || name === 'ink' || name === 'closed-regions') {
      var pdf = value && value.pdf;
      var epub = value && value.epub;
      var pdfEmpty = Array.isArray(pdf) ? !pdf.length :
        !!(pdf && typeof pdf === 'object' && !Object.keys(pdf).length);
      var epubEmpty = Array.isArray(epub) ? !epub.length :
        !!(epub && typeof epub === 'object' && !Object.keys(epub).length);
      return pdfEmpty && epubEmpty;
    }
    if (value == null || value === '') return true;
    if (Array.isArray(value)) return !value.length;
    return typeof value === 'object' && !Object.keys(value).length;
  }

  function validateSurfaceMap(value, regions) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    return Object.keys(value).every(function (surface) {
      var embedded = /^pdf\|(.{1,512})\|([1-9]\d*)$/.exec(surface);
      var validEmbedded = !!(embedded && embedded[1][0] !== '/' &&
        utf8(embedded[1]).byteLength <= 512 &&
        !/^[A-Za-z][A-Za-z0-9+.-]*:/.test(embedded[1]) &&
        embedded[1].indexOf('\\') < 0 && embedded[1].indexOf('\0') < 0 &&
        embedded[1].split('/').every(function (part) {
          return !!part && part !== '.' && part !== '..';
        }) && Number(embedded[2]) >= 1);
      if (!(/^(?:\d+|u_[a-fA-F0-9]{4,32})$/.test(surface) || validEmbedded) ||
          !Array.isArray(value[surface])) return false;
      return value[surface].every(function (stroke) {
        return !!(stroke && typeof stroke === 'object' && !Array.isArray(stroke) &&
          ((stroke.t === 'region') === regions));
      });
    });
  }

  function validateUserStateDomainShape(name, value) {
    if (name === 'reading-position') {
      return value === null || !!(value && typeof value === 'object' && !Array.isArray(value));
    }
    if (['notes', 'user-pages', 'card-placements', 'entity-references'].indexOf(name) >= 0) {
      return Array.isArray(value);
    }
    if (!exactKeys(value, ['pdf', 'epub'])) return false;
    if (name === 'highlights') return Array.isArray(value.pdf) && Array.isArray(value.epub);
    return validateSurfaceMap(value.pdf, name === 'closed-regions') &&
      validateSurfaceMap(value.epub, name === 'closed-regions');
  }

  function validateUserStateRequest(request, action, extraKeys) {
    var keys = ['contract', 'action', 'requestId', 'localBookId'].concat(extraKeys || []);
    if (!exactKeys(request, keys) || request.contract !== USER_STATE_REQUEST_CONTRACT ||
        request.action !== action || !/^usr_[a-f0-9]{32}$/.test(String(request.requestId || '')) ||
        request.localBookId !== bookId) {
      throw new RuntimeError('本机书籍数据请求无效', 'BW_USER_STATE_REQUEST_INVALID');
    }
  }

  function userStateResponse(action, requestId, value) {
    return Object.assign({
      contract: USER_STATE_RESPONSE_CONTRACT,
      action: action,
      requestId: requestId,
      ok: true,
      localBookId: bookId
    }, value);
  }

  function computeUserStateHeaders(records) {
    var domains = userStateDomainsFromRecords(records);
    var metadata = recordPayload(records['user-state-domain-meta'], {});
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) {
      metadata = {};
    }
    return Promise.all(USER_STATE_DOMAINS.map(function (name) {
      var canonical = canonicalJSONString(domains[name]);
      return sha256Hex(canonical).then(function (localDigest) {
        var remembered = metadata[name];
        var digest = remembered && remembered.localDigest === localDigest &&
          /^[a-f0-9]{64}$/.test(String(remembered.remoteDigest || ''))
          ? remembered.remoteDigest : localDigest;
        return {
          name: name,
          digest: digest,
          revision: userStateDomainRevision(name, records),
          empty: userStateDomainEmpty(name, domains[name])
        };
      });
    }));
  }

  function snapshotUserStateHeaders(request) {
    return bootPromise.then(function () {
      validateUserStateRequest(request, 'snapshot-headers');
      return userStateSnapshotRecords();
    }).then(function (records) {
      return computeUserStateHeaders(records);
    }).then(function (headers) {
      return userStateResponse('snapshot-headers', request.requestId, { headers: headers });
    });
  }

  function parseUserStateTransaction(request) {
    validateUserStateRequest(request, 'apply-atomically', ['transaction']);
    var transaction = request.transaction;
    if (!exactKeys(transaction, [
      'contract', 'transactionId', 'localBookId', 'remoteBookId',
      'contentSha256', 'packageRevision', 'expectedLocalHeaders', 'domains'
    ]) || transaction.contract !== USER_STATE_IMPORT_CONTRACT ||
        !/^us_[a-f0-9]{32}$/.test(String(transaction.transactionId || '')) ||
        transaction.localBookId !== bookId ||
        !/^book_[a-f0-9]{32}$/.test(String(transaction.remoteBookId || '')) ||
        !/^[a-f0-9]{64}$/.test(String(transaction.contentSha256 || '')) ||
        !Number.isSafeInteger(transaction.packageRevision) ||
        transaction.packageRevision < 1 || !Array.isArray(transaction.domains) ||
        !transaction.domains.length || transaction.domains.length > USER_STATE_DOMAINS.length) {
      throw new RuntimeError('本机书籍数据事务无效', 'BW_USER_STATE_TRANSACTION_INVALID');
    }
    var names = new Set();
    var totalBytes = 0;
    var parsed = [];
    var checks = transaction.domains.map(function (domain) {
      if (!exactKeys(domain, [
        'name', 'revision', 'digest', 'byteCount', 'empty', 'payloadJson'
      ]) || USER_STATE_DOMAINS.indexOf(domain.name) < 0 || names.has(domain.name) ||
          !Number.isSafeInteger(domain.revision) || domain.revision < 1 ||
          !/^[a-f0-9]{64}$/.test(String(domain.digest || '')) ||
          !Number.isSafeInteger(domain.byteCount) || domain.byteCount < 0 ||
          typeof domain.empty !== 'boolean' || typeof domain.payloadJson !== 'string') {
        throw new RuntimeError('本机书籍数据域无效', 'BW_USER_STATE_DOMAIN_INVALID');
      }
      names.add(domain.name);
      var bytes = utf8(domain.payloadJson).byteLength;
      totalBytes += bytes;
      if (bytes !== domain.byteCount || bytes > USER_STATE_DOMAIN_LIMITS[domain.name] ||
          totalBytes > 64 * 1024 * 1024) {
        throw new RuntimeError('本机书籍数据域大小无效', 'BW_USER_STATE_DOMAIN_LIMIT');
      }
      var value;
      try { value = JSON.parse(domain.payloadJson); }
      catch (_) { throw new RuntimeError('本机书籍数据域不是 JSON', 'BW_USER_STATE_DOMAIN_INVALID'); }
      if (!validateUserStateDomainShape(domain.name, value) ||
          userStateDomainEmpty(domain.name, value) !== domain.empty) {
        throw new RuntimeError('本机书籍数据域结构无效', 'BW_USER_STATE_DOMAIN_INVALID');
      }
      var parsedDomain = {
        name: domain.name, value: value, digest: domain.digest,
        revision: domain.revision, empty: domain.empty, localDigest: null
      };
      parsed.push(parsedDomain);
      return Promise.all([
        sha256Hex(domain.payloadJson),
        sha256Hex(canonicalJSONString(value))
      ]).then(function (digests) {
        if (digests[0] !== domain.digest) {
          throw new RuntimeError('本机书籍数据域摘要不一致', 'BW_USER_STATE_DOMAIN_DIGEST');
        }
        parsedDomain.localDigest = digests[1];
      });
    });
    if (!transaction.expectedLocalHeaders ||
        typeof transaction.expectedLocalHeaders !== 'object' ||
        Array.isArray(transaction.expectedLocalHeaders)) {
      throw new RuntimeError('本机书籍数据预期状态无效', 'BW_USER_STATE_TRANSACTION_INVALID');
    }
    var expectedNames = Object.keys(transaction.expectedLocalHeaders).sort();
    var domainNames = transaction.domains.map(function (domain) { return domain.name; }).sort();
    if (expectedNames.length !== domainNames.length ||
        expectedNames.some(function (name, index) { return name !== domainNames[index]; })) {
      throw new RuntimeError('本机书籍数据预期域不完整', 'BW_USER_STATE_TRANSACTION_INVALID');
    }
    expectedNames.forEach(function (name) {
      var header = transaction.expectedLocalHeaders[name];
      if (!exactKeys(header, ['digest', 'revision', 'empty']) ||
          !/^[a-f0-9]{64}$/.test(String(header.digest || '')) ||
          !Number.isSafeInteger(header.revision) || header.revision < 0 ||
          typeof header.empty !== 'boolean') {
        throw new RuntimeError('本机书籍数据预期头无效', 'BW_USER_STATE_TRANSACTION_INVALID');
      }
    });
    return Promise.all(checks).then(function () {
      return { transaction: transaction, domains: parsed };
    });
  }

  function applyUserStateAtomically(request) {
    return bootPromise.then(function () {
      return parseUserStateTransaction(request);
    }).then(function (parsed) {
      return userStateSnapshotRecords().then(function (records) {
        return computeUserStateHeaders(records).then(function (headers) {
          var actual = Object.create(null);
          headers.forEach(function (header) { actual[header.name] = header; });
          Object.keys(parsed.transaction.expectedLocalHeaders).forEach(function (name) {
            var expected = parsed.transaction.expectedLocalHeaders[name];
            var observed = actual[name];
            if (!observed || observed.digest !== expected.digest ||
                observed.revision !== expected.revision ||
                observed.empty !== expected.empty) {
              throw new RuntimeError(
                '准备导入后本机书籍数据已发生变化',
                'BW_USER_STATE_LOCAL_CHANGED',
                { domain: name }
              );
            }
          });
          return records;
        });
      }).then(function (records) {
        var current = userStateDomainsFromRecords(records);
        var imported = new Set();
        parsed.domains.forEach(function (domain) {
          current[domain.name] = clone(domain.value);
          imported.add(domain.name);
        });
        var domainMeta = recordPayload(records['user-state-domain-meta'], {});
        if (!domainMeta || typeof domainMeta !== 'object' || Array.isArray(domainMeta)) {
          domainMeta = {};
        }
        parsed.domains.forEach(function (domain) {
          domainMeta[domain.name] = {
            remoteDigest: domain.digest,
            localDigest: domain.localDigest,
            remoteRevision: domain.revision,
            empty: domain.empty
          };
        });
        var kinds = new Set();
        if (imported.has('reading-position')) kinds.add('reading-position');
        if (imported.has('highlights')) {
          kinds.add('document-highlights'); kinds.add('epub-highlights');
        }
        if (imported.has('ink') || imported.has('closed-regions')) {
          kinds.add('ink'); kinds.add('epub-ink');
        }
        if (imported.has('notes')) kinds.add('document-notes-legacy');
        if (imported.has('user-pages')) kinds.add('user-pages');
        if (imported.has('card-placements')) kinds.add('card-placements');
        if (imported.has('entity-references')) kinds.add('entity-references');
        kinds.add('user-state-domain-meta');
        var payloads = {
          'reading-position': current['reading-position'],
          'document-highlights': current.highlights.pdf,
          'epub-highlights': current.highlights.epub,
          'ink': mergeStrokeMaps(current.ink.pdf, current['closed-regions'].pdf),
          'epub-ink': mergeStrokeMaps(current.ink.epub, current['closed-regions'].epub),
          'document-notes-legacy': current.notes,
          'user-pages': current['user-pages'],
          'card-placements': current['card-placements'],
          'entity-references': current['entity-references'],
          'user-state-domain-meta': domainMeta
        };
        var suffix = parsed.transaction.transactionId.slice(3);
        var mutations = Array.from(kinds).map(function (kind) {
          return stateRecordMutation(
            kind,
            payloads[kind],
            suffix + '-' + kind,
            Number(records[kind] && records[kind].rev || 0)
          );
        });
        return stores.document.batch(mutations).then(function () {
          var digests = {};
          parsed.domains.forEach(function (domain) { digests[domain.name] = domain.digest; });
          return userStateResponse('apply-atomically', request.requestId, {
            receipt: {
              contract: USER_STATE_RECEIPT_CONTRACT,
              transactionId: parsed.transaction.transactionId,
              committed: true,
              domainDigests: digests
            }
          });
        });
      });
    });
  }

  var bookUserStateAPI = Object.freeze({
    contract: 'reader-book-user-state-web/1',
    snapshotHeaders: snapshotUserStateHeaders,
    applyAtomically: applyUserStateAtomically
  });

  function jsonResponse(value, status) {
    return new Response(JSON.stringify(value), {
      status: status || 200,
      headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' }
    });
  }
  function textResponse(value, status, type) {
    return new Response(String(value == null ? '' : value), {
      status: status || 200,
      headers: { 'Content-Type': type || 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' }
    });
  }
  function bodyJSON(input, init) {
    var body = init && init.body;
    if (body == null && typeof Request !== 'undefined' && input instanceof Request) {
      return input.clone().text().then(function (text) {
        return text ? JSON.parse(text) : {};
      });
    }
    if (body == null || body === '') return Promise.resolve({});
    if (typeof body === 'string') {
      try { return Promise.resolve(JSON.parse(body)); }
      catch (_) { return Promise.reject(new RuntimeError('本机状态 JSON 无效', 'BW_LOCAL_STATE_BODY')); }
    }
    if (typeof Blob !== 'undefined' && body instanceof Blob) {
      return body.text().then(function (text) { return text ? JSON.parse(text) : {}; });
    }
    return Promise.reject(new RuntimeError('本机状态请求体不受支持', 'BW_LOCAL_STATE_BODY'));
  }
  function requestBodyText(input, init) {
    var body = init && init.body;
    if (body == null && typeof Request !== 'undefined' && input instanceof Request) {
      return input.clone().text();
    }
    if (body == null) return Promise.resolve('');
    if (typeof body === 'string') return Promise.resolve(body);
    if (typeof Blob !== 'undefined' && body instanceof Blob) return body.text();
    return Promise.reject(new RuntimeError(
      '请求体不受支持', 'BW_REQUEST_BODY'
    ));
  }
  function urlOf(input) {
    try {
      return new URL(typeof input === 'string' || input instanceof URL
        ? String(input) : String(input && input.url || ''), root.location.href);
    } catch (_) { return null; }
  }
  function methodOf(input, init) {
    return String(init && init.method ||
      (typeof Request !== 'undefined' && input instanceof Request ? input.method : 'GET') || 'GET'
    ).toUpperCase();
  }
  function queryId(url) { return String(url.searchParams.get('id') || ''); }

  function defaultLocalContextSync() {
    var enabled = false;
    try { enabled = root.localStorage.getItem('eph-ctx-sync') === '1'; } catch (_) {}
    return {
      contract: LOCAL_CONTEXT_SYNC_CONTRACT,
      enabled: enabled,
      deliveryMode: 'snapshot-mcp',
      ts: 0
    };
  }
  function normalizeLocalContextSync(value) {
    if (!exactKeys(value, ['contract', 'enabled', 'deliveryMode', 'ts']) ||
        value.contract !== LOCAL_CONTEXT_SYNC_CONTRACT ||
        typeof value.enabled !== 'boolean' ||
        !LOCAL_CONTEXT_SYNC_MODES.has(value.deliveryMode) ||
        !Number.isSafeInteger(value.ts) || value.ts < 0) {
      throw new RuntimeError(
        '本机上下文同步偏好无效', 'BW_LOCAL_CONTEXT_SYNC_PREFERENCE'
      );
    }
    return {
      contract: LOCAL_CONTEXT_SYNC_CONTRACT,
      enabled: value.enabled,
      deliveryMode: value.deliveryMode,
      ts: value.ts
    };
  }
  function readLocalContextSync() {
    var raw = null;
    try { raw = root.localStorage.getItem(LOCAL_CONTEXT_SYNC_KEY); }
    catch (error) {
      throw new RuntimeError(
        '无法读取本机上下文同步偏好', 'BW_LOCAL_CONTEXT_SYNC_STORAGE'
      );
    }
    if (raw == null || raw === '') return defaultLocalContextSync();
    try { return normalizeLocalContextSync(JSON.parse(raw)); }
    catch (error) {
      if (error && error.code === 'BW_LOCAL_CONTEXT_SYNC_PREFERENCE') throw error;
      throw new RuntimeError(
        '本机上下文同步偏好 JSON 无效', 'BW_LOCAL_CONTEXT_SYNC_PREFERENCE'
      );
    }
  }
  function writeLocalContextSync(value) {
    var normalized = normalizeLocalContextSync(value);
    var previousPreference;
    var previousLegacy;
    var preferenceRead = false;
    var legacyRead = false;
    try {
      previousPreference = root.localStorage.getItem(LOCAL_CONTEXT_SYNC_KEY);
      preferenceRead = true;
      previousLegacy = root.localStorage.getItem('eph-ctx-sync');
      legacyRead = true;
      root.localStorage.setItem(
        LOCAL_CONTEXT_SYNC_KEY, JSON.stringify(normalized)
      );
      root.localStorage.setItem('eph-ctx-sync', normalized.enabled ? '1' : '0');
    } catch (error) {
      var rollbackError = null;
      try {
        if (preferenceRead) {
          if (previousPreference == null) {
            root.localStorage.removeItem(LOCAL_CONTEXT_SYNC_KEY);
          } else {
            root.localStorage.setItem(LOCAL_CONTEXT_SYNC_KEY, previousPreference);
          }
        }
        if (legacyRead) {
          if (previousLegacy == null) {
            root.localStorage.removeItem('eph-ctx-sync');
          } else {
            root.localStorage.setItem('eph-ctx-sync', previousLegacy);
          }
        }
      } catch (rollbackFailure) {
        rollbackError = rollbackFailure;
      }
      throw new RuntimeError(
        rollbackError
          ? '无法保存且无法回滚本机上下文同步偏好'
          : '无法保存本机上下文同步偏好',
        rollbackError
          ? 'BW_LOCAL_CONTEXT_SYNC_ROLLBACK'
          : 'BW_LOCAL_CONTEXT_SYNC_STORAGE'
      );
    }
    return normalized;
  }

  function outgoingRequestError(message, code, status) {
    var error = new RuntimeError(message, code);
    error.httpStatus = status || 400;
    return error;
  }
  function outgoingFailureResponse(error, fallbackCode, fallbackStatus) {
    return jsonResponse({
      ok: false,
      code: error && error.code || fallbackCode || 'BW_LOCAL_OUTGOING_FAILED',
      error: String(error && error.message || error || '本机上下文接口失败'),
      retryable: false
    }, error && error.httpStatus || fallbackStatus || 500);
  }
  function contextSyncEnabled() {
    return readLocalContextSync().enabled === true;
  }
  function requireContextSyncEnabled() {
    if (!contextSyncEnabled()) {
      throw outgoingRequestError(
        'context sync disabled', 'BW_LOCAL_CONTEXT_SYNC_DISABLED', 409
      );
    }
  }
  function assertObjectFields(value, allowed, requiredKeys, code) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw outgoingRequestError('请求体必须是 JSON 对象', code, 400);
    }
    var unknown = Object.keys(value).filter(function (key) {
      return allowed.indexOf(key) < 0;
    });
    var missing = (requiredKeys || []).filter(function (key) {
      return !Object.prototype.hasOwnProperty.call(value, key);
    });
    if (unknown.length || missing.length) {
      throw outgoingRequestError(
        unknown.length
          ? '请求含未知字段: ' + unknown.join(',')
          : '请求缺少字段: ' + missing.join(','),
        code, 400
      );
    }
    return value;
  }
  function strictRequestJSON(input, init, maximumBytes, code) {
    var contentType = requestHeader(input, init, 'Content-Type').toLowerCase();
    if (!/^application\/json(?:\s*;|$)/.test(contentType)) {
      return Promise.reject(outgoingRequestError(
        '请求必须使用 application/json', code, 415
      ));
    }
    return requestBodyText(input, init).then(function (text) {
      if (new TextEncoder().encode(text).length > maximumBytes) {
        throw outgoingRequestError('请求体过大', code, 413);
      }
      if (!text) throw outgoingRequestError('请求体不能为空', code, 400);
      try { return JSON.parse(text); }
      catch (_) { throw outgoingRequestError('请求体 JSON 无效', code, 400); }
    }).catch(function (error) {
      if (error && error.code === code) throw error;
      throw outgoingRequestError(
        String(error && error.message || '无法读取请求体'), code, 400
      );
    });
  }
  function strictQuery(url, allowed, requiredKeys, code) {
    if (url.search.length > OUTGOING_QUERY_MAX_CHARS) {
      throw outgoingRequestError('查询参数过长', code, 414);
    }
    var seen = Object.create(null);
    url.searchParams.forEach(function (_, key) {
      if (allowed.indexOf(key) < 0) {
        throw outgoingRequestError('查询含未知字段: ' + key, code, 400);
      }
      if (seen[key]) {
        throw outgoingRequestError('查询字段重复: ' + key, code, 400);
      }
      seen[key] = true;
    });
    (requiredKeys || []).forEach(function (key) {
      if (!seen[key]) throw outgoingRequestError('查询缺少字段: ' + key, code, 400);
    });
    return seen;
  }
  function strictInteger(value, minimum, maximum, label, code) {
    if (typeof value === 'string' && !/^-?\d+$/.test(value)) {
      throw outgoingRequestError(label + ' 必须是整数', code, 400);
    }
    var number = Number(value);
    if (!Number.isSafeInteger(number) || number < minimum || number > maximum) {
      throw outgoingRequestError(label + ' 超出范围', code, 400);
    }
    return number;
  }
  function strictFinite(value, minimum, maximum, label, code) {
    if (value === '' || typeof value === 'boolean') {
      throw outgoingRequestError(label + ' 必须是数字', code, 400);
    }
    var number = Number(value);
    if (!Number.isFinite(number) || number < minimum || number > maximum) {
      throw outgoingRequestError(label + ' 超出范围', code, 400);
    }
    return number;
  }
  function validateOpaqueJSON(value, code) {
    var nodes = 0;
    function walk(current, depth) {
      nodes += 1;
      if (nodes > 4096 || depth > 16) {
        throw outgoingRequestError('对象结构过深或过大', code, 400);
      }
      if (current == null || typeof current === 'boolean') return;
      if (typeof current === 'number') {
        if (!Number.isFinite(current)) {
          throw outgoingRequestError('对象含非有限数字', code, 400);
        }
        return;
      }
      if (typeof current === 'string') {
        if (current.length > 8192 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(current)) {
          throw outgoingRequestError('对象字符串无效或过长', code, 400);
        }
        return;
      }
      if (Array.isArray(current)) {
        current.forEach(function (item) { walk(item, depth + 1); });
        return;
      }
      if (!current || typeof current !== 'object') {
        throw outgoingRequestError('对象含非 JSON 值', code, 400);
      }
      Object.keys(current).forEach(function (key) {
        if (!key || key.length > 128 ||
            key === '__proto__' || key === 'prototype' || key === 'constructor' ||
            /[\u0000-\u001f\u007f]/.test(key)) {
          throw outgoingRequestError('对象字段名无效', code, 400);
        }
        walk(current[key], depth + 1);
      });
    }
    walk(value, 0);
    return value;
  }
  function localFileIdentity(value, code) {
    value = String(value == null ? '' : value);
    if (value !== bookId && value !== 'localbook:' + bookId) {
      throw outgoingRequestError('file 不是当前本机书籍', code, 400);
    }
    return value;
  }
  function optionalPageScalar(value, label, code) {
    if (value == null || value === '') return null;
    if (typeof value === 'number') {
      return strictInteger(value, 0, 10000000, label, code);
    }
    value = String(value);
    if (!value || value.length > 256 || /[\u0000-\u001f\u007f]/.test(value)) {
      throw outgoingRequestError(label + ' 无效', code, 400);
    }
    return value;
  }
  function methodNotAllowed(message, code) {
    return Promise.resolve(jsonResponse({
      ok: false, code: code, error: message, retryable: false
    }, 405));
  }

  var ACTIVE_READING_ALLOWED_KEYS = [
    'kind', 'file', 'pos', 'title', 'total', 'selection',
    'sel_page', 'sel_anchor', 'reason', 'viewport'
  ];
  function normalizeActiveReadingBody(body) {
    var code = 'BW_LOCAL_ACTIVE_READING_BODY';
    assertObjectFields(body, ACTIVE_READING_ALLOWED_KEYS, ['kind', 'file'], code);
    if (body.kind !== 'pdf' && body.kind !== 'epub') {
      throw outgoingRequestError('kind 必须是 pdf 或 epub', code, 400);
    }
    var record = {
      kind: body.kind,
      file: localFileIdentity(body.file, code),
      ts: nowSeconds()
    };
    if (Object.prototype.hasOwnProperty.call(body, 'pos')) {
      record.pos = strictInteger(body.pos, 0, 10000000, 'pos', code);
    }
    if (Object.prototype.hasOwnProperty.call(body, 'title')) {
      if (typeof body.title !== 'string' || !body.title.trim() || body.title.trim().length > 200) {
        throw outgoingRequestError('title 无效或过长', code, 400);
      }
      record.title = body.title.trim();
    }
    if (Object.prototype.hasOwnProperty.call(body, 'reason')) {
      if (typeof body.reason !== 'string' || !body.reason.trim() || body.reason.trim().length > 40) {
        throw outgoingRequestError('reason 无效或过长', code, 400);
      }
      record.reason = body.reason.trim();
    }
    if (Object.prototype.hasOwnProperty.call(body, 'total')) {
      record.total = strictInteger(body.total, 1, 10000000, 'total', code);
    }
    if (Object.prototype.hasOwnProperty.call(body, 'selection')) {
      if (typeof body.selection !== 'string' || body.selection.length > 400) {
        throw outgoingRequestError('selection 无效或过长', code, 400);
      }
      record.selection = body.selection.trim();
      record.has_selection = !!record.selection;
    }
    if (Object.prototype.hasOwnProperty.call(body, 'sel_page')) {
      if (!Object.prototype.hasOwnProperty.call(body, 'selection')) {
        throw outgoingRequestError('sel_page 必须与 selection 一起提交', code, 400);
      }
      record.sel_page = strictInteger(body.sel_page, 0, 10000000, 'sel_page', code);
    }
    if (Object.prototype.hasOwnProperty.call(body, 'sel_anchor')) {
      if (!Object.prototype.hasOwnProperty.call(body, 'selection') ||
          typeof body.sel_anchor !== 'string' ||
          !body.sel_anchor.trim() || body.sel_anchor.trim().length > 200) {
        throw outgoingRequestError('sel_anchor 无效', code, 400);
      }
      record.sel_anchor = body.sel_anchor.trim();
    }
    if (Object.prototype.hasOwnProperty.call(body, 'viewport')) {
      var viewport = body.viewport;
      assertObjectFields(
        viewport, ['para', 'index', 'ratio', 'progress'], [], code
      );
      var viewportKeys = Object.keys(viewport);
      if (viewportKeys.length !== 1) {
        throw outgoingRequestError('viewport 必须只有一个定位字段', code, 400);
      }
      if (viewportKeys[0] === 'para' || viewportKeys[0] === 'index') {
        strictInteger(viewport[viewportKeys[0]], 0, 10000000, 'viewport', code);
      } else {
        strictFinite(viewport[viewportKeys[0]], 0, 1, 'viewport', code);
      }
    }
    return record;
  }
  function normalizeStoredActiveReading(value) {
    if (value == null) return null;
    var code = 'BW_LOCAL_ACTIVE_READING_CORRUPT';
    try {
      assertObjectFields(value, ACTIVE_READING_ALLOWED_KEYS.concat([
        'ts', 'has_selection'
      ]), ['kind', 'file', 'ts'], code);
      if ((value.kind !== 'pdf' && value.kind !== 'epub') ||
          (value.file !== bookId && value.file !== 'localbook:' + bookId) ||
          !Number.isSafeInteger(value.ts) || value.ts < 0 ||
          (Object.prototype.hasOwnProperty.call(value, 'has_selection') &&
           typeof value.has_selection !== 'boolean')) {
        throw new Error('invalid active record');
      }
      return clone(value);
    } catch (error) {
      if (error && error.code === code) {
        error.httpStatus = 500;
        throw error;
      }
      var corrupt = new RuntimeError(
        '本机 active-reading 状态损坏', code
      );
      corrupt.httpStatus = 500;
      throw corrupt;
    }
  }
  function getActiveReading() {
    requireContextSyncEnabled();
    return readDeviceState('outgoing-active-reading', null).then(function (value) {
      var active = normalizeStoredActiveReading(value);
      var age = active ? Math.max(0, nowSeconds() - active.ts) : null;
      return {
        ok: true,
        enabled: true,
        active: active,
        fresh: !!(active && age <= OUTGOING_ACTIVE_FRESH_S),
        age_sec: age,
        fresh_window_sec: OUTGOING_ACTIVE_FRESH_S
      };
    });
  }
  function postActiveReading(input, init) {
    requireContextSyncEnabled();
    return strictRequestJSON(
      input, init, OUTGOING_BODY_MAX_BYTES, 'BW_LOCAL_ACTIVE_READING_BODY'
    ).then(normalizeActiveReadingBody).then(function (record) {
      return writeDeviceState('outgoing-active-reading', record).then(function () {
        return activeReadingLocalReceipt(record);
      });
    });
  }

  function activeReadingLocalReceipt(record) {
    return {
      ok: true,
      ts: record.ts,
      canonical: {
        kind: record.kind,
        file: record.file,
        page: Object.prototype.hasOwnProperty.call(record, 'pos') ? record.pos : null,
        viewFile: null,
        viewPage: null
      }
    };
  }

  function readNativePiJSON(response, code, label) {
    return response.text().then(function (text) {
      if (new TextEncoder().encode(text).length > OUTGOING_BODY_MAX_BYTES) {
        throw new RuntimeError(label + '响应过大', code);
      }
      var payload;
      try { payload = text ? JSON.parse(text) : null; }
      catch (_) { throw new RuntimeError(label + '响应不是 JSON', code); }
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new RuntimeError(label + '响应结构无效', code);
      }
      return { response: response, payload: payload };
    });
  }

  function piCompatibilityFailure(error, fallbackCode) {
    var failure = nativePiFailure(error);
    return {
      state: 'unconfirmed',
      confirmed: false,
      code: failure.code || fallbackCode,
      error: String(failure.message || 'Pi 兼容链路未确认').slice(0, 400)
    };
  }

  function forwardActiveReadingToPi(url, route, record, signal) {
    var body = {};
    ACTIVE_READING_ALLOWED_KEYS.forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(record, key)) body[key] = record[key];
    });
    return nativePiFetch(url.href, {
      method: 'POST',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: signal
    }, route).then(function (response) {
      return readNativePiJSON(
        response,
        'BW_NATIVE_ACTIVE_READING_PI_RESPONSE',
        'Pi active-reading '
      );
    }).then(function (result) {
      var payload = result.payload;
      if (!result.response.ok || payload.ok !== true) {
        return {
          state: 'rejected',
          confirmed: false,
          status: result.response.status,
          error: String(payload.error || 'Pi active-reading 拒绝').slice(0, 400)
        };
      }
      return {
        state: 'confirmed',
        confirmed: true,
        status: result.response.status
      };
    }).catch(function (error) {
      return piCompatibilityFailure(error, 'BW_NATIVE_ACTIVE_READING_PI');
    });
  }

  function nativeActiveReadingFetch(input, init, url, method, route) {
    if (method === 'GET') {
      return outgoingJSONRoute(function () {
        strictQuery(url, [], [], 'BW_LOCAL_ACTIVE_READING_QUERY');
        return getActiveReading();
      }, 'BW_LOCAL_ACTIVE_READING_FAILED');
    }
    if (method !== 'POST') {
      return methodNotAllowed(
        '本机 active-reading 只接受 GET/POST',
        'BW_LOCAL_ACTIVE_READING_METHOD'
      );
    }
    var localReceipt = null;
    var localRecord = null;
    return outgoingJSONRoute(function () {
      strictQuery(url, [], [], 'BW_LOCAL_ACTIVE_READING_QUERY');
      requireContextSyncEnabled();
      return strictRequestJSON(
        input, init, OUTGOING_BODY_MAX_BYTES, 'BW_LOCAL_ACTIVE_READING_BODY'
      ).then(normalizeActiveReadingBody).then(function (record) {
        localRecord = record;
        return writeDeviceState('outgoing-active-reading', record);
      }).then(function () {
        // The context-only WSS reads this local fact. Pi is a compatibility
        // branch only, so an offline Pi must never make the live Windows
        // snapshot look as if the local write failed.
        localReceipt = activeReadingLocalReceipt(localRecord);
        return forwardActiveReadingToPi(
          url, route, localRecord, requestSignal(input, init)
        );
      }).then(function (compatibility) {
        return Object.assign({}, localReceipt, {
          local_persisted: true,
          windows_context_source: 'native-context-wss',
          pi_compatibility: compatibility
        });
      });
    }, 'BW_LOCAL_ACTIVE_READING_FAILED');
  }

  function normalizeRemoteContextSync(payload) {
    if (!payload || payload.ok !== true || typeof payload.enabled !== 'boolean' ||
        !LOCAL_CONTEXT_SYNC_MODES.has(payload.deliveryMode)) {
      throw new RuntimeError(
        'Pi 上下文同步配置无效', 'BW_NATIVE_CONTEXT_SYNC_PI_RESPONSE'
      );
    }
    return {
      contract: LOCAL_CONTEXT_SYNC_CONTRACT,
      enabled: payload.enabled,
      deliveryMode: payload.deliveryMode,
      ts: Number.isSafeInteger(payload.ts) && payload.ts >= 0
        ? payload.ts : nowSeconds()
    };
  }

  function nativeContextSyncLocalPayload(value, compatibility) {
    return {
      ok: true,
      enabled: value.enabled,
      deliveryMode: value.deliveryMode,
      ts: value.ts,
      local_persisted: true,
      windows_context_source: 'native-context-wss',
      pi_compatibility: compatibility
    };
  }

  function nativeContextSyncFetch(input, init, url, method, route) {
    if (method === 'GET') {
      var local;
      try {
        strictQuery(url, [], [], 'BW_LOCAL_CONTEXT_SYNC_QUERY');
        local = readLocalContextSync();
      } catch (error) {
        return Promise.resolve(outgoingFailureResponse(
          error, 'BW_LOCAL_CONTEXT_SYNC_PREFERENCE', 500
        ));
      }
      return nativePiFetch(url.href, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
        signal: requestSignal(input, init)
      }, route).then(function (response) {
        return readNativePiJSON(
          response, 'BW_NATIVE_CONTEXT_SYNC_PI_RESPONSE', 'Pi context-sync '
        );
      }).then(function (result) {
        if (!result.response.ok || result.payload.ok !== true) {
          return nativeContextSyncLocalPayload(local, {
            state: 'rejected', confirmed: false,
            status: result.response.status,
            error: String(result.payload.error || 'Pi context-sync 拒绝').slice(0, 400)
          });
        }
        var remote = normalizeRemoteContextSync(result.payload);
        var saved = local.enabled === remote.enabled &&
          local.deliveryMode === remote.deliveryMode
          ? local : writeLocalContextSync(remote);
        return nativeContextSyncLocalPayload(saved, {
          state: 'confirmed', confirmed: true, status: result.response.status
        });
      }).catch(function (error) {
        // Local context/WSS remains usable when the legacy Pi branch is down.
        // The explicit compatibility state prevents this from masquerading as
        // a confirmed two-end write.
        return nativeContextSyncLocalPayload(
          local,
          piCompatibilityFailure(error, 'BW_NATIVE_CONTEXT_SYNC_PI')
        );
      }).then(function (payload) { return jsonResponse(payload); });
    }
    if (method !== 'POST') {
      return methodNotAllowed(
        '本机上下文同步只接受 GET/POST',
        'BW_LOCAL_CONTEXT_SYNC_METHOD'
      );
    }
    return strictRequestJSON(
      input, init, OUTGOING_BODY_MAX_BYTES, 'BW_LOCAL_CONTEXT_SYNC_BODY'
    ).then(function (body) {
      if (!exactKeys(body, ['enabled']) &&
          !exactKeys(body, ['enabled', 'deliveryMode'])) {
        throw outgoingRequestError(
          '本机上下文同步请求字段无效', 'BW_LOCAL_CONTEXT_SYNC_BODY', 400
        );
      }
      if (typeof body.enabled !== 'boolean') {
        throw outgoingRequestError(
          '本机上下文同步开关无效', 'BW_LOCAL_CONTEXT_SYNC_BODY', 400
        );
      }
      var previous = readLocalContextSync();
      var deliveryMode = Object.prototype.hasOwnProperty.call(body, 'deliveryMode')
        ? body.deliveryMode : previous.deliveryMode;
      if (!LOCAL_CONTEXT_SYNC_MODES.has(deliveryMode)) {
        throw outgoingRequestError(
          '本机上下文交付模式无效', 'BW_LOCAL_CONTEXT_SYNC_MODE', 400
        );
      }
      var saved = writeLocalContextSync({
        contract: LOCAL_CONTEXT_SYNC_CONTRACT,
        enabled: body.enabled,
        deliveryMode: deliveryMode,
        ts: nowSeconds()
      });
      return nativePiFetch(url.href, {
        method: 'POST',
        headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: saved.enabled, deliveryMode: saved.deliveryMode }),
        signal: requestSignal(input, init)
      }, route).then(function (response) {
        return readNativePiJSON(
          response, 'BW_NATIVE_CONTEXT_SYNC_PI_RESPONSE', 'Pi context-sync '
        );
      }).then(function (result) {
        var compatibility;
        if (!result.response.ok || result.payload.ok !== true ||
            result.payload.enabled !== saved.enabled ||
            result.payload.deliveryMode !== saved.deliveryMode) {
          compatibility = {
            state: 'rejected', confirmed: false,
            status: result.response.status,
            error: String(result.payload.error || 'Pi context-sync 回执不一致').slice(0, 400)
          };
        } else {
          compatibility = {
            state: 'confirmed', confirmed: true, status: result.response.status
          };
        }
        return nativeContextSyncLocalPayload(saved, compatibility);
      }).catch(function (error) {
        return nativeContextSyncLocalPayload(
          saved,
          piCompatibilityFailure(error, 'BW_NATIVE_CONTEXT_SYNC_PI')
        );
      });
    }).then(function (payload) {
      return jsonResponse(payload);
    }).catch(function (error) {
      return outgoingFailureResponse(
        error, 'BW_LOCAL_CONTEXT_SYNC_PREFERENCE', 400
      );
    });
  }

  function validateOutgoingEvent(event) {
    if (!event || typeof event !== 'object' || Array.isArray(event) ||
        event.v !== 1 || !Number.isSafeInteger(event.seq) || event.seq <= 0 ||
        !OUTGOING_EVENT_TYPES.has(event.type) ||
        !Number.isSafeInteger(event.ts) || event.ts < 0 ||
        typeof event.id !== 'string' || !/^[0-9a-f]{16}$/.test(event.id)) {
      throw new RuntimeError(
        '本机 outgoing journal 事件损坏', 'BW_LOCAL_OUTGOING_JOURNAL_CORRUPT'
      );
    }
    validateOpaqueJSON(event, 'BW_LOCAL_OUTGOING_JOURNAL_CORRUPT');
    return event;
  }
  function normalizeOutgoingJournalState(value) {
    if (value == null) {
      return { contract: OUTGOING_JOURNAL_CONTRACT, nextSeq: 1, events: [] };
    }
    if (!exactKeys(value, ['contract', 'nextSeq', 'events']) ||
        value.contract !== OUTGOING_JOURNAL_CONTRACT ||
        !Number.isSafeInteger(value.nextSeq) || value.nextSeq < 1 ||
        !Array.isArray(value.events) || value.events.length > OUTGOING_JOURNAL_KEEP) {
      throw new RuntimeError(
        '本机 outgoing journal 损坏或含未知字段',
        'BW_LOCAL_OUTGOING_JOURNAL_CORRUPT'
      );
    }
    var previous = 0;
    value.events.forEach(function (event) {
      validateOutgoingEvent(event);
      if (previous && event.seq !== previous + 1) {
        throw new RuntimeError(
          '本机 outgoing journal 序号不连续',
          'BW_LOCAL_OUTGOING_JOURNAL_CORRUPT'
        );
      }
      previous = event.seq;
    });
    var expectedNext = value.events.length
      ? value.events[value.events.length - 1].seq + 1 : value.nextSeq;
    if (value.events.length && value.nextSeq !== expectedNext) {
      throw new RuntimeError(
        '本机 outgoing journal 游标损坏',
        'BW_LOCAL_OUTGOING_JOURNAL_CORRUPT'
      );
    }
    return clone(value);
  }
  function readOutgoingJournalState() {
    return readDeviceState('outgoing-journal', null).then(
      normalizeOutgoingJournalState
    ).catch(function (error) {
      if (error && error.code === 'BW_LOCAL_OUTGOING_JOURNAL_CORRUPT') {
        error.httpStatus = 500;
      }
      throw error;
    });
  }
  function journalWithEvent(journal, type, payload, eventId) {
    if (!OUTGOING_EVENT_TYPES.has(type)) {
      throw new RuntimeError(
        '本机 outgoing event 类型无效', 'BW_LOCAL_OUTGOING_JOURNAL_CORRUPT'
      );
    }
    if (eventId && journal.events.some(function (event) { return event.id === eventId; })) {
      return { journal: journal, event: null };
    }
    var event = Object.assign({
      v: 1,
      seq: journal.nextSeq,
      type: type,
      ts: nowSeconds(),
      id: eventId || randomHex(8)
    }, clone(payload));
    validateOutgoingEvent(event);
    var events = journal.events.concat([event]);
    if (events.length > OUTGOING_JOURNAL_KEEP) {
      events = events.slice(events.length - OUTGOING_JOURNAL_KEEP);
    }
    return {
      journal: {
        contract: OUTGOING_JOURNAL_CONTRACT,
        nextSeq: event.seq + 1,
        events: events
      },
      event: event
    };
  }

  // App-local books never pass through the Pi page-context producer.  Give
  // the shared Reader bridge one local writer that joins the same monotonic
  // journal as focus/drawing instead of inventing a second sequence stream.
  // The caller supplies already-bounded display context; this layer owns
  // identity validation and the durable sequence number.
  function publishLocalPageContext(value) {
    requireContextSyncEnabled();
    var code = 'BW_LOCAL_OUTGOING_PAGE_CONTEXT';
    assertObjectFields(value, [
      'kind', 'file', 'page', 'title', 'text', 'textAvailable',
      'textSource', 'fallbackReason', 'truncated'
    ], [
      'kind', 'file', 'page', 'title', 'text', 'textAvailable',
      'textSource', 'fallbackReason', 'truncated'
    ], code);
    if (value.kind !== 'pdf' && value.kind !== 'epub') {
      throw outgoingRequestError('kind 必须是 pdf 或 epub', code, 400);
    }
    var file = localFileIdentity(value.file, code);
    var page = optionalPageScalar(value.page, 'page', code);
    if (page == null) {
      throw outgoingRequestError('page 不能为空', code, 400);
    }
    if (typeof value.title !== 'string' || value.title.length > 1024 ||
        /[\u0000-\u001f\u007f]/.test(value.title)) {
      throw outgoingRequestError('title 无效或过长', code, 400);
    }
    if (typeof value.text !== 'string' || value.text.length > 12000 ||
        /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value.text)) {
      throw outgoingRequestError('text 无效或过长', code, 400);
    }
    if (typeof value.textAvailable !== 'boolean' ||
        value.textAvailable !== !!value.text.trim() ||
        typeof value.textSource !== 'string' ||
        !/^[a-z][a-z0-9._-]{0,95}$/.test(value.textSource) ||
        typeof value.truncated !== 'boolean' ||
        (value.fallbackReason != null &&
          (typeof value.fallbackReason !== 'string' ||
           value.fallbackReason.length > 400 ||
           /[\u0000-\u001f\u007f]/.test(value.fallbackReason)))) {
      throw outgoingRequestError('页面文字元数据无效', code, 400);
    }
    return serializeOutgoingMutation(function () {
      return readOutgoingJournalState().then(function (journal) {
        var appended = journalWithEvent(journal, 'page.context', {
          event: 'page.context',
          stable: true,
          book_id: file,
          file: file,
          kind: value.kind,
          page: page,
          title: value.title,
          text_available: value.textAvailable,
          page_context: {
            reason: 'app-local-visible-window',
            text: value.text,
            text_available: value.textAvailable,
            text_source: value.textSource,
            fallback_reason: value.fallbackReason,
            truncated: value.truncated,
            visual: null,
            embeds: { highlights: 0, blocks: 0, unanchored: [] }
          }
        });
        return writeDeviceState('outgoing-journal', appended.journal).then(function () {
          return {
            ok: true,
            contract: OUTGOING_CONTEXT_CONTRACT,
            seq: appended.event.seq,
            eventId: appended.event.id
          };
        });
      });
    });
  }
  function outgoingJournal(url) {
    requireContextSyncEnabled();
    strictQuery(
      url, ['since', 'limit', 'wait'], [], 'BW_LOCAL_OUTGOING_JOURNAL_QUERY'
    );
    var since = url.searchParams.has('since')
      ? strictInteger(url.searchParams.get('since'), 0, Number.MAX_SAFE_INTEGER,
        'since', 'BW_LOCAL_OUTGOING_JOURNAL_QUERY') : 0;
    var limit = url.searchParams.has('limit')
      ? strictInteger(url.searchParams.get('limit'), 1, OUTGOING_JOURNAL_LIMIT,
        'limit', 'BW_LOCAL_OUTGOING_JOURNAL_QUERY') : 200;
    var wait = url.searchParams.has('wait')
      ? strictFinite(url.searchParams.get('wait'), 0, OUTGOING_JOURNAL_MAX_WAIT_S,
        'wait', 'BW_LOCAL_OUTGOING_JOURNAL_QUERY') : 0;
    return readOutgoingJournalState().then(function (journal) {
      var rows = journal.events;
      var head = rows.length ? rows[0].seq : 0;
      var tail = rows.length ? rows[rows.length - 1].seq : 0;
      var gap = !!(rows.length && since && since + 1 < head);
      var events = rows.filter(function (event) {
        return event.seq > since;
      }).slice(0, limit);
      var result = {
        ok: true,
        contract: OUTGOING_CONTEXT_CONTRACT,
        cursor: tail,
        head: head,
        events: events,
        gap: gap,
        note: gap
          ? '游标落后于保留窗口,中间事件已丢失;请按 head 重新对齐' : '',
        waited: 0
      };
      // IndexedDB has no blocking read primitive. Explicitly deny an empty
      // long-poll so the existing pump takes its 1s low-overhead retry path;
      // returning immediately without this marker would create a busy loop.
      if (!events.length && wait > 0) result.waitDenied = true;
      return result;
    });
  }

  function normalizeOutgoingFocusState(value) {
    if (value == null) {
      return { contract: OUTGOING_CONTEXT_CONTRACT, seq: 0, current: null };
    }
    if (!exactKeys(value, ['contract', 'seq', 'current']) ||
        value.contract !== OUTGOING_CONTEXT_CONTRACT ||
        !Number.isSafeInteger(value.seq) || value.seq < 0) {
      throw new RuntimeError(
        '本机 focus 状态损坏或含未知字段', 'BW_LOCAL_OUTGOING_FOCUS_CORRUPT'
      );
    }
    var current = value.current;
    if (current != null &&
        (!exactKeys(current, ['kind', 'ref', 'ts', 'seq', 'task', 'cancelled']) ||
         (current.kind != null && !OUTGOING_FOCUS_KINDS.has(current.kind)) ||
         (current.ref != null && (typeof current.ref !== 'object' || Array.isArray(current.ref))) ||
         !Number.isFinite(current.ts) || current.ts < 0 ||
         !Number.isSafeInteger(current.seq) || current.seq < 1 ||
         current.seq !== value.seq || typeof current.task !== 'string' ||
         typeof current.cancelled !== 'boolean')) {
      throw new RuntimeError(
        '本机 focus 当前值损坏', 'BW_LOCAL_OUTGOING_FOCUS_CORRUPT'
      );
    }
    if (current && current.ref != null) {
      validateOpaqueJSON(current.ref, 'BW_LOCAL_OUTGOING_FOCUS_CORRUPT');
    }
    return clone(value);
  }
  function readOutgoingFocusState() {
    return readDeviceState('outgoing-focus', null).then(
      normalizeOutgoingFocusState
    ).catch(function (error) {
      if (error && error.code === 'BW_LOCAL_OUTGOING_FOCUS_CORRUPT') {
        error.httpStatus = 500;
      }
      throw error;
    });
  }
  function focusSnapshot(focusState) {
    var current = focusState.current;
    if (!current) {
      return {
        contract: OUTGOING_CONTEXT_CONTRACT,
        state: 'never',
        focus: null,
        note: '本会话从未上报过焦点(不是「没有焦点」)'
      };
    }
    var age = Math.max(0, Date.now() / 1000 - current.ts);
    if (current.cancelled) {
      return {
        contract: OUTGOING_CONTEXT_CONTRACT,
        state: 'cancelled',
        focus: null,
        cancelledObject: { kind: current.kind, ref: clone(current.ref) },
        ageSec: Math.round(age * 1000) / 1000,
        note: '此前的焦点对象已被明确取消,不要再当作当前选中'
      };
    }
    if (age > OUTGOING_FOCUS_FRESH_S) {
      return {
        contract: OUTGOING_CONTEXT_CONTRACT,
        state: 'stale',
        focus: null,
        lastObject: { kind: current.kind, ref: clone(current.ref) },
        ageSec: Math.round(age * 1000) / 1000,
        note: '焦点上报已超过 300 秒未更新,按未知处理'
      };
    }
    return {
      contract: OUTGOING_CONTEXT_CONTRACT,
      state: 'active',
      focus: {
        kind: current.kind, ref: clone(current.ref),
        seq: current.seq, task: current.task
      },
      ageSec: Math.round(age * 1000) / 1000
    };
  }
  function postOutgoingFocus(input, init) {
    requireContextSyncEnabled();
    return strictRequestJSON(
      input, init, OUTGOING_BODY_MAX_BYTES, 'BW_LOCAL_OUTGOING_FOCUS_BODY'
    ).then(function (body) {
      var cancel = body.cancel === true;
      if (cancel) {
        assertObjectFields(
          body, ['cancel', 'task'], ['cancel'], 'BW_LOCAL_OUTGOING_FOCUS_BODY'
        );
      } else {
        assertObjectFields(
          body, ['kind', 'ref', 'task'], ['kind', 'ref'],
          'BW_LOCAL_OUTGOING_FOCUS_BODY'
        );
      }
      var task = Object.prototype.hasOwnProperty.call(body, 'task') ? body.task : '';
      if (typeof task !== 'string' || task.length > 80 ||
          /[\u0000-\u001f\u007f]/.test(task)) {
        throw outgoingRequestError(
          'task 无效或过长', 'BW_LOCAL_OUTGOING_FOCUS_BODY', 400
        );
      }
      if (!cancel) {
        if (!OUTGOING_FOCUS_KINDS.has(body.kind) ||
            !body.ref || typeof body.ref !== 'object' || Array.isArray(body.ref) ||
            !Object.keys(body.ref).length) {
          throw outgoingRequestError(
            'focus.kind/ref 无效', 'BW_LOCAL_OUTGOING_FOCUS_BODY', 400
          );
        }
        validateOpaqueJSON(body.ref, 'BW_LOCAL_OUTGOING_FOCUS_BODY');
        if (new TextEncoder().encode(JSON.stringify(body.ref)).length > OUTGOING_REF_MAX_BYTES) {
          throw outgoingRequestError(
            'focus.ref 过大', 'BW_LOCAL_OUTGOING_FOCUS_BODY', 413
          );
        }
        if (Object.prototype.hasOwnProperty.call(body.ref, 'file')) {
          localFileIdentity(body.ref.file, 'BW_LOCAL_OUTGOING_FOCUS_BODY');
        }
        if (Object.prototype.hasOwnProperty.call(body.ref, 'page')) {
          optionalPageScalar(body.ref.page, 'focus.ref.page', 'BW_LOCAL_OUTGOING_FOCUS_BODY');
        }
      }
      return serializeOutgoingMutation(function () {
        return Promise.all([
          readOutgoingFocusState(), readOutgoingJournalState()
        ]).then(function (values) {
          var focusState = values[0], journal = values[1];
          var seq = focusState.seq + 1;
          var previous = focusState.current;
          var current = {
            kind: cancel ? (previous && previous.kind || null) : body.kind,
            ref: cancel ? (previous && clone(previous.ref) || null) : clone(body.ref),
            ts: Date.now() / 1000,
            seq: seq,
            task: task,
            cancelled: cancel
          };
          var payload = cancel ? {
            action: 'cancel',
            taskId: task,
            cancelledObject: { kind: current.kind, ref: clone(current.ref) },
            seq_focus: seq
          } : {
            action: 'set', kind: current.kind, ref: clone(current.ref),
            taskId: task, seq_focus: seq
          };
          var appended = journalWithEvent(journal, 'focus', payload);
          var nextFocus = {
            contract: OUTGOING_CONTEXT_CONTRACT, seq: seq, current: current
          };
          var suffix = randomHex(12);
          return stores.device.batch([
            deviceStateMutation('outgoing-focus', nextFocus, suffix + '-focus'),
            deviceStateMutation('outgoing-journal', appended.journal, suffix + '-journal')
          ]).then(function () {
            return Object.assign({ ok: true }, clone(current));
          });
        });
      });
    });
  }

  function validateInkMap(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new RuntimeError(
        '本机墨迹记录不是页面映射', 'BW_LOCAL_OUTGOING_DRAWING_CORRUPT'
      );
    }
    Object.keys(value).forEach(function (key) {
      if (!key || key.length > 256 || !Array.isArray(value[key])) {
        throw new RuntimeError(
          '本机墨迹页面记录损坏', 'BW_LOCAL_OUTGOING_DRAWING_CORRUPT'
        );
      }
      value[key].forEach(function (stroke) {
        if (!stroke || typeof stroke !== 'object' || Array.isArray(stroke)) {
          throw new RuntimeError(
            '本机墨迹笔画记录损坏', 'BW_LOCAL_OUTGOING_DRAWING_CORRUPT'
          );
        }
        validateOpaqueJSON(stroke, 'BW_LOCAL_OUTGOING_DRAWING_CORRUPT');
      });
    });
    if (new TextEncoder().encode(JSON.stringify(value)).length > 24 * 1024 * 1024) {
      throw new RuntimeError(
        '本机墨迹记录超过安全上限', 'BW_LOCAL_OUTGOING_DRAWING_CORRUPT'
      );
    }
    return value;
  }
  function normalizeDrawingTracker(value) {
    if (value == null) {
      return { contract: OUTGOING_DRAWING_CONTRACT, entries: {} };
    }
    if (!exactKeys(value, ['contract', 'entries']) ||
        value.contract !== OUTGOING_DRAWING_CONTRACT ||
        !value.entries || typeof value.entries !== 'object' || Array.isArray(value.entries) ||
        Object.keys(value.entries).length > 10000) {
      throw new RuntimeError(
        '本机绘图版本状态损坏或含未知字段',
        'BW_LOCAL_OUTGOING_DRAWING_CORRUPT'
      );
    }
    Object.keys(value.entries).forEach(function (key) {
      var entry = value.entries[key];
      if (!exactKeys(entry, [
        'digest', 'changedAt', 'stableAt', 'revision', 'journaledRevision'
      ]) || !/^[0-9a-f]{64}$/.test(entry.digest) ||
          !Number.isFinite(entry.changedAt) || entry.changedAt < 0 ||
          (entry.stableAt != null && (!Number.isFinite(entry.stableAt) || entry.stableAt < 0)) ||
          (entry.revision != null && !/^dr_[0-9a-f]{16}$/.test(entry.revision)) ||
          (entry.journaledRevision != null && !/^dr_[0-9a-f]{16}$/.test(entry.journaledRevision))) {
        throw new RuntimeError(
          '本机绘图版本条目损坏', 'BW_LOCAL_OUTGOING_DRAWING_CORRUPT'
        );
      }
    });
    return clone(value);
  }
  function drawingMaterial(page) {
    return Promise.all([
      readDocumentOutgoingState('ink', {}),
      readDocumentOutgoingState('epub-ink', {})
    ]).then(function (values) {
      var pdf = validateInkMap(values[0]);
      var epub = validateInkMap(values[1]);
      if (page == null) return { pdf: pdf, epub: epub };
      var key = String(page);
      return {
        pdf: Array.isArray(pdf[key]) ? pdf[key] : [],
        epub: Array.isArray(epub[key]) ? epub[key] : []
      };
    });
  }
  function materialIsEmpty(material) {
    function hasStroke(value) {
      if (Array.isArray(value)) return value.length > 0;
      return Object.keys(value || {}).some(function (key) {
        return Array.isArray(value[key]) && value[key].length > 0;
      });
    }
    return !hasStroke(material.pdf) && !hasStroke(material.epub);
  }
  function drawingSnapshot(file, page, entry, empty, now) {
    var stable = !!(entry && entry.revision && !empty);
    var age = entry ? Math.max(0, now - entry.changedAt) : 0;
    var freshness = empty ? 'none'
      : (age <= OUTGOING_DRAWING_FRESH_S ? 'recent' : 'stale');
    return {
      contract: OUTGOING_CONTEXT_CONTRACT,
      file: file,
      page: page,
      freshness: freshness,
      lastEditedAt: empty || !entry ? null : Math.round(entry.changedAt * 1000) / 1000,
      freshWindowS: OUTGOING_DRAWING_FRESH_S,
      inProgress: !empty && !stable,
      stable: stable,
      drawingRevision: stable ? entry.revision : null,
      pendingSince: !empty && !stable ? Math.round(age * 1000) / 1000 : null,
      ref: stable ? {
        kind: 'drawing', file: file, page: page, revision: entry.revision
      } : null,
      empty: empty,
      artifact: 'revision-only',
      compositeAvailable: false,
      note: '本机接口只提供墨迹内容版本；未生成或伪装合成图地址'
    };
  }
  function outgoingDrawing(url) {
    requireContextSyncEnabled();
    strictQuery(
      url, ['file', 'page'], ['file'], 'BW_LOCAL_OUTGOING_DRAWING_QUERY'
    );
    var file = localFileIdentity(
      url.searchParams.get('file'), 'BW_LOCAL_OUTGOING_DRAWING_QUERY'
    );
    var page = url.searchParams.has('page')
      ? optionalPageScalar(
        url.searchParams.get('page'), 'page', 'BW_LOCAL_OUTGOING_DRAWING_QUERY'
      ) : null;
    var trackerKey = page == null ? '*' : String(page);
    return serializeOutgoingMutation(function () {
      return drawingMaterial(page).then(function (material) {
        var empty = materialIsEmpty(material);
        return sha256Hex(canonicalJSONString(material)).then(function (digest) {
          return readDocumentOutgoingState(
            'outgoing-drawing-tracker', null
          ).then(normalizeDrawingTracker).then(function (tracker) {
            var now = Date.now() / 1000;
            var entry = tracker.entries[trackerKey];
            var changed = false;
            if (!entry || entry.digest !== digest) {
              entry = {
                digest: digest,
                changedAt: now,
                stableAt: null,
                revision: null,
                journaledRevision: null
              };
              tracker.entries[trackerKey] = entry;
              changed = true;
            } else if (!empty && !entry.revision &&
                       now - entry.changedAt >= OUTGOING_DRAWING_STABLE_S) {
              entry.revision = 'dr_' + digest.slice(0, 16);
              entry.stableAt = now;
              changed = true;
            }
            var snapshot = drawingSnapshot(file, page, entry, empty, now);
            if (snapshot.stable && entry.journaledRevision !== entry.revision) {
              return Promise.all([
                readOutgoingJournalState(),
                sha256Hex('drawing|' + bookId + '|' + trackerKey + '|' + entry.revision)
              ]).then(function (values) {
                var appended = journalWithEvent(values[0], 'drawing', {
                  state: 'stable',
                  file: file,
                  page: page,
                  drawingRevision: entry.revision,
                  ref: clone(snapshot.ref),
                  revisionOnly: true
                }, values[1].slice(0, 16));
                entry.journaledRevision = entry.revision;
                return Promise.all([
                  writeDocumentOutgoingState('outgoing-drawing-tracker', tracker),
                  appended.event
                    ? writeDeviceState('outgoing-journal', appended.journal)
                    : Promise.resolve(null)
                ]).then(function () { return snapshot; });
              });
            }
            if (!changed) return snapshot;
            return writeDocumentOutgoingState(
              'outgoing-drawing-tracker', tracker
            ).then(function () { return snapshot; });
          });
        });
      });
    });
  }
  function outgoingState(url) {
    requireContextSyncEnabled();
    strictQuery(
      url, ['file', 'page'], [], 'BW_LOCAL_OUTGOING_STATE_QUERY'
    );
    if (url.searchParams.has('page') && !url.searchParams.has('file')) {
      throw outgoingRequestError(
        'page 必须与 file 一起查询', 'BW_LOCAL_OUTGOING_STATE_QUERY', 400
      );
    }
    var drawing = url.searchParams.has('file') ? outgoingDrawing(url) : null;
    return Promise.all([
      readOutgoingFocusState(), drawing || Promise.resolve(null)
    ]).then(function (values) {
      var result = {
        contract: OUTGOING_CONTEXT_CONTRACT,
        ok: true,
        focus: focusSnapshot(values[0])
      };
      if (values[1]) result.drawing = values[1];
      return result;
    });
  }

  // One text provider feeds the existing PDF char-layer, search and assistant
  // surfaces. Embedded PDF text stays in JavaScript; Apple/Pi OCR is a passive
  // read through the native reply bridge. These reads must never start OCR or
  // select a fallback engine: preprocessing is an explicit native UI action.
  var PAGE_TEXT_REQUEST_CONTRACT = 'reader-native-page-text-request/1';
  var PAGE_TEXT_RESPONSE_CONTRACT = 'reader-native-page-text-response/1';
  var PAGE_TEXT_UPDATE_CONTRACT = 'reader-native-page-text-update/1';
  var PAGE_TEXT_PROVIDER_CONTRACT = 'reader-page-text-provider/1';
  var PAGE_TEXT_STATES = new Set(['idle', 'pending', 'ready', 'readyEmpty', 'failed']);
  var PAGE_TEXT_NATIVE_SOURCES = new Set(['apple', 'pi', 'pc']);
  var PAGE_TEXT_AUTHORITIES = new Set(['supplemental', 'local-override']);
  var PAGE_TEXT_RESPONSE_COMMON_KEYS = new Set([
    'contract', 'action', 'requestId', 'ok', 'state', 'source', 'revision', 'error'
  ]);
  var PAGE_TEXT_RESPONSE_ACTION_KEYS = Object.freeze({
    'page-chars': new Set([
      'page', 'pageWidth', 'pageHeight', 'chars', 'furigana',
      'wordSegmentation', 'characterGeometry', 'formulaCoverage', 'formulaRegions',
      'textAuthority'
    ]),
    status: new Set(['progress']),
    search: new Set(['matches', 'total', 'pages', 'incomplete']),
    'ocr-selection': new Set(['page', 'text', 'cv', 'persisted', 'textAuthority']),
    'reocr-page': new Set(['page', 'chars', 'cv', 'textAuthority']),
    'clear-reocr-page': new Set(['page', 'cleared', 'cv', 'textAuthority'])
  });
  var PAGE_TEXT_WORD_STATES = new Set(['ready', 'partial', 'unavailable']);
  var PAGE_TEXT_GEOMETRY_STATES = new Set(['exact', 'estimated', 'unavailable']);
  var PAGE_TEXT_FORMULA_COVERAGE = new Set(['unknown', 'unavailable', 'partial', 'complete']);
  var PAGE_TEXT_PROGRESS_KEYS = new Set([
    'total', 'ready', 'pending', 'failed', 'activePage', 'currentPage',
    'textProgress', 'wordProgress', 'formulaProgress',
    'formulaPendingRegions', 'formulaFailedRegions'
  ]);
  var PAGE_TEXT_PHASE_PROGRESS_KEYS = new Set([
    'total', 'completed', 'pending', 'failed', 'unavailable'
  ]);
  var PAGE_TEXT_UPDATE_KEYS = new Set([
    'contract', 'localBookId', 'page', 'state', 'source', 'revision'
  ]);
  var PAGE_TEXT_CHAR_KEYS = new Set([
    'c', 'x0', 'y0', 'x1', 'y1', 'w', 'bk', 'sp', 'b', 'fml', 'flx'
  ]);
  var PAGE_TEXT_FURIGANA_KEYS = new Set(['x0', 'y0', 'x1', 'y1', 'rt', 'wd', 'ctx']);
  var PAGE_TEXT_FORMULA_KEYS = new Set([
    'id', 'x0', 'y0', 'x1', 'y1', 'state', 'latex', 'multiline', 'error'
  ]);
  var embeddedPageText = Object.create(null);
  var embeddedPageLoader = null;
  var embeddedPageCount = 0;
  var nativePageTextCache = Object.create(null);
  var nativePageTextPending = Object.create(null);
  var nativePageTextGeneration = Object.create(null);
  // A native-only PDFKit page may first answer idle while Swift is still
  // establishing the current file's full-content identity. Keep that page in
  // the document's passive read set even though transient replies are not
  // cached, so the later whole-layer update can ask its char layer to retry.
  var nativePageTextKnownPages = Object.create(null);
  var nativeFormulaPrefetchPending = Object.create(null);
  var nativeTextOverridePages = Object.create(null);
  var nativeSearchCache = new Map();
  var nativeSearchPending = new Map();
  var nativeSearchGeneration = 0;

  function pageTextError(code, message, retryable) {
    return {
      code: String(code || 'BW_PAGE_TEXT_FAILED').slice(0, 96),
      message: String(message || '页面文字不可用').slice(0, 500),
      retryable: !!retryable
    };
  }
  function pageTextRequestId() {
    return 'pt-' + randomHex(12);
  }
  function validPage(value) {
    value = Number(value);
    return Number.isInteger(value) && value >= 1 && value <= 100000 ? value : 0;
  }
  function normalizedPageSize(value) {
    value = Number(value);
    return Number.isFinite(value) && value > 0 && value <= 100000 ? value : 0;
  }
  // Drops the characters it cannot read, not the page they came from.
  //
  // Every rejection here used to return null for the whole page, so a single
  // malformed glyph -- one unexpected key, one out-of-range box -- erased the
  // entire text layer. And the text layer is what selection is built on: with
  // it gone there is nothing to select, so no highlight, no lookup, no
  // sentence. The reader then falls back to OCR, whose lower quality shows up
  // as characters recognised a line apart. One bad glyph, three symptoms, none
  // of them pointing at the glyph.
  //
  // A page of prose does not become unreadable because one box is wrong. Bad
  // characters are skipped and counted; the page survives. Only a page with
  // nothing usable left is refused, and the count is reported so a page that
  // silently lost half its glyphs can be told from one that lost none.
  function normalizePageTextChars(raw) {
    if (!Array.isArray(raw) || raw.length > 250000) return null;
    var out = [];
    var dropped = 0;
    for (var index = 0; index < raw.length; index += 1) {
      var item = raw[index];
      if (!item || typeof item !== 'object' || Array.isArray(item) ||
          Object.keys(item).some(function (key) { return !PAGE_TEXT_CHAR_KEYS.has(key); })) {
        dropped += 1;
        continue;
      }
      var c = String(item.c == null ? '' : item.c);
      var x0 = Number(item.x0), y0 = Number(item.y0);
      var x1 = Number(item.x1), y1 = Number(item.y1);
      if (!c || c.length > 16 || ![x0, y0, x1, y1].every(Number.isFinite) ||
          x0 < 0 || y0 < 0 || x1 < x0 || y1 < y0 ||
          x1 > 100000 || y1 > 100000 ||
          (item.w != null && !Number.isInteger(Number(item.w))) ||
          (item.bk != null && !Number.isInteger(Number(item.bk))) ||
          (item.flx != null && typeof item.flx !== 'string')) {
        dropped += 1;
        continue;
      }
      out.push({
        c: c,
        x0: x0, y0: y0, x1: x1, y1: y1,
        w: Number.isInteger(Number(item.w)) ? Number(item.w) : -1,
        bk: Number.isInteger(Number(item.bk)) ? Number(item.bk) : -1,
        sp: !!item.sp,
        fml: !!item.fml,
        flx: item.fml ? String(item.flx || '').slice(0, 4000) : ''
      });
    }
    // An originally empty page is a valid, selectable-text-free PDF page. It
    // is different from a non-empty payload where every character failed
    // validation: the former is readyEmpty, while the latter must stay an
    // explicit invalid text layer instead of disguising corruption as a
    // successfully empty page.
    if (!out.length && raw.length) return null;
    if (dropped) {
      try {
        if (window.__bwProbe) {
          window.__bwProbe.probe(
            'page-chars',
            '跳过 ' + dropped + ' 个无法解析的字符，保留 ' + out.length + ' 个'
          );
        }
      } catch (_) {}
    }
    return out;
  }

  function normalizePageTextFurigana(raw) {
    if (raw == null) return [];
    if (!Array.isArray(raw) || raw.length > 50000) return null;
    var out = [];
    for (var index = 0; index < raw.length; index += 1) {
      var item = raw[index];
      if (!item || typeof item !== 'object' || Array.isArray(item) ||
          Object.keys(item).some(function (key) { return !PAGE_TEXT_FURIGANA_KEYS.has(key); })) return null;
      var x0 = Number(item.x0), y0 = Number(item.y0);
      var x1 = Number(item.x1), y1 = Number(item.y1);
      var rt = String(item.rt == null ? '' : item.rt).trim();
      if (!rt || rt.length > 500 || ![x0, y0, x1, y1].every(Number.isFinite) ||
          x0 < 0 || y0 < 0 || x1 < x0 || y1 < y0 ||
          x1 > 100000 || y1 > 100000) return null;
      out.push({
        x0: x0, y0: y0, x1: x1, y1: y1,
        rt: rt,
        wd: String(item.wd == null ? '' : item.wd).slice(0, 500),
        ctx: String(item.ctx == null ? '' : item.ctx).slice(0, 500)
      });
    }
    return out;
  }
  function embeddedPageResult(page, input) {
    input = input || {};
    // Only an explicitly supplied empty array means a genuinely empty text
    // layer. Missing or non-array payloads are malformed, not readyEmpty.
    var chars = normalizePageTextChars(input.chars);
    var furigana = normalizePageTextFurigana(input.furigana);
    var pageWidth = normalizedPageSize(input.pageWidth || input.page_w);
    var pageHeight = normalizedPageSize(input.pageHeight || input.page_h);
    if (!chars || !furigana || !pageWidth || !pageHeight) {
      throw new RuntimeError('PDF 内嵌文字层无效', 'BW_PAGE_TEXT_EMBEDDED_INVALID');
    }
    return Object.freeze({
      state: chars.length ? 'ready' : 'readyEmpty',
      source: 'embedded',
      revision: String(input.revision || ('embedded-page-' + page)).slice(0, 160),
      page: page,
      pageWidth: pageWidth,
      pageHeight: pageHeight,
      chars: chars,
      furigana: furigana,
      wordSegmentation: chars.some(function (item) { return item.w >= 0; }) ? 'ready' : 'unavailable',
      // PDF.js exposes item geometry, but per-character boxes below are bounded
      // estimates within each item rather than server/Pi exact glyph boxes.
      characterGeometry: chars.length ? 'estimated' : 'unavailable',
      formulaCoverage: 'unknown',
      formulaRegions: [],
      textAuthority: 'supplemental',
      error: null
    });
  }
  function registerEmbeddedPage(page, input) {
    page = validPage(page);
    if (!page) throw new RuntimeError('PDF 页码无效', 'BW_PAGE_TEXT_PAGE');
    embeddedPageText[page] = embeddedPageResult(page, input);
    return embeddedPageText[page];
  }
  function setEmbeddedPageLoader(loader, pageCount) {
    if (loader != null && typeof loader !== 'function') {
      throw new RuntimeError('PDF 文字加载器无效', 'BW_PAGE_TEXT_LOADER');
    }
    embeddedPageLoader = loader || null;
    pageCount = Number(pageCount);
    embeddedPageCount = Number.isInteger(pageCount) && pageCount > 0 && pageCount <= 100000
      ? pageCount : 0;
    return true;
  }
  function loadEmbeddedPage(page) {
    page = validPage(page);
    if (!page) return Promise.reject(new RuntimeError('PDF 页码无效', 'BW_PAGE_TEXT_PAGE'));
    if (embeddedPageText[page]) return Promise.resolve(embeddedPageText[page]);
    if (!embeddedPageLoader) return Promise.resolve(null);
    return Promise.resolve(embeddedPageLoader(page)).then(function (result) {
      if (!result) return null;
      return registerEmbeddedPage(page, result);
    });
  }
  function nativePageTextHandler() {
    var handlers = root.webkit && root.webkit.messageHandlers;
    var handler = handlers && handlers.bwNativePageText;
    return handler && typeof handler.postMessage === 'function' ? handler : null;
  }
  function nativePageTextRequest(action, fields) {
    var handler = nativePageTextHandler();
    if (!handler) return Promise.resolve(null);
    var requestId = pageTextRequestId();
    var request = Object.assign({
      contract: PAGE_TEXT_REQUEST_CONTRACT,
      action: action,
      requestId: requestId,
      localBookId: bookId
    }, fields || {});
    return Promise.resolve(handler.postMessage(request)).then(function (raw) {
      if (!raw || raw.contract !== PAGE_TEXT_RESPONSE_CONTRACT ||
          raw.action !== action || raw.requestId !== requestId ||
          typeof raw.ok !== 'boolean' || !PAGE_TEXT_STATES.has(raw.state)) {
        throw new RuntimeError('原生文字响应合同无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
      }
      var actionKeys = PAGE_TEXT_RESPONSE_ACTION_KEYS[action];
      if (!actionKeys || Object.keys(raw).some(function (key) {
        return !PAGE_TEXT_RESPONSE_COMMON_KEYS.has(key) && !actionKeys.has(key);
      })) {
        throw new RuntimeError('原生文字响应含未知字段', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
      }
      if (raw.error != null) {
        if (!raw.error || typeof raw.error !== 'object' || Array.isArray(raw.error) ||
            Object.keys(raw.error).some(function (key) {
              return ['code', 'message', 'retryable'].indexOf(key) < 0;
            }) || typeof raw.error.code !== 'string' ||
            typeof raw.error.message !== 'string' ||
            typeof raw.error.retryable !== 'boolean') {
          throw new RuntimeError('原生文字错误字段无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
        }
      }
      var source = raw.source == null ? null : String(raw.source);
      if (source !== null && !PAGE_TEXT_NATIVE_SOURCES.has(source)) {
        throw new RuntimeError('原生文字来源无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
      }
      var textAuthority = raw.textAuthority == null
        ? null : String(raw.textAuthority);
      if (textAuthority !== null && !PAGE_TEXT_AUTHORITIES.has(textAuthority)) {
        throw new RuntimeError('原生文字权威字段无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
      }
      return Object.assign({}, raw, {
        source: source,
        revision: String(raw.revision || '').slice(0, 160),
        textAuthority: textAuthority
      });
    });
  }
  function normalizeFormulaRegions(raw) {
    if (raw == null) return [];
    if (!Array.isArray(raw) || raw.length > 1000) {
      throw new RuntimeError('公式区域响应无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
    }
    return raw.map(function (item, index) {
      if (!item || typeof item !== 'object' || Array.isArray(item) ||
          Object.keys(item).some(function (key) { return !PAGE_TEXT_FORMULA_KEYS.has(key); })) {
        throw new RuntimeError('公式区域响应无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
      }
      var state = String(item.state || '');
      var x0 = Number(item.x0), y0 = Number(item.y0);
      var x1 = Number(item.x1), y1 = Number(item.y1);
      if (['pending', 'ready', 'failed'].indexOf(state) < 0 ||
          ![x0, y0, x1, y1].every(Number.isFinite) ||
          x0 < 0 || y0 < 0 || x1 <= x0 || y1 <= y0 ||
          x1 > 100000 || y1 > 100000) {
        throw new RuntimeError('公式区域响应无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
      }
      var latex = String(item.latex || '').trim().slice(0, 4000);
      if (state === 'ready' && !latex) {
        throw new RuntimeError('已完成公式缺少 LaTeX', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
      }
      if (item.error != null && (!item.error || typeof item.error !== 'object' ||
          Array.isArray(item.error) || Object.keys(item.error).some(function (key) {
            return ['code', 'message', 'retryable'].indexOf(key) < 0;
          }) || typeof item.error.code !== 'string' ||
          typeof item.error.message !== 'string' ||
          typeof item.error.retryable !== 'boolean')) {
        throw new RuntimeError('公式错误字段无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
      }
      return {
        id: String(item.id || ('formula-' + index)).slice(0, 160),
        x0: x0, y0: y0, x1: x1, y1: y1,
        state: state,
        latex: latex,
        multiline: !!item.multiline,
        error: item.error && typeof item.error === 'object'
          ? pageTextError(item.error.code, item.error.message, item.error.retryable)
          : null
      };
    });
  }
  function charInsideRegion(item, region) {
    var x = (item.x0 + item.x1) / 2;
    var y = (item.y0 + item.y1) / 2;
    return x >= region.x0 && x <= region.x1 && y >= region.y0 && y <= region.y1;
  }
  function applyFormulaRegions(chars, regions) {
    if (!regions.length) return chars;
    // Never expose ordinary OCR noise from a detected formula rectangle. A
    // pending/failed formula remains an explicit region; a ready one reuses the
    // established fml/flx character contract consumed by selection and AI.
    var out = chars.filter(function (item) {
      if (item.fml) return true;
      return !regions.some(function (region) { return charInsideRegion(item, region); });
    });
    regions.forEach(function (region, regionIndex) {
      if (region.state !== 'ready') return;
      var already = out.some(function (item) {
        return item.fml && charInsideRegion(item, region);
      });
      if (already) return;
      var wrapped = region.multiline ? '$$' + region.latex + '$$' : '$' + region.latex + '$';
      var units = Array.from(wrapped);
      var sliceWidth = Math.max(0.5, (region.x1 - region.x0) / Math.max(1, units.length));
      var wordId = 950000000 + regionIndex;
      units.forEach(function (unit, index) {
        out.push({
          c: unit,
          x0: region.x0 + index * sliceWidth,
          y0: region.y0,
          x1: region.x0 + (index + 1) * sliceWidth,
          y1: region.y1,
          w: wordId,
          bk: 950000 + regionIndex,
          sp: false,
          fml: true,
          flx: index === 0 ? region.latex : ''
        });
      });
    });
    return out;
  }
  function furiganaOutsideFormulaRegions(furigana, regions) {
    if (!regions.length) return furigana;
    return furigana.filter(function (item) {
      return !regions.some(function (region) { return charInsideRegion(item, region); });
    });
  }
  function pageTextEnum(value, allowed, fallback, message) {
    if (value == null || value === '') return fallback;
    value = String(value);
    if (!allowed.has(value)) {
      throw new RuntimeError(message, 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
    }
    return value;
  }
  function normalizeNativePage(raw, page) {
    if (!raw) return null;
    if (Number(raw.page) !== page) {
      throw new RuntimeError('原生文字页码不匹配', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
    }
    var state = raw.state;
    var formulaRegions = normalizeFormulaRegions(raw.formulaRegions);
    var furigana = normalizePageTextFurigana(raw.furigana);
    if (!furigana) {
      throw new RuntimeError('原生注音响应无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
    }
    var wordSegmentation = pageTextEnum(
      raw.wordSegmentation, PAGE_TEXT_WORD_STATES, 'unavailable', '原生分词状态无效');
    var characterGeometry = pageTextEnum(
      raw.characterGeometry, PAGE_TEXT_GEOMETRY_STATES, 'unavailable', '原生字符几何状态无效');
    var formulaCoverage = pageTextEnum(
      raw.formulaCoverage, PAGE_TEXT_FORMULA_COVERAGE, 'unknown', '原生公式覆盖状态无效');
    var textAuthority = pageTextEnum(
      raw.textAuthority, PAGE_TEXT_AUTHORITIES, 'supplemental', '原生文字权威字段无效');
    var error = raw.error && typeof raw.error === 'object'
      ? pageTextError(raw.error.code, raw.error.message, raw.error.retryable)
      : null;
    if (state === 'pending' || state === 'failed' || state === 'idle') {
      if (raw.ok || (Array.isArray(raw.chars) && raw.chars.length)) {
        throw new RuntimeError('未完成的原生文字不能返回空成功', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
      }
      return {
        state: state, source: raw.source, revision: raw.revision,
        page: page, pageWidth: 0, pageHeight: 0, chars: null,
        furigana: [],
        wordSegmentation: wordSegmentation,
        characterGeometry: characterGeometry,
        formulaCoverage: formulaCoverage,
        formulaRegions: formulaRegions,
        textAuthority: textAuthority,
        error: error || pageTextError(
          state === 'pending' ? 'BW_PAGE_TEXT_PENDING' :
            state === 'idle' ? 'BW_PAGE_TEXT_IDLE' : 'BW_PAGE_TEXT_FAILED',
          state === 'pending' ? '页面文字正在识别' :
            state === 'idle' ? '页面尚未预处理' : '页面文字识别失败',
          state !== 'failed'
        )
      };
    }
    var chars = normalizePageTextChars(raw.chars);
    var pageWidth = normalizedPageSize(raw.pageWidth);
    var pageHeight = normalizedPageSize(raw.pageHeight);
    if (!raw.ok || !PAGE_TEXT_NATIVE_SOURCES.has(raw.source) || !chars ||
        !pageWidth || !pageHeight ||
        (state === 'ready' && !chars.length) ||
        (state === 'readyEmpty' && chars.length)) {
      throw new RuntimeError('原生文字页数据无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
    }
    chars = applyFormulaRegions(chars, formulaRegions);
    furigana = furiganaOutsideFormulaRegions(furigana, formulaRegions);
    if (!chars.length) {
      var formulaPending = formulaRegions.some(function (region) { return region.state === 'pending'; });
      var formulaFailed = formulaRegions.some(function (region) { return region.state === 'failed'; });
      if (formulaPending || formulaFailed) {
        return {
          state: formulaPending ? 'pending' : 'failed',
          source: raw.source,
          revision: raw.revision,
          page: page,
          pageWidth: pageWidth,
          pageHeight: pageHeight,
          chars: null,
          furigana: furigana,
          wordSegmentation: wordSegmentation,
          characterGeometry: characterGeometry,
          formulaCoverage: formulaCoverage,
          formulaRegions: formulaRegions,
          textAuthority: textAuthority,
          error: pageTextError(
            formulaPending ? 'BW_PAGE_FORMULA_PENDING' : 'BW_PAGE_FORMULA_FAILED',
            formulaPending ? '页面公式正在处理' : '页面公式识别失败',
            formulaPending
          )
        };
      }
      state = 'readyEmpty';
    } else {
      state = 'ready';
    }
    return {
      state: state, source: raw.source, revision: raw.revision,
      page: page, pageWidth: pageWidth, pageHeight: pageHeight,
      chars: chars, furigana: furigana,
      wordSegmentation: wordSegmentation,
      characterGeometry: characterGeometry,
      formulaCoverage: formulaCoverage,
      formulaRegions: formulaRegions,
      textAuthority: textAuthority,
      error: null
    };
  }
  function nativePageForPage(page) {
    nativePageTextKnownPages[page] = true;
    if (nativePageTextCache[page]) return Promise.resolve(nativePageTextCache[page]);
    if (nativePageTextPending[page]) return nativePageTextPending[page];
    var generation = nativePageTextGeneration[page] || 0;
    var pending = nativePageTextRequest('page-chars', { page: page }).then(function (raw) {
      var result = normalizeNativePage(raw, page);
      if (result && (nativePageTextGeneration[page] || 0) === generation) {
        if (result.textAuthority === 'local-override') {
          nativeTextOverridePages[page] = true;
        } else {
          // A whole-layer switch uses this flag once to force an authoritative
          // native read even when PDF.js already has embedded text. If the
          // selected layer is embedded/supplemental, release that temporary
          // force after the reply so later reads stay on the fast embedded path.
          delete nativeTextOverridePages[page];
        }
        // idle/pending describe a moment, not immutable page content. In
        // particular, the first bridge request may race native status restore;
        // caching that answer would make OCR look idle until an unrelated
        // update event happened. Terminal page data remains cached and keeps
        // the existing generation fence.
        if (result.state === 'idle' || result.state === 'pending') {
          delete nativePageTextCache[page];
        } else {
          nativePageTextCache[page] = result;
        }
      }
      return result;
    }).finally(function () {
      if (nativePageTextPending[page] === pending) delete nativePageTextPending[page];
    });
    nativePageTextPending[page] = pending;
    return pending;
  }
  function mergeEmbeddedFormulaResult(embedded, nativeResult) {
    if (nativeResult && nativeResult.textAuthority === 'local-override' &&
        (nativeResult.state === 'ready' || nativeResult.state === 'readyEmpty')) {
      return nativeResult;
    }
    if (!embedded || embedded.state !== 'ready' || !nativeResult ||
        !Array.isArray(nativeResult.formulaRegions) || !nativeResult.formulaRegions.length) {
      return embedded;
    }
    // A real PDF text layer remains authoritative and selectable even while a
    // formula is pending or failed. Only completed formula recognition may
    // replace the characters inside its own rectangle; all formula states stay
    // separately visible through formulaRegions.
    var readyRegions = nativeResult.formulaRegions.filter(function (region) {
      return region.state === 'ready';
    });
    var chars = applyFormulaRegions(embedded.chars, readyRegions);
    var furigana = furiganaOutsideFormulaRegions(embedded.furigana || [], readyRegions);
    return {
      state: 'ready', source: 'embedded',
      revision: embedded.revision + '+' + String(nativeResult.revision || 'formula'),
      page: embedded.page,
      pageWidth: embedded.pageWidth,
      pageHeight: embedded.pageHeight,
      chars: chars,
      furigana: furigana,
      wordSegmentation: embedded.wordSegmentation,
      characterGeometry: embedded.characterGeometry,
      formulaCoverage: nativeResult.formulaCoverage,
      formulaRegions: nativeResult.formulaRegions,
      textAuthority: 'supplemental',
      error: null
    };
  }
  function dispatchPageTextUpdated(page, state, source, revision) {
    try {
      root.dispatchEvent(new CustomEvent('bw:page-text-updated', {
        detail: {
          contract: PAGE_TEXT_PROVIDER_CONTRACT,
          page: Number(page), state: state,
          source: source == null ? null : String(source),
          revision: String(revision || '').slice(0, 160)
        }
      }));
    } catch (_) {}
  }
  function prefetchNativeFormulaForPage(page) {
    if (!nativePageTextHandler() || nativePageTextCache[page] ||
        nativeFormulaPrefetchPending[page]) return;
    var generation = nativePageTextGeneration[page] || 0;
    var pending = nativePageForPage(page).then(function (result) {
      // A refresh is useful only when the result survived the generation fence
      // and is synchronously available to the next char-layer read. Transient
      // idle/pending replies deliberately remain uncached.
      if ((nativePageTextGeneration[page] || 0) !== generation ||
          nativePageTextCache[page] !== result || !result ||
          ((!Array.isArray(result.formulaRegions) || !result.formulaRegions.length) &&
           result.textAuthority !== 'local-override')) return;
      dispatchPageTextUpdated(page, result.state, result.source, result.revision);
    }).catch(function () {
      // Embedded text is already usable. Optional formula enrichment must never
      // turn a passive text-layer read into a failure or an unhandled rejection.
    }).finally(function () {
      if (nativeFormulaPrefetchPending[page] === pending) {
        delete nativeFormulaPrefetchPending[page];
      }
    });
    nativeFormulaPrefetchPending[page] = pending;
  }
  function pageTextForPage(page) {
    page = validPage(page);
    if (!page) return Promise.reject(new RuntimeError('PDF 页码无效', 'BW_PAGE_TEXT_PAGE'));
    return loadEmbeddedPage(page).then(function (embedded) {
      // A real PDF text layer is authoritative for prose, but formula regions
      // may still be supplied by native preprocessing. Never wait for that
      // optional enrichment: downloaded PDFs with a real text layer must be
      // selectable immediately even while the native bridge is restoring or
      // hung. A later cached formula result asks the char layer to re-read.
      if (embedded && embedded.state === 'ready') {
        if (nativeTextOverridePages[page]) {
          return nativePageForPage(page).then(function (nativeResult) {
            return mergeEmbeddedFormulaResult(embedded, nativeResult);
          });
        }
        if (nativePageTextCache[page]) {
          return mergeEmbeddedFormulaResult(embedded, nativePageTextCache[page]);
        }
        prefetchNativeFormulaForPage(page);
        return embedded;
      }
      // An empty embedded layer is never authoritative: an explicit Apple/Pi
      // result may exist, so that path still waits for the passive native read.
      return nativePageForPage(page).then(function (nativeResult) {
        if (nativeResult && nativeResult.state !== 'idle') return nativeResult;
        return embedded || nativeResult || {
          state: 'idle', source: null, revision: '', page: page,
          pageWidth: 0, pageHeight: 0, chars: null, furigana: [],
          wordSegmentation: 'unavailable', characterGeometry: 'unavailable',
          formulaCoverage: 'unknown', formulaRegions: [],
          textAuthority: 'supplemental',
          error: pageTextError('BW_PAGE_TEXT_IDLE', '页面尚未预处理', true)
        };
      });
    });
  }
  function pageTextHTTP(result) {
    var common = {
      state: result.state,
      source: result.source,
      revision: result.revision || '',
      page: result.page,
      word_segmentation: result.wordSegmentation || 'unavailable',
      character_geometry: result.characterGeometry || 'unavailable',
      formula_coverage: result.formulaCoverage || 'unknown'
    };
    if (result.state === 'ready' || result.state === 'readyEmpty') {
      return jsonResponse(Object.assign(common, {
        ok: true,
        chars: result.chars || [],
        page_w: result.pageWidth,
        page_h: result.pageHeight,
        furigana: result.furigana || [],
        formula_regions: result.formulaRegions || []
      }));
    }
    var status = result.state === 'pending' ? 202 :
      result.state === 'failed' ? 422 : 404;
    return jsonResponse(Object.assign(common, {
      ok: false,
      code: result.error && result.error.code || 'BW_PAGE_TEXT_UNAVAILABLE',
      error: result.error && result.error.message || '页面文字不可用',
      retryable: !!(result.error && result.error.retryable),
      // Keep formula state visible even when the whole page is still pending;
      // consumers must not misread a formula-only page as generic blank OCR.
      formula_regions: result.formulaRegions || []
    }), status);
  }
  function searchableText(result) {
    return result && Array.isArray(result.chars)
      ? result.chars.map(function (item) { return item.c || ''; }).join('') : '';
  }

  function nativeVoicePageText(url) {
    var code = 'BW_LOCAL_VOICE_PAGE_TEXT';
    return localJSONRoute(function () {
      localFileQuery(url, ['file', 'page'], ['file', 'page'], code);
      var page = strictInteger(
        url.searchParams.get('page'), 1, 10000000, 'page', code
      );
      if (nativeInterfaceSurface === 'epub') {
        return loadEPUB().then(function (epub) {
          var index = page - 1;
          if (index >= epub.spine.length) {
            throw outgoingRequestError('EPUB 章节越界', code, 400);
          }
          return epubSectionVisibleText(epub, index);
        }).then(function (text) {
          return { ok: true, text: String(text || '').slice(0, 1500) };
        });
      }
      return pageTextForPage(page).then(function (result) {
        return { ok: true, text: searchableText(result).slice(0, 1500) };
      });
    }, code);
  }

  function roundedPageCoordinate(value) {
    return Math.round(Number(value || 0) * 100) / 100;
  }

  function nativeSentenceRectangles(chars) {
    var rects = [];
    var current = null;
    (chars || []).forEach(function (character) {
      if (character.sp && !current) return;
      var x0 = Number(character.x0 || 0);
      var y0 = Number(character.y0 || 0);
      var x1 = Number(character.x1 || 0);
      var y1 = Number(character.y1 || 0);
      var lineHeight = Math.max(0.1, y1 - y0);
      if (current && Math.abs(y0 - current[1]) <= lineHeight * 0.5) {
        current[2] = Math.max(current[2], x1);
        current[1] = Math.min(current[1], y0);
        current[3] = Math.max(current[3], y1);
        return;
      }
      if (current) rects.push(current.map(roundedPageCoordinate));
      if (character.sp) {
        current = null;
        return;
      }
      current = [x0, y0, x1, y1];
    });
    if (current) rects.push(current.map(roundedPageCoordinate));
    return rects;
  }

  function nativeSplitPageSentences(chars) {
    chars = Array.isArray(chars) ? chars : [];
    var sentences = [];
    var current = [];
    var heights = chars.filter(function (character) {
      return !character.sp && String(character.c || '').trim();
    }).map(function (character) {
      return Number(character.y1 || 0) - Number(character.y0 || 0);
    }).filter(function (height) {
      return Number.isFinite(height) && height > 0;
    }).sort(function (left, right) { return left - right; });
    var medianHeight = heights.length ? heights[Math.floor(heights.length / 2)] : 0;

    function isRuby(character) {
      return medianHeight > 0 &&
        Number(character.y1 || 0) - Number(character.y0 || 0) < medianHeight * 0.6 &&
        /^[\u3041-\u3093\u30a1-\u30f6\u30fc]$/.test(String(character.c || ''));
    }
    function characterRect(character) {
      return ['x0', 'y0', 'x1', 'y1'].map(function (key) {
        return roundedPageCoordinate(character[key]);
      });
    }
    function flush() {
      var nonSpace = current.filter(function (character) { return !character.sp; });
      if (nonSpace.length) {
        var text = current.map(function (character) {
          return String(character.c || '');
        }).join('').trim().replace(/\s+/g, ' ').slice(0, 500);
        if (text.length >= 4) {
          var xs0 = nonSpace.map(function (character) { return Number(character.x0 || 0); });
          var ys0 = nonSpace.map(function (character) { return Number(character.y0 || 0); });
          var xs1 = nonSpace.map(function (character) { return Number(character.x1 || 0); });
          var ys1 = nonSpace.map(function (character) { return Number(character.y1 || 0); });
          var width = Math.max.apply(Math, xs1) - Math.min.apply(Math, xs0);
          var height = Math.max.apply(Math, ys1) - Math.min.apply(Math, ys0);
          // Preserve the legacy contract: vertical columns are omitted because
          // their horizontal overlay geometry is not meaningful.
          if (!(height > width * 1.6)) {
            sentences.push({
              text: text,
              rects: nativeSentenceRectangles(current),
              first_char: characterRect(nonSpace[0]),
              last_char: characterRect(nonSpace[nonSpace.length - 1])
            });
          }
        }
      }
      current = [];
    }

    var previous = null;
    var pendingPeriod = false;
    chars.forEach(function (character) {
      if (!character.sp && isRuby(character)) return;
      var text = String(character.c || '');
      if (pendingPeriod) {
        var previousHeight = previous
          ? Math.max(1, Number(previous.y1 || 0) - Number(previous.y0 || 0)) : 1;
        var sameLine = !!previous && !previous.sp &&
          Math.abs(Number(character.y0 || 0) - Number(previous.y0 || 0)) <
            previousHeight * 0.5;
        var lowerAlpha = /^[a-z]$/.test(text);
        var continuation = sameLine && !character.sp && text.length === 1 &&
          (/^[0-9]$/.test(text) || lowerAlpha);
        if (!continuation) flush();
        pendingPeriod = false;
      }
      if (previous) {
        var previousBlock = previous.bk;
        var currentBlock = character.bk;
        if (previousBlock != null && currentBlock != null && previousBlock !== currentBlock) {
          var blockHeight = Math.max(
            0.1, Number(previous.y1 || 0) - Number(previous.y0 || 0)
          );
          if (Number(character.y0 || 0) - Number(previous.y0 || 0) < -0.5 * blockHeight) {
            flush();
          }
        }
      }
      if (previous && !previous.sp && !character.sp) {
        var previousLineHeight = Math.max(
          0.1, Number(previous.y1 || 0) - Number(previous.y0 || 0)
        );
        if (Number(character.y0 || 0) - Number(previous.y0 || 0) >
            previousLineHeight * 1.5) {
          flush();
        }
      }
      if ('•▪▶◆●○◇'.indexOf(text) >= 0) {
        flush();
        current.push(character);
        previous = character;
        return;
      }
      if (character.sp) {
        current.push(character);
        previous = character;
        return;
      }
      if ('!?。！？'.indexOf(text) >= 0) {
        current.push(character);
        flush();
        previous = character;
        return;
      }
      if (text === '.') {
        current.push(character);
        pendingPeriod = true;
        previous = character;
        return;
      }
      current.push(character);
      previous = character;
    });
    flush();
    return sentences;
  }

  function nativePageTranslate(input, init, url) {
    var code = 'BW_LOCAL_PAGE_TRANSLATE';
    return localJSONRoute(function () {
      localFileQuery(url, ['file', 'page'], ['file', 'page'], code);
      var page = strictInteger(
        url.searchParams.get('page'), 1, 10000000, 'page', code
      );
      return pageTextForPage(page).then(function (result) {
        var sentences = nativeSplitPageSentences(result && result.chars);
        var response = {
          ok: true,
          sentences: sentences,
          page_w: Number(result && result.pageWidth || 0),
          page_h: Number(result && result.pageHeight || 0),
          translated: 0,
          total: sentences.length
        };
        if (!sentences.length) return response;
        var translationURL = new URL(
          '/pdf/api/epub-translate-section', root.location.origin
        );
        var translationRoute = declaredNativeInterface(
          translationURL.pathname, 'POST'
        );
        if (translationRoute.owner !== 'pi') {
          throw outgoingRequestError(
            '页面翻译服务未由 Pi 提供', code + '_ROUTE', 503
          );
        }
        return nativePiJSON(translationURL, {
          texts: sentences.map(function (sentence) { return sentence.text; })
        }, translationRoute, requestSignal(input, init)).then(function (translated) {
          if (!translated || !Array.isArray(translated.translations) ||
              translated.translations.length !== sentences.length) {
            throw outgoingRequestError(
              'Pi 页面翻译响应无效', code + '_RESPONSE', 502
            );
          }
          translated.translations.forEach(function (value, index) {
            sentences[index].zh = typeof value === 'string' ? value.slice(0, 8000) : '';
          });
          response.translated = sentences.filter(function (sentence) {
            return !!sentence.zh;
          }).length;
          return response;
        }).catch(function (error) {
          if (error && error.httpStatus) throw error;
          throw outgoingRequestError(
            String(error && error.message || error || 'Pi 页面翻译不可用'),
            code + '_REMOTE', 503
          );
        });
      });
    }, code);
  }
  function pageMatches(text, query, page) {
    var lower = text.toLocaleLowerCase();
    var needle = query.toLocaleLowerCase();
    var positions = [];
    var cursor = 0;
    while (positions.length < 1000) {
      var at = lower.indexOf(needle, cursor);
      if (at < 0) break;
      positions.push(at);
      cursor = at + Math.max(1, needle.length);
    }
    if (!positions.length) return null;
    var first = positions[0];
    var start = Math.max(0, first - 70);
    var end = Math.min(text.length, first + query.length + 110);
    return {
      page: page,
      count: positions.length,
      snippet: (start ? '…' : '') + text.slice(start, end) + (end < text.length ? '…' : ''),
      pos: first
    };
  }
  function nativeSearch(query, limit) {
    return nativePageTextRequest('search', { query: query, limit: limit }).then(function (raw) {
      if (!raw) return { matches: [], incomplete: true, available: false };
      if (!Array.isArray(raw.matches) || raw.matches.length > 200 ||
          typeof raw.incomplete !== 'boolean') {
        throw new RuntimeError('原生搜索响应无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
      }
      pageTextProgressCount(raw.total, 'search.total');
      pageTextProgressCount(raw.pages, 'search.pages');
      var matches = raw.matches.map(function (item) {
        if (!item || typeof item !== 'object' || Array.isArray(item) ||
            Object.keys(item).some(function (key) {
              return ['page', 'count', 'snippet', 'pos'].indexOf(key) < 0;
            })) {
          throw new RuntimeError('原生搜索结果字段无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
        }
        var page = validPage(item && item.page);
        if (!page) throw new RuntimeError('原生搜索页码无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
        var count = pageTextProgressCount(item.count, 'search.matches.count');
        if (!count) throw new RuntimeError('原生搜索命中数无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
        return {
          page: page,
          count: Math.min(1000, count),
          snippet: String(item.snippet || '').slice(0, 500),
          pos: item.pos == null ? 0 : pageTextProgressCount(item.pos, 'search.matches.pos')
        };
      });
      return { matches: matches, incomplete: !!raw.incomplete, available: true };
    });
  }
  function nativeSearchKey(query, limit) {
    return query + '\u0000' + limit;
  }
  function nativeSearchProbe(query, limit) {
    var key = nativeSearchKey(query, limit);
    if (nativeSearchCache.has(key)) {
      return { key: key, settled: true, result: nativeSearchCache.get(key) };
    }
    if (!nativePageTextHandler()) return { key: key, settled: true, result: null };
    if (nativeSearchPending.has(key)) return nativeSearchPending.get(key);
    var generation = nativeSearchGeneration;
    var probe = { key: key, settled: false, result: null, promise: null };
    probe.promise = nativeSearch(query, limit).then(function (result) {
      probe.settled = true;
      if (generation !== nativeSearchGeneration) return null;
      probe.result = result && result.available ? result : null;
      if (probe.result) {
        if (!nativeSearchCache.has(key) && nativeSearchCache.size >= 32) {
          nativeSearchCache.delete(nativeSearchCache.keys().next().value);
        }
        nativeSearchCache.set(key, probe.result);
      }
      return probe.result;
    }).catch(function () {
      probe.settled = true;
      probe.result = null;
      return null;
    }).finally(function () {
      if (nativeSearchPending.get(key) === probe) nativeSearchPending.delete(key);
    });
    nativeSearchPending.set(key, probe);
    return probe;
  }
  function combinePageTextSearch(query, embeddedMatches, embeddedPages, embeddedIncomplete,
      nativeResult, limit) {
    var nativeMatches = nativeResult && nativeResult.available ? nativeResult.matches : [];
    var combined = embeddedMatches.concat(nativeMatches.filter(function (item) {
      return !embeddedPages.has(item.page);
    }));
    combined.sort(function (a, b) { return a.page - b.page; });
    var total = combined.reduce(function (sum, item) { return sum + item.count; }, 0);
    var incomplete = nativeResult && nativeResult.available
      ? nativeResult.incomplete : embeddedIncomplete;
    return {
      ok: true,
      q: query,
      state: incomplete ? 'pending' : 'ready',
      matches: combined.slice(0, limit),
      total: total,
      pages: new Set(combined.map(function (item) { return item.page; })).size,
      incomplete: incomplete
    };
  }
  function searchPageText(query, limit) {
    query = String(query || '').trim();
    limit = Math.max(1, Math.min(200, Number(limit) || 200));
    if (!query || query.length > 256) {
      return Promise.reject(new RuntimeError('搜索文字无效', 'BW_PAGE_TEXT_SEARCH_QUERY'));
    }
    // With no PDF text-layer pages to search, native sidecars are the only
    // source and this read must retain the established await semantics.
    if (!embeddedPageCount) {
      return nativeSearch(query, limit).catch(function () {
        return { matches: [], incomplete: true, available: false };
      }).then(function (nativeResult) {
        return combinePageTextSearch(query, [], new Set(), true, nativeResult, limit);
      });
    }
    // Otherwise start the passive native search in parallel, but never let a
    // restoring or hung bridge block authoritative embedded PDF text. A result
    // that is already settled when the text-layer scan completes is merged;
    // later results stay in the bounded cache for the next identical search.
    var nativeProbe = nativeSearchProbe(query, limit);
    var embeddedPages = new Set();
    var embeddedMatches = [];
    var embeddedIncomplete = false;
    var chain = Promise.resolve();
    for (let page = 1; page <= embeddedPageCount; page += 1) {
      chain = chain.then(function () {
        return loadEmbeddedPage(page).then(function (result) {
          if (!result || result.state !== 'ready') {
            embeddedIncomplete = true;
            return;
          }
          embeddedPages.add(page);
          var match = pageMatches(searchableText(result), query, page);
          if (match) embeddedMatches.push(match);
        });
      });
    }
    return chain.then(function () {
      var nativeResult = nativeSearchCache.get(nativeProbe.key) ||
        (nativeProbe.settled ? nativeProbe.result : null);
      return combinePageTextSearch(
        query, embeddedMatches, embeddedPages, embeddedIncomplete, nativeResult, limit);
    });
  }
  function pageTextProgressCount(value, field) {
    value = Number(value);
    if (!Number.isInteger(value) || value < 0 || value > 10000000) {
      throw new RuntimeError('原生进度字段无效: ' + field, 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
    }
    return value;
  }
  function normalizePhaseProgress(raw, field) {
    if (raw == null) return null;
    if (!raw || typeof raw !== 'object' || Array.isArray(raw) ||
        Object.keys(raw).some(function (key) { return !PAGE_TEXT_PHASE_PROGRESS_KEYS.has(key); }) ||
        Array.from(PAGE_TEXT_PHASE_PROGRESS_KEYS).some(function (key) {
          return !Object.prototype.hasOwnProperty.call(raw, key);
        })) {
      throw new RuntimeError('原生分阶段进度无效: ' + field, 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
    }
    var result = {};
    PAGE_TEXT_PHASE_PROGRESS_KEYS.forEach(function (key) {
      result[key] = pageTextProgressCount(raw[key], field + '.' + key);
    });
    return result;
  }
  function normalizePageTextProgress(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw) ||
        Object.keys(raw).some(function (key) { return !PAGE_TEXT_PROGRESS_KEYS.has(key); })) {
      throw new RuntimeError('原生总进度字段无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
    }
    var result = {
      total: pageTextProgressCount(raw.total || 0, 'total'),
      ready: pageTextProgressCount(raw.ready || 0, 'ready'),
      pending: pageTextProgressCount(raw.pending || 0, 'pending'),
      failed: pageTextProgressCount(raw.failed || 0, 'failed'),
      activePage: raw.activePage == null ? null : validPage(raw.activePage),
      currentPage: raw.currentPage == null ? null : validPage(raw.currentPage),
      textProgress: normalizePhaseProgress(raw.textProgress, 'textProgress'),
      wordProgress: normalizePhaseProgress(raw.wordProgress, 'wordProgress'),
      formulaProgress: normalizePhaseProgress(raw.formulaProgress, 'formulaProgress'),
      formulaPendingRegions: pageTextProgressCount(raw.formulaPendingRegions || 0, 'formulaPendingRegions'),
      formulaFailedRegions: pageTextProgressCount(raw.formulaFailedRegions || 0, 'formulaFailedRegions')
    };
    if ((raw.activePage != null && !result.activePage) ||
        (raw.currentPage != null && !result.currentPage)) {
      throw new RuntimeError('原生进度页码无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
    }
    return result;
  }
  function pageTextStatus() {
    return nativePageTextRequest('status').then(function (raw) {
      if (!raw) {
        return {
          ok: true, state: 'idle', source: null, revision: '',
          progress: { total: embeddedPageCount, ready: Object.keys(embeddedPageText).length, pending: 0, failed: 0, activePage: null }
        };
      }
      var progress = normalizePageTextProgress(raw.progress);
      return {
        ok: raw.ok,
        state: raw.state,
        source: raw.source,
        revision: raw.revision,
        progress: progress
      };
    });
  }
  function invalidateNativePageText(page, state, source, revision) {
    page = Number(page);
    nativePageTextGeneration[page] =
      (nativePageTextGeneration[page] || 0) + 1;
    delete nativePageTextCache[page];
    delete nativePageTextPending[page];
    delete nativeFormulaPrefetchPending[page];
    nativeSearchGeneration += 1;
    nativeSearchCache.clear();
    nativeSearchPending.clear();
    dispatchPageTextUpdated(page, state, source, revision);
  }
  function invalidateAllNativePageText(state, source, revision) {
    var pages = Object.create(null);
    [
      embeddedPageText,
      nativePageTextKnownPages,
      nativePageTextCache,
      nativePageTextPending,
      nativeFormulaPrefetchPending,
      nativeTextOverridePages
    ].forEach(function (values) {
      Object.keys(values || {}).forEach(function (key) {
        var page = Number(key);
        if (validPage(page)) pages[page] = true;
      });
    });
    Object.keys(pages).forEach(function (key) {
      var page = Number(key);
      nativePageTextGeneration[page] =
        (nativePageTextGeneration[page] || 0) + 1;
    });
    nativePageTextCache = Object.create(null);
    nativePageTextPending = Object.create(null);
    nativeFormulaPrefetchPending = Object.create(null);
    nativeTextOverridePages = Object.create(null);
    // The selected layer is native state, not inferable from the previous JS
    // cache. Force exactly one native read for each page already known to this
    // document; nativePageForPage keeps the flag only when the reply declares
    // local-override (Pi/PC/Vision/legacy) and drops it for embedded text.
    Object.keys(pages).forEach(function (key) {
      nativeTextOverridePages[Number(key)] = true;
    });
    nativeSearchGeneration += 1;
    nativeSearchCache.clear();
    nativeSearchPending.clear();
    Object.keys(pages).forEach(function (key) {
      dispatchPageTextUpdated(Number(key), state, source, revision);
    });
  }
  function pageTextUpdate(event) {
    var detail = event && event.detail;
    if (!detail || typeof detail !== 'object' || Array.isArray(detail) ||
        Object.keys(detail).some(function (key) { return !PAGE_TEXT_UPDATE_KEYS.has(key); }) ||
        detail.contract !== PAGE_TEXT_UPDATE_CONTRACT ||
        detail.localBookId !== bookId ||
        (detail.page !== null && !validPage(detail.page)) ||
        !PAGE_TEXT_STATES.has(detail.state) ||
        (detail.source != null && !PAGE_TEXT_NATIVE_SOURCES.has(detail.source))) return;
    if (detail.page === null) {
      invalidateAllNativePageText(
        detail.state, detail.source, detail.revision
      );
      return;
    }
    invalidateNativePageText(
      detail.page, detail.state, detail.source, detail.revision
    );
  }
  root.addEventListener('bw:native-page-text-updated', pageTextUpdate);

  var pageTextProvider = Object.freeze({
    contract: PAGE_TEXT_PROVIDER_CONTRACT,
    registerEmbeddedPage: registerEmbeddedPage,
    setEmbeddedPageLoader: setEmbeddedPageLoader,
    pageChars: pageTextForPage,
    search: searchPageText,
    status: pageTextStatus
  });
  runtimeRoot.pageTextProvider = pageTextProvider;

  function localJSONRoute(task, fallbackCode) {
    return Promise.resolve().then(task).then(function (value) {
      return value instanceof Response ? value : jsonResponse(value);
    }).catch(function (error) {
      return outgoingFailureResponse(error, fallbackCode, 500);
    });
  }

  function nativeTextMutationReply(action, fields, page, code) {
    return nativePageTextRequest(action, fields).then(function (raw) {
      if (!raw) {
        throw outgoingRequestError(
          '本机文字处理桥不可用', code + '_UNAVAILABLE', 503
        );
      }
      if (!raw.ok) {
        throw outgoingRequestError(
          raw.error && raw.error.message || '本机文字处理失败',
          raw.error && raw.error.code || code,
          422
        );
      }
      if (Number(raw.page) !== page || !raw.cv ||
          !PAGE_TEXT_AUTHORITIES.has(raw.textAuthority)) {
        throw outgoingRequestError('本机文字处理响应无效', code, 500);
      }
      if (raw.textAuthority === 'local-override') {
        nativeTextOverridePages[page] = true;
      } else {
        delete nativeTextOverridePages[page];
      }
      invalidateNativePageText(
        page, raw.state, raw.source, raw.revision || raw.cv
      );
      return raw;
    });
  }

  function localOCRSelection(input, init, url) {
    var code = 'BW_LOCAL_OCR_SELECTION';
    return localJSONRoute(function () {
      strictQuery(url, [], [], code + '_QUERY');
      return requestObject(
        input, init,
        ['file', 'page', 'bbox', 'model', 'effort'],
        ['file', 'page', 'bbox'], code + '_BODY', 32 * 1024
      ).then(function (body) {
        requireLocalFile(body.file, code + '_BODY');
        var page = strictInteger(body.page, 1, 100000, 'page', code + '_BODY');
        if (!Array.isArray(body.bbox) || body.bbox.length !== 4) {
          throw outgoingRequestError('bbox 必须含四个坐标', code + '_BODY', 400);
        }
        var bbox = body.bbox.map(function (value) { return Number(value); });
        if (!bbox.every(Number.isFinite) || bbox.some(function (value) { return value < 0; }) ||
            bbox[2] - bbox[0] < 0.5 || bbox[3] - bbox[1] < 0.5) {
          throw outgoingRequestError('文字识别选区无效', code + '_BODY', 400);
        }
        ['model', 'effort'].forEach(function (field) {
          if (body[field] != null &&
              (typeof body[field] !== 'string' || body[field].length > 160)) {
            throw outgoingRequestError(field + ' 无效', code + '_BODY', 400);
          }
        });
        return nativeTextMutationReply(
          'ocr-selection', { page: page, bbox: bbox }, page, code
        ).then(function (raw) {
          if (typeof raw.text !== 'string' || !raw.text.trim() ||
              raw.text.length > 100000 || raw.persisted !== true ||
              raw.textAuthority !== 'local-override') {
            throw outgoingRequestError('选区识别未持久化', code, 500);
          }
          return {
            ok: true,
            text: raw.text,
            cv: String(raw.cv),
            persisted: true,
            persistence: 'native-ocr-fix'
          };
        });
      });
    }, code);
  }

  function localReOCRPage(input, init, url, clearOnly) {
    var code = clearOnly ? 'BW_LOCAL_REOCR_CLEAR' : 'BW_LOCAL_REOCR_PAGE';
    return localJSONRoute(function () {
      strictQuery(url, [], [], code + '_QUERY');
      return requestObject(
        input, init, ['file', 'page'], ['file', 'page'], code + '_BODY', 16 * 1024
      ).then(function (body) {
        requireLocalFile(body.file, code + '_BODY');
        var page = strictInteger(body.page, 1, 100000, 'page', code + '_BODY');
        return nativeTextMutationReply(
          clearOnly ? 'clear-reocr-page' : 'reocr-page',
          { page: page }, page, code
        ).then(function (raw) {
          if (clearOnly) {
            if (typeof raw.cleared !== 'boolean') {
              throw outgoingRequestError('撤销重扫响应无效', code, 500);
            }
            return { ok: true, cleared: raw.cleared, cv: String(raw.cv) };
          }
          if (!Number.isInteger(Number(raw.chars)) || Number(raw.chars) < 0 ||
              raw.textAuthority !== 'local-override') {
            throw outgoingRequestError('单页重扫响应无效', code, 500);
          }
          return { ok: true, chars: Number(raw.chars), cv: String(raw.cv) };
        });
      });
    }, code);
  }

  function requireLocalFile(value, code) {
    value = String(value || '');
    if (value !== bookId && value !== localFileRef()) {
      throw outgoingRequestError(
        '请求书籍与当前本机书籍不一致', code || 'BW_LOCAL_FILE', 409
      );
    }
    return localFileRef();
  }

  function requestObject(input, init, allowed, requiredKeys, code, maximumBytes) {
    return strictRequestJSON(
      input, init, maximumBytes || 16 * 1024 * 1024, code
    ).then(function (body) {
      return assertObjectFields(body, allowed, requiredKeys, code);
    });
  }

  function localFileQuery(url, allowed, requiredKeys, code) {
    strictQuery(url, allowed, requiredKeys, code);
    requireLocalFile(url.searchParams.get('file'), code);
  }

  function deleteRecordRequest(input, init, url, code) {
    var queryMode = url.searchParams.has('file') || url.searchParams.has('id');
    if (queryMode) {
      localFileQuery(url, ['file', 'id'], ['file', 'id'], code);
      return Promise.resolve({
        file: localFileRef(), id: localRecordId(url.searchParams.get('id'), code)
      });
    }
    strictQuery(url, [], [], code);
    return requestObject(input, init, ['file', 'id'], ['file', 'id'], code, 64 * 1024)
      .then(function (body) {
        requireLocalFile(body.file, code);
        body.id = localRecordId(body.id, code);
        return body;
      });
  }

  function localRecordId(value, code) {
    value = String(value || '').trim();
    if (!/^[A-Za-z0-9_-]{2,96}$/.test(value)) {
      throw outgoingRequestError('记录编号无效', code, 400);
    }
    return value;
  }

  function boundedLocalString(value, maximum, fallback, code, label, trim) {
    if (value == null) value = fallback == null ? '' : fallback;
    if (typeof value !== 'string') {
      throw outgoingRequestError((label || '文字') + '必须是字符串', code, 400);
    }
    if (trim !== false) value = value.trim();
    if (new TextEncoder().encode(value).length > maximum) {
      throw outgoingRequestError((label || '文字') + '过长', code, 413);
    }
    return value;
  }

  function finiteLocalNumber(value, minimum, maximum, fallback, code, label) {
    if (value == null && fallback != null) value = fallback;
    value = Number(value);
    if (!Number.isFinite(value) || value < minimum || value > maximum) {
      throw outgoingRequestError((label || '数字') + '无效', code, 400);
    }
    return value;
  }

  function boundedCanonicalJSON(value, maximumBytes, code, label) {
    var canonical;
    try { canonical = canonicalJSONValue(value); }
    catch (_) {
      throw outgoingRequestError((label || 'JSON') + '无效', code, 400);
    }
    if (utf8(JSON.stringify(canonical)).byteLength > maximumBytes) {
      throw outgoingRequestError((label || 'JSON') + '过大', code, 413);
    }
    return canonical;
  }

  function storedList(value, code) {
    if (!Array.isArray(value)) {
      throw outgoingRequestError('本机列表状态损坏', code, 500);
    }
    return value;
  }

  function normalizedRectangles(value, code) {
    if (!Array.isArray(value) || !value.length || value.length > 2000) {
      throw outgoingRequestError('高亮矩形无效', code, 400);
    }
    var output = value.map(function (rect) {
      if (!Array.isArray(rect) || rect.length !== 4) {
        throw outgoingRequestError('高亮矩形无效', code, 400);
      }
      var numbers = rect.map(Number);
      if (numbers.some(function (number) { return !Number.isFinite(number); })) {
        throw outgoingRequestError('高亮矩形无效', code, 400);
      }
      var x0 = Math.min(numbers[0], numbers[2]);
      var y0 = Math.min(numbers[1], numbers[3]);
      var x1 = Math.max(numbers[0], numbers[2]);
      var y1 = Math.max(numbers[1], numbers[3]);
      if (x0 < -100000 || y0 < -100000 || x1 > 100000 || y1 > 100000 ||
          x1 <= x0 || y1 <= y0) {
        throw outgoingRequestError('高亮矩形越界', code, 400);
      }
      return [x0, y0, x1, y1].map(function (number) {
        return Math.round(number * 100) / 100;
      });
    });
    return output;
  }

  function normalizedHighlightColor(value, fallback, code) {
    value = boundedLocalString(value, 64, fallback, code, '高亮颜色', true);
    if (!value) return '';
    if (!/^#[0-9a-fA-F]{3,8}$/.test(value) &&
        !/^(?:rgb|rgba|hsl|hsla)\([0-9.,%\s-]+\)$/.test(value)) {
      throw outgoingRequestError('高亮颜色无效', code, 400);
    }
    return value;
  }

  function persistLocalPDFHighlight(body, code, independent) {
    requireLocalFile(body.file, code);
    var page = strictInteger(body.page, 1, 10000000, 'page', code);
    var rects = normalizedRectangles(body.rects, code);
    var clientId = typeof body.id === 'string' && /^c_[a-f0-9]{8,32}$/.test(body.id)
      ? body.id : '';
    var highlight = {
      id: clientId || ('h_' + randomHex(6)),
      page: page,
      rects: rects,
      color: normalizedHighlightColor(body.color, '#ffd54a', code),
      text: boundedLocalString(body.text, 2000, '', code, '高亮文字', false),
      note: boundedLocalString(body.note, 2000, '', code, '高亮备注', false),
      kind: ['note', 'translate', 'explain'].indexOf(body.kind) >= 0
        ? body.kind : 'note',
      sentence: boundedLocalString(body.sentence, 2000, '', code, '高亮句子', false),
      body: boundedLocalString(body.body, 8000, '', code, '高亮正文', false),
      time: nowSeconds()
    };
    if (body.page_w != null && body.page_h != null) {
      highlight.page_w = finiteLocalNumber(body.page_w, 1, 100000, null, code, 'page_w');
      highlight.page_h = finiteLocalNumber(body.page_h, 1, 100000, null, code, 'page_h');
    }
    var mutate = independent ? mutateDocumentStateNow : mutateDocumentState;
    return mutate('document-highlights', [], function (items) {
      items = storedList(items, code).filter(function (item) {
        return !item || item.id !== highlight.id;
      });
      items.push(highlight);
      return localStateMutationResult(items, {
        ok: true, id: highlight.id, highlight: highlight
      });
      // 上界与 independent 无关：普通高亮写入同样会挂住 object store，
      // 一旦挂住，后面连精确高亮也进不来。independent 只决定走哪个入口。
    }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS });
  }

  function localPDFHighlights(input, init, url, method) {
    var code = 'BW_LOCAL_HIGHLIGHTS';
    if (method === 'GET') {
      return localJSONRoute(function () {
        localFileQuery(url, ['file'], ['file'], code);
        return readState('document-highlights', [], {
          transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS
        }).then(function (items) {
          items = storedList(items, code).slice().sort(function (a, b) {
            return Number(a.page || 0) - Number(b.page || 0) ||
              Number(a.time || 0) - Number(b.time || 0);
          });
          return { ok: true, highlights: items };
        });
      }, code);
    }
    if (method === 'DELETE') {
      return localJSONRoute(function () {
        return deleteRecordRequest(input, init, url, code).then(function (request) {
          // 删除同样需要事务上界。一次不 settle 的删除会占住 object store,
          // 之后每一次高亮读写都排在它后面 —— 挂起源只是从写入换到了删除。
          return mutateDocumentState('document-highlights', [], function (items) {
            items = storedList(items, code);
            var before = items.length;
            items = items.filter(function (item) { return item && item.id !== request.id; });
            if (items.length === before) {
              throw outgoingRequestError('未找到高亮', code, 404);
            }
            return localStateMutationResult(items, { ok: true });
          }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS });
        });
      }, code);
    }
    var allowed = method === 'POST'
      ? ['file', 'id', 'page', 'rects', 'color', 'text', 'note', 'kind',
        'sentence', 'body', 'page_w', 'page_h']
      : ['file', 'id', 'color', 'text', 'note', 'kind', 'sentence', 'body'];
    var required = method === 'POST' ? ['file', 'page', 'rects'] : ['file', 'id'];
    return localJSONRoute(function () {
      strictQuery(url, [], [], code);
      return requestObject(input, init, allowed, required, code).then(function (body) {
        requireLocalFile(body.file, code);
        if (method === 'POST') {
          return persistLocalPDFHighlight(body, code, false);
        }
        var id = localRecordId(body.id, code);
        return mutateDocumentState('document-highlights', [], function (items) {
          items = storedList(items, code);
          var found = items.find(function (item) { return item && item.id === id; });
          if (!found) throw outgoingRequestError('未找到高亮', code, 404);
          if (Object.prototype.hasOwnProperty.call(body, 'color')) {
            found.color = normalizedHighlightColor(body.color, '', code);
          }
          ['text', 'note', 'sentence'].forEach(function (key) {
            if (Object.prototype.hasOwnProperty.call(body, key)) {
              found[key] = boundedLocalString(body[key], 2000, '', code, '高亮字段', false);
            }
          });
          if (Object.prototype.hasOwnProperty.call(body, 'body')) {
            found.body = boundedLocalString(body.body, 8000, '', code, '高亮正文', false);
          }
          if (Object.prototype.hasOwnProperty.call(body, 'kind')) {
            if (['note', 'translate', 'explain'].indexOf(body.kind) < 0) {
              throw outgoingRequestError('高亮类型无效', code, 400);
            }
            found.kind = body.kind;
          }
          return localStateMutationResult(items, { ok: true, highlight: found });
        }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS });
      });
    }, code);
  }

  function localEPUBHighlights(input, init, url, method) {
    var code = 'BW_LOCAL_EPUB_HIGHLIGHTS';
    if (method === 'GET') {
      return localJSONRoute(function () {
        localFileQuery(url, ['file'], ['file'], code);
        return readState('epub-highlights', [], {
          transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS
        }).then(function (items) {
          return { ok: true, highlights: storedList(items, code) };
        });
      }, code);
    }
    if (method === 'DELETE') {
      return localJSONRoute(function () {
        return deleteRecordRequest(input, init, url, code).then(function (request) {
          return mutateDocumentState('epub-highlights', [], function (items) {
            items = storedList(items, code);
            var before = items.length;
            items = items.filter(function (item) { return item && item.id !== request.id; });
            if (items.length === before) throw outgoingRequestError('未找到高亮', code, 404);
            return localStateMutationResult(items, { ok: true });
          }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS });
        });
      }, code);
    }
    var allowed = method === 'POST'
      ? ['file', 'id', 'cfi', 'anchor', 'text', 'color', 'note', 'sentence', 'body', 'kind']
      : ['file', 'id', 'color', 'note', 'sentence', 'body', 'kind'];
    var required = method === 'POST' ? ['file'] : ['file', 'id'];
    return localJSONRoute(function () {
      strictQuery(url, [], [], code);
      return requestObject(input, init, allowed, required, code).then(function (body) {
        requireLocalFile(body.file, code);
        if (method === 'POST') {
          var cfi = boundedLocalString(body.cfi, 8192, '', code, 'EPUB CFI', true);
          var anchor = body.anchor == null ? null : boundedCanonicalJSON(
            body.anchor, 64 * 1024, code, 'EPUB 高亮锚点'
          );
          if (!cfi && (!anchor || typeof anchor !== 'object' || Array.isArray(anchor))) {
            throw outgoingRequestError('缺少 cfi/anchor', code, 400);
          }
          var clientId = typeof body.id === 'string' && /^c_[a-f0-9]{8,32}$/.test(body.id)
            ? body.id : '';
          var highlight = {
            id: clientId || ('e' + randomHex(6).slice(0, 11)),
            cfi: cfi,
            anchor: anchor,
            text: boundedLocalString(body.text, 2000, '', code, '高亮文字', false),
            color: normalizedHighlightColor(body.color, '#ffd54a', code),
            note: boundedLocalString(body.note, 2000, '', code, '高亮备注', false),
            sentence: boundedLocalString(body.sentence, 2000, '', code, '高亮句子', false),
            body: boundedLocalString(body.body, 8000, '', code, '高亮正文', false),
            kind: boundedLocalString(body.kind, 32, '', code, '高亮类型', true),
            time: nowSeconds()
          };
          return mutateDocumentState('epub-highlights', [], function (items) {
            items = storedList(items, code).filter(function (item) {
              return !item || item.id !== highlight.id;
            });
            items.push(highlight);
            return localStateMutationResult(items, {
              ok: true, id: highlight.id, highlight: highlight
            });
          }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS });
        }
        var id = localRecordId(body.id, code);
        return mutateDocumentState('epub-highlights', [], function (items) {
          items = storedList(items, code);
          var found = items.find(function (item) { return item && item.id === id; });
          if (!found) throw outgoingRequestError('未找到高亮', code, 404);
          if (Object.prototype.hasOwnProperty.call(body, 'color')) {
            found.color = normalizedHighlightColor(body.color, '', code);
          }
          ['note', 'sentence'].forEach(function (key) {
            if (Object.prototype.hasOwnProperty.call(body, key)) {
              found[key] = boundedLocalString(body[key], 2000, '', code, '高亮字段', false);
            }
          });
          if (Object.prototype.hasOwnProperty.call(body, 'body')) {
            found.body = boundedLocalString(body.body, 8000, '', code, '高亮正文', false);
          }
          if (Object.prototype.hasOwnProperty.call(body, 'kind')) {
            found.kind = boundedLocalString(body.kind, 32, '', code, '高亮类型', true);
          }
          return localStateMutationResult(items, { ok: true, highlight: found });
        }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS });
      });
    }, code);
  }

  function normalizedNoteAnchor(value, code) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw outgoingRequestError('便签锚点无效', code, 400);
    }
    assertObjectFields(
      value,
      ['kind', 'page', 'section', 'x', 'y', 'off', 'dx', 'dy', 'clamped'],
      ['kind'], code
    );
    if (value.kind !== 'pdf' && value.kind !== 'epub') {
      throw outgoingRequestError('便签锚点类型无效', code, 400);
    }
    var output = { kind: value.kind };
    if (value.kind === 'pdf') {
      if (typeof value.page === 'string' && /^u_[0-9a-fA-F]{4,16}$/.test(value.page)) {
        output.page = value.page;
      } else {
        output.page = strictInteger(value.page, 1, 10000000, 'anchor.page', code);
      }
    } else if (typeof value.section === 'string' && /^u_[0-9a-fA-F]{4,16}$/.test(value.section)) {
      output.section = value.section;
    } else {
      output.section = strictInteger(value.section, 0, 10000000, 'anchor.section', code);
    }
    ['x', 'y'].forEach(function (key) {
      if (value[key] != null) {
        output[key] = finiteLocalNumber(value[key], -0.05, 1.05, null, code, 'anchor.' + key);
      }
    });
    if (value.off != null) output.off = strictInteger(value.off, 0, 100000000, 'anchor.off', code);
    ['dx', 'dy'].forEach(function (key) {
      if (value[key] != null) {
        output[key] = finiteLocalNumber(value[key], -1000000, 1000000, null, code, 'anchor.' + key);
      }
    });
    if (value.clamped != null) output.clamped = value.clamped ? 1 : 0;
    return output;
  }

  function normalizedStrokeList(value, code) {
    if (!Array.isArray(value)) throw outgoingRequestError('strokes 必须是数组', code, 400);
    if (value.length > 5000) throw outgoingRequestError('strokes 数量过多', code, 413);
    var strokes = boundedCanonicalJSON(value, 16 * 1024 * 1024, code, 'strokes');
    if (strokes.some(function (stroke) {
      return !stroke || typeof stroke !== 'object' || Array.isArray(stroke);
    })) {
      throw outgoingRequestError('stroke 项无效', code, 400);
    }
    return strokes;
  }

  function localNotes(input, init, url, method) {
    var code = 'BW_LOCAL_NOTES';
    if (method === 'GET') {
      return localJSONRoute(function () {
        localFileQuery(url, ['file'], ['file'], code);
        return readState('document-notes-legacy', []).then(function (items) {
          return { ok: true, notes: storedList(items, code) };
        });
      }, code);
    }
    if (method === 'DELETE') {
      return localJSONRoute(function () {
        return deleteRecordRequest(input, init, url, code).then(function (request) {
          return mutateDocumentState('document-notes-legacy', [], function (items) {
            items = storedList(items, code);
            var before = items.length;
            items = items.filter(function (item) { return item && item.id !== request.id; });
            if (items.length === before) throw outgoingRequestError('未找到便签', code, 404);
            return localStateMutationResult(items, { ok: true });
          });
        });
      }, code);
    }
    var allowed = ['file', 'id', 'anchor', 'text', 'color', 'w', 'h', 'collapsed',
      'strokes', 'video', 'card', 'html', 'iar'];
    var required = method === 'POST' ? ['file', 'anchor'] : ['file', 'id'];
    return localJSONRoute(function () {
      strictQuery(url, [], [], code);
      return requestObject(input, init, allowed, required, code).then(function (body) {
        requireLocalFile(body.file, code);
        if (method === 'POST') {
          var clientId = typeof body.id === 'string' && /^c_[a-f0-9]{8,32}$/.test(body.id)
            ? body.id : '';
          var now = nowSeconds();
          var note = {
            id: clientId || ('n' + randomHex(6).slice(0, 11)),
            anchor: normalizedNoteAnchor(body.anchor, code),
            text: boundedLocalString(body.text, 8000, '', code, '便签文字', false),
            color: boundedLocalString(body.color, 64, '#fff8c5', code, '便签颜色', true),
            w: Math.round(finiteLocalNumber(body.w, 40, 4096, 260, code, '便签宽度')),
            h: Math.round(finiteLocalNumber(body.h, 40, 4096, 180, code, '便签高度')),
            collapsed: body.collapsed === true,
            strokes: body.strokes == null ? [] : normalizedStrokeList(body.strokes, code),
            video: body.video == null ? null : boundedCanonicalJSON(body.video, 512 * 1024, code, '视频便签'),
            card: body.card == null ? null : boundedCanonicalJSON(body.card, 2 * 1024 * 1024, code, '卡片便签'),
            html: body.html == null ? null : boundedCanonicalJSON(body.html, 2 * 1024 * 1024, code, 'HTML 便签'),
            iar: body.iar == null ? null : finiteLocalNumber(body.iar, 0.01, 100, null, code, '便签笔迹比例'),
            created: now,
            updated: now
          };
          return mutateDocumentState('document-notes-legacy', [], function (items) {
            items = storedList(items, code).filter(function (item) {
              return !item || item.id !== note.id;
            });
            items.push(note);
            return localStateMutationResult(items, { ok: true, id: note.id, note: note });
          });
        }
        var id = localRecordId(body.id, code);
        return mutateDocumentState('document-notes-legacy', [], function (items) {
          items = storedList(items, code);
          var note = items.find(function (item) { return item && item.id === id; });
          if (!note) throw outgoingRequestError('未找到便签', code, 404);
          if (Object.prototype.hasOwnProperty.call(body, 'anchor')) {
            note.anchor = normalizedNoteAnchor(body.anchor, code);
          }
          if (Object.prototype.hasOwnProperty.call(body, 'text')) {
            note.text = boundedLocalString(body.text, 8000, '', code, '便签文字', false);
          }
          if (Object.prototype.hasOwnProperty.call(body, 'color')) {
            note.color = boundedLocalString(body.color, 64, '#fff8c5', code, '便签颜色', true);
          }
          if (Object.prototype.hasOwnProperty.call(body, 'w')) {
            note.w = Math.round(finiteLocalNumber(body.w, 40, 4096, null, code, '便签宽度'));
          }
          if (Object.prototype.hasOwnProperty.call(body, 'h')) {
            note.h = Math.round(finiteLocalNumber(body.h, 40, 4096, null, code, '便签高度'));
          }
          if (Object.prototype.hasOwnProperty.call(body, 'collapsed')) {
            if (typeof body.collapsed !== 'boolean') {
              throw outgoingRequestError('便签折叠状态无效', code, 400);
            }
            note.collapsed = body.collapsed;
          }
          if (Object.prototype.hasOwnProperty.call(body, 'strokes')) {
            note.strokes = normalizedStrokeList(body.strokes, code);
          }
          ['video', 'card', 'html'].forEach(function (key) {
            if (Object.prototype.hasOwnProperty.call(body, key)) {
              note[key] = body[key] == null ? null : boundedCanonicalJSON(
                body[key], key === 'video' ? 512 * 1024 : 2 * 1024 * 1024,
                code, key + ' 便签'
              );
            }
          });
          if (Object.prototype.hasOwnProperty.call(body, 'iar')) {
            note.iar = body.iar == null ? null : finiteLocalNumber(
              body.iar, 0.01, 100, null, code, '便签笔迹比例'
            );
          }
          note.updated = nowSeconds();
          return localStateMutationResult(items, { ok: true, note: note });
        });
      });
    }, code);
  }

  function localUserPages(input, init, url, method) {
    var code = 'BW_LOCAL_USERPAGES';
    if (method === 'GET') {
      return localJSONRoute(function () {
        localFileQuery(url, ['file'], ['file'], code);
        return readState('user-pages', []).then(function (items) {
          items = storedList(items, code).slice().sort(function (a, b) {
            return Number(a.after || 0) - Number(b.after || 0) ||
              Number(a.created || 0) - Number(b.created || 0);
          });
          return { ok: true, pages: items };
        });
      }, code);
    }
    if (method === 'DELETE') {
      return localJSONRoute(function () {
        return deleteRecordRequest(input, init, url, code).then(function (request) {
          return mutateDocumentState('user-pages', [], function (items) {
            items = storedList(items, code);
            var found = items.find(function (item) { return item && item.id === request.id; });
            if (!found) throw outgoingRequestError('未找到用户页', code, 404);
            if (Number.isInteger(found.page)) {
              throw outgoingRequestError('真实插入页不能只删除本机边车', code, 409);
            }
            return localStateMutationResult(items.filter(function (item) {
              return item && item.id !== request.id;
            }), { ok: true });
          });
        });
      }, code);
    }
    var allowed = method === 'POST'
      ? ['file', 'after', 'title', 'md']
      : ['file', 'id', 'after', 'title', 'md', 'h', 'blocks'];
    var required = method === 'POST' ? ['file'] : ['file', 'id'];
    return localJSONRoute(function () {
      strictQuery(url, [], [], code);
      return requestObject(input, init, allowed, required, code).then(function (body) {
        requireLocalFile(body.file, code);
        if (method === 'POST') {
          var now = nowSeconds();
          var page = {
            id: 'u_' + randomHex(4),
            after: body.after == null ? 0 : strictInteger(body.after, 0, 10000000, 'after', code),
            title: boundedLocalString(body.title, 120, '', code, '用户页标题', false),
            md: boundedLocalString(body.md, 100000, '', code, '用户页正文', false),
            created: now,
            updated: now
          };
          return mutateDocumentState('user-pages', [], function (items) {
            items = storedList(items, code);
            items.push(page);
            return localStateMutationResult(items, { ok: true, id: page.id, page: page });
          });
        }
        var id = localRecordId(body.id, code);
        return mutateDocumentState('user-pages', [], function (items) {
          items = storedList(items, code);
          var page = items.find(function (item) { return item && item.id === id; });
          if (!page) throw outgoingRequestError('未找到用户页', code, 404);
          var real = Number.isInteger(page.page);
          if (real && page.mode !== 'overlay') {
            throw outgoingRequestError('真实插入页必须通过原生 PDF 修改接口编辑', code, 409);
          }
          if (real && (body.after != null || body.h != null)) {
            throw outgoingRequestError('真实插入页锚点不能只改本机边车', code, 409);
          }
          if (Object.prototype.hasOwnProperty.call(body, 'title')) {
            page.title = boundedLocalString(body.title, 120, '', code, '用户页标题', false);
          }
          if (Object.prototype.hasOwnProperty.call(body, 'md')) {
            page.md = boundedLocalString(body.md, 100000, '', code, '用户页正文', false);
          }
          if (!real && Object.prototype.hasOwnProperty.call(body, 'after')) {
            page.after = strictInteger(body.after, 0, 10000000, 'after', code);
          }
          if (!real && Object.prototype.hasOwnProperty.call(body, 'h')) {
            var height = Number(body.h);
            if (!Number.isFinite(height)) throw outgoingRequestError('用户页高度无效', code, 400);
            if (height > 0) page.h = Math.round(Math.max(60, Math.min(30000, height)));
            else delete page.h;
          }
          if (Object.prototype.hasOwnProperty.call(body, 'blocks')) {
            if (!real) throw outgoingRequestError('虚拟页不接受结构化 blocks', code, 400);
            if (!Array.isArray(body.blocks) || body.blocks.length > 400) {
              throw outgoingRequestError('用户页 blocks 无效', code, 400);
            }
            page.blocks = boundedCanonicalJSON(body.blocks, 4 * 1024 * 1024, code, '用户页 blocks');
          }
          if (real) page.md_ver = Number(page.md_ver || 0) + 1;
          page.updated = nowSeconds();
          return localStateMutationResult(items, {
            ok: true, page: page, md_ver: real ? page.md_ver : undefined
          });
        });
      });
    }, code);
  }

  function storedStrokeMap(value, code) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw outgoingRequestError('本机墨迹状态损坏', code, 500);
    }
    Object.keys(value).forEach(function (key) {
      if (!Array.isArray(value[key])) {
        throw outgoingRequestError('本机墨迹状态损坏', code, 500);
      }
    });
    return value;
  }

  function localInk(input, init, url, method, epub) {
    var code = epub ? 'BW_LOCAL_EPUB_INK' : 'BW_LOCAL_PDF_INK';
    var kind = epub ? 'epub-ink' : 'ink';
    var responseKey = epub ? 'sections' : 'pages';
    if (method === 'GET') {
      return localJSONRoute(function () {
        localFileQuery(url, ['file'], ['file'], code);
        return readState(kind, {}).then(function (map) {
          var response = { ok: true };
          response[responseKey] = storedStrokeMap(map, code);
          return response;
        });
      }, code);
    }
    return localJSONRoute(function () {
      strictQuery(url, [], [], code);
      var allowed = epub ? ['file', 'idx', 'strokes'] : ['file', 'page', 'strokes'];
      var required = epub ? ['file', 'idx', 'strokes'] : ['file', 'page', 'strokes'];
      return requestObject(input, init, allowed, required, code).then(function (body) {
        requireLocalFile(body.file, code);
        var key;
        if (epub && typeof body.idx === 'string' &&
            /^u_[0-9a-fA-F]{4,16}$/.test(body.idx)) {
          key = body.idx;
        } else {
          key = String(strictInteger(
            epub ? body.idx : body.page, epub ? 0 : 1, 10000000,
            epub ? 'idx' : 'page', code
          ));
        }
        var strokes = normalizedStrokeList(body.strokes, code);
        return mutateDocumentState(kind, {}, function (map) {
          map = storedStrokeMap(map, code);
          if (strokes.length) map[key] = strokes;
          else delete map[key];
          return localStateMutationResult(map, { ok: true, count: strokes.length });
        });
      });
    }, code);
  }

  function validPreferenceMap(value, code) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw outgoingRequestError('本机设置状态损坏', code, 500);
    }
    return value;
  }

  function localPreferences(input, init, url, method) {
    var code = 'BW_LOCAL_PREFS';
    if (method === 'GET') {
      return localJSONRoute(function () {
        strictQuery(url, [], [], code);
        return readDeviceState('reader-prefs', {}).then(function (prefs) {
          return { ok: true, prefs: validPreferenceMap(prefs, code) };
        });
      }, code);
    }
    return localJSONRoute(function () {
      strictQuery(url, [], [], code);
      return requestObject(input, init, ['patch'], ['patch'], code, 512 * 1024)
        .then(function (body) {
          if (!body.patch || typeof body.patch !== 'object' || Array.isArray(body.patch) ||
              Object.keys(body.patch).length > 256) {
            throw outgoingRequestError('设置 patch 无效', code, 400);
          }
          Object.keys(body.patch).forEach(function (key) {
            if (!key || key.length > 160 || /[\u0000-\u001f]/.test(key)) {
              throw outgoingRequestError('设置键无效', code, 400);
            }
          });
          var patch = boundedCanonicalJSON(body.patch, 512 * 1024, code, '设置 patch');
          return mutateDeviceState('reader-prefs', {}, function (prefs) {
            prefs = validPreferenceMap(prefs, code);
            Object.keys(patch).forEach(function (key) {
              if (patch[key] === null) delete prefs[key];
              else prefs[key] = patch[key];
            });
            return localStateMutationResult(prefs, { ok: true, prefs: prefs });
          });
        });
    }, code);
  }

  var LOCAL_VIDEO_PREFERENCE_KEYS = new Set([
    'x', 'y', 'w', 'h', 'showEn', 'subOut'
  ]);
  function validVideoPlayerPreferences(value, code) {
    value = validPreferenceMap(value, code);
    Object.keys(value).forEach(function (key) {
      if (!LOCAL_VIDEO_PREFERENCE_KEYS.has(key)) {
        throw outgoingRequestError('视频浮窗设置含未知字段', code, 500);
      }
      var item = value[key];
      if ((key === 'showEn' || key === 'subOut') && typeof item !== 'boolean') {
        throw outgoingRequestError('视频浮窗开关状态损坏', code, 500);
      }
      if (key !== 'showEn' && key !== 'subOut' &&
          (!Number.isFinite(item) || item < -100000 || item > 100000)) {
        throw outgoingRequestError('视频浮窗尺寸状态损坏', code, 500);
      }
    });
    return value;
  }

  function localVideoPlayerPreferences(input, init, url, method) {
    var code = 'BW_LOCAL_VIDEO_PLAYER_PREFS';
    if (method === 'GET') {
      return localJSONRoute(function () {
        strictQuery(url, [], [], code);
        return readDeviceState('video-player-prefs', {}).then(function (prefs) {
          return {
            ok: true,
            prefs: validVideoPlayerPreferences(prefs, code)
          };
        });
      }, code);
    }
    return localJSONRoute(function () {
      strictQuery(url, [], [], code);
      return requestObject(input, init, ['patch'], ['patch'], code, 64 * 1024)
        .then(function (body) {
          if (!body.patch || typeof body.patch !== 'object' ||
              Array.isArray(body.patch)) {
            throw outgoingRequestError('视频浮窗设置 patch 无效', code, 400);
          }
          var patch = boundedCanonicalJSON(
            body.patch, 64 * 1024, code, '视频浮窗设置 patch'
          );
          Object.keys(patch).forEach(function (key) {
            if (!LOCAL_VIDEO_PREFERENCE_KEYS.has(key)) {
              throw outgoingRequestError('视频浮窗设置字段无效', code, 400);
            }
            var item = patch[key];
            if (item === null) return;
            if ((key === 'showEn' || key === 'subOut') &&
                typeof item !== 'boolean') {
              throw outgoingRequestError('视频浮窗开关无效', code, 400);
            }
            if (key !== 'showEn' && key !== 'subOut' &&
                (!Number.isFinite(item) || item < -100000 || item > 100000)) {
              throw outgoingRequestError('视频浮窗尺寸无效', code, 400);
            }
          });
          return mutateDeviceState('video-player-prefs', {}, function (prefs) {
            prefs = validVideoPlayerPreferences(prefs, code);
            Object.keys(patch).forEach(function (key) {
              if (patch[key] === null) delete prefs[key];
              else prefs[key] = patch[key];
            });
            return localStateMutationResult(prefs, {
              ok: true,
              prefs: prefs
            });
          });
        });
    }, code);
  }

  function validReadingPositions(value, code) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw outgoingRequestError('本机续读位置状态损坏', code, 500);
    }
    return value;
  }

  function localReadingPosition(input, init, url, method) {
    var code = 'BW_LOCAL_READING_POSITION';
    var identity = localFileRef();
    if (method === 'GET') {
      return localJSONRoute(function () {
        strictQuery(url, [], [], code);
        return Promise.all([
          readDeviceState('reader-positions', {}),
          readState('reading-position', null)
        ]).then(function (values) {
          var positions = validReadingPositions(values[0], code);
          if (!positions[identity] && values[1]) positions[identity] = clone(values[1]);
          return { ok: true, positions: positions };
        });
      }, code);
    }
    return localJSONRoute(function () {
      strictQuery(url, [], [], code);
      return requestObject(
        input, init, ['file', 'kind', 'pos'], ['file', 'kind', 'pos'], code, 64 * 1024
      ).then(function (body) {
        requireLocalFile(body.file, code);
        if (body.kind !== 'pdf' && body.kind !== 'epub') {
          throw outgoingRequestError('续读位置类型无效', code, 400);
        }
        var value = {
          kind: body.kind,
          pos: strictInteger(body.pos, 0, 10000000, 'pos', code),
          ts: nowSeconds()
        };
        return mutateDocumentState('reading-position', null, function () {
          return localStateMutationResult(value, value);
        }).then(function () {
          return mutateDeviceState('reader-positions', {}, function (positions) {
            positions = validReadingPositions(positions, code);
            positions[identity] = value;
            return localStateMutationResult(positions, { ok: true, pos: value.pos });
          });
        });
      });
    }, code);
  }

  var LOCAL_BOOK_LANGUAGES = new Set(['en', 'ja', 'zh', 'ko', 'fr', 'de']);
  function localBookLanguages(input, init, url, method) {
    var code = 'BW_LOCAL_BOOK_LANGUAGES';
    if (method === 'GET') {
      return localJSONRoute(function () {
        localFileQuery(url, ['file'], ['file'], code);
        return readState('book-languages', []).then(function (langs) {
          if (!Array.isArray(langs) || langs.some(function (lang) {
            return !LOCAL_BOOK_LANGUAGES.has(lang);
          })) {
            throw outgoingRequestError('本机书籍语言状态损坏', code, 500);
          }
          return { ok: true, langs: langs };
        });
      }, code);
    }
    return localJSONRoute(function () {
      strictQuery(url, [], [], code);
      return requestObject(input, init, ['file', 'langs'], ['file', 'langs'], code, 64 * 1024)
        .then(function (body) {
          requireLocalFile(body.file, code);
          if (!Array.isArray(body.langs) || body.langs.length > 16 ||
              body.langs.some(function (lang) { return typeof lang !== 'string'; })) {
            throw outgoingRequestError('书籍语言列表无效', code, 400);
          }
          var langs = [];
          body.langs.forEach(function (lang) {
            if (LOCAL_BOOK_LANGUAGES.has(lang) && langs.indexOf(lang) < 0) langs.push(lang);
          });
          return mutateDocumentState('book-languages', [], function () {
            return localStateMutationResult(langs, { ok: true, langs: langs });
          }).then(function (response) {
            nativeSearchGeneration += 1;
            nativeSearchCache.clear();
            nativeSearchPending.clear();
            try {
              root.dispatchEvent(new CustomEvent('bw:native-book-languages-changed', {
                detail: {
                  contract: 'reader-native-book-languages/1',
                  localBookId: bookId,
                  langs: clone(langs)
                }
              }));
            } catch (_) {}
            return response;
          });
        });
    }, code);
  }

  function normalizedLocalBookCrop(value, code) {
    if (value == null) value = {};
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        Object.keys(value).some(function (key) {
          return ['l', 'r', 't', 'b'].indexOf(key) < 0;
        })) {
      throw outgoingRequestError('本机书籍裁边状态无效', code, 400);
    }
    var crop = {};
    ['l', 'r', 't', 'b'].forEach(function (key) {
      crop[key] = finiteLocalNumber(value[key], 0, 45, 0, code, '裁边 ' + key);
    });
    if (crop.l + crop.r >= 90 || crop.t + crop.b >= 90) {
      throw outgoingRequestError('书籍裁边不能裁掉整页', code, 400);
    }
    return crop;
  }

  function localBookCrop(input, init, url, method) {
    var code = 'BW_LOCAL_BOOK_CROP';
    if (method === 'GET') {
      return localJSONRoute(function () {
        localFileQuery(url, ['file'], ['file'], code);
        return readState('book-crop', {}).then(function (value) {
          return { ok: true, crop: normalizedLocalBookCrop(value, code) };
        });
      }, code);
    }
    return localJSONRoute(function () {
      strictQuery(url, [], [], code);
      return requestObject(input, init, ['file', 'crop'], ['file', 'crop'], code, 64 * 1024)
        .then(function (body) {
          requireLocalFile(body.file, code);
          var crop = normalizedLocalBookCrop(body.crop, code);
          return mutateDocumentState('book-crop', {}, function () {
            return localStateMutationResult(crop, { ok: true, crop: crop });
          });
        });
    }, code);
  }

  function noteCompositeSize(value, fallback, minimum) {
    value = Number(value);
    if (!Number.isFinite(value) || value <= 0) value = fallback;
    // Notes are normally only a few hundred CSS pixels. Keep malformed or old
    // local records from allocating an unbounded 2x canvas while preserving a
    // generous working size for deliberately large notes.
    return Math.min(2048, Math.max(minimum, Math.round(value)));
  }
  function noteCompositeInkBox(iar, width, height) {
    iar = Number(iar);
    var box = { ox: 0, oy: 0, w: width, h: height };
    if (!Number.isFinite(iar) || iar <= 0) return box;
    if (width / height > iar) {
      box.w = height * iar;
      box.ox = (width - box.w) / 2;
    } else {
      box.h = width / iar;
      box.oy = (height - box.h) / 2;
    }
    return box;
  }
  function noteCompositePoint(value, box) {
    if (!Array.isArray(value) || value.length < 2) return null;
    var x = Number(value[0]), y = Number(value[1]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    return [box.ox + x * box.w, box.oy + y * box.h];
  }
  function noteCompositeLines(ctx, text, maximumWidth) {
    var lines = [];
    var line = '';
    Array.from(String(text || '')).forEach(function (character) {
      if (character === '\n') {
        lines.push(line); line = ''; return;
      }
      var candidate = line + character;
      if (line && ctx.measureText(candidate).width > maximumWidth) {
        lines.push(line); line = character;
      } else {
        line = candidate;
      }
    });
    if (line || !lines.length) lines.push(line);
    return lines;
  }
  function renderLocalNoteComposite(note) {
    if (!root.document || typeof root.document.createElement !== 'function') {
      throw outgoingRequestError(
        '本机便签合成需要 Canvas', 'BW_LOCAL_NOTE_COMPOSITE_CANVAS', 501
      );
    }
    var scale = 2;
    var cssWidth = noteCompositeSize(note && note.w, 260, 120);
    var cssHeight = noteCompositeSize(note && note.h, 180, 80);
    var canvas = root.document.createElement('canvas');
    canvas.width = cssWidth * scale;
    canvas.height = cssHeight * scale;
    var ctx = canvas && typeof canvas.getContext === 'function'
      ? canvas.getContext('2d') : null;
    if (!ctx || typeof canvas.toDataURL !== 'function') {
      throw outgoingRequestError(
        '本机便签合成 Canvas 不可用', 'BW_LOCAL_NOTE_COMPOSITE_CANVAS', 501
      );
    }

    ctx.fillStyle = '#fff8c5';
    if (typeof note.color === 'string' && note.color) ctx.fillStyle = note.color;
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    var text = typeof note.text === 'string' ? note.text.slice(0, 8000) : '';
    if (text) {
      var insetX = 10 * scale, insetY = 8 * scale;
      var lineHeight = Math.round(15 * scale * 1.5);
      ctx.fillStyle = '#1b1b1b';
      ctx.font = (15 * scale) + 'px -apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif';
      ctx.textBaseline = 'top';
      var lines = noteCompositeLines(ctx, text, canvas.width - insetX * 2);
      for (var lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
        var y = insetY + lineIndex * lineHeight;
        if (y > canvas.height - lineHeight) break;
        ctx.fillText(lines[lineIndex], insetX, y);
      }
    }

    var box = noteCompositeInkBox(note && note.iar, canvas.width, canvas.height);
    (Array.isArray(note && note.strokes) ? note.strokes : []).forEach(function (stroke) {
      if (!stroke || typeof stroke !== 'object' || !Array.isArray(stroke.pts)) return;
      var points = stroke.pts.map(function (point) {
        return noteCompositePoint(point, box);
      }).filter(Boolean);
      if (!points.length) return;
      var width = Number(stroke.w);
      if (!Number.isFinite(width) || width <= 0) width = 2;
      ctx.strokeStyle = '#e33';
      if (typeof stroke.c === 'string' && stroke.c) ctx.strokeStyle = stroke.c;
      ctx.fillStyle = ctx.strokeStyle;
      ctx.lineWidth = Math.max(1, Math.min(256, width * scale));
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      if (points.length === 1) {
        ctx.beginPath();
        ctx.arc(points[0][0], points[0][1], ctx.lineWidth / 2, 0, Math.PI * 2);
        ctx.fill();
        return;
      }
      ctx.beginPath();
      ctx.moveTo(points[0][0], points[0][1]);
      for (var pointIndex = 1; pointIndex < points.length; pointIndex += 1) {
        ctx.lineTo(points[pointIndex][0], points[pointIndex][1]);
      }
      ctx.stroke();
    });

    var dataURL;
    try { dataURL = canvas.toDataURL('image/png'); }
    catch (error) {
      throw outgoingRequestError(
        '本机便签合成失败：' + String(error && error.message || error),
        'BW_LOCAL_NOTE_COMPOSITE_RENDER', 500
      );
    }
    if (!/^data:image\/png;base64,[A-Za-z0-9+/]+=*$/.test(String(dataURL || ''))) {
      throw outgoingRequestError(
        '本机便签合成结果无效', 'BW_LOCAL_NOTE_COMPOSITE_RENDER', 500
      );
    }
    return dataURL;
  }
  function localNoteComposite(input, init, url) {
    var code = 'BW_LOCAL_NOTE_COMPOSITE_BODY';
    return Promise.resolve().then(function () {
      strictQuery(url, [], [], 'BW_LOCAL_NOTE_COMPOSITE_QUERY');
      return strictRequestJSON(input, init, 32 * 1024, code);
    }).then(function (body) {
      assertObjectFields(body, ['file', 'id'], ['file', 'id'], code);
      localFileIdentity(body.file, code);
      if (typeof body.id !== 'string' || !body.id.trim() ||
          body.id.trim().length > 240 || /[\u0000-\u001f\u007f]/.test(body.id)) {
        throw outgoingRequestError('便签 id 无效', code, 400);
      }
      var id = body.id.trim();
      return readState('document-notes-legacy', []).then(function (notes) {
        var note = (Array.isArray(notes) ? notes : []).find(function (item) {
          return item && String(item.id || '') === id;
        });
        if (!note) {
          throw outgoingRequestError(
            '未找到便签', 'BW_LOCAL_NOTE_COMPOSITE_NOT_FOUND', 404
          );
        }
        return jsonResponse({ ok: true, data_url: renderLocalNoteComposite(note) });
      });
    }).catch(function (error) {
      return outgoingFailureResponse(error, 'BW_LOCAL_NOTE_COMPOSITE_FAILED', 500);
    });
  }
  function outgoingJSONRoute(task, fallbackCode) {
    return Promise.resolve().then(task).then(function (value) {
      return jsonResponse(value);
    }).catch(function (error) {
      return outgoingFailureResponse(error, fallbackCode, 500);
    });
  }

  function nativePDFMutationHandler() {
    var handlers = root.webkit && root.webkit.messageHandlers;
    var handler = handlers && handlers.bwNativePDFMutation;
    return handler && typeof handler.postMessage === 'function' ? handler : null;
  }

  function nativePDFMutationRequest(action, fields) {
    var handler = nativePDFMutationHandler();
    if (!handler) {
      return Promise.reject(new RuntimeError(
        '原生 PDF 改页桥不可用', 'BW_NATIVE_PDF_MUTATION_BRIDGE'
      ));
    }
    var requestId = 'npm_' + randomHex(12);
    var request = Object.assign({
      contract: NATIVE_PDF_MUTATION_REQUEST_CONTRACT,
      action: action,
      requestId: requestId,
      localBookId: bookId
    }, fields || {});
    return Promise.resolve(handler.postMessage(request)).then(function (raw) {
      var common = raw && raw.contract === NATIVE_PDF_MUTATION_RESPONSE_CONTRACT &&
        raw.requestId === requestId && raw.localBookId === bookId && raw.ok === true;
      var valid = false;
      if (common && action === 'prepare') {
        valid = exactKeys(raw, [
          'contract', 'action', 'requestId', 'ok', 'localBookId', 'ticket',
          'operation', 'pivotPage', 'oldPageCount', 'newPageCount',
          'oldContentSHA256', 'stagedContentSHA256', 'warnings'
        ]) && raw.action === 'prepared' &&
          /^npmt_[a-f0-9]{32}$/.test(String(raw.ticket || '')) &&
          ['insert', 'edit', 'delete'].indexOf(raw.operation) >= 0 &&
          Number.isInteger(raw.pivotPage) && raw.pivotPage >= 1 &&
          Number.isInteger(raw.oldPageCount) && raw.oldPageCount >= 1 &&
          Number.isInteger(raw.newPageCount) && raw.newPageCount >= 1 &&
          /^[a-f0-9]{64}$/.test(String(raw.oldContentSHA256 || '')) &&
          /^[a-f0-9]{64}$/.test(String(raw.stagedContentSHA256 || '')) &&
          Array.isArray(raw.warnings) && raw.warnings.length <= 10 &&
          raw.warnings.every(function (warning) {
            return typeof warning === 'string' && warning.length <= 500;
          });
      } else if (common && action === 'commit') {
        valid = exactKeys(raw, [
          'contract', 'action', 'requestId', 'ok', 'localBookId', 'ticket',
          'operation', 'pivotPage', 'oldPageCount', 'newPageCount',
          'contentSHA256', 'mtime', 'byteCount'
        ]) && raw.action === 'committed' &&
          /^npmt_[a-f0-9]{32}$/.test(String(raw.ticket || '')) &&
          ['insert', 'edit', 'delete'].indexOf(raw.operation) >= 0 &&
          Number.isInteger(raw.pivotPage) && raw.pivotPage >= 1 &&
          Number.isInteger(raw.oldPageCount) && raw.oldPageCount >= 1 &&
          Number.isInteger(raw.newPageCount) && raw.newPageCount >= 1 &&
          /^[a-f0-9]{64}$/.test(String(raw.contentSHA256 || '')) &&
          Number.isInteger(raw.mtime) && raw.mtime >= 0 &&
          Number.isInteger(raw.byteCount) && raw.byteCount > 0;
      } else if (common && (action === 'finalize' || action === 'cancel')) {
        valid = exactKeys(raw, [
          'contract', 'action', 'requestId', 'ok', 'localBookId', 'ticket'
        ]) && raw.action === (action === 'finalize' ? 'finalized' : 'cancelled') &&
          /^npmt_[a-f0-9]{32}$/.test(String(raw.ticket || ''));
      } else if (raw && raw.contract === NATIVE_PDF_MUTATION_RESPONSE_CONTRACT &&
          raw.requestId === requestId && raw.localBookId === bookId &&
          raw.ok === true && action === 'recover') {
        valid = exactKeys(raw, [
          'contract', 'action', 'requestId', 'ok', 'localBookId', 'ticket',
          'outcome', 'contentSHA256', 'mtime', 'byteCount'
        ]) && raw.action === 'recovered' &&
          (raw.ticket === null ||
            /^npmt_[a-f0-9]{32}$/.test(String(raw.ticket || ''))) &&
          ['none', 'committed', 'rolled-back'].indexOf(raw.outcome) >= 0 &&
          ((raw.outcome === 'none' && raw.contentSHA256 === null) ||
            /^[a-f0-9]{64}$/.test(String(raw.contentSHA256 || ''))) &&
          Number.isInteger(raw.mtime) && raw.mtime >= 0 &&
          Number.isInteger(raw.byteCount) && raw.byteCount > 0;
      }
      if (!valid) {
        throw new RuntimeError(
          '原生 PDF 改页响应合同无效',
          'BW_NATIVE_PDF_MUTATION_BRIDGE_RESPONSE'
        );
      }
      return raw;
    });
  }

  function nativePDFPageMap(page, operation, pivotPage) {
    page = Number(page);
    if (!Number.isInteger(page) || page < 1) return page;
    if (operation === 'insert') return page >= pivotPage ? page + 1 : page;
    if (operation === 'delete') {
      if (page === pivotPage) return null;
      return page > pivotPage ? page - 1 : page;
    }
    return page;
  }

  function migrateNativePDFHighlights(value, plan) {
    if (!Array.isArray(value)) {
      throw new RuntimeError(
        '本机高亮状态损坏', 'BW_NATIVE_PDF_MUTATION_STATE'
      );
    }
    return value.reduce(function (output, item) {
      item = clone(item);
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        output.push(item);
        return output;
      }
      var mapped = nativePDFPageMap(item.page, plan.operation, plan.pivotPage);
      if (mapped !== null) {
        if (Number.isInteger(Number(item.page))) item.page = mapped;
        output.push(item);
      }
      return output;
    }, []);
  }

  function migratedNativePDFInkKey(key, plan) {
    var numeric = /^([1-9]\d*)$/.exec(String(key));
    if (numeric) {
      var mapped = nativePDFPageMap(Number(numeric[1]), plan.operation, plan.pivotPage);
      return mapped == null ? null : String(mapped);
    }
    var embedded = /^(pdf\|.{1,512}\|)([1-9]\d*)$/.exec(String(key));
    if (!embedded) return String(key);
    var mappedEmbedded = nativePDFPageMap(
      Number(embedded[2]), plan.operation, plan.pivotPage
    );
    return mappedEmbedded == null ? null : embedded[1] + mappedEmbedded;
  }

  function migrateNativePDFInk(value, plan) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new RuntimeError(
        '本机笔迹状态损坏', 'BW_NATIVE_PDF_MUTATION_STATE'
      );
    }
    var output = {};
    Object.keys(value).forEach(function (key) {
      if (!Array.isArray(value[key])) {
        throw new RuntimeError(
          '本机笔迹状态损坏', 'BW_NATIVE_PDF_MUTATION_STATE'
        );
      }
      var mapped = migratedNativePDFInkKey(key, plan);
      if (mapped != null) output[mapped] = clone(value[key]);
    });
    return output;
  }

  function migrateNativePDFNotes(value, plan) {
    if (!Array.isArray(value)) {
      throw new RuntimeError(
        '本机便签状态损坏', 'BW_NATIVE_PDF_MUTATION_STATE'
      );
    }
    return value.reduce(function (output, note) {
      note = clone(note);
      var anchor = note && note.anchor;
      if (anchor && anchor.kind === 'pdf' && Number.isInteger(anchor.page)) {
        var mapped = nativePDFPageMap(
          anchor.page, plan.operation, plan.pivotPage
        );
        if (mapped == null) return output;
        anchor.page = mapped;
      }
      output.push(note);
      return output;
    }, []);
  }

  function migrateNativePDFCardPlacements(value, beforeNotes, notes, plan) {
    if (!Array.isArray(value)) {
      throw new RuntimeError(
        '本机卡片位置状态损坏', 'BW_NATIVE_PDF_MUTATION_STATE'
      );
    }
    var priorNoteIDs = new Set((Array.isArray(beforeNotes) ? beforeNotes : [])
      .map(function (note) { return String(note && note.id || ''); })
      .filter(Boolean));
    var retainedNoteIDs = new Set((Array.isArray(notes) ? notes : [])
      .map(function (note) { return String(note && note.id || ''); })
      .filter(Boolean));
    var output = value.reduce(function (items, raw) {
      var item = clone(raw);
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        items.push(item);
        return items;
      }
      var noteID = String(item.noteId || '');
      if (noteID && priorNoteIDs.has(noteID) && !retainedNoteIDs.has(noteID)) {
        return items;
      }
      var anchor = item.anchor;
      if (anchor && anchor.kind === 'pdf' && Number.isInteger(anchor.page)) {
        var mapped = nativePDFPageMap(
          anchor.page, plan.operation, plan.pivotPage
        );
        if (mapped == null) return items;
        anchor.page = mapped;
      }
      items.push(item);
      return items;
    }, []);
    deriveCardPlacements(notes).forEach(function (derived) {
      var index = output.findIndex(function (item) {
        return item && item.placementId === derived.placementId;
      });
      if (index >= 0) output[index] = derived;
      else output.push(derived);
    });
    return output;
  }

  function migrateNativePDFReadingPosition(value, plan) {
    value = value == null ? null : clone(value);
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        value.kind === 'epub' || !Number.isInteger(Number(value.pos))) {
      return value;
    }
    var mapped = nativePDFPageMap(
      Number(value.pos), plan.operation, plan.pivotPage
    );
    value.pos = mapped == null ? Math.max(1, Number(value.pos) - 1) : mapped;
    value.ts = nowSeconds();
    return value;
  }

  function migrateNativePDFUserPages(value, plan) {
    if (!Array.isArray(value)) {
      throw new RuntimeError(
        '本机用户页状态损坏', 'BW_NATIVE_PDF_MUTATION_STATE'
      );
    }
    var output = value.reduce(function (items, raw) {
      var item = clone(raw);
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        items.push(item);
        return items;
      }
      if (Number.isInteger(item.page)) {
        var mapped = nativePDFPageMap(
          item.page, plan.operation, plan.pivotPage
        );
        if (mapped == null) return items;
        item.page = mapped;
      } else if (Number.isInteger(Number(item.after))) {
        var after = Number(item.after);
        if (plan.operation === 'insert' && after > plan.after) {
          item.after = after + 1;
        } else if (plan.operation === 'delete' && after >= plan.pivotPage) {
          item.after = Math.max(0, after - 1);
        }
      }
      items.push(item);
      return items;
    }, []);
    var now = nowSeconds();
    if (plan.operation === 'insert') {
      output.push({
        id: plan.id,
        page: plan.pivotPage,
        title: plan.title,
        md: plan.markdown,
        real: true,
        mode: 'overlay',
        md_ver: 0,
        synced_ver: 0,
        created: now,
        updated: now
      });
    } else if (plan.operation === 'edit') {
      var edited = output.find(function (item) {
        return item && item.id === plan.id;
      });
      if (!edited) {
        throw new RuntimeError(
          '改页期间用户页记录消失', 'BW_NATIVE_PDF_MUTATION_CONFLICT'
        );
      }
      if (plan.titleProvided) edited.title = plan.title;
      if (plan.markdownProvided) edited.md = plan.markdown;
      if (edited.mode === 'overlay') {
        var currentVersion = Number.isInteger(Number(edited.md_ver))
          ? Number(edited.md_ver) : 0;
        if (plan.titleProvided || plan.markdownProvided) currentVersion += 1;
        edited.md_ver = currentVersion;
        edited.synced_ver = currentVersion;
      }
      edited.updated = now;
    } else if (plan.operation === 'delete') {
      output = output.filter(function (item) {
        return !item || item.id !== plan.id;
      });
    }
    return output;
  }

  function migrateNativePDFDevicePositions(value, plan) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new RuntimeError(
        '本机续读位置状态损坏', 'BW_NATIVE_PDF_MUTATION_STATE'
      );
    }
    var output = clone(value);
    var identity = localFileRef();
    if (output[identity]) {
      output[identity] = migrateNativePDFReadingPosition(
        output[identity], plan
      );
    }
    return output;
  }

  function nativePDFMutationSnapshot(plan) {
    var fallbackByKind = {
      'reading-position': null,
      'document-highlights': [],
      'ink': {},
      'document-notes-legacy': [],
      'user-pages': [],
      'card-placements': [],
      'entity-references': []
    };
    return Promise.all(PDF_MUTATION_DOCUMENT_KINDS.map(function (kind) {
      return storedStateRecord(
        stores.document, kind, 'documentId', bookId, fallbackByKind[kind]
      ).then(function (record) {
        return { kind: kind, payload: record.payload, rev: record.rev };
      });
    }).concat([
      storedStateRecord(
        stores.device, 'reader-positions', 'deviceId', deviceId, {}
      )
    ])).then(function (records) {
      var device = records.pop();
      var before = Object.create(null);
      records.forEach(function (record) { before[record.kind] = record; });
      var beforeNotes = before['document-notes-legacy'].payload;
      var notes = migrateNativePDFNotes(beforeNotes, plan);
      var placements = migrateNativePDFCardPlacements(
        before['card-placements'].payload, beforeNotes, notes, plan
      );
      var after = {
        'reading-position': migrateNativePDFReadingPosition(
          before['reading-position'].payload, plan
        ),
        'document-highlights': migrateNativePDFHighlights(
          before['document-highlights'].payload, plan
        ),
        'ink': migrateNativePDFInk(before.ink.payload, plan),
        'document-notes-legacy': notes,
        'user-pages': migrateNativePDFUserPages(
          before['user-pages'].payload, plan
        ),
        'card-placements': placements,
        'entity-references': deriveEntityReferences(placements)
      };
      return {
        before: before,
        after: after,
        deviceBefore: device,
        deviceAfter: migrateNativePDFDevicePositions(device.payload, plan),
        applied: false,
        plan: clone(plan),
        journal: null,
        journalRev: 0
      };
    });
  }

  function nativePDFMutationJournalPayload(transaction, prepared, phase) {
    return {
      contract: NATIVE_PDF_MUTATION_JOURNAL_CONTRACT,
      localBookId: bookId,
      ticket: prepared.ticket,
      phase: phase,
      oldContentSHA256: prepared.oldContentSHA256,
      stagedContentSHA256: prepared.stagedContentSHA256,
      plan: clone(transaction.plan),
      before: clone(transaction.before),
      after: clone(transaction.after),
      deviceBefore: clone(transaction.deviceBefore),
      deviceAfter: clone(transaction.deviceAfter),
      domainPolicy: PDF_MUTATION_PAGE_ANCHOR_DOMAINS.map(function (item) {
        return item.slice();
      })
    };
  }

  function validateNativePDFMutationJournal(value) {
    var phases = new Set([
      'prepared', 'document-applied', 'anchors-applied',
      'pdf-replaced', 'committed'
    ]);
    var valid = value && exactKeys(value, [
      'contract', 'localBookId', 'ticket', 'phase',
      'oldContentSHA256', 'stagedContentSHA256', 'plan', 'before', 'after',
      'deviceBefore', 'deviceAfter', 'domainPolicy'
    ]) && value.contract === NATIVE_PDF_MUTATION_JOURNAL_CONTRACT &&
      value.localBookId === bookId &&
      /^npmt_[a-f0-9]{32}$/.test(String(value.ticket || '')) &&
      phases.has(value.phase) &&
      /^[a-f0-9]{64}$/.test(String(value.oldContentSHA256 || '')) &&
      /^[a-f0-9]{64}$/.test(String(value.stagedContentSHA256 || '')) &&
      value.plan && typeof value.plan === 'object' &&
      ['insert', 'edit', 'delete'].indexOf(value.plan.operation) >= 0 &&
      value.before && typeof value.before === 'object' &&
      value.after && typeof value.after === 'object' &&
      value.deviceBefore && typeof value.deviceBefore === 'object' &&
      value.deviceAfter && typeof value.deviceAfter === 'object' &&
      Array.isArray(value.domainPolicy) && value.domainPolicy.length === 20 &&
      canonicalJSONString(value.domainPolicy) === canonicalJSONString(
        PDF_MUTATION_PAGE_ANCHOR_DOMAINS
      );
    if (!valid || PDF_MUTATION_DOCUMENT_KINDS.some(function (kind) {
      var before = value && value.before && value.before[kind];
      return !before || !Number.isInteger(before.rev) || before.rev < 0 ||
        !Object.prototype.hasOwnProperty.call(before, 'payload') ||
        !Object.prototype.hasOwnProperty.call(value.after, kind);
    }) || !Number.isInteger(value.deviceBefore.rev) ||
        value.deviceBefore.rev < 0 ||
        !Object.prototype.hasOwnProperty.call(value.deviceBefore, 'payload')) {
      throw new RuntimeError(
        '本机 PDF 改页 journal 损坏', 'BW_NATIVE_PDF_MUTATION_JOURNAL'
      );
    }
    return clone(value);
  }

  function nativePDFMutationJournalRecord() {
    var id = stateId(NATIVE_PDF_MUTATION_JOURNAL_KIND);
    return stores.document.get(
      'native-' + NATIVE_PDF_MUTATION_JOURNAL_KIND, id
    ).then(function (record) {
      if (record == null) return null;
      var payload = validateStoredEnvelope(
        record, id, 'documentId', bookId
      );
      return {
        payload: validateNativePDFMutationJournal(payload),
        rev: Number(record.rev || 0)
      };
    });
  }

  function persistNativePDFMutationJournal(transaction, prepared) {
    transaction.journal = nativePDFMutationJournalPayload(
      transaction, prepared, 'prepared'
    );
    return nativePDFMutationJournalRecord().then(function (existing) {
      if (existing) {
        throw new RuntimeError(
          '本书已有未恢复的改页 journal',
          'BW_NATIVE_PDF_MUTATION_BUSY'
        );
      }
      return stores.document.batch([
        stateRecordMutation(
          NATIVE_PDF_MUTATION_JOURNAL_KIND,
          transaction.journal,
          randomHex(12),
          0
        )
      ]);
    }).then(function () {
      return nativePDFMutationJournalRecord();
    }).then(function (record) {
      if (!record || record.payload.ticket !== prepared.ticket) {
        throw new RuntimeError(
          '本机 PDF 改页 journal 写入后无法读回',
          'BW_NATIVE_PDF_MUTATION_JOURNAL'
        );
      }
      transaction.journal = record.payload;
      transaction.journalRev = record.rev;
      return transaction;
    });
  }

  function setNativePDFMutationJournalPhase(transaction, phase) {
    return nativePDFMutationJournalRecord().then(function (record) {
      if (!record || record.payload.ticket !== transaction.journal.ticket) {
        throw new RuntimeError(
          '本机 PDF 改页 journal 在事务中消失',
          'BW_NATIVE_PDF_MUTATION_JOURNAL'
        );
      }
      var next = clone(record.payload);
      next.phase = phase;
      return stores.document.batch([
        stateRecordMutation(
          NATIVE_PDF_MUTATION_JOURNAL_KIND,
          next,
          randomHex(12),
          record.rev
        )
      ]).then(function () {
        transaction.journal = next;
        transaction.journalRev = record.rev + 1;
        return transaction;
      });
    });
  }

  function removeNativePDFMutationJournal(transaction) {
    return nativePDFMutationJournalRecord().then(function (record) {
      if (!record) return;
      if (!transaction || record.payload.ticket !== transaction.journal.ticket) {
        throw new RuntimeError(
          '拒绝删除另一项 PDF 改页 journal',
          'BW_NATIVE_PDF_MUTATION_JOURNAL'
        );
      }
      return stores.document.remove(
        'native-' + NATIVE_PDF_MUTATION_JOURNAL_KIND,
        stateId(NATIVE_PDF_MUTATION_JOURNAL_KIND),
        {
          mutationId: 'native-pdf-mutation-journal-remove-' + randomHex(12),
          ifRev: record.rev
        }
      );
    });
  }

  function nativePDFMutationTransactionFromJournal(record) {
    var value = record.payload;
    return {
      before: clone(value.before),
      after: clone(value.after),
      deviceBefore: clone(value.deviceBefore),
      deviceAfter: clone(value.deviceAfter),
      applied: value.phase !== 'prepared',
      plan: clone(value.plan),
      journal: clone(value),
      journalRev: record.rev
    };
  }

  function nativePDFMutationPayloadEqual(left, right) {
    return canonicalJSONString(left) === canonicalJSONString(right);
  }

  function reconcileNativePDFMutationDocuments(transaction, desired, phase) {
    return Promise.all(PDF_MUTATION_DOCUMENT_KINDS.map(function (kind) {
      var fallback = transaction.before[kind].payload;
      return storedStateRecord(
        stores.document, kind, 'documentId', bookId, fallback
      ).then(function (record) { return { kind: kind, record: record }; });
    }).concat([nativePDFMutationJournalRecord()])).then(function (records) {
      var journalRecord = records.pop();
      if (!journalRecord ||
          journalRecord.payload.ticket !== transaction.journal.ticket) {
        throw new RuntimeError(
          '本机 PDF 改页 journal 在恢复中消失',
          'BW_NATIVE_PDF_MUTATION_JOURNAL'
        );
      }
      var suffix = randomHex(12);
      var mutations = [];
      records.forEach(function (item) {
        var before = transaction.before[item.kind].payload;
        var after = transaction.after[item.kind];
        var current = item.record.payload;
        if (!nativePDFMutationPayloadEqual(current, before) &&
            !nativePDFMutationPayloadEqual(current, after)) {
          throw new RuntimeError(
            '改页恢复发现并发页锚写入：' + item.kind,
            'BW_NATIVE_PDF_MUTATION_CONFLICT'
          );
        }
        var wanted = desired === 'after' ? after : before;
        if (!nativePDFMutationPayloadEqual(current, wanted)) {
          mutations.push(stateRecordMutation(
            item.kind, wanted, suffix + '-' + item.kind, item.record.rev
          ));
        }
      });
      var nextJournal = clone(journalRecord.payload);
      nextJournal.phase = phase;
      mutations.push(stateRecordMutation(
        NATIVE_PDF_MUTATION_JOURNAL_KIND,
        nextJournal,
        suffix + '-journal',
        journalRecord.rev
      ));
      return stores.document.batch(mutations).then(function () {
        transaction.journal = nextJournal;
        transaction.journalRev = journalRecord.rev + 1;
        return transaction;
      });
    });
  }

  function reconcileNativePDFMutationDevice(transaction, desired) {
    return storedStateRecord(
      stores.device, 'reader-positions', 'deviceId', deviceId, {}
    ).then(function (current) {
      var before = transaction.deviceBefore.payload;
      var after = transaction.deviceAfter;
      if (!nativePDFMutationPayloadEqual(current.payload, before) &&
          !nativePDFMutationPayloadEqual(current.payload, after)) {
        throw new RuntimeError(
          '改页恢复发现并发设备位置写入',
          'BW_NATIVE_PDF_MUTATION_CONFLICT'
        );
      }
      var wanted = desired === 'after' ? after : before;
      if (nativePDFMutationPayloadEqual(current.payload, wanted)) {
        return transaction;
      }
      return stores.device.batch([
        deviceStateMutation(
          'reader-positions', wanted, randomHex(12), current.rev
        )
      ]).then(function () { return transaction; });
    });
  }

  function reconcileNativePDFMutationSnapshot(transaction, desired) {
    var documentPhase = desired === 'after'
      ? 'document-applied' : 'prepared';
    return reconcileNativePDFMutationDocuments(
      transaction, desired, documentPhase
    ).then(function () {
      return reconcileNativePDFMutationDevice(transaction, desired);
    }).then(function () {
      transaction.applied = desired === 'after';
      return setNativePDFMutationJournalPhase(
        transaction,
        desired === 'after' ? 'anchors-applied' : 'prepared'
      );
    });
  }

  function applyNativePDFMutationSnapshot(transaction) {
    return reconcileNativePDFMutationSnapshot(transaction, 'after');
  }

  function rollbackNativePDFMutationSnapshot(transaction) {
    if (!transaction || !transaction.journal) return Promise.resolve();
    return reconcileNativePDFMutationSnapshot(transaction, 'before');
  }

  function nativePDFMutationErrorText(error) {
    return String(error && error.message || error || '本机 PDF 改页失败')
      .slice(0, 2000);
  }

  function updateNativePDFMutationJob(jobId, patch) {
    var job = nativePDFMutationJobs[jobId];
    if (!job) return;
    Object.keys(patch || {}).forEach(function (key) { job[key] = patch[key]; });
    job.ts = nowSeconds();
  }

  function pruneNativePDFMutationJobs() {
    var ids = Object.keys(nativePDFMutationJobs).sort(function (a, b) {
      return Number(nativePDFMutationJobs[b].ts || 0) -
        Number(nativePDFMutationJobs[a].ts || 0);
    });
    ids.forEach(function (id, index) {
      var job = nativePDFMutationJobs[id];
      if (index >= 32 || (job.status !== 'running' &&
          nowSeconds() - Number(job.ts || 0) > 3600)) {
        delete nativePDFMutationJobs[id];
      }
    });
  }

  function recoverNativePDFMutationTransaction(transaction, ticket) {
    var journal = transaction && transaction.journal;
    return nativePDFMutationRequest('recover', {
      ticket: journal ? (ticket || journal.ticket) : null,
      oldContentSHA256: journal ? journal.oldContentSHA256 : null,
      stagedContentSHA256: journal ? journal.stagedContentSHA256 : null
    }).then(function (recovered) {
      if (!transaction) return recovered;
      var desired = recovered.outcome === 'committed' ? 'after' : 'before';
      return reconcileNativePDFMutationSnapshot(
        transaction, desired
      ).then(function () {
        return removeNativePDFMutationJournal(transaction);
      }).then(function () { return recovered; });
    });
  }

  function recoverNativePDFMutationOnBoot() {
    if (nativeInterfaceSurface !== 'pdf') return Promise.resolve();
    return nativePDFMutationJournalRecord().then(function (record) {
      if (!nativePDFMutationHandler()) {
        if (record) {
          throw new RuntimeError(
            '检测到 PDF 改页 journal，但原生恢复桥不可用',
            'BW_NATIVE_PDF_MUTATION_BRIDGE'
          );
        }
        return;
      }
      if (!record) {
        return nativePDFMutationRequest('recover', {
          ticket: null,
          oldContentSHA256: null,
          stagedContentSHA256: null
        }).then(function (recovered) {
          if (recovered.outcome === 'committed') {
            throw new RuntimeError(
              '原生 PDF 已提交但网页 journal 缺失，不能证明页锚一致',
              'BW_NATIVE_PDF_MUTATION_JOURNAL'
            );
          }
        });
      }
      var transaction = nativePDFMutationTransactionFromJournal(record);
      return recoverNativePDFMutationTransaction(
        transaction, record.payload.ticket
      );
    });
  }

  function runNativePDFMutationJob(jobId, plan) {
    var ticket = null;
    var transaction = null;
    var warnings = [];
    var writerBarrier = null;
    Promise.resolve().then(function () {
      updateNativePDFMutationJob(jobId, { step: '等待本书写入落盘' });
      return beginNativePDFWriterBarrier();
    }).then(function (barrier) {
      writerBarrier = barrier;
      updateNativePDFMutationJob(jobId, { step: '生成并验证本机 PDF staging' });
      return nativePDFMutationRequest('prepare', {
        operation: plan.operation,
        after: plan.operation === 'insert' ? plan.after : null,
        page: plan.operation === 'insert' ? null : plan.pivotPage,
        title: plan.title,
        markdown: plan.markdown
      });
    }).then(function (prepared) {
      ticket = prepared.ticket;
      warnings = prepared.warnings.slice();
      if (prepared.operation !== plan.operation ||
          prepared.pivotPage !== plan.pivotPage ||
          prepared.newPageCount !== prepared.oldPageCount +
            (plan.operation === 'insert' ? 1 : 0) -
            (plan.operation === 'delete' ? 1 : 0)) {
        throw new RuntimeError(
          '原生 PDF staging 与页锚计划不一致',
          'BW_NATIVE_PDF_MUTATION_PLAN'
        );
      }
      updateNativePDFMutationJob(jobId, { step: '原子迁移本机页锚' });
      return nativePDFMutationSnapshot(plan).then(function (snapshot) {
        return persistNativePDFMutationJournal(snapshot, prepared);
      }).then(applyNativePDFMutationSnapshot);
    }).then(function (applied) {
      transaction = applied;
      updateNativePDFMutationJob(jobId, { step: '原子替换并重新打开 PDF' });
      return nativePDFMutationRequest('commit', { ticket: ticket });
    }).then(function (committed) {
      return setNativePDFMutationJournalPhase(
        transaction, 'pdf-replaced'
      ).then(function () { return committed; });
    }).then(function (committed) {
      updateNativePDFMutationJob(jobId, { step: '确认 PDF 与页锚事务' });
      return nativePDFMutationRequest('finalize', { ticket: ticket }).then(function () {
        return committed;
      });
    }).then(function (committed) {
      return setNativePDFMutationJournalPhase(
        transaction, 'committed'
      ).then(function () {
        return recoverNativePDFMutationTransaction(transaction, ticket);
      }).then(function (recovered) {
        if (recovered.outcome !== 'committed') {
          throw new RuntimeError(
            '原生 PDF 最终确认没有得到 committed',
            'BW_NATIVE_PDF_MUTATION_COMMIT'
          );
        }
        return committed;
      });
    }).then(function (committed) {
      var result = {
        ok: true,
        mode: plan.operation,
        warnings: warnings.concat([
          '本机 OCR、分词与公式页已随 PDF 页号迁移；新插入页或改写页可按需重新预处理',
          'Pi 页锚数据保留在旧内容摘要下；上传/同步新 PDF 前，联网页锚接口会拒绝旧绑定'
        ]),
        mtime: committed.mtime
      };
      if (plan.operation === 'insert') result.page = plan.pivotPage;
      updateNativePDFMutationJob(jobId, {
        status: 'done', step: '完成', result: result
      });
      embeddedPageText = Object.create(null);
      nativePageTextCache = Object.create(null);
      nativePageTextPending = Object.create(null);
      nativeSearchGeneration += 1;
      nativeSearchCache.clear();
      nativeSearchPending.clear();
    }).catch(function (error) {
      var failures = [nativePDFMutationErrorText(error)];
      var recovery = (ticket || transaction)
        ? recoverNativePDFMutationTransaction(transaction, ticket).catch(function (recoveryError) {
          failures.push('持久事务恢复失败：' + nativePDFMutationErrorText(recoveryError));
        }) : Promise.resolve();
      return recovery.then(function () {
        updateNativePDFMutationJob(jobId, {
          status: 'error', step: '失败', error: failures.join('；')
        });
      });
    }).then(function () {
      endNativePDFWriterBarrier(writerBarrier);
      if (activeNativePDFMutationJob === jobId) {
        activeNativePDFMutationJob = null;
      }
      pruneNativePDFMutationJobs();
    });
  }

  function beginNativePDFMutationJob(plan) {
    if (activeNativePDFMutationJob) {
      throw outgoingRequestError(
        '本书已有改页任务进行中', 'BW_NATIVE_PDF_MUTATION_BUSY', 409
      );
    }
    var jobId = NATIVE_PDF_MUTATION_JOB_PREFIX + randomHex(12);
    nativePDFMutationJobs[jobId] = {
      status: 'running', kind: 'pdf-inspage-native',
      step: '排队中', ts: nowSeconds()
    };
    activeNativePDFMutationJob = jobId;
    runNativePDFMutationJob(jobId, plan);
    return { ok: true, job_id: jobId };
  }

  function localPDFInsertPage(input, init, url, method) {
    var code = 'BW_NATIVE_PDF_MUTATION_REQUEST';
    return localJSONRoute(function () {
      if (nativeInterfaceSurface !== 'pdf') {
        throw outgoingRequestError('只有 PDF 支持真实改页', code, 404);
      }
      if (method === 'DELETE') {
        localFileQuery(url, ['file', 'id'], ['file', 'id'], code);
        var deleteID = localRecordId(url.searchParams.get('id'), code);
        return readState('user-pages', []).then(function (items) {
          items = storedList(items, code);
          var record = items.find(function (item) {
            return item && item.id === deleteID;
          });
          if (!record) throw outgoingRequestError('未找到该用户页记录', code, 404);
          if (!Number.isInteger(record.page)) {
            throw outgoingRequestError(
              '这是虚拟页，请用用户页接口删除', code, 400
            );
          }
          return beginNativePDFMutationJob({
            operation: 'delete', id: deleteID, pivotPage: record.page,
            after: null, title: '', markdown: '',
            titleProvided: false, markdownProvided: false
          });
        });
      }
      strictQuery(url, [], [], code);
      var allowed = method === 'POST'
        ? ['file', 'after', 'title', 'md']
        : ['file', 'id', 'title', 'md'];
      var required = method === 'POST' ? ['file', 'after'] : ['file', 'id'];
      return requestObject(input, init, allowed, required, code, 512 * 1024)
        .then(function (body) {
          requireLocalFile(body.file, code);
          var title = boundedLocalString(
            body.title, 120, '', code, '用户页标题', false
          );
          var markdown = boundedLocalString(
            body.md, 100000, '', code, '用户页正文', false
          );
          if (method === 'POST') {
            var after = strictInteger(
              body.after, 0, 10000000, 'after', code
            );
            return beginNativePDFMutationJob({
              operation: 'insert', id: 'u_' + randomHex(4),
              after: after, pivotPage: after + 1,
              title: title, markdown: markdown,
              titleProvided: Object.prototype.hasOwnProperty.call(body, 'title'),
              markdownProvided: Object.prototype.hasOwnProperty.call(body, 'md')
            });
          }
          var editID = localRecordId(body.id, code);
          return readState('user-pages', []).then(function (items) {
            items = storedList(items, code);
            var record = items.find(function (item) {
              return item && item.id === editID;
            });
            if (!record) throw outgoingRequestError('未找到该用户页记录', code, 404);
            if (!Number.isInteger(record.page)) {
              throw outgoingRequestError(
                '这是虚拟页，请用用户页接口编辑', code, 400
              );
            }
            var titleProvided = Object.prototype.hasOwnProperty.call(body, 'title');
            var markdownProvided = Object.prototype.hasOwnProperty.call(body, 'md');
            return beginNativePDFMutationJob({
              operation: 'edit', id: editID, after: null,
              pivotPage: record.page,
              title: titleProvided ? title : String(record.title || '').slice(0, 120),
              markdown: markdownProvided
                ? markdown : String(record.md || '').slice(0, 100000),
              titleProvided: titleProvided,
              markdownProvided: markdownProvided
            });
          });
        });
    }, code);
  }

  function localNativePDFJobStatus(url) {
    var code = 'BW_NATIVE_PDF_JOB_STATUS';
    return localJSONRoute(function () {
      strictQuery(url, ['id'], ['id'], code);
      var jobId = String(url.searchParams.get('id') || '');
      if (!/^npj_[a-f0-9]{24}$/.test(jobId)) {
        throw outgoingRequestError('本机 PDF 任务 id 无效', code, 400);
      }
      pruneNativePDFMutationJobs();
      return clone(nativePDFMutationJobs[jobId] || { status: 'unknown' });
    }, code);
  }

  function assertNoNativePDFMutationJournal() {
    if (activeNativePDFMutationJob) {
      return Promise.reject(outgoingRequestError(
        'PDF 正在改页；为避免把新数据写到旧页号，完成后请重试',
        'BW_NATIVE_PDF_MUTATION_BUSY', 409
      ));
    }
    return nativePDFMutationJournalRecord().then(function (record) {
      if (record) {
        throw outgoingRequestError(
          '检测到未恢复的 PDF 改页 journal；恢复完成前拒绝写入',
          'BW_NATIVE_PDF_MUTATION_BUSY', 409
        );
      }
    });
  }

  function localPageOverlay(url) {
    var code = 'BW_LOCAL_PAGE_OVERLAY';
    return localJSONRoute(function () {
      localFileQuery(url, ['file', 'page'], ['file', 'page'], code);
      var page = strictInteger(
        url.searchParams.get('page'), 1, 10000000, 'page', code
      );
      return pageTextForPage(page).then(function (result) {
        return {
          ok: true,
          state: result.state,
          source: result.source,
          cv: result.revision || '',
          formula_regions: result.formulaRegions || [],
          native_formula_state: String(result.state || 'unknown'),
          native_formula_source: String(result.source || 'none'),
          vocab_marks: [],
          vocab_sentences: [],
          mastered_furi: [],
          offset: { dx: 0, dy: 0, scale: 1 }
        };
      });
    }, code);
  }

  function handleLocalState(input, init, url, method, mutationGatePassed) {
    var path = url.pathname;
    if (!mutationGatePassed && nativeInterfaceSurface === 'pdf' &&
        method !== 'GET' && PDF_MUTATION_WRITE_PATHS.has(path)) {
      return assertNoNativePDFMutationJournal().then(function () {
        return handleLocalState(input, init, url, method, true);
      }).catch(function (error) {
        return outgoingFailureResponse(
          error, 'BW_NATIVE_PDF_MUTATION_BUSY', 409
        );
      });
    }
    if (path === '/pdf/api/pdf-insert-page' &&
        ['POST', 'PATCH', 'DELETE'].indexOf(method) >= 0) {
      return localPDFInsertPage(input, init, url, method);
    }
    if (path === '/pdf/api/context-sync') {
      if (method === 'GET') {
        try {
          var currentContextSync = readLocalContextSync();
          return Promise.resolve(jsonResponse({
            ok: true,
            enabled: currentContextSync.enabled,
            deliveryMode: currentContextSync.deliveryMode,
            ts: currentContextSync.ts
          }));
        } catch (error) {
          return Promise.resolve(jsonResponse({
            ok: false,
            code: error.code || 'BW_LOCAL_CONTEXT_SYNC_PREFERENCE',
            error: error.message
          }, 500));
        }
      }
      if (method !== 'POST') {
        return Promise.resolve(jsonResponse({
          ok: false,
          code: 'BW_LOCAL_CONTEXT_SYNC_METHOD',
          error: '本机上下文同步只接受 GET/POST'
        }, 405));
      }
      return bodyJSON(input, init).then(function (body) {
        if (!exactKeys(body, ['enabled']) &&
            !exactKeys(body, ['enabled', 'deliveryMode'])) {
          throw new RuntimeError(
            '本机上下文同步请求字段无效', 'BW_LOCAL_CONTEXT_SYNC_BODY'
          );
        }
        if (typeof body.enabled !== 'boolean') {
          throw new RuntimeError(
            '本机上下文同步开关无效', 'BW_LOCAL_CONTEXT_SYNC_BODY'
          );
        }
        var previous = readLocalContextSync();
        var deliveryMode = Object.prototype.hasOwnProperty.call(
          body, 'deliveryMode'
        ) ? body.deliveryMode : previous.deliveryMode;
        if (!LOCAL_CONTEXT_SYNC_MODES.has(deliveryMode)) {
          throw new RuntimeError(
            '本机上下文交付模式无效', 'BW_LOCAL_CONTEXT_SYNC_MODE'
          );
        }
        var saved = writeLocalContextSync({
          contract: LOCAL_CONTEXT_SYNC_CONTRACT,
          enabled: body.enabled,
          deliveryMode: deliveryMode,
          ts: nowSeconds()
        });
        return jsonResponse({
          ok: true,
          enabled: saved.enabled,
          deliveryMode: saved.deliveryMode
        });
      }).catch(function (error) {
        return jsonResponse({
          ok: false,
          code: error.code || 'BW_LOCAL_CONTEXT_SYNC_PREFERENCE',
          error: error.message
        }, 400);
      });
    }
    if (path === '/pdf/api/active-reading') {
      if (method === 'GET') {
        return outgoingJSONRoute(function () {
          strictQuery(url, [], [], 'BW_LOCAL_ACTIVE_READING_QUERY');
          return getActiveReading();
        }, 'BW_LOCAL_ACTIVE_READING_FAILED');
      }
      if (method === 'POST') {
        return outgoingJSONRoute(function () {
          strictQuery(url, [], [], 'BW_LOCAL_ACTIVE_READING_QUERY');
          return postActiveReading(input, init);
        }, 'BW_LOCAL_ACTIVE_READING_FAILED');
      }
      return methodNotAllowed(
        '本机 active-reading 只接受 GET/POST',
        'BW_LOCAL_ACTIVE_READING_METHOD'
      );
    }
    if (path === '/pdf/api/outgoing/journal') {
      if (method !== 'GET') {
        return methodNotAllowed(
          '本机 outgoing journal 只接受 GET',
          'BW_LOCAL_OUTGOING_JOURNAL_METHOD'
        );
      }
      return outgoingJSONRoute(function () {
        return outgoingJournal(url);
      }, 'BW_LOCAL_OUTGOING_JOURNAL_FAILED');
    }
    if (path === '/pdf/api/outgoing/drawing') {
      if (method !== 'GET') {
        return methodNotAllowed(
          '本机 outgoing drawing 只接受 GET',
          'BW_LOCAL_OUTGOING_DRAWING_METHOD'
        );
      }
      return outgoingJSONRoute(function () {
        return outgoingDrawing(url).then(function (value) {
          return Object.assign({ ok: true }, value);
        });
      }, 'BW_LOCAL_OUTGOING_DRAWING_FAILED');
    }
    if (path === '/pdf/api/outgoing/focus') {
      if (method !== 'POST') {
        return methodNotAllowed(
          '本机 outgoing focus 只接受 POST',
          'BW_LOCAL_OUTGOING_FOCUS_METHOD'
        );
      }
      return outgoingJSONRoute(function () {
        strictQuery(url, [], [], 'BW_LOCAL_OUTGOING_FOCUS_QUERY');
        return postOutgoingFocus(input, init);
      }, 'BW_LOCAL_OUTGOING_FOCUS_FAILED');
    }
    if (path === '/pdf/api/outgoing/state') {
      if (method !== 'GET') {
        return methodNotAllowed(
          '本机 outgoing state 只接受 GET',
          'BW_LOCAL_OUTGOING_STATE_METHOD'
        );
      }
      return outgoingJSONRoute(function () {
        return outgoingState(url);
      }, 'BW_LOCAL_OUTGOING_STATE_FAILED');
    }
    if (path === '/pdf/api/ocr-selection' && method === 'POST') {
      return localOCRSelection(input, init, url);
    }
    if (path === '/pdf/api/reocr-page' && method === 'POST') {
      return localReOCRPage(input, init, url, false);
    }
    if (path === '/pdf/api/reocr-page/clear' && method === 'POST') {
      return localReOCRPage(input, init, url, true);
    }
    if (path === '/pdf/api/page-overlay' && method === 'GET') {
      return localPageOverlay(url);
    }
    if (path === '/pdf/api/page-chars' && method === 'GET') {
      return pageTextForPage(url.searchParams.get('page')).then(pageTextHTTP).catch(function (error) {
        return jsonResponse({ ok: false, state: 'failed', code: error.code || 'BW_PAGE_TEXT_FAILED', error: error.message }, 422);
      });
    }
    if (path === '/pdf/api/page-text-status' && method === 'GET') {
      return pageTextStatus().then(function (status) { return jsonResponse(status); }).catch(function (error) {
        return jsonResponse({ ok: false, state: 'failed', code: error.code || 'BW_PAGE_TEXT_FAILED', error: error.message }, 422);
      });
    }
    if (path === '/pdf/api/search' && method === 'GET') {
      var searchCode = 'BW_PAGE_TEXT_SEARCH_FAILED';
      return localJSONRoute(function () {
        localFileQuery(url, ['file', 'q', 'limit'], ['file', 'q'], searchCode);
        var query = String(url.searchParams.get('q') || '').trim();
        if (!query || query.length > 256) {
          throw outgoingRequestError('搜索文字无效', searchCode, 400);
        }
        var limit = url.searchParams.has('limit')
          ? strictInteger(url.searchParams.get('limit'), 1, 200, 'limit', searchCode)
          : 200;
        return searchPageText(query, limit);
      }, searchCode);
    }
    if (path === '/pdf/api/reading-pos') {
      return localReadingPosition(input, init, url, method);
    }
    if (path === '/pdf/api/highlights') {
      return localPDFHighlights(input, init, url, method);
    }
    if (path === '/pdf/api/epub-highlights') {
      return localEPUBHighlights(input, init, url, method);
    }
    if (path === '/pdf/api/userpages') {
      return localUserPages(input, init, url, method);
    }
    if (path === '/pdf/api/notes') {
      // A card or generic result pinned on the page is a note placement. The
      // complete card/html payload therefore survives offline inside the same
      // App-owned document record as an ordinary sticky note.
      return localNotes(input, init, url, method);
    }
    if (path === '/pdf/api/note-composite') {
      return localNoteComposite(input, init, url);
    }
    if (path === '/pdf/api/ink' || path === '/pdf/api/epub-ink') {
      return localInk(input, init, url, method, path.indexOf('epub-') >= 0);
    }
    if (path === '/pdf/api/prefs') {
      return localPreferences(input, init, url, method);
    }
    if (path === '/pdf/api/video-player-prefs') {
      return localVideoPlayerPreferences(input, init, url, method);
    }
    if (path === '/pdf/api/book-langs') {
      return localBookLanguages(input, init, url, method);
    }
    if (path === '/pdf/api/prewarm-async' && method === 'POST') {
      var prewarmCode = 'BW_LOCAL_PREWARM';
      return localJSONRoute(function () {
        strictQuery(url, [], [], prewarmCode);
        return requestObject(
          input, init, ['file', 'width'], ['file', 'width'], prewarmCode, 64 * 1024
        ).then(function (body) {
          requireLocalFile(body.file, prewarmCode);
          if (typeof body.width !== 'number') {
            throw outgoingRequestError('width 必须是整数', prewarmCode, 400);
          }
          strictInteger(body.width, 400, 3000, 'width', prewarmCode);
          return {
            ok: true, started: false, running: false,
            not_applicable: true, reason: 'native-pdfjs'
          };
        });
      }, prewarmCode);
    }
    if (path === '/pdf/api/prewarm-status' && method === 'GET') {
      var prewarmStatusCode = 'BW_LOCAL_PREWARM_STATUS';
      return localJSONRoute(function () {
        localFileQuery(
          url, ['file', 'width'], ['file', 'width'], prewarmStatusCode
        );
        strictInteger(
          url.searchParams.get('width'), 400, 3000, 'width', prewarmStatusCode
        );
        var total = Number.isInteger(embeddedPageCount) && embeddedPageCount > 0
          ? embeddedPageCount : 0;
        return {
          ok: true, total: total, done: total, percent: 100, running: false,
          not_applicable: true, reason: 'native-pdfjs'
        };
      }, prewarmStatusCode);
    }
    if (path === '/pdf/api/ui-version') return Promise.resolve(jsonResponse({ ok: true, v: 'native-local' }));
    return null;
  }

  function canonicalZipPath(value, base) {
    value = String(value || '').replace(/\\/g, '/');
    if (!value || value[0] === '/' || /^[A-Za-z][A-Za-z0-9+.-]*:/.test(value) || value.indexOf('\0') >= 0) return null;
    var stack = base ? String(base).split('/').slice(0, -1) : [];
    value.split('/').forEach(function (part) {
      if (!part || part === '.') return;
      if (part === '..') { if (!stack.length) stack.push('..'); else stack.pop(); }
      else stack.push(part);
    });
    if (stack.indexOf('..') >= 0 || !stack.length) return null;
    return stack.join('/');
  }
  function xmlDocument(text, type) {
    var doc = new DOMParser().parseFromString(text, type || 'application/xml');
    if (doc.querySelector('parsererror')) throw new RuntimeError('EPUB XML 无效', 'BW_LOCAL_EPUB_XML');
    return doc;
  }
  function boundedInflate(entry, maximumBytes, code, label) {
    return new Promise(function (resolve, reject) {
      var chunks = [];
      var length = 0;
      var settled = false;
      var stream;
      function fail(error) {
        if (settled) return;
        settled = true;
        chunks = [];
        try { if (stream && typeof stream.pause === 'function') stream.pause(); } catch (_) {}
        reject(error instanceof Error ? error : new RuntimeError(
          String(label || 'EPUB 项') + ' 解压失败', code
        ));
      }
      try {
        stream = entry.internalStream('uint8array');
        stream.on('data', function (chunk) {
          if (settled) return;
          var bytes = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
          if (length + bytes.byteLength > maximumBytes) {
            fail(new RuntimeError(
              String(label || 'EPUB 项') + ' 实际解压大小超过安全上限', code
            ));
            return;
          }
          chunks.push(bytes);
          length += bytes.byteLength;
        });
        stream.on('error', fail);
        stream.on('end', function () {
          if (settled) return;
          settled = true;
          var output = new Uint8Array(length);
          var offset = 0;
          chunks.forEach(function (chunk) {
            output.set(chunk, offset);
            offset += chunk.byteLength;
          });
          chunks = [];
          resolve(output);
        });
        stream.resume();
      } catch (error) {
        fail(error);
      }
    });
  }
  function zipText(zip, path) {
    if (epubTextByPath[path]) return epubTextByPath[path];
    var entry = zip.file(path);
    if (!entry) return Promise.reject(new RuntimeError('EPUB 缺少 ' + path, 'BW_LOCAL_EPUB_ENTRY'));
    var size = Number(entry._data && entry._data.uncompressedSize);
    if (!Number.isSafeInteger(size) || size < 0 || size > maximumEPUBTextBytes) {
      return Promise.reject(new RuntimeError(
        'EPUB 文本项超过 8 MiB 或缺少可信大小',
        'BW_LOCAL_EPUB_TEXT_LIMIT',
        { path: path }
      ));
    }
    var task = epubTextQueue.then(function () {
      return boundedInflate(
        entry, maximumEPUBTextBytes, 'BW_LOCAL_EPUB_TEXT_LIMIT', 'EPUB 文本项'
      );
    }).then(function (bytes) {
      if (epubActualTextBytes + bytes.byteLength > maximumEPUBTextTotalBytes) {
        throw new RuntimeError(
          'EPUB 文本实际解压总量超过 128 MiB',
          'BW_LOCAL_EPUB_TEXT_LIMIT'
        );
      }
      epubActualTextBytes += bytes.byteLength;
      return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
    });
    epubTextQueue = task.then(function () {}, function () {});
    epubTextByPath[path] = task;
    return task;
  }
  function assertEPUBCentralDirectoryEnvelope(bytes) {
    var view = new DataView(bytes);
    var floor = Math.max(0, view.byteLength - 65557);
    var eocd = -1;
    for (var offset = view.byteLength - 22; offset >= floor; offset -= 1) {
      if (view.getUint32(offset, true) === 0x06054b50 &&
          offset + 22 + view.getUint16(offset + 20, true) === view.byteLength) {
        eocd = offset;
        break;
      }
    }
    if (eocd < 0 || eocd + 22 > view.byteLength) {
      throw new RuntimeError('EPUB 中央目录缺失', 'BW_LOCAL_EPUB_ZIP');
    }
    if (view.getUint16(eocd + 4, true) !== 0 ||
        view.getUint16(eocd + 6, true) !== 0) {
      throw new RuntimeError('EPUB 不支持分卷 ZIP', 'BW_LOCAL_EPUB_ZIP');
    }
    var entriesOnDisk = view.getUint16(eocd + 8, true);
    var entries = view.getUint16(eocd + 10, true);
    var directorySize = view.getUint32(eocd + 12, true);
    var directoryOffset = view.getUint32(eocd + 16, true);
    if (entries === 0xffff || entriesOnDisk === 0xffff ||
        directorySize === 0xffffffff || directoryOffset === 0xffffffff) {
      throw new RuntimeError('EPUB 暂不接受 ZIP64', 'BW_LOCAL_EPUB_ZIP');
    }
    if (entries !== entriesOnDisk || entries > 10000 ||
        directorySize > 64 * 1024 * 1024 ||
        directoryOffset + directorySize !== eocd) {
      throw new RuntimeError('EPUB 声明的文件项过多', 'BW_LOCAL_EPUB_LIMIT');
    }
    var cursor = directoryOffset;
    var directoryEnd = directoryOffset + directorySize;
    var actualEntries = 0;
    while (cursor < directoryEnd) {
      if (cursor + 46 > directoryEnd ||
          view.getUint32(cursor, true) !== 0x02014b50) {
        throw new RuntimeError('EPUB 中央目录结构无效', 'BW_LOCAL_EPUB_ZIP');
      }
      var nameLength = view.getUint16(cursor + 28, true);
      var extraLength = view.getUint16(cursor + 30, true);
      var commentLength = view.getUint16(cursor + 32, true);
      cursor += 46 + nameLength + extraLength + commentLength;
      actualEntries += 1;
      if (cursor > directoryEnd || actualEntries > 10000) {
        throw new RuntimeError('EPUB 中央目录超过安全上限', 'BW_LOCAL_EPUB_LIMIT');
      }
    }
    if (cursor !== directoryEnd || actualEntries !== entries) {
      throw new RuntimeError('EPUB 中央目录计数不一致', 'BW_LOCAL_EPUB_ZIP');
    }
  }
  function xmlElementsByLocalName(rootNode, localName) {
    if (!rootNode) return [];
    if (typeof rootNode.getElementsByTagNameNS === 'function') {
      return Array.prototype.slice.call(
        rootNode.getElementsByTagNameNS('*', localName) || []
      );
    }
    if (typeof rootNode.getElementsByTagName === 'function') {
      return Array.prototype.slice.call(
        rootNode.getElementsByTagName(localName) || []
      );
    }
    return [];
  }
  function compactEPUBText(node, maximumLength) {
    return String(node && node.textContent || '').replace(/\s+/g, ' ').trim()
      .slice(0, maximumLength || 500);
  }
  function configuredEPUBSHA() {
    var configured = String(
      root.EPUB_CFG && root.EPUB_CFG.sha || ''
    ).trim();
    if (/^[A-Za-z0-9._-]{1,128}$/.test(configured)) return configured;
    return String(bookId || '').replace(/^localbook-/, '').slice(0, 128);
  }
  function epubPathFromHref(raw, basePath) {
    raw = String(raw || '').split('#')[0].split('?')[0];
    if (!raw) return null;
    try { raw = decodeURIComponent(raw); } catch (_) { return null; }
    return canonicalZipPath(raw, basePath);
  }
  function tocIndexByHref(epub, raw, basePath) {
    var path = epubPathFromHref(raw, basePath);
    if (!path) return -1;
    for (var index = 0; index < epub.spine.length; index += 1) {
      if (epub.spine[index].path === path) return index;
    }
    return -1;
  }
  function epub3TOC(epub, documentNode, navPath) {
    var navs = xmlElementsByLocalName(documentNode, 'nav');
    var nav = navs.find(function (node) {
      var type = String(
        node.getAttribute('epub:type') || node.getAttribute('type') ||
        (typeof node.getAttributeNS === 'function'
          ? node.getAttributeNS('http://www.idpf.org/2007/ops', 'type') : '') || ''
      );
      return type.split(/\s+/).indexOf('toc') >= 0;
    }) || navs[0];
    if (!nav) return [];
    var seen = Object.create(null);
    return xmlElementsByLocalName(nav, 'a').reduce(function (toc, anchor) {
      if (toc.length >= 5000) return toc;
      var idx = tocIndexByHref(epub, anchor.getAttribute('href'), navPath);
      var label = compactEPUBText(anchor, 80);
      var key = idx + ':' + label;
      if (idx >= 0 && label && !seen[key]) {
        seen[key] = true;
        toc.push({ label: label, idx: idx });
      }
      return toc;
    }, []);
  }
  function epub2TOC(epub, documentNode, ncxPath) {
    var seen = Object.create(null);
    return xmlElementsByLocalName(documentNode, 'navPoint').reduce(function (toc, point) {
      if (toc.length >= 5000) return toc;
      var content = xmlElementsByLocalName(point, 'content')[0];
      var labelNode = xmlElementsByLocalName(point, 'text')[0];
      var idx = tocIndexByHref(
        epub, content && content.getAttribute('src'), ncxPath
      );
      var label = compactEPUBText(labelNode, 80);
      var key = idx + ':' + label;
      if (idx >= 0 && label && !seen[key]) {
        seen[key] = true;
        toc.push({ label: label, idx: idx });
      }
      return toc;
    }, []);
  }
  function fallbackEPUBTOC(epub) {
    return epub.spine.map(function (item, index) {
      var label = item.path.split('/').pop() || ('章节 ' + (index + 1));
      try { label = decodeURIComponent(label); } catch (_) {}
      return { label: label.slice(0, 80), idx: index };
    });
  }
  function loadEPUBTOC(epub, opf, spineNode) {
    var ids = Object.keys(epub.manifest);
    var navItem = ids.map(function (id) { return epub.manifest[id]; }).find(function (item) {
      return String(item.properties || '').split(/\s+/).indexOf('nav') >= 0 ||
        /(?:^|\/)nav\.x?html?$/i.test(item.path);
    });
    var ncxId = spineNode && String(spineNode.getAttribute('toc') || '');
    var ncxItem = epub.manifest[ncxId] || ids.map(function (id) {
      return epub.manifest[id];
    }).find(function (item) {
      return /(?:^|\/)toc\.ncx$/i.test(item.path) ||
        String(item.mediaType || '').toLowerCase() === 'application/x-dtbncx+xml';
    });
    var navTask = navItem
      ? zipText(epub.zip, navItem.path).then(function (text) {
          return epub3TOC(epub, xmlDocument(text, 'application/xhtml+xml'), navItem.path);
        }).catch(function () { return []; })
      : Promise.resolve([]);
    return navTask.then(function (toc) {
      if (toc.length || !ncxItem) return toc;
      return zipText(epub.zip, ncxItem.path).then(function (text) {
        return epub2TOC(epub, xmlDocument(text), ncxItem.path);
      }).catch(function () { return []; });
    }).then(function (toc) {
      return toc.length ? toc : fallbackEPUBTOC(epub);
    });
  }
  function loadEPUB() {
    if (epubPromise) return epubPromise;
    epubPromise = Promise.resolve().then(function () {
      if (!root.JSZip || typeof root.JSZip.loadAsync !== 'function') {
        throw new RuntimeError('EPUB 解包器未加载', 'BW_LOCAL_EPUB_ZIP');
      }
      return originalFetch(basePath + '/books/' + encodeURIComponent(bookId) + '/content', { cache: 'no-store' });
    }).then(function (response) {
      if (!response.ok) throw new RuntimeError('无法读取本机 EPUB', 'BW_LOCAL_EPUB_FETCH');
      return response.arrayBuffer();
    // JSZip's CRC pass inflates every member. Read only central-directory
    // metadata here, enforce all limits, then inflate an entry on demand.
    }).then(function (bytes) {
      assertEPUBCentralDirectoryEnvelope(bytes);
      return root.JSZip.loadAsync(bytes, { checkCRC32: false });
    })
      .then(function (zip) {
        var names = Object.keys(zip.files);
        if (names.length > 10000) throw new RuntimeError('EPUB 文件项过多', 'BW_LOCAL_EPUB_LIMIT');
        var total = 0;
        var canonical = Object.create(null);
        names.forEach(function (name) {
          var safe = canonicalZipPath(name, '');
          if (!safe || canonical[safe]) throw new RuntimeError('EPUB 路径不安全', 'BW_LOCAL_EPUB_PATH', { path: name });
          canonical[safe] = true;
          var entry = zip.files[name];
          var mode = Number(entry.unixPermissions || 0) & 0xF000;
          if (mode === 0xA000) throw new RuntimeError('EPUB 不允许符号链接', 'BW_LOCAL_EPUB_SYMLINK');
          if (entry.dir) return;
          var size = Number(entry._data && entry._data.uncompressedSize);
          var compressed = Number(entry._data && entry._data.compressedSize);
          if (!Number.isSafeInteger(size) || size < 0 ||
              !Number.isSafeInteger(compressed) || compressed < 0) {
            throw new RuntimeError('EPUB 项缺少可信大小', 'BW_LOCAL_EPUB_LIMIT');
          }
          if (size > 128 * 1024 * 1024) throw new RuntimeError('EPUB 单项过大', 'BW_LOCAL_EPUB_LIMIT');
          if (size > 1024 * 1024 && compressed > 0 && size / compressed > 200) {
            throw new RuntimeError('EPUB 压缩比异常', 'BW_LOCAL_EPUB_BOMB');
          }
          total += size;
        });
        if (total > 1024 * 1024 * 1024) throw new RuntimeError('EPUB 解压后过大', 'BW_LOCAL_EPUB_LIMIT');
        return zipText(zip, 'META-INF/container.xml').then(function (containerText) {
          var container = xmlDocument(containerText);
          var rootfile = container.querySelector('rootfile');
          var opfPath = canonicalZipPath(rootfile && rootfile.getAttribute('full-path'), '');
          if (!opfPath) throw new RuntimeError('EPUB 缺少 OPF', 'BW_LOCAL_EPUB_OPF');
          return zipText(zip, opfPath).then(function (opfText) {
            var opf = xmlDocument(opfText);
            var manifest = Object.create(null);
            Array.prototype.forEach.call(opf.querySelectorAll('manifest > item'), function (node) {
              var id = String(node.getAttribute('id') || '');
              var path = canonicalZipPath(node.getAttribute('href'), opfPath);
              if (id && path && zip.file(path)) manifest[id] = {
                id: id, path: path, mediaType: String(node.getAttribute('media-type') || ''),
                properties: String(node.getAttribute('properties') || '')
              };
            });
            var spine = [];
            var spineNode = opf.querySelector('spine');
            Array.prototype.forEach.call(opf.querySelectorAll('spine > itemref'), function (node) {
              var item = manifest[String(node.getAttribute('idref') || '')];
              if (item) spine.push(item);
            });
            if (!spine.length) throw new RuntimeError('EPUB spine 为空', 'BW_LOCAL_EPUB_SPINE');
            var titleNode = xmlElementsByLocalName(opf, 'title')[0];
            var epub = {
              zip: zip,
              opfPath: opfPath,
              manifest: manifest,
              spine: spine,
              title: compactEPUBText(titleNode, 500) || String(
                root.EPUB_CFG && root.EPUB_CFG.fileName || ''
              ).slice(0, 500),
              sha: configuredEPUBSHA(),
              toc: []
            };
            return loadEPUBTOC(epub, opf, spineNode).then(function (toc) {
              epub.toc = toc;
              return epub;
            });
          });
        });
      });
    return epubPromise;
  }
  function manifestItemForPath(epub, path) {
    var found = null;
    Object.keys(epub.manifest).some(function (id) {
      if (epub.manifest[id].path === path) { found = epub.manifest[id]; return true; }
      return false;
    });
    return found;
  }
  function createEPUBResourceBudget() {
    return {
      count: 0,
      bytes: 0,
      queue: Promise.resolve(),
      byPath: Object.create(null),
      urlByPath: Object.create(null),
      urls: []
    };
  }
  function sharedEPUBResourceBudget() {
    if (!epubSharedResourceBudget) {
      epubSharedResourceBudget = createEPUBResourceBudget();
    }
    return epubSharedResourceBudget;
  }
  function budgetedResource(epub, path, budget) {
    var item = manifestItemForPath(epub, path);
    var entry = item && epub.zip.file(path);
    if (!item || !entry || entry.dir) return null;
    var size = Number(entry._data && entry._data.uncompressedSize);
    if (!Number.isSafeInteger(size) || size < 0 || size > maximumEPUBResourceBytes) {
      return null;
    }
    if (budget.count >= maximumEPUBResourceCount ||
        budget.bytes + size > maximumEPUBResourceTotalBytes) {
      return null;
    }
    budget.count += 1;
    budget.bytes += size;
    return { item: item, entry: entry, size: size };
  }
  function sharedResourceBlob(epub, path) {
    var shared = sharedEPUBResourceBudget();
    if (shared.byPath[path]) return shared.byPath[path];
    var resource = budgetedResource(epub, path, shared);
    if (!resource) return Promise.resolve(null);
    var task = shared.queue.then(function () {
      return boundedInflate(
        resource.entry,
        maximumEPUBResourceBytes,
        'BW_LOCAL_EPUB_RESOURCE_LIMIT',
        'EPUB 资源'
      );
    }).then(function (bytes) {
      var adjustment = bytes.byteLength - resource.size;
      if (shared.bytes + adjustment > maximumEPUBResourceTotalBytes) {
        throw new RuntimeError(
          'EPUB 资源实际解压总量超过 128 MiB',
          'BW_LOCAL_EPUB_RESOURCE_LIMIT'
        );
      }
      shared.bytes += adjustment;
      var type = resource.item.mediaType;
      return {
        blob: new Blob([bytes], type ? { type: type } : undefined),
        size: bytes.byteLength
      };
    });
    shared.queue = task.then(function () {}, function () {});
    shared.byPath[path] = task;
    return task;
  }
  function blobFor(epub, path, budget) {
    if (budget.byPath[path]) return budget.byPath[path];
    var localResource = budgetedResource(epub, path, budget);
    if (!localResource) return Promise.resolve('');
    var shared = sharedEPUBResourceBudget();
    var urlTask = shared.urlByPath[path];
    if (!urlTask) {
      urlTask = sharedResourceBlob(epub, path).then(function (result) {
        if (!result) return null;
        var url = URL.createObjectURL(result.blob);
        blobURLs.push(url);
        shared.urls.push(url);
        return { url: url, size: result.size };
      });
      shared.urlByPath[path] = urlTask;
    }
    var task = urlTask.then(function (result) {
      if (!result) return '';
      var adjustment = result.size - localResource.size;
      if (budget.bytes + adjustment > maximumEPUBResourceTotalBytes) return '';
      budget.bytes += adjustment;
      return result.url;
    });
    budget.byPath[path] = task;
    return task;
  }
  function epubSanitizerOptions() {
    return {
      USE_PROFILES: { html: true },
      FORBID_TAGS: [
        'script', 'style', 'svg', 'math', 'iframe', 'object', 'embed',
        'form', 'base', 'meta', 'link', 'template', 'area', 'map'
      ],
      FORBID_ATTR: [
        'style', 'srcdoc', 'srcset', 'usemap', 'ismap', 'background',
        'ping', 'action', 'formaction', 'xlink:href'
      ],
      ALLOW_DATA_ATTR: false,
      ALLOW_UNKNOWN_PROTOCOLS: false,
      CUSTOM_ELEMENT_HANDLING: {
        tagNameCheck: null,
        attributeNameCheck: null,
        allowCustomizedBuiltInElements: false
      }
    };
  }
  function sanitizeSection(epub, item, html) {
    if (!root.DOMPurify || typeof root.DOMPurify.sanitize !== 'function') {
      throw new RuntimeError(
        'EPUB 安全清洗器未加载',
        'BW_LOCAL_EPUB_SANITIZER_UNAVAILABLE'
      );
    }
    // Do not inherit hooks registered by later Reader components. EPUB bytes
    // are untrusted and must pass the same closed policy every time.
    if (typeof root.DOMPurify.removeAllHooks === 'function') {
      root.DOMPurify.removeAllHooks();
    }
    var sanitized = root.DOMPurify.sanitize(
      String(html || ''), epubSanitizerOptions()
    );
    var doc = new DOMParser().parseFromString(sanitized, 'text/html');
    Array.prototype.forEach.call(doc.querySelectorAll('script,style,svg,math,iframe,object,embed,form,base,meta,link,template,area,map'), function (node) { node.remove(); });
    var tasks = [];
    var budget = createEPUBResourceBudget();
    Array.prototype.forEach.call(doc.querySelectorAll('*'), function (node) {
      Array.prototype.slice.call(node.attributes || []).forEach(function (attribute) {
        var name = attribute.name.toLowerCase();
        if (name.indexOf('on') === 0 || name === 'srcdoc' ||
            name === 'srcset' || name === 'usemap' || name === 'ismap' ||
            name === 'background' || name === 'ping' || name === 'action' ||
            name === 'formaction' || name === 'xlink:href' || name === 'style') {
          node.removeAttribute(attribute.name);
        }
      });
    });
    Array.prototype.forEach.call(doc.querySelectorAll('[src],[poster]'), function (node) {
      var attr = node.hasAttribute('src') ? 'src' : 'poster';
      var raw = String(node.getAttribute(attr) || '');
      var path = canonicalZipPath(raw.split('#')[0].split('?')[0], item.path);
      node.removeAttribute(attr);
      if (!path) return;
      tasks.push(function () {
        return blobFor(epub, path, budget).then(function (url) {
          if (url) node.setAttribute(attr, url);
        });
      });
    });
    Array.prototype.forEach.call(doc.querySelectorAll('[href]'), function (node) {
      var raw = String(node.getAttribute('href') || '');
      // Keep only in-section anchors. Navigation and external protocols are
      // owned by the native shell, never by untrusted book markup.
      if (String(node.tagName || '').toLowerCase() !== 'a' ||
          !/^#[A-Za-z0-9_.:-]{1,240}$/.test(raw)) {
        node.removeAttribute('href');
      }
    });
    return tasks.reduce(function (chain, task) {
      return chain.then(task);
    }, Promise.resolve()).then(function () { return doc.body.innerHTML; });
  }

  function sanitizedEPUBVisibleText(html) {
    if (!root.DOMPurify || typeof root.DOMPurify.sanitize !== 'function') {
      throw new RuntimeError(
        'EPUB 安全清洗器未加载', 'BW_LOCAL_EPUB_SANITIZER_UNAVAILABLE'
      );
    }
    if (typeof root.DOMPurify.removeAllHooks === 'function') {
      root.DOMPurify.removeAllHooks();
    }
    var sanitized = root.DOMPurify.sanitize(
      String(html || ''), epubSanitizerOptions()
    );
    var doc = new DOMParser().parseFromString(sanitized, 'text/html');
    Array.prototype.forEach.call(doc.querySelectorAll(
      'script,style,svg,math,iframe,object,embed,form,base,meta,link,template,area,map,' +
      'noscript,[hidden],[aria-hidden="true"]'
    ), function (node) { node.remove(); });
    var text = String(doc.body && doc.body.textContent || '')
      .replace(/\s+/g, ' ').trim();
    if (text.length > 8 * 1024 * 1024) {
      throw new RuntimeError('EPUB 单章正文超过搜索上限', 'BW_LOCAL_EPUB_SEARCH_LIMIT');
    }
    return text;
  }

  function epubSectionVisibleText(epub, index) {
    if (!epub || !Array.isArray(epub.spine) || index < 0 || index >= epub.spine.length) {
      return Promise.reject(new RuntimeError(
        'EPUB 章节越界', 'BW_LOCAL_EPUB_SECTION_QUERY'
      ));
    }
    if (!epub.visibleTextByIndex) epub.visibleTextByIndex = Object.create(null);
    if (Object.prototype.hasOwnProperty.call(epub.visibleTextByIndex, index)) {
      return Promise.resolve(epub.visibleTextByIndex[index]);
    }
    var item = epub.spine[index];
    return zipText(epub.zip, item.path).then(function (html) {
      var text = sanitizedEPUBVisibleText(html);
      epub.visibleTextByIndex[index] = text;
      return text;
    });
  }

  function appendEPUBSearchMatches(text, query, index, loc, results) {
    text = String(text || '');
    query = String(query || '');
    results = Array.isArray(results) ? results : [];
    var lower = text.toLocaleLowerCase();
    var needle = query.toLocaleLowerCase();
    var start = 0;
    var count = 0;
    while (needle && count < 3 && results.length < 80) {
      var found = lower.indexOf(needle, start);
      if (found < 0) break;
      var left = Math.max(0, found - 40);
      var right = Math.min(text.length, found + query.length + 50);
      results.push({
        idx: index,
        loc: String(loc || '').slice(0, 240),
        excerpt: (left > 0 ? '…' : '') + text.slice(left, found) +
          '\u0001' + text.slice(found, found + query.length) + '\u0002' +
          text.slice(found + query.length, right) +
          (right < text.length ? '…' : '')
      });
      start = found + Math.max(1, query.length);
      count += 1;
    }
    return results;
  }

  function localEPUBSearch(url) {
    var code = 'BW_LOCAL_EPUB_SEARCH';
    return epubJSONRoute(function () {
      localFileQuery(url, ['file', 'q'], ['file', 'q'], code);
      var query = String(url.searchParams.get('q') || '').trim();
      if (query.length > 240 || /[\u0000-\u001f\u007f]/.test(query)) {
        throw outgoingRequestError('EPUB 搜索词无效', code, 400);
      }
      if (!query) return { ok: true, results: [], truncated: false };
      return loadEPUB().then(function (epub) {
        var results = [];
        function scan(index) {
          if (index >= epub.spine.length || results.length >= 80) {
            return Promise.resolve();
          }
          return epubSectionVisibleText(epub, index).then(function (text) {
            var path = String(epub.spine[index].path || '');
            var loc = (path.split('/').pop() || ('section-' + index))
              .replace(/\.[^.]*$/, '').slice(0, 240);
            appendEPUBSearchMatches(text, query, index, loc, results);
          }).then(function () { return scan(index + 1); });
        }
        return scan(0).then(function () {
          return { ok: true, results: results, truncated: results.length >= 80 };
        });
      });
    }, code);
  }
  function splitEPUBSelectorList(selectorText) {
    var selectors = [];
    var start = 0;
    var roundDepth = 0;
    var squareDepth = 0;
    var quote = '';
    var escaped = false;
    for (var index = 0; index < selectorText.length; index += 1) {
      var character = selectorText[index];
      if (escaped) { escaped = false; continue; }
      if (character === '\\') { escaped = true; continue; }
      if (quote) {
        if (character === quote) quote = '';
        continue;
      }
      if (character === '"' || character === "'") { quote = character; continue; }
      if (character === '(') roundDepth += 1;
      else if (character === ')') roundDepth -= 1;
      else if (character === '[') squareDepth += 1;
      else if (character === ']') squareDepth -= 1;
      else if (character === ',' && roundDepth === 0 && squareDepth === 0) {
        selectors.push(selectorText.slice(start, index).trim());
        start = index + 1;
      }
      if (roundDepth < 0 || squareDepth < 0) break;
    }
    selectors.push(selectorText.slice(start).trim());
    if (quote || escaped || roundDepth !== 0 || squareDepth !== 0 ||
        selectors.some(function (selector) { return !selector; })) {
      throw new RuntimeError(
        'EPUB CSS 选择器列表无效', 'BW_LOCAL_EPUB_CSS_UNSAFE'
      );
    }
    return selectors;
  }
  function scopeEPUBSelector(selector) {
    selector = String(selector || '').trim();
    if (!selector || selector.length > 4096 || /[\u0000-\u0008\u000B\u000C\u000E-\u001F]/.test(selector)) {
      throw new RuntimeError('EPUB CSS 选择器无效', 'BW_LOCAL_EPUB_CSS_UNSAFE');
    }
    // The section endpoint returns body.innerHTML, so document-root selectors
    // must target the trusted chapter container rather than a discarded body.
    selector = selector.replace(/^(?::root|html|body)(?=$|[\s>+~.#:\[])/i, '').trim();
    if (!selector || selector === '*') return '#ep-col';
    return '#ep-col ' + selector;
  }
  function rewriteEPUBCSSURLs(value, cssItem, epub) {
    var output = '';
    var cursor = 0;
    var lower = value.toLowerCase();
    while (cursor < value.length) {
      var found = lower.indexOf('url', cursor);
      if (found < 0) { output += value.slice(cursor); break; }
      var before = found > 0 ? value[found - 1] : '';
      if (before && /[A-Za-z0-9_-]/.test(before)) {
        output += value.slice(cursor, found + 3);
        cursor = found + 3;
        continue;
      }
      var open = found + 3;
      while (/\s/.test(value[open] || '')) open += 1;
      if (value[open] !== '(') {
        output += value.slice(cursor, found + 3);
        cursor = found + 3;
        continue;
      }
      var end = open + 1;
      var quote = '';
      var escaped = false;
      for (; end < value.length; end += 1) {
        var character = value[end];
        if (escaped) { escaped = false; continue; }
        if (character === '\\') { escaped = true; continue; }
        if (quote) {
          if (character === quote) quote = '';
          continue;
        }
        if (character === '"' || character === "'") { quote = character; continue; }
        if (character === ')') break;
      }
      if (end >= value.length || quote || escaped) {
        throw new RuntimeError('EPUB CSS URL 无效', 'BW_LOCAL_EPUB_CSS_UNSAFE');
      }
      var raw = value.slice(open + 1, end).trim();
      if ((raw[0] === '"' && raw[raw.length - 1] === '"') ||
          (raw[0] === "'" && raw[raw.length - 1] === "'")) {
        raw = raw.slice(1, -1).trim();
      }
      if (!raw || raw.indexOf('\\') >= 0 || /[\u0000-\u001F]/.test(raw)) {
        throw new RuntimeError('EPUB CSS URL 无效', 'BW_LOCAL_EPUB_CSS_UNSAFE');
      }
      var path = epubPathFromHref(raw, cssItem.path);
      var resource = path && manifestItemForPath(epub, path);
      if (!path || !resource || !epub.zip.file(path)) {
        throw new RuntimeError(
          'EPUB CSS 只能引用书内 manifest 资源', 'BW_LOCAL_EPUB_CSS_UNSAFE',
          { path: raw }
        );
      }
      output += value.slice(cursor, found) +
        'url("/pdf/api/epub-resource?path=' + encodeURIComponent(path) + '")';
      cursor = end + 1;
    }
    return output;
  }
  function safeEPUBDeclarations(style, cssItem, epub, fontFace) {
    var blocked = new Set([
      'all', 'animation', 'animation-name', 'backdrop-filter', 'behavior',
      'clip-path', 'filter', 'mask', 'mask-image', '-webkit-mask',
      '-webkit-mask-image', '-moz-binding', 'pointer-events', 'position',
      'transition', 'transition-property', 'z-index'
    ]);
    var fontAllowed = new Set([
      'font-family', 'font-style', 'font-weight', 'font-stretch',
      'font-display', 'font-feature-settings', 'font-variation-settings',
      'unicode-range', 'src'
    ]);
    var declarations = [];
    for (var index = 0; index < style.length; index += 1) {
      var name = String(style[index] || '').toLowerCase();
      if (!name || name.indexOf('--') === 0 || blocked.has(name) ||
          (fontFace && !fontAllowed.has(name))) continue;
      var value = String(style.getPropertyValue(name) || '').trim();
      if (!value || /(?:javascript\s*:|expression\s*\(|-moz-binding|behavior\s*:)/i.test(value)) {
        throw new RuntimeError(
          'EPUB CSS 声明不安全', 'BW_LOCAL_EPUB_CSS_UNSAFE'
        );
      }
      value = rewriteEPUBCSSURLs(value, cssItem, epub);
      if (fontFace && name === 'src' && /\blocal\s*\(/i.test(value)) {
        throw new RuntimeError(
          'EPUB 字体只能引用书内资源', 'BW_LOCAL_EPUB_CSS_UNSAFE'
        );
      }
      declarations.push(
        name + ':' + value + (style.getPropertyPriority(name) === 'important'
          ? ' !important' : '') + ';'
      );
    }
    return declarations.join('');
  }
  function scopeEPUBCSSRules(rules, cssItem, epub) {
    var output = [];
    Array.prototype.forEach.call(rules || [], function (rule) {
      if (rule.type === 1) {
        var selectors = splitEPUBSelectorList(String(rule.selectorText || ''))
          .map(scopeEPUBSelector);
        var declarations = safeEPUBDeclarations(rule.style, cssItem, epub, false);
        if (selectors.length && declarations) {
          output.push(selectors.join(',') + '{' + declarations + '}');
        }
        return;
      }
      if (rule.type === 4 || rule.type === 12) {
        var nested = scopeEPUBCSSRules(rule.cssRules, cssItem, epub);
        if (nested) {
          output.push((rule.type === 4 ? '@media ' : '@supports ') +
            String(rule.conditionText || '') + '{' + nested + '}');
        }
        return;
      }
      if (rule.type === 5) {
        var fontDeclarations = safeEPUBDeclarations(rule.style, cssItem, epub, true);
        if (fontDeclarations) output.push('@font-face{' + fontDeclarations + '}');
        return;
      }
      throw new RuntimeError(
        'EPUB CSS 含不支持的 at-rule', 'BW_LOCAL_EPUB_CSS_UNSAFE'
      );
    });
    return output.join('\n');
  }
  function scopeEPUBCSS(epub, cssItem, cssText) {
    if (typeof root.CSSStyleSheet !== 'function') {
      throw new RuntimeError(
        '当前 WebKit 无可用 CSSOM 安全解析器', 'BW_LOCAL_EPUB_CSS_UNAVAILABLE'
      );
    }
    var sheet;
    try {
      sheet = new root.CSSStyleSheet();
      if (typeof sheet.replaceSync !== 'function') throw new Error('replaceSync unavailable');
      sheet.replaceSync(String(cssText || ''));
      return scopeEPUBCSSRules(sheet.cssRules, cssItem, epub);
    } catch (error) {
      if (error && /^BW_LOCAL_EPUB_CSS_/.test(String(error.code || ''))) throw error;
      throw new RuntimeError(
        'EPUB CSS 无法由 CSSOM 安全解析', 'BW_LOCAL_EPUB_CSS_UNAVAILABLE',
        { cause: String(error && error.message || error) }
      );
    }
  }
  function loadScopedEPUBCSS(epub) {
    var cssItems = Object.keys(epub.manifest).map(function (id) {
      return epub.manifest[id];
    }).filter(function (item) {
      return String(item.mediaType || '').toLowerCase() === 'text/css' ||
        /\.css$/i.test(item.path);
    }).sort(function (left, right) { return left.path.localeCompare(right.path); });
    if (cssItems.length > 256) {
      return Promise.reject(new RuntimeError(
        'EPUB 样式表数量超过安全上限', 'BW_LOCAL_EPUB_CSS_LIMIT'
      ));
    }
    var chunks = [];
    return cssItems.reduce(function (chain, item) {
      return chain.then(function () {
        return zipText(epub.zip, item.path).then(function (text) {
          var scoped = scopeEPUBCSS(epub, item, text);
          if (scoped) chunks.push(scoped);
        });
      });
    }, Promise.resolve()).then(function () { return chunks.join('\n'); });
  }
  function epubJSONRoute(task, fallbackCode) {
    return Promise.resolve().then(task).then(function (value) {
      return value instanceof Response ? value : jsonResponse(value);
    }).catch(function (error) {
      return outgoingFailureResponse(error, fallbackCode, 500);
    });
  }
  function epubCSSFailureResponse(error) {
    var code = String(error && error.code || 'BW_LOCAL_EPUB_CSS_FAILED');
    var status = Number(error && (error.httpStatus || error.status) || 0) ||
      (code === 'BW_LOCAL_EPUB_CSS_UNAVAILABLE' ? 501 : 422);
    var message = String(error && error.message || error || 'EPUB CSS 不可用')
      .replace(/\*\//g, '* /');
    return new Response('/* ' + code + ': ' + message + ' */', {
      status: status,
      headers: {
        'Content-Type': 'text/css; charset=utf-8',
        'Cache-Control': 'no-store',
        'X-BW-Reader-Error': code
      }
    });
  }
  function handleEPUB(url) {
    if (url.pathname === '/pdf/api/epub-search') {
      return localEPUBSearch(url);
    }
    if (url.pathname === '/pdf/api/epub-manifest') {
      return epubJSONRoute(function () {
        localFileQuery(
          url, ['file'], ['file'], 'BW_LOCAL_EPUB_MANIFEST_QUERY'
        );
        return loadEPUB().then(function (epub) {
        return jsonResponse({
            ok: true, title: epub.title, count: epub.spine.length,
            toc: epub.toc, sha: epub.sha
          });
        });
      }, 'BW_LOCAL_EPUB_MANIFEST_FAILED');
    }
    if (url.pathname === '/pdf/api/epub-section') {
      return epubJSONRoute(function () {
        var sectionCode = 'BW_LOCAL_EPUB_SECTION_QUERY';
        localFileQuery(url, ['file', 'idx'], ['file', 'idx'], sectionCode);
        var idx = strictInteger(
          url.searchParams.get('idx'), 0, 100000, 'idx', sectionCode
        );
        return loadEPUB().then(function (epub) {
          if (idx >= epub.spine.length) {
            throw outgoingRequestError('idx 越界', sectionCode, 400);
          }
          var item = epub.spine[idx];
          return zipText(epub.zip, item.path).then(function (html) {
            return sanitizeSection(epub, item, html);
          }).then(function (html) {
            return { ok: true, html: html, idx: idx };
          });
        });
      }, 'BW_LOCAL_EPUB_SECTION_FAILED');
    }
    if (url.pathname === '/pdf/api/epub-css') {
      return Promise.resolve().then(function () {
        localFileQuery(url, ['file'], ['file'], 'BW_LOCAL_EPUB_CSS_QUERY');
        return loadEPUB().then(loadScopedEPUBCSS).then(function (css) {
          return textResponse(css, 200, 'text/css; charset=utf-8');
        });
      }).catch(epubCSSFailureResponse);
    }
    if (url.pathname === '/pdf/api/epub-resource') {
      return Promise.resolve().then(function () {
        strictQuery(
          url, ['path', 'file'], ['path'], 'BW_LOCAL_EPUB_RESOURCE_QUERY'
        );
        if (url.searchParams.has('file')) {
          requireLocalFile(
            url.searchParams.get('file'), 'BW_LOCAL_EPUB_RESOURCE_QUERY'
          );
        }
        return loadEPUB();
      }).then(function (epub) {
        var path = canonicalZipPath(url.searchParams.get('path'), '');
        var item = path && manifestItemForPath(epub, path);
        if (!path || !item || !epub.zip.file(path)) {
          return textResponse('resource unavailable', 404);
        }
        return sharedResourceBlob(epub, path).then(function (result) {
          if (!result) return textResponse('resource exceeds local budget', 413);
          return new Response(result.blob, {
            status: 200,
            headers: {
              'Content-Type': item.mediaType || 'application/octet-stream',
              'Cache-Control': 'no-store',
              'X-Content-Type-Options': 'nosniff'
            }
          });
        });
      }).catch(function (error) {
        var status = Number(error && (error.httpStatus || error.status) || 0) || 500;
        return textResponse(
          String(error && error.code || 'BW_LOCAL_EPUB_RESOURCE_FAILED') + ': ' +
            String(error && error.message || error),
          status
        );
      });
    }
    return null;
  }

  function requestHeader(input, init, name) {
    var headers = init && init.headers;
    if (!headers && typeof Request !== 'undefined' && input instanceof Request) headers = input.headers;
    if (headers && typeof headers.get === 'function') return String(headers.get(name) || '');
    if (Array.isArray(headers)) {
      var match = headers.find(function (item) {
        return Array.isArray(item) && String(item[0]).toLowerCase() === name.toLowerCase();
      });
      return match ? String(match[1] || '') : '';
    }
    if (headers && typeof headers === 'object') {
      var key = Object.keys(headers).find(function (item) {
        return item.toLowerCase() === name.toLowerCase();
      });
      return key ? String(headers[key] || '') : '';
    }
    return '';
  }
  var PI_GATEWAY_MAX_BODY_BYTES = 8 * 1024 * 1024;
  function bytesToBase64(bytes) {
    var parts = [];
    // Keep every non-final chunk divisible by three so concatenating the
    // encoded chunks remains one canonical base64 value. Small chunks also
    // avoid overflowing apply()/the JS argument stack on multi-megabyte audio.
    var chunkSize = 24 * 1024;
    for (var offset = 0; offset < bytes.length; offset += chunkSize) {
      var chunk = bytes.subarray(offset, Math.min(offset + chunkSize, bytes.length));
      var binary = '';
      for (var i = 0; i < chunk.length; i += 1) binary += String.fromCharCode(chunk[i]);
      parts.push(btoa(binary));
    }
    return parts.join('');
  }
  function binaryRequestPayload(buffer, contentType) {
    var bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
    if (bytes.byteLength > PI_GATEWAY_MAX_BODY_BYTES) {
      throw new RuntimeError('Pi 请求过大', 'BW_PI_GATEWAY_LIMIT');
    }
    return {
      body: bytesToBase64(bytes), bodyEncoding: 'base64',
      contentType: String(contentType || '')
    };
  }
  function utf8RequestPayload(text, contentType) {
    text = String(text || '');
    if (new TextEncoder().encode(text).byteLength > PI_GATEWAY_MAX_BODY_BYTES) {
      throw new RuntimeError('Pi 请求过大', 'BW_PI_GATEWAY_LIMIT');
    }
    return { body: text, bodyEncoding: 'utf8', contentType: String(contentType || '') };
  }
  function requestBodyPayload(input, init, url, method) {
    var body = init && Object.prototype.hasOwnProperty.call(init, 'body') ? init.body : null;
    var contentType = requestHeader(input, init, 'Content-Type');
    if (body == null && typeof Request !== 'undefined' && input instanceof Request) {
      var cloned;
      try { cloned = input.clone(); }
      catch (error) {
        return Promise.reject(new RuntimeError('Pi 请求体已被读取', 'BW_PI_GATEWAY_BODY'));
      }
      contentType = contentType || String(cloned.headers.get('Content-Type') || '');
      return cloned.arrayBuffer().then(function (buffer) {
        return binaryRequestPayload(buffer, contentType);
      });
    }
    if (body == null) return Promise.resolve(utf8RequestPayload('', contentType));
    if (typeof body === 'string') return Promise.resolve(utf8RequestPayload(body, contentType));
    if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) {
      return Promise.resolve(utf8RequestPayload(
        body.toString(),
        contentType || 'application/x-www-form-urlencoded;charset=UTF-8'
      ));
    }
    if (typeof Blob !== 'undefined' && body instanceof Blob) {
      return body.arrayBuffer().then(function (buffer) {
        return binaryRequestPayload(buffer, contentType || body.type);
      });
    }
    if (typeof ArrayBuffer !== 'undefined' && body instanceof ArrayBuffer) {
      return Promise.resolve(binaryRequestPayload(body, contentType));
    }
    if (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView && ArrayBuffer.isView(body)) {
      return Promise.resolve(binaryRequestPayload(
        new Uint8Array(body.buffer, body.byteOffset, body.byteLength), contentType
      ));
    }
    if (typeof FormData !== 'undefined' && body instanceof FormData && typeof Request !== 'undefined') {
      var generated;
      try {
        generated = new Request(url.href, { method: method, headers: init && init.headers, body: body });
      } catch (error) {
        return Promise.reject(new RuntimeError('Pi 表单请求体无效', 'BW_PI_GATEWAY_BODY'));
      }
      contentType = contentType || String(generated.headers.get('Content-Type') || '');
      return generated.arrayBuffer().then(function (buffer) {
        return binaryRequestPayload(buffer, contentType);
      });
    }
    return Promise.reject(new RuntimeError('Pi 请求体不受支持', 'BW_PI_GATEWAY_BODY'));
  }
  function piProxyStreamURL(value) {
    if (typeof value !== 'string' || value.length > 512) {
      throw new RuntimeError('Pi 流地址无效', 'BW_PI_GATEWAY_RESPONSE');
    }
    var shellMatch = String(root.location.pathname || '').match(
      /^(\/r\/[0-9a-f]{64})\/shells\/(?:pdf|epub)\.html$/
    );
    var parsed;
    try { parsed = new URL(value); }
    catch (error) {
      throw new RuntimeError('Pi 流地址无效', 'BW_PI_GATEWAY_RESPONSE');
    }
    if (!shellMatch || parsed.origin !== root.location.origin || parsed.username || parsed.password ||
        parsed.search || parsed.hash ||
        !new RegExp('^' + shellMatch[1] + '\\/pi-proxy\\/[0-9a-f]{32}$').test(parsed.pathname)) {
      throw new RuntimeError('Pi 流地址越界', 'BW_PI_GATEWAY_RESPONSE');
    }
    return parsed.href;
  }
  function requestSignal(input, init) {
    if (init && init.signal) return init.signal;
    if (typeof Request !== 'undefined' && input instanceof Request) return input.signal;
    return undefined;
  }
  function nativePiFetch(input, init, declaredRoute) {
    var url = urlOf(input);
    var method = methodOf(input, init);
    var route;
    try {
      if (!url || url.origin !== root.location.origin) {
        throw new RuntimeError('Pi API 来源无效', 'BW_PI_GATEWAY_ROUTE');
      }
      route = declaredRoute || declaredNativeInterface(url.pathname, method);
      if (route.owner !== 'pi') {
        throw new RuntimeError(
          '接口不由 Pi 网关认领：' + url.pathname,
          'BW_PI_GATEWAY_ROUTE'
        );
      }
    } catch (error) {
      return Promise.reject(error);
    }
    var handler = root.webkit && root.webkit.messageHandlers && root.webkit.messageHandlers.bwNativePiGateway;
    if (!handler || typeof handler.postMessage !== 'function') {
      return Promise.reject(new RuntimeError('Pi 网关不可用', 'BW_PI_GATEWAY_UNAVAILABLE'));
    }
    return requestBodyPayload(input, init, url, method).then(function (payload) {
      return handler.postMessage({
        contract: 'reader-native-pi-request/2',
        action: 'fetch', method: method, path: url.pathname + url.search,
        headers: {
          'Accept': requestHeader(input, init, 'Accept') || '*/*',
          'Content-Type': payload.contentType
        },
        body: payload.body,
        bodyEncoding: payload.bodyEncoding
      });
    }).then(function (result) {
      if (!result || result.contract !== 'reader-native-pi-response/2' ||
          Object.keys(result).sort().join(',') !== 'contract,streamURL') {
        throw new RuntimeError('Pi 网关响应无效', 'BW_PI_GATEWAY_RESPONSE');
      }
      return originalFetch(piProxyStreamURL(result.streamURL), {
        method: 'GET', credentials: 'omit', cache: 'no-store',
        signal: requestSignal(input, init)
      });
    });
  }

  function nativePiFailure(error) {
    var message = String(error && error.message || error || 'Pi 网关请求失败');
    var embeddedCode = message.match(/\b(BW_PI_GATEWAY_[A-Z0-9_]+)\b/);
    var code = error && typeof error.code === 'string' && error.code
      ? error.code
      : (embeddedCode ? embeddedCode[1] : 'BW_PI_GATEWAY_OFFLINE');
    return {
      code: code,
      message: message,
      status: code === 'BW_PI_GATEWAY_REMOTE_BOOK' ? 409 : 503
    };
  }

  function nativePDFAuthoritySnapshot() {
    return Promise.all([
      storedStateRecord(stores.document, 'document-highlights', 'documentId', bookId, []),
      storedStateRecord(stores.document, 'document-notes-legacy', 'documentId', bookId, []),
      storedStateRecord(stores.document, 'ink', 'documentId', bookId, {}),
      storedStateRecord(stores.document, 'user-pages', 'documentId', bookId, [])
    ]).then(function (records) {
      var snapshot = {
        contract: NATIVE_PDF_ASSISTANT_STATE_CONTRACT,
        file: localFileRef(),
        revisions: {
          highlights: records[0].rev,
          notes: records[1].rev,
          ink: records[2].rev,
          user_pages: records[3].rev
        },
        highlights: clone(records[0].payload),
        notes: clone(records[1].payload),
        ink: clone(records[2].payload),
        user_pages: clone(records[3].payload)
      };
      var encoded = JSON.stringify(snapshot);
      // The gateway allows 8 MiB total.  Never truncate authoritative ink or
      // notes: a visible refusal is safer than letting Pi reason over a state
      // that only looks complete.
      if (utf8(encoded).byteLength > 6 * 1024 * 1024) {
        throw new RuntimeError(
          '本机 PDF 批注状态过大，无法完整交给助手',
          'BW_NATIVE_PDF_ASSISTANT_STATE_LIMIT'
        );
      }
      return snapshot;
    });
  }

  function nativePDFAssistantContext(context) {
    context = Object.assign({}, context || {});
    if (String(context.visible_text || '').trim()) return Promise.resolve(context);
    var page = Number(context.page || (
      Array.isArray(context.pages) && context.pages.length ? context.pages[0] : 0
    ));
    if (!Number.isInteger(page) || page < 1) return Promise.resolve(context);
    return pageTextForPage(page).then(function (result) {
      var text = searchableText(result).slice(0, 4000);
      if (text) context.visible_text = text;
      context.native_page_text = {
        state: String(result && result.state || 'unknown'),
        source: String(result && result.source || 'none'),
        page: page
      };
      return context;
    }).catch(function () { return context; });
  }

  function nativePDFRequestBody(input, init, contextKey, writerLease) {
    assertNativePDFWriterLease(writerLease);
    return Promise.all([bodyJSON(input, init), nativePDFAuthoritySnapshot()])
      .then(function (values) {
        assertNativePDFWriterLease(writerLease);
        var body = values[0];
        var snapshot = values[1];
        if (!body || typeof body !== 'object' || Array.isArray(body)) {
          throw new RuntimeError(
            'PDF 助手请求体无效', 'BW_NATIVE_PDF_ASSISTANT_BODY'
          );
        }
        var context = body[contextKey];
        if (!context || typeof context !== 'object' || Array.isArray(context)) {
          context = {};
        }
        return nativePDFAssistantContext(context).then(function (suppliedContext) {
          assertNativePDFWriterLease(writerLease);
          body = Object.assign({}, body);
          body[contextKey] = Object.assign({}, suppliedContext, {
            file_rel: localFileRef(),
            native_local_state: snapshot
          });
          return { body: body, snapshot: snapshot };
        });
      });
  }

  function nativePDFOperationID(action, descriptor) {
    var value = descriptor && descriptor.data && descriptor.data.native_operation_id;
    if (descriptor && descriptor.kind === 'undo') {
      value = action && Array.isArray(action.args) ? action.args[0] : '';
    }
    value = String(value || '');
    if (!/^npdf_[0-9a-f]{24}$/.test(value)) {
      throw new RuntimeError(
        'PDF 助手本机动作缺少可信操作编号',
        'BW_NATIVE_PDF_ASSISTANT_ACTION'
      );
    }
    return value;
  }

  function nativePDFActionDescriptor(action) {
    if (!action || typeof action !== 'object' || Array.isArray(action)) return null;
    if (action.fn === '_nativePDFUndoLast') {
      return { kind: 'undo', data: null };
    }
    if (action.fn !== '_assistEdit' || !Array.isArray(action.args) ||
        !action.args[0] || typeof action.args[0] !== 'object' ||
        Array.isArray(action.args[0])) return null;
    var data = action.args[0];
    if (data.type === 'highlight') return { kind: 'highlight', data: data };
    if (data.type === 'note' && data.op === 'create') {
      return { kind: 'note-create', data: data };
    }
    if (data.type === 'note' && data.op === 'edit') {
      return { kind: 'note-edit', data: data };
    }
    return null;
  }

  function nativePDFRefreshAction() {
    return { fn: '_nativePDFRefreshAnnotations', args: [] };
  }

  function nativePDFSanitizeAction(action, descriptor) {
    if (!descriptor) return clone(action);
    if (descriptor.kind === 'undo') return nativePDFRefreshAction();
    var output = clone(action);
    output.args[0].file = localFileRef();
    return output;
  }

  function nativePDFNoteFields(value, code) {
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        Object.keys(value).some(function (key) {
          return key !== 'text' && key !== 'color';
        })) {
      throw outgoingRequestError('便签修改快照无效', code, 400);
    }
    return {
      text: boundedLocalString(value.text, 8000, '', code, '便签文字', false),
      color: boundedLocalString(value.color, 64, '#ffffff', code, '便签颜色', true)
    };
  }

  function nativePDFNoteFieldsEqual(note, fields) {
    return String(note && note.text || '') === String(fields && fields.text || '') &&
      String(note && note.color || '#ffffff') === String(fields && fields.color || '#ffffff');
  }

  function nativePDFNoteSnapshot(value, fallbackID, code) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw outgoingRequestError('便签快照无效', code, 400);
    }
    var now = nowSeconds();
    var id = localRecordId(value.id || fallbackID, code);
    var note = {
      id: id,
      anchor: normalizedNoteAnchor(value.anchor, code),
      text: boundedLocalString(value.text, 8000, '', code, '便签文字', false),
      color: boundedLocalString(value.color, 64, '#fff8c5', code, '便签颜色', true),
      w: Math.round(finiteLocalNumber(value.w, 40, 4096, 260, code, '便签宽度')),
      h: Math.round(finiteLocalNumber(value.h, 40, 4096, 180, code, '便签高度')),
      collapsed: value.collapsed === true,
      strokes: value.strokes == null ? [] : normalizedStrokeList(value.strokes, code),
      video: value.video == null ? null : boundedCanonicalJSON(value.video, 512 * 1024, code, '视频便签'),
      card: value.card == null ? null : boundedCanonicalJSON(value.card, 2 * 1024 * 1024, code, '卡片便签'),
      html: value.html == null ? null : boundedCanonicalJSON(value.html, 2 * 1024 * 1024, code, 'HTML 便签'),
      iar: value.iar == null ? null : finiteLocalNumber(value.iar, 0.01, 100, null, code, '便签笔迹比例'),
      created: Number.isInteger(Number(value.created)) && Number(value.created) >= 0
        ? Number(value.created) : now,
      updated: Number.isInteger(Number(value.updated)) && Number(value.updated) >= 0
        ? Number(value.updated) : now
    };
    return note;
  }

  function nativePDFHighlightSnapshot(value, code) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw outgoingRequestError('高亮快照无效', code, 400);
    }
    var page = strictInteger(
      value.pdf_page == null ? value.page : value.pdf_page,
      1, 10000000, 'page', code
    );
    var highlight = {
      id: localRecordId(value.id, code),
      page: page,
      rects: normalizedRectangles(value.rects, code),
      color: normalizedHighlightColor(value.color, '#ffd54a', code),
      text: boundedLocalString(value.text, 2000, '', code, '高亮文字', false),
      note: boundedLocalString(value.note, 2000, '', code, '高亮备注', false),
      kind: ['note', 'translate', 'explain'].indexOf(value.kind) >= 0
        ? value.kind : 'note',
      sentence: boundedLocalString(value.sentence, 2000, '', code, '高亮句子', false),
      body: boundedLocalString(value.body, 8000, '', code, '高亮正文', false),
      time: Number.isInteger(Number(value.time)) && Number(value.time) >= 0
        ? Number(value.time) : nowSeconds()
    };
    if (value.page_w != null && value.page_h != null) {
      highlight.page_w = finiteLocalNumber(value.page_w, 1, 100000, null, code, 'page_w');
      highlight.page_h = finiteLocalNumber(value.page_h, 1, 100000, null, code, 'page_h');
    }
    return highlight;
  }

  function nativePDFRecordSet() {
    var specs = [
      ['document-highlights', []],
      ['document-notes-legacy', []],
      ['card-placements', []],
      ['entity-references', []],
      ['pdf-assistant-undo', []],
      ['pdf-assistant-ops', []]
    ];
    return Promise.all(specs.map(function (spec) {
      return storedStateRecord(
        stores.document, spec[0], 'documentId', bookId, spec[1]
      );
    })).then(function (records) {
      return { specs: specs, records: records };
    });
  }

  function nativePDFRevisionConflict(label) {
    throw new RuntimeError(
      label + '已在助手处理期间变化，请重试这次操作',
      'BW_NATIVE_PDF_ASSISTANT_CONFLICT'
    );
  }

  function nativePDFCommitActions(actions, expectedRevisions, writerLease) {
    assertNativePDFWriterLease(writerLease);
    if (!Array.isArray(actions)) {
      return Promise.reject(new RuntimeError(
        'PDF 助手动作列表无效', 'BW_NATIVE_PDF_ASSISTANT_ACTION'
      ));
    }
    var hasLocal = actions.some(function (action) {
      return !!nativePDFActionDescriptor(action);
    });
    if (!hasLocal) {
      return Promise.resolve({ actions: clone(actions), revisions: clone(expectedRevisions) });
    }
    return serializeLocalStateMutation('document', 'pdf-assistant-bundle', function () {
      assertNativePDFWriterLease(writerLease);
      return nativePDFRecordSet().then(function (recordSet) {
        assertNativePDFWriterLease(writerLease);
        var code = 'BW_NATIVE_PDF_ASSISTANT_ACTION';
        var highlights = storedList(clone(recordSet.records[0].payload), code);
        var notes = storedList(clone(recordSet.records[1].payload), code);
        var undo = storedList(clone(recordSet.records[4].payload), code);
        var receipts = storedList(clone(recordSet.records[5].payload), code);
        var received = new Map(receipts.map(function (item) {
          return [String(item && item.id || ''), item];
        }));
        var output = [];
        var touchedHighlights = false;
        var touchedNotes = false;
        var touchedUndo = false;
        var touchedReceipts = false;

        function assertRevision(kind, index, label) {
          if (!expectedRevisions || !Number.isInteger(Number(expectedRevisions[kind])) ||
              Number(recordSet.records[index].rev) !== Number(expectedRevisions[kind])) {
            nativePDFRevisionConflict(label);
          }
        }

        actions.forEach(function (action) {
          var descriptor = nativePDFActionDescriptor(action);
          if (!descriptor) {
            output.push(clone(action));
            return;
          }
          var operationID = nativePDFOperationID(action, descriptor);
          if (received.has(operationID)) {
            output.push(nativePDFRefreshAction());
            return;
          }

          if (descriptor.kind === 'highlight') {
            assertRevision('highlights', 0, '本机高亮');
            var highlightItems = Array.isArray(descriptor.data.items)
              ? descriptor.data.items.map(function (item) {
                  return nativePDFHighlightSnapshot(item, code);
                }) : [];
            if (!highlightItems.length) {
              throw outgoingRequestError('高亮动作没有项目', code, 400);
            }
            var existingHighlightIDs = new Set(highlights.map(function (item) {
              return String(item && item.id || '');
            }));
            if (highlightItems.some(function (item) {
              return existingHighlightIDs.has(item.id);
            })) nativePDFRevisionConflict('本机高亮');
            highlights = highlights.concat(highlightItems);
            undo.push({
              id: operationID, kind: 'highlight-create',
              ids: highlightItems.map(function (item) { return item.id; }),
              ts: nowSeconds()
            });
            touchedHighlights = true;
            touchedUndo = true;
          } else if (descriptor.kind === 'note-create') {
            assertRevision('notes', 1, '本机便签');
            var createdNotes = Array.isArray(descriptor.data.items)
              ? descriptor.data.items.map(function (item) {
                  return nativePDFNoteSnapshot(item && item.note, item && item.id, code);
                }) : [];
            if (!createdNotes.length) {
              throw outgoingRequestError('便签创建动作没有项目', code, 400);
            }
            var existingNoteIDs = new Set(notes.map(function (item) {
              return String(item && item.id || '');
            }));
            if (createdNotes.some(function (item) {
              return existingNoteIDs.has(item.id);
            })) nativePDFRevisionConflict('本机便签');
            notes = notes.concat(createdNotes);
            undo.push({
              id: operationID, kind: 'note-create',
              ids: createdNotes.map(function (item) { return item.id; }),
              ts: nowSeconds()
            });
            touchedNotes = true;
            touchedUndo = true;
          } else if (descriptor.kind === 'note-edit') {
            assertRevision('notes', 1, '本机便签');
            var edits = Array.isArray(descriptor.data.items)
              ? descriptor.data.items : [];
            if (!edits.length) {
              throw outgoingRequestError('便签修改动作没有项目', code, 400);
            }
            var undoItems = [];
            edits.forEach(function (item) {
              var id = localRecordId(item && item.id, code);
              var oldFields = nativePDFNoteFields(item && item.old, code);
              var newFields = nativePDFNoteFields(item && item.new, code);
              var note = notes.find(function (candidate) {
                return candidate && String(candidate.id || '') === id;
              });
              if (!note || !nativePDFNoteFieldsEqual(note, oldFields)) {
                nativePDFRevisionConflict('本机便签');
              }
              note.text = newFields.text;
              note.color = newFields.color;
              note.updated = nowSeconds();
              undoItems.push({ id: id, old: oldFields, current: newFields });
            });
            undo.push({
              id: operationID, kind: 'note-edit', items: undoItems,
              ts: nowSeconds()
            });
            touchedNotes = true;
            touchedUndo = true;
          } else if (descriptor.kind === 'undo') {
            var last = undo.length ? undo[undo.length - 1] : null;
            if (!last || typeof last !== 'object') {
              throw outgoingRequestError('没有可撤销的本机书籍改动', code, 409);
            }
            var ids = new Set((last.ids || []).map(String));
            if (last.kind === 'highlight-create') {
              assertRevision('highlights', 0, '本机高亮');
              if (highlights.filter(function (item) {
                return item && ids.has(String(item.id || ''));
              }).length !== ids.size) nativePDFRevisionConflict('本机高亮');
              highlights = highlights.filter(function (item) {
                return !item || !ids.has(String(item.id || ''));
              });
              touchedHighlights = true;
            } else if (last.kind === 'note-create') {
              assertRevision('notes', 1, '本机便签');
              if (notes.filter(function (item) {
                return item && ids.has(String(item.id || ''));
              }).length !== ids.size) nativePDFRevisionConflict('本机便签');
              notes = notes.filter(function (item) {
                return !item || !ids.has(String(item.id || ''));
              });
              touchedNotes = true;
            } else if (last.kind === 'note-edit' && Array.isArray(last.items)) {
              assertRevision('notes', 1, '本机便签');
              last.items.forEach(function (item) {
                var note = notes.find(function (candidate) {
                  return candidate && String(candidate.id || '') === String(item.id || '');
                });
                if (!note || !nativePDFNoteFieldsEqual(note, item.current)) {
                  nativePDFRevisionConflict('本机便签');
                }
                note.text = String(item.old && item.old.text || '');
                note.color = String(item.old && item.old.color || '#ffffff');
                note.updated = nowSeconds();
              });
              touchedNotes = true;
            } else {
              throw outgoingRequestError('最近的本机改动无法安全撤销', code, 409);
            }
            undo.pop();
            touchedUndo = true;
          }

          var receipt = { id: operationID, kind: descriptor.kind, ts: nowSeconds() };
          receipts.push(receipt);
          received.set(operationID, receipt);
          touchedReceipts = true;
          output.push(nativePDFSanitizeAction(action, descriptor));
        });

        undo = undo.slice(-80);
        receipts = receipts.slice(-160);
        var suffix = randomHex(12);
        var mutations = [];
        if (touchedHighlights) {
          mutations.push(stateRecordMutation(
            'document-highlights', highlights, suffix + '-highlights',
            recordSet.records[0].rev
          ));
        }
        if (touchedNotes) {
          var placements = deriveCardPlacements(notes);
          var references = deriveEntityReferences(placements);
          mutations.push(
            stateRecordMutation('document-notes-legacy', notes, suffix + '-notes', recordSet.records[1].rev),
            stateRecordMutation('card-placements', placements, suffix + '-cards', recordSet.records[2].rev),
            stateRecordMutation('entity-references', references, suffix + '-entities', recordSet.records[3].rev)
          );
        }
        if (touchedUndo) {
          mutations.push(stateRecordMutation(
            'pdf-assistant-undo', undo, suffix + '-undo', recordSet.records[4].rev
          ));
        }
        if (touchedReceipts) {
          mutations.push(stateRecordMutation(
            'pdf-assistant-ops', receipts, suffix + '-ops', recordSet.records[5].rev
          ));
        }
        if (!mutations.length) {
          return { actions: output, revisions: clone(expectedRevisions) };
        }
        assertNativePDFWriterLease(writerLease);
        return stores.document.batch(mutations).then(function () {
          assertNativePDFWriterLease(writerLease);
          return Promise.all([
            storedStateRecord(stores.document, 'document-highlights', 'documentId', bookId, []),
            storedStateRecord(stores.document, 'document-notes-legacy', 'documentId', bookId, [])
          ]).then(function (updated) {
            return {
              actions: output,
              revisions: Object.assign({}, expectedRevisions, {
                highlights: updated[0].rev,
                notes: updated[1].rev
              })
            };
          });
        });
      });
    });
  }

  function nativePDFResponseHeaders(response, contentType) {
    var headers = {};
    response.headers.forEach(function (value, name) {
      var lower = String(name).toLowerCase();
      if (lower !== 'content-length' && lower !== 'content-encoding') headers[name] = value;
    });
    if (contentType) headers['Content-Type'] = contentType;
    headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0';
    return headers;
  }

  function nativePDFSSEEvent(chunk) {
    var event = 'message';
    var data = '';
    chunk.split('\n').forEach(function (line) {
      if (line.indexOf('event:') === 0) event = line.slice(6).trim();
      else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
    });
    return { event: event, data: data };
  }

  function nativePDFCommittedSSE(response, initialRevisions, writerLease) {
    if (!response.ok || !response.body || typeof ReadableStream === 'undefined') {
      releaseNativePDFWriterLease(writerLease);
      return Promise.resolve(response);
    }
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var encoder = new TextEncoder();
    var buffer = '';
    var revisions = clone(initialRevisions);
    var stopped = false;
    var stream = new ReadableStream({
      start: function (controller) {
        function fail(error) {
          if (stopped) return;
          stopped = true;
          try { reader.cancel(error); } catch (_) {}
          releaseNativePDFWriterLease(writerLease);
          var message = String(error && error.message || error || '本机 PDF 动作提交失败');
          controller.enqueue(encoder.encode(
            'event: error\ndata: ' + JSON.stringify('本机写入失败：' + message) +
            '\n\nevent: done\ndata: {}\n\n'
          ));
          controller.close();
        }
        function emitChunk(chunk) {
          var parsed = nativePDFSSEEvent(chunk);
          if (parsed.event !== 'actions') {
            controller.enqueue(encoder.encode(chunk + '\n\n'));
            return Promise.resolve();
          }
          var actions;
          try { actions = JSON.parse(parsed.data); }
          catch (_) {
            return Promise.reject(new RuntimeError(
              'PDF 助手动作事件不是 JSON', 'BW_NATIVE_PDF_ASSISTANT_ACTION'
            ));
          }
          assertNativePDFWriterLease(writerLease);
          return nativePDFCommitActions(
            actions, revisions, writerLease
          ).then(function (result) {
            revisions = result.revisions;
            controller.enqueue(encoder.encode(
              'event: actions\ndata: ' + JSON.stringify(result.actions) + '\n\n'
            ));
          });
        }
        function drain() {
          var boundary = buffer.indexOf('\n\n');
          if (boundary < 0) return Promise.resolve();
          var chunk = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          return emitChunk(chunk).then(drain);
        }
        function pump() {
          reader.read().then(function (result) {
            if (stopped) return;
            if (result.done) {
              buffer += decoder.decode();
              drain().then(function () {
                if (buffer) controller.enqueue(encoder.encode(buffer));
                stopped = true;
                releaseNativePDFWriterLease(writerLease);
                controller.close();
              }).catch(fail);
              return;
            }
            buffer += decoder.decode(result.value, { stream: true });
            drain().then(pump).catch(fail);
          }).catch(fail);
        }
        pump();
      },
      cancel: function (reason) {
        stopped = true;
        releaseNativePDFWriterLease(writerLease);
        return reader.cancel(reason);
      }
    });
    return Promise.resolve(new Response(stream, {
      status: response.status,
      statusText: response.statusText,
      headers: nativePDFResponseHeaders(response, 'text/event-stream; charset=utf-8')
    }));
  }

  function nativePDFChatFailure(error) {
    var message = String(error && error.message || error || '本机 PDF 助手请求失败');
    return new Response(
      'event: error\ndata: ' + JSON.stringify(message) +
      '\n\nevent: done\ndata: {}\n\n',
      { status: 200, headers: {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-store'
      } }
    );
  }

  function nativePDFChatFetch(input, init, url, route) {
    var writerLease;
    return Promise.resolve().then(function () {
      writerLease = acquireNativePDFWriterLease('assistant-sse');
      return assertNoNativePDFMutationJournal();
    }).then(function () {
      return nativePDFRequestBody(input, init, 'context', writerLease);
    }).then(function (request) {
      return nativePiFetch(url.href, {
        method: 'POST',
        headers: {
          'Accept': requestHeader(input, init, 'Accept') || 'text/event-stream',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(request.body),
        signal: requestSignal(input, init)
      }, route).then(function (response) {
        return nativePDFCommittedSSE(
          response, request.snapshot.revisions, writerLease
        );
      });
    }).catch(function (error) {
      releaseNativePDFWriterLease(writerLease);
      return nativePDFChatFailure(error);
    });
  }

  function nativePDFVoiceToolFetch(input, init, url, route) {
    return withNativePDFWriter('assistant-voice', function (writerLease) {
      return nativePDFRequestBody(input, init, 'ctx', writerLease).then(function (request) {
      return nativePiFetch(url.href, {
        method: 'POST',
        headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(request.body),
        signal: requestSignal(input, init)
      }, route).then(function (response) {
        return response.text().then(function (text) {
          var payload;
          try { payload = text ? JSON.parse(text) : null; }
          catch (_) {
            throw new RuntimeError(
              '语音工具响应不是 JSON', 'BW_NATIVE_PDF_ASSISTANT_RESPONSE'
            );
          }
          if (!response.ok || !payload || typeof payload !== 'object' || Array.isArray(payload)) {
            return new Response(text, {
              status: response.status, statusText: response.statusText,
              headers: nativePDFResponseHeaders(response, 'application/json; charset=utf-8')
            });
          }
          var result = payload.result;
          if (!result || typeof result !== 'object' || Array.isArray(result)) {
            return jsonResponse(payload, response.status);
          }
          var singular = result.client_action && typeof result.client_action === 'object';
          var plural = Array.isArray(result.client_actions);
          var actions = plural ? result.client_actions : (singular ? [result.client_action] : []);
          return nativePDFCommitActions(
            actions, request.snapshot.revisions, writerLease
          ).then(function (committed) {
            payload = clone(payload);
            if (plural) payload.result.client_actions = committed.actions;
            if (singular) payload.result.client_action = committed.actions[0] || null;
            return jsonResponse(payload, response.status);
          });
        });
      });
      });
    }).catch(function (error) {
      var code = String(error && error.code || 'BW_NATIVE_PDF_ASSISTANT_FAILED');
      var status = code === 'BW_NATIVE_PDF_ASSISTANT_CONFLICT' ? 409 : 500;
      return jsonResponse({
        ok: false, code: code,
        error: String(error && error.message || error)
      }, status);
    });
  }

  function nativeEPUBAuthoritySnapshot() {
    return Promise.all([
      storedStateRecord(stores.document, 'epub-highlights', 'documentId', bookId, []),
      storedStateRecord(stores.document, 'document-notes-legacy', 'documentId', bookId, []),
      storedStateRecord(stores.document, 'epub-ink', 'documentId', bookId, {})
    ]).then(function (records) {
      var snapshot = {
        contract: NATIVE_EPUB_ASSISTANT_STATE_CONTRACT,
        file: localFileRef(),
        revisions: {
          highlights: records[0].rev,
          notes: records[1].rev,
          ink: records[2].rev
        },
        highlights: clone(records[0].payload),
        notes: clone(records[1].payload),
        ink: clone(records[2].payload)
      };
      var encoded = JSON.stringify(snapshot);
      // Leave headroom for the user's prompt, context and continuation fields
      // inside the gateway's 8 MiB request contract. Never silently truncate
      // an authority snapshot: stale or partial state is worse than a visible
      // refusal.
      if (utf8(encoded).byteLength > 6 * 1024 * 1024) {
        throw new RuntimeError(
          '本机 EPUB 批注状态过大，无法完整交给助手',
          'BW_NATIVE_EPUB_ASSISTANT_STATE_LIMIT'
        );
      }
      return snapshot;
    });
  }

  function nativeEPUBAssistantContext(context) {
    context = Object.assign({}, context || {});
    if (context.current_section_idx == null && context.page != null) {
      var page = Number(context.page);
      if (Number.isInteger(page) && page > 0) context.current_section_idx = page - 1;
    }
    if (String(context.visible_text || '').trim()) return Promise.resolve(context);
    var index = Number(context.current_section_idx);
    if (!Number.isInteger(index) || index < 0) return Promise.resolve(context);
    return loadEPUB().then(function (epub) {
      if (index >= epub.spine.length) return context;
      return epubSectionVisibleText(epub, index).then(function (text) {
        if (text) context.visible_text = String(text).slice(0, 4000);
        context.native_section_text = {
          state: 'ready', source: 'app-epub', index: index
        };
        return context;
      });
    }).catch(function () { return context; });
  }

  function nativePiJSON(url, body, route, signal) {
    return nativePiFetch(url.href, {
      method: 'POST',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: signal
    }, route).then(function (response) {
      return response.text().then(function (text) {
        var payload;
        try { payload = text ? JSON.parse(text) : null; }
        catch (_) {
          throw new RuntimeError('Pi action 响应不是 JSON', 'BW_NATIVE_EPUB_ACTION_METADATA');
        }
        if (!response.ok || !payload || payload.ok !== true) {
          throw new RuntimeError(
            String(payload && payload.error || 'Pi action 元数据写入失败'),
            'BW_NATIVE_EPUB_ACTION_METADATA'
          );
        }
        return payload;
      });
    });
  }

  function nativeEPUBAssistantFetch(input, init, url, route) {
    return Promise.all([bodyJSON(input, init), nativeEPUBAuthoritySnapshot()])
      .then(function (values) {
        var body = values[0];
        if (!body || typeof body !== 'object' || Array.isArray(body)) {
          throw new RuntimeError('EPUB 助手请求体无效', 'BW_NATIVE_EPUB_ASSISTANT_BODY');
        }
        var context = body.context;
        if (!context || typeof context !== 'object' || Array.isArray(context)) context = {};
        return nativeEPUBAssistantContext(context).then(function (suppliedContext) {
          body = Object.assign({}, body, {
            context: Object.assign({}, suppliedContext, {
              file: localFileRef(),
              native_local_state: values[1]
            })
          });
          return nativePiFetch(url.href, {
            method: 'POST',
            headers: {
              'Accept': requestHeader(input, init, 'Accept') || 'text/event-stream',
              'Content-Type': 'application/json'
            },
            body: JSON.stringify(body),
            signal: requestSignal(input, init)
          }, route);
        });
      });
  }

  function nativeEPUBGenericRequestBody(input, init, contextKey) {
    return Promise.all([bodyJSON(input, init), nativeEPUBAuthoritySnapshot()])
      .then(function (values) {
        var body = values[0];
        var snapshot = values[1];
        if (!body || typeof body !== 'object' || Array.isArray(body)) {
          throw new RuntimeError(
            'EPUB 通用助手请求体无效', 'BW_NATIVE_EPUB_ASSISTANT_BODY'
          );
        }
        var context = body[contextKey];
        if (!context || typeof context !== 'object' || Array.isArray(context)) context = {};
        return nativeEPUBAssistantContext(context).then(function (suppliedContext) {
          body = Object.assign({}, body);
          body[contextKey] = Object.assign({}, suppliedContext, {
            file_rel: localFileRef(),
            native_local_state: snapshot
          });
          return { body: body, snapshot: snapshot };
        });
      });
  }

  function nativeEPUBClientActionKind(action) {
    if (!action || typeof action !== 'object' || Array.isArray(action)) return '';
    if (action.fn === 'nativeLocalEPUBMutation') return 'notes';
    if (action.fn === 'epubHighlight') return 'highlights';
    return '';
  }

  function nativeEPUBAssertAssistantRevisions(expectedRevisions, kinds) {
    return nativeEPUBAuthoritySnapshot().then(function (current) {
      Array.from(kinds).forEach(function (kind) {
        if (!expectedRevisions || !Number.isInteger(Number(expectedRevisions[kind])) ||
            Number(current.revisions[kind]) !== Number(expectedRevisions[kind])) {
          throw new RuntimeError(
            '本机 EPUB ' + (kind === 'notes' ? '便签' : '高亮') +
              '已在助手处理期间变化，请重试这次操作',
            'BW_NATIVE_EPUB_ASSISTANT_CONFLICT'
          );
        }
      });
      return current;
    });
  }

  function nativeEPUBCommitAssistantActions(actions, expectedRevisions) {
    if (!Array.isArray(actions)) {
      return Promise.reject(new RuntimeError(
        'EPUB 助手动作列表无效', 'BW_NATIVE_EPUB_ASSISTANT_ACTION'
      ));
    }
    var localKinds = new Set(actions.map(nativeEPUBClientActionKind).filter(Boolean));
    if (!localKinds.size) {
      return Promise.resolve({ actions: clone(actions), revisions: clone(expectedRevisions) });
    }
    return nativeEPUBAssertAssistantRevisions(expectedRevisions, localKinds)
      .then(function () {
        var output = [];
        var sequence = Promise.resolve();
        actions.forEach(function (action) {
          sequence = sequence.then(function () {
            var kind = nativeEPUBClientActionKind(action);
            if (!kind) {
              output.push(clone(action));
              return;
            }
            if (!Array.isArray(action.args) || !action.args[0] ||
                typeof action.args[0] !== 'object' || Array.isArray(action.args[0])) {
              throw new RuntimeError(
                'EPUB 助手本机动作参数无效', 'BW_NATIVE_EPUB_ASSISTANT_ACTION'
              );
            }
            if (kind === 'notes') {
              if (typeof root.nativeLocalEPUBMutationTransaction !== 'function') {
                throw new RuntimeError(
                  'EPUB 本机便签事务入口不可用', 'BW_NATIVE_EPUB_ASSISTANT_ACTION'
                );
              }
              return Promise.resolve(root.nativeLocalEPUBMutationTransaction(
                clone(action.args[0])
              ));
            }
            if (typeof root.nativeLocalEPUBHighlight !== 'function') {
              throw new RuntimeError(
                'EPUB 本机高亮事务入口不可用', 'BW_NATIVE_EPUB_ASSISTANT_ACTION'
              );
            }
            return Promise.resolve(root.nativeLocalEPUBHighlight(clone(action.args[0])));
          });
        });
        return sequence.then(function () {
          return nativeEPUBAuthoritySnapshot().then(function (snapshot) {
            return { actions: output, revisions: snapshot.revisions };
          });
        });
      });
  }

  function nativeEPUBCommittedSSE(response, initialRevisions) {
    if (!response.ok || !response.body || typeof ReadableStream === 'undefined') {
      return Promise.resolve(response);
    }
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var encoder = new TextEncoder();
    var buffer = '';
    var revisions = clone(initialRevisions);
    var stopped = false;
    var stream = new ReadableStream({
      start: function (controller) {
        function fail(error) {
          if (stopped) return;
          stopped = true;
          try { reader.cancel(error); } catch (_) {}
          var message = String(error && error.message || error || '本机 EPUB 动作提交失败');
          controller.enqueue(encoder.encode(
            'event: error\ndata: ' + JSON.stringify('本机写入失败：' + message) +
            '\n\nevent: done\ndata: {}\n\n'
          ));
          controller.close();
        }
        function emitChunk(chunk) {
          var parsed = nativePDFSSEEvent(chunk);
          if (parsed.event !== 'actions') {
            controller.enqueue(encoder.encode(chunk + '\n\n'));
            return Promise.resolve();
          }
          var actions;
          try { actions = JSON.parse(parsed.data); }
          catch (_) {
            return Promise.reject(new RuntimeError(
              'EPUB 助手动作事件不是 JSON', 'BW_NATIVE_EPUB_ASSISTANT_ACTION'
            ));
          }
          return nativeEPUBCommitAssistantActions(actions, revisions).then(function (result) {
            revisions = result.revisions;
            controller.enqueue(encoder.encode(
              'event: actions\ndata: ' + JSON.stringify(result.actions) + '\n\n'
            ));
          });
        }
        function drain() {
          var boundary = buffer.indexOf('\n\n');
          if (boundary < 0) return Promise.resolve();
          var chunk = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          return emitChunk(chunk).then(drain);
        }
        function pump() {
          reader.read().then(function (result) {
            if (stopped) return;
            if (result.done) {
              buffer += decoder.decode();
              drain().then(function () {
                if (buffer) controller.enqueue(encoder.encode(buffer));
                stopped = true;
                controller.close();
              }).catch(fail);
              return;
            }
            buffer += decoder.decode(result.value, { stream: true });
            drain().then(pump).catch(fail);
          }).catch(fail);
        }
        pump();
      },
      cancel: function (reason) {
        stopped = true;
        return reader.cancel(reason);
      }
    });
    return Promise.resolve(new Response(stream, {
      status: response.status,
      statusText: response.statusText,
      headers: nativePDFResponseHeaders(response, 'text/event-stream; charset=utf-8')
    }));
  }

  function nativeEPUBGenericChatFailure(error) {
    var message = String(error && error.message || error || '本机 EPUB 助手请求失败');
    return new Response(
      'event: error\ndata: ' + JSON.stringify(message) +
      '\n\nevent: done\ndata: {}\n\n',
      { status: 200, headers: {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-store'
      } }
    );
  }

  function nativeEPUBGenericChatFetch(input, init, url, route) {
    return nativeEPUBGenericRequestBody(input, init, 'context').then(function (request) {
      return nativePiFetch(url.href, {
        method: 'POST',
        headers: {
          'Accept': requestHeader(input, init, 'Accept') || 'text/event-stream',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(request.body),
        signal: requestSignal(input, init)
      }, route).then(function (response) {
        return nativeEPUBCommittedSSE(response, request.snapshot.revisions);
      });
    }).catch(nativeEPUBGenericChatFailure);
  }

  function nativeEPUBGenericVoiceToolFetch(input, init, url, route) {
    return nativeEPUBGenericRequestBody(input, init, 'ctx').then(function (request) {
      return nativePiFetch(url.href, {
        method: 'POST',
        headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(request.body),
        signal: requestSignal(input, init)
      }, route).then(function (response) {
        return response.text().then(function (text) {
          var payload;
          try { payload = text ? JSON.parse(text) : null; }
          catch (_) {
            throw new RuntimeError(
              'EPUB 语音工具响应不是 JSON', 'BW_NATIVE_EPUB_ASSISTANT_RESPONSE'
            );
          }
          if (!response.ok || !payload || typeof payload !== 'object' || Array.isArray(payload)) {
            return new Response(text, {
              status: response.status, statusText: response.statusText,
              headers: nativePDFResponseHeaders(response, 'application/json; charset=utf-8')
            });
          }
          var result = payload.result;
          if (!result || typeof result !== 'object' || Array.isArray(result)) {
            return jsonResponse(payload, response.status);
          }
          var singular = result.client_action && typeof result.client_action === 'object';
          var plural = Array.isArray(result.client_actions);
          var actions = plural ? result.client_actions : (singular ? [result.client_action] : []);
          return nativeEPUBCommitAssistantActions(actions, request.snapshot.revisions)
            .then(function (committed) {
              payload = clone(payload);
              if (plural) payload.result.client_actions = committed.actions;
              if (singular) payload.result.client_action = committed.actions[0] || null;
              return jsonResponse(payload, response.status);
            });
        });
      });
    }).catch(function (error) {
      var code = String(error && error.code || 'BW_NATIVE_EPUB_ASSISTANT_FAILED');
      var status = code === 'BW_NATIVE_EPUB_ASSISTANT_CONFLICT' ? 409 : 500;
      return jsonResponse({
        ok: false, code: code,
        error: String(error && error.message || error)
      }, status);
    });
  }

  function nativeEPUBActionOperation(action, requestedOp) {
    if (!action || typeof action !== 'object' || Array.isArray(action) ||
        typeof action.id !== 'string' || !action.id) return null;
    var payload = requestedOp === 'undo' ? action.undo : action.redo;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    var operation = String(payload.op || '');
    var kind = operation.indexOf('hl_') === 0 ? 'epub-highlights' :
      (operation.indexOf('sticky_') === 0 ? 'document-notes-legacy' : '');
    return kind ? { kind: kind, operation: operation, payload: payload,
      invalidFile: payload.file !== localFileRef() } : null;
  }

  function nativeEPUBRecordSet(kind, queryOptions) {
    var kinds = kind === 'document-notes-legacy'
      ? ['document-notes-legacy', 'card-placements', 'entity-references']
      : [kind];
    return Promise.all(kinds.map(function (item) {
      return storedStateRecord(
        stores.document, item, 'documentId', bookId,
        item === 'document-notes-legacy' || item === 'epub-highlights' ? [] : [],
        queryOptions
      );
    })).then(function (records) { return { kinds: kinds, records: records }; });
  }

  function nativeEPUBRecordMutations(recordSet, payload, suffix, revisionDelta) {
    return recordSet.kinds.map(function (kind, index) {
      var value;
      if (kind === recordSet.kinds[0]) value = payload;
      else if (kind === 'card-placements') value = deriveCardPlacements(payload);
      else value = deriveEntityReferences(deriveCardPlacements(payload));
      return stateRecordMutation(
        kind, value, suffix + '-' + index,
        recordSet.records[index].rev + (revisionDelta || 0)
      );
    });
  }

  function nativeEPUBFieldsEqual(note, fields) {
    fields = fields || {};
    return String(note && note.text || '') === String(fields.text || '') &&
      String(note && note.color || '#ffffff') === String(fields.color || '#ffffff');
  }

  function nativeEPUBNormalizeNoteSnapshot(note) {
    var code = 'BW_NATIVE_EPUB_ACTION_BODY';
    assertObjectFields(note, [
      'id', 'anchor', 'text', 'color', 'w', 'h', 'collapsed', 'strokes',
      'video', 'card', 'html', 'iar', 'created', 'updated'
    ], ['id', 'anchor'], code);
    if (typeof note.id !== 'string' || !/^c_[a-f0-9]{16,32}$/.test(note.id)) {
      throw new RuntimeError('本机便签 id 无效', code);
    }
    var output = {
      id: note.id,
      anchor: normalizedNoteAnchor(note.anchor, code),
      text: boundedLocalString(note.text, 8000, '', code, '便签文字', false),
      color: boundedLocalString(note.color, 64, '#ffffff', code, '便签颜色', true),
      w: Math.round(finiteLocalNumber(note.w, 40, 4096, 260, code, '便签宽度')),
      h: Math.round(finiteLocalNumber(note.h, 40, 4096, 180, code, '便签高度')),
      collapsed: note.collapsed === true,
      strokes: note.strokes == null ? [] : normalizedStrokeList(note.strokes, code),
      video: note.video == null ? null : boundedCanonicalJSON(note.video, 512 * 1024, code, '视频便签'),
      card: note.card == null ? null : boundedCanonicalJSON(note.card, 2 * 1024 * 1024, code, '卡片便签'),
      html: note.html == null ? null : boundedCanonicalJSON(note.html, 2 * 1024 * 1024, code, 'HTML 便签'),
      iar: note.iar == null ? null : finiteLocalNumber(note.iar, 0.01, 100, null, code, '便签笔迹比例'),
      created: strictInteger(note.created == null ? nowSeconds() : note.created, 0, 100000000000, 'created', code),
      updated: strictInteger(note.updated == null ? nowSeconds() : note.updated, 0, 100000000000, 'updated', code)
    };
    return output;
  }

  function nativeEPUBApplyActionPayload(current, descriptor, action, requestedOp) {
    current = clone(current);
    var payload = descriptor.payload;
    var operation = descriptor.operation;
    if (descriptor.invalidFile) {
      throw new RuntimeError('本机 EPUB action 文件不匹配', 'BW_NATIVE_EPUB_ACTION_BODY');
    }
    if (!Array.isArray(current)) {
      throw new RuntimeError('本机 EPUB action 状态损坏', 'BW_NATIVE_EPUB_ACTION_STATE');
    }
    if (operation === 'hl_delete') {
      var deleteIds = new Set((payload.ids || []).map(String).filter(Boolean));
      if (!deleteIds.size) throw new RuntimeError('高亮撤销缺少 id', 'BW_NATIVE_EPUB_ACTION_BODY');
      var foundCount = current.filter(function (item) {
        return item && deleteIds.has(String(item.id || ''));
      }).length;
      if (foundCount !== deleteIds.size) {
        throw new RuntimeError('高亮状态已经变化，请刷新动作卡', 'BW_NATIVE_EPUB_ACTION_CONFLICT');
      }
      return current.filter(function (item) {
        return !item || !deleteIds.has(String(item.id || ''));
      });
    }
    if (operation === 'hl_create') {
      var items = Array.isArray(payload.items) ? payload.items : [];
      if (!items.length) throw new RuntimeError('高亮重做缺少快照', 'BW_NATIVE_EPUB_ACTION_BODY');
      var fresh = items.map(function (item) {
        if (!item || typeof item !== 'object' || !item.anchor) {
          throw new RuntimeError('高亮重做快照无效', 'BW_NATIVE_EPUB_ACTION_BODY');
        }
        return {
          id: 'e' + randomHex(6).slice(0, 11),
          cfi: String(item.cfi || ''), anchor: clone(item.anchor),
          text: String(item.text || '').slice(0, 2000),
          color: String(item.color || '#ffd54a'), note: String(item.note || '').slice(0, 2000),
          sentence: String(item.sentence || '').slice(0, 2000),
          body: String(item.body || '').slice(0, 8000), kind: String(item.kind || '').slice(0, 32),
          time: nowSeconds()
        };
      });
      action.undo = { op: 'hl_delete', file: localFileRef(), ids: fresh.map(function (item) { return item.id; }) };
      action.redo = Object.assign({}, action.redo, { items: clone(fresh) });
      return current.concat(fresh);
    }
    if (operation === 'sticky_delete') {
      var noteIds = new Set((payload.ids || []).map(String).filter(Boolean));
      if (!noteIds.size) throw new RuntimeError('便签撤销缺少 id', 'BW_NATIVE_EPUB_ACTION_BODY');
      if (current.filter(function (item) { return item && noteIds.has(String(item.id || '')); }).length !== noteIds.size) {
        throw new RuntimeError('便签状态已经变化，请刷新动作卡', 'BW_NATIVE_EPUB_ACTION_CONFLICT');
      }
      return current.filter(function (item) { return !item || !noteIds.has(String(item.id || '')); });
    }
    if (operation === 'sticky_create') {
      var notes = Array.isArray(payload.notes)
        ? payload.notes.map(nativeEPUBNormalizeNoteSnapshot) : [];
      if (!notes.length || notes.some(function (note) { return !note || !note.id; })) {
        throw new RuntimeError('便签重做快照无效', 'BW_NATIVE_EPUB_ACTION_BODY');
      }
      var existing = new Set(current.map(function (item) { return String(item && item.id || ''); }));
      if (notes.some(function (note) { return existing.has(String(note.id)); })) {
        throw new RuntimeError('便签 id 已存在，请刷新动作卡', 'BW_NATIVE_EPUB_ACTION_CONFLICT');
      }
      return current.concat(notes);
    }
    if (operation === 'sticky_set') {
      var note = current.find(function (item) { return item && String(item.id || '') === String(payload.id || ''); });
      if (!note) throw new RuntimeError('便签已不存在', 'BW_NATIVE_EPUB_ACTION_CONFLICT');
      var expected = requestedOp === 'undo' ? action.redo && action.redo.fields : action.undo && action.undo.fields;
      if (!nativeEPUBFieldsEqual(note, expected)) {
        throw new RuntimeError('便签已被修改，请刷新动作卡', 'BW_NATIVE_EPUB_ACTION_CONFLICT');
      }
      var fields = payload.fields || {};
      if (Object.prototype.hasOwnProperty.call(fields, 'text')) note.text = String(fields.text).slice(0, 8000);
      if (Object.prototype.hasOwnProperty.call(fields, 'color')) note.color = String(fields.color).slice(0, 64);
      note.updated = nowSeconds();
      return current;
    }
    throw new RuntimeError('本机 EPUB action 操作不受支持', 'BW_NATIVE_EPUB_ACTION_BODY');
  }

  function nativeEPUBActionTransaction(action, requestedOp, metadataTask) {
    var descriptor = nativeEPUBActionOperation(action, requestedOp);
    if (!descriptor) {
      return Promise.reject(new RuntimeError(
        '不是本机 EPUB action', 'BW_NATIVE_EPUB_ACTION_NOT_LOCAL'
      ));
    }
    // 队列里只放本地的读与写，而且两者都有界。
    //
    // 这个队列与 epub-highlights 的 CRUD 是同一条：先前 Pi 的 metadataTask 也被
    // 关在里面，于是一次网络往返期间，用户的每一次高亮增删改都排在它后面；网络
    // 若不回应，整条队列就此不动。本地提交完成即释放队列，Pi 留到队列之外去等。
    var bound = { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS };
    return serializeLocalStateMutation('document', descriptor.kind, function () {
      return nativeEPUBRecordSet(descriptor.kind, bound).then(function (recordSet) {
        var before = clone(recordSet.records[0].payload);
        var nextAction = clone(action);
        var next = nativeEPUBApplyActionPayload(
          before, nativeEPUBActionOperation(nextAction, requestedOp), nextAction, requestedOp
        );
        nextAction.state = requestedOp === 'undo' ? 'undone' : 'done';
        var suffix = randomHex(12);
        return stores.document.batch(
          nativeEPUBRecordMutations(recordSet, next, suffix + '-commit', 0),
          bound
        ).then(function () { return nextAction; });
      });
    }).then(function (nextAction) {
      return nativeEPUBActionMetadata(nextAction, metadataTask);
    });
  }

  // Pi 元数据：队列之外，有限等待。
  //
  // 笔记与高亮是 App 的事实，Pi 只留会话/动作卡的镜像，所以它没回应不能回滚用户
  // 已经完成的本地写入 —— 也不能让用户一直等着。超时按 metadata_pending 返回，
  // 说明"本地已写、镜像未确认"，而不是假称同步成功。
  function nativeEPUBActionMetadata(nextAction, metadataTask) {
    return new Promise(function (resolve) {
      var settled = false;
      var timer = root.setTimeout(function () {
        if (settled) return;
        settled = true;
        resolve({
          action: nextAction, metadata: null, metadataSynced: false,
          metadataStatus: 'metadata_pending',
          metadataError: 'Pi 元数据在 ' + NATIVE_EPUB_METADATA_TIMEOUT_MS +
            'ms 内未回应；本地写入已完成'
        });
      }, NATIVE_EPUB_METADATA_TIMEOUT_MS);
      Promise.resolve().then(function () {
        return metadataTask(nextAction);
      }).then(function (metadata) {
        if (settled) return;
        settled = true; root.clearTimeout(timer);
        resolve({
          action: nextAction, metadata: metadata,
          metadataSynced: true, metadataStatus: 'ok', metadataError: null
        });
      }, function (metadataError) {
        if (settled) return;
        settled = true; root.clearTimeout(timer);
        resolve({
          action: nextAction, metadata: null, metadataSynced: false,
          metadataStatus: 'metadata_error',
          metadataError: String(
            metadataError && metadataError.message || metadataError || 'Pi 元数据未同步'
          ).slice(0, 500)
        });
      });
    });
  }

  function nativeEPUBActionError(error) {
    var code = String(error && error.code || 'BW_NATIVE_EPUB_ACTION_FAILED');
    var status = code === 'BW_NATIVE_EPUB_ACTION_CONFLICT' ? 409 :
      code === 'BW_NATIVE_EPUB_ACTION_BODY' ? 400 :
      code === 'BW_NATIVE_EPUB_ACTION_METADATA' ? 502 : 500;
    return jsonResponse({ ok: false, code: code,
      error: String(error && error.message || error) }, status);
  }

  function nativeEPUBActionFetch(input, init, url, route) {
    var signal = requestSignal(input, init);
    return bodyJSON(input, init).then(function (body) {
      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        throw new RuntimeError('EPUB action 请求体无效', 'BW_NATIVE_EPUB_ACTION_BODY');
      }
      var op = String(body.op || '');
      if (op === 'native_apply') {
        if (body.contract !== NATIVE_EPUB_ACTION_CONTRACT ||
            body.file !== localFileRef() || !body.action) {
          throw new RuntimeError('本机 EPUB action 合同无效', 'BW_NATIVE_EPUB_ACTION_BODY');
        }
        return nativeEPUBActionTransaction(body.action, 'redo', function (nextAction) {
          return nativePiJSON(url, {
            op: 'attach', file: body.file, actions: [nextAction],
            native_contract: NATIVE_EPUB_ACTION_CONTRACT
          }, route, signal);
        }).then(function (result) {
          return jsonResponse({
            ok: true, state: 'done', action: result.action,
            metadata_synced: result.metadataSynced,
            metadata_pending: !result.metadataSynced,
            warning: result.metadataError
          });
        });
      }
      if (op === 'attach') {
        var actions = [];
        (Array.isArray(body.actions) ? body.actions : []).forEach(function (action) {
          var descriptor = nativeEPUBActionOperation(action, 'undo');
          if (!descriptor) return;
          if (descriptor.invalidFile) {
            throw new RuntimeError('本机 EPUB action 书籍不匹配', 'BW_NATIVE_EPUB_ACTION_BODY');
          }
          actions.push(action);
        });
        var remoteBody = Object.assign({}, body, {
          native_contract: NATIVE_EPUB_ACTION_CONTRACT
        });
        return nativePiJSON(url, remoteBody, route, signal).then(function (payload) {
          if (payload.stored !== true) {
            throw new RuntimeError('Pi 没有保存 action 元数据', 'BW_NATIVE_EPUB_ACTION_METADATA');
          }
          return jsonResponse(payload);
        }).catch(function (metadataError) {
          if (!actions.length) throw metadataError;
          return jsonResponse({
            ok: true,
            stored: true,
            storage: 'local',
            metadata_synced: false,
            metadata_pending: true,
            actions: clone(body.actions || []),
            warning: String(
              metadataError && metadataError.message || metadataError || 'Pi 元数据未同步'
            ).slice(0, 500)
          });
        });
      }
      if (op === 'undo' || op === 'redo') {
        var action = body.action;
        if (!nativeEPUBActionOperation(action, op)) {
          // Anki/Obsidian and other external effects remain Pi-owned.
          return nativePiFetch(input, init, route);
        }
        if (body.file !== localFileRef()) {
          throw new RuntimeError('本机 EPUB action 书籍不匹配', 'BW_NATIVE_EPUB_ACTION_BODY');
        }
        var previous = clone(action);
        return nativeEPUBActionTransaction(action, op, function (nextAction) {
          return nativePiJSON(url, {
            op: 'native_commit', requested_op: op, file: body.file,
            previous_action: previous, action: nextAction,
            native_contract: NATIVE_EPUB_ACTION_CONTRACT
          }, route, signal);
        }).then(function (result) {
          return jsonResponse({
            ok: true, state: result.action.state, action: result.action,
            metadata_synced: result.metadataSynced,
            metadata_pending: !result.metadataSynced,
            warning: result.metadataError
          });
        });
      }
      return nativePiFetch(input, init, route);
    }).catch(nativeEPUBActionError);
  }

  function mergeNativePageOverlay(url, localResponse, piResponse) {
    if (url.pathname !== '/pdf/api/page-overlay' || !localResponse || !localResponse.ok) {
      return Promise.resolve(piResponse && piResponse.ok ? piResponse : localResponse);
    }
    if (!piResponse || !piResponse.ok) return Promise.resolve(localResponse);
    // The App can always describe its local text/formula layer. Pi is an
    // optional enrichment source for learned vocabulary, translated sentences
    // and calibration. A missing Pi copy must never turn the local overlay into
    // an error or erase formula regions that already exist on the device.
    return Promise.all([
      localResponse.clone().json(),
      piResponse.clone().json()
    ]).then(function (values) {
      var local = values[0];
      var remote = values[1];
      if (!remote || typeof remote !== 'object' || Array.isArray(remote) ||
          remote.ok !== true) return localResponse;
      var payload = Object.assign({}, local, {
        vocab_marks: Array.isArray(remote.vocab_marks) ? remote.vocab_marks : [],
        vocab_sentences: Array.isArray(remote.vocab_sentences) ? remote.vocab_sentences : [],
        mastered_furi: Array.isArray(remote.mastered_furi) ? remote.mastered_furi : [],
        offset: remote.offset && typeof remote.offset === 'object'
          ? remote.offset : local.offset
      });
      var remoteCV = String(remote.cv || '');
      var localCV = String(local.cv || '');
      payload.cv = remoteCV + (remoteCV && localCV ? '|' : '') +
        (localCV ? 'native:' + localCV : '');
      var headers = {};
      piResponse.headers.forEach(function (value, name) {
        var lower = String(name).toLowerCase();
        if (lower !== 'content-length' && lower !== 'content-encoding') headers[name] = value;
      });
      headers['Content-Type'] = 'application/json; charset=utf-8';
      headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0';
      return new Response(JSON.stringify(payload), {
        status: 200, headers: headers
      });
    }).catch(function () {
      return localResponse;
    });
  }

  function nativePageOverlayFetch(input, init, url, route) {
    var remote = new Promise(function (resolve) {
      var settled = false;
      var timer = root.setTimeout(function () {
        settled = true;
        resolve(null);
      }, 750);
      nativePiFetch(input, init, route).then(function (response) {
        if (settled) {
          try {
            if (response && response.body && typeof response.body.cancel === 'function') {
              var cancelled = response.body.cancel('native overlay deadline elapsed');
              if (cancelled && typeof cancelled.catch === 'function') {
                cancelled.catch(function () {});
              }
            }
          } catch (_) {}
          return;
        }
        settled = true;
        root.clearTimeout(timer);
        resolve(response);
      }).catch(function () {
        if (settled) return;
        settled = true;
        root.clearTimeout(timer);
        resolve(null);
      });
    });
    return Promise.all([
      localPageOverlay(url),
      remote
    ]).then(function (responses) {
      return mergeNativePageOverlay(url, responses[0], responses[1]);
    });
  }

  var NATIVE_SYNC_BATCH_CONTRACT = 'command-outbox/2';
  var NATIVE_SYNC_BATCH_ENDPOINTS = Object.freeze({
    '/pdf/api/lookup-event': Object.freeze(['POST']),
    '/pdf/api/vocab-mark': Object.freeze(['POST']),
    '/pdf/api/jp-vocab-mark': Object.freeze(['POST']),
    '/pdf/api/phrases': Object.freeze(['POST', 'DELETE']),
    '/pdf/api/phrase-mark': Object.freeze(['POST']),
    '/pdf/api/highlights': Object.freeze(['POST', 'PATCH', 'DELETE']),
    '/pdf/api/notes': Object.freeze(['POST', 'PATCH', 'DELETE']),
    '/pdf/api/anki-add-cards': Object.freeze(['POST']),
    '/pdf/api/review-answer': Object.freeze(['POST']),
    '/pdf/api/reading-pos': Object.freeze(['POST'])
  });

  function nativeSyncBatchTarget(op) {
    if (!exactKeys(op, ['mutationId', 'url', 'method', 'body']) ||
        !/^mut-v2-[a-f0-9]{32}$/.test(String(op.mutationId || '')) ||
        typeof op.url !== 'string' || op.url.length > 2048 ||
        typeof op.method !== 'string') {
      return null;
    }
    var method = op.method.toUpperCase();
    var url;
    try { url = new URL(op.url, root.location.origin); }
    catch (_) { return null; }
    if (url.origin !== root.location.origin || url.username || url.password || url.hash) {
      return null;
    }
    var methods = NATIVE_SYNC_BATCH_ENDPOINTS[url.pathname];
    var allowed = !!methods && methods.indexOf(method) >= 0;
    if (!allowed) {
      allowed = method === 'PATCH' &&
        /^\/pdf\/api\/entity\/[A-Za-z0-9_-]{1,160}$/.test(url.pathname);
    }
    return allowed ? { url: url, method: method } : null;
  }

  function nativeSyncBatchRouteFailure(error) {
    var code = String(error && error.code || 'BW_NATIVE_SYNC_BATCH_ROUTE');
    var status = code === 'BW_NATIVE_INTERFACE_METHOD' ? 405 :
      code === 'BW_NATIVE_INTERFACE_SURFACE' ? 404 : 501;
    return {
      status: status,
      code: code,
      error: String(error && error.message || error || '同步子请求未分类').slice(0, 160)
    };
  }

  function nativeSyncBatchOperation(op, signal, writerLease) {
    assertNativePDFWriterLease(writerLease);
    var target = nativeSyncBatchTarget(op);
    if (!target) {
      return Promise.resolve({
        status: 400,
        code: 'BW_NATIVE_SYNC_BATCH_OPERATION',
        error: '同步子请求格式或端点无效'
      });
    }
    var route;
    try { route = declaredNativeInterface(target.url.pathname, target.method); }
    catch (error) { return Promise.resolve(nativeSyncBatchRouteFailure(error)); }
    if (route.owner !== 'local' && route.owner !== 'pi') {
      return Promise.resolve({
        status: 501,
        code: 'BW_NATIVE_SYNC_BATCH_OWNER',
        error: '同步子请求不属于本地或 Pi'
      });
    }
    var headers = {
      'Accept': 'application/json',
      'Content-Type': 'application/json',
      'X-BW-Command-Outbox': NATIVE_SYNC_BATCH_CONTRACT,
      'X-BW-Mutation-Id': String(op.mutationId)
    };
    var nestedInit = {
      method: target.method,
      headers: headers,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: signal
    };
    if (op.body !== null) nestedInit.body = JSON.stringify(op.body);
    return Promise.resolve().then(function () {
      assertNativePDFWriterLease(writerLease);
      return route.owner === 'local'
        ? localFetch(target.url.href, nestedInit, true, writerLease)
        : nativePiFetch(target.url.href, nestedInit, route);
    }).then(function (response) {
      assertNativePDFWriterLease(writerLease);
      return { status: Number(response && response.status) || 500 };
    }).catch(function (error) {
      if (route.owner === 'pi') {
        var failure = nativePiFailure(error);
        return {
          status: failure.status,
          code: failure.code,
          error: failure.message.slice(0, 160)
        };
      }
      return {
        status: Number(error && error.httpStatus) || 500,
        code: String(error && error.code || 'BW_NATIVE_SYNC_BATCH_LOCAL'),
        error: String(error && error.message || error || '本地同步子请求失败').slice(0, 160)
      };
    });
  }

  function nativeSyncBatchFetchWithLease(input, init, writerLease) {
    return requestObject(
      input, init,
      ['contract', 'ownerNamespace', 'generation', 'ops'],
      ['contract', 'ownerNamespace', 'ops'],
      'BW_NATIVE_SYNC_BATCH_REQUEST',
      16 * 1024 * 1024
    ).then(function (body) {
      if (body.contract !== NATIVE_SYNC_BATCH_CONTRACT ||
          !/^acct-v1-[a-f0-9]{64}$/.test(String(body.ownerNamespace || '')) ||
          !Array.isArray(body.ops) || body.ops.length > 100) {
        throw outgoingRequestError(
          '原生命令队列合同无效', 'BW_NATIVE_SYNC_BATCH_REQUEST', 400
        );
      }
      var account = runtimeRoot.accountContext;
      if (!account || account.CONTRACT !== 'account-context/1' ||
          typeof account.lease !== 'function' ||
          typeof account.assertCurrent !== 'function') {
        throw outgoingRequestError(
          '账户上下文尚未就绪', 'BW_NATIVE_SYNC_BATCH_ACCOUNT', 409
        );
      }
      var lease = account.lease();
      if (body.ownerNamespace !== lease.namespace ||
          (Object.prototype.hasOwnProperty.call(body, 'generation') &&
            strictInteger(
              body.generation, 0, Number.MAX_SAFE_INTEGER,
              'generation', 'BW_NATIVE_SYNC_BATCH_REQUEST'
            ) !== Number(lease.generation))) {
        throw outgoingRequestError(
          '命令队列账户或租约已变化', 'BW_NATIVE_SYNC_BATCH_ACCOUNT', 409
        );
      }
      var results = new Array(body.ops.length);
      var sequence = Promise.resolve();
      body.ops.forEach(function (op, index) {
        sequence = sequence.then(function () {
          account.assertCurrent(lease);
          assertNativePDFWriterLease(writerLease);
          return nativeSyncBatchOperation(
            op, requestSignal(input, init), writerLease
          );
        }).then(function (result) {
          results[index] = result;
        });
      });
      return sequence.then(function () {
        account.assertCurrent(lease);
        assertNativePDFWriterLease(writerLease);
        return jsonResponse({
          ok: true,
          contract: NATIVE_SYNC_BATCH_CONTRACT,
          ownerNamespace: lease.namespace,
          generation: lease.generation,
          results: results
        });
      });
    }).catch(function (error) {
      return outgoingFailureResponse(
        error, 'BW_NATIVE_SYNC_BATCH_REQUEST', 400
      );
    });
  }

  function nativeSyncBatchFetch(input, init) {
    return withNativePDFWriter('sync-batch', function (writerLease) {
      return nativeSyncBatchFetchWithLease(input, init, writerLease);
    }).catch(function (error) {
      return outgoingFailureResponse(
        error, 'BW_NATIVE_SYNC_BATCH_REQUEST', 409
      );
    });
  }

  function localFetch(input, init, mutationGatePassed, writerLease) {
    var url = urlOf(input);
    var method = methodOf(input, init);
    if (!url || url.origin !== root.location.origin) return originalFetch(input, init);
    var isReaderAPI = url.pathname.indexOf('/pdf/api/') === 0 ||
      url.pathname.indexOf('/api/assistant/') === 0;
    if (!isReaderAPI) return originalFetch(input, init);
    var route;
    try {
      route = declaredNativeInterface(url.pathname, method);
    } catch (error) {
      return Promise.resolve(nativeInterfaceErrorResponse(error));
    }
    if (mutationGatePassed && writerLease) {
      try { assertNativePDFWriterLease(writerLease); }
      catch (error) {
        return Promise.resolve(outgoingFailureResponse(
          error, 'BW_NATIVE_PDF_WRITER_STALE', 409
        ));
      }
    }
    if (!mutationGatePassed && nativeInterfaceSurface === 'pdf' &&
        method !== 'GET' && PDF_MUTATION_WRITE_PATHS.has(url.pathname)) {
      return withNativePDFWriter(url.pathname, function (lease) {
        assertNativePDFWriterLease(lease);
        return localFetch(input, init, true, lease);
      }).catch(function (error) {
        return outgoingFailureResponse(
          error, 'BW_NATIVE_PDF_MUTATION_BUSY', 409
        );
      });
    }
    if (!mutationGatePassed && nativeInterfaceSurface === 'pdf' &&
        route.owner === 'pi') {
      return assertNoNativePDFMutationJournal().then(function () {
        return localFetch(input, init, true);
      }).catch(function (error) {
        return outgoingFailureResponse(
          error, 'BW_NATIVE_PDF_MUTATION_BUSY', 409
        );
      });
    }
    if (url.pathname === '/pdf/api/sync-batch' && method === 'POST') {
      return nativeSyncBatchFetch(input, init);
    }
    if (url.pathname === '/pdf/api/job-status' && method === 'GET' &&
        /^npj_[a-f0-9]{24}$/.test(String(url.searchParams.get('id') || ''))) {
      return localNativePDFJobStatus(url);
    }
    // Hybrid compatibility routes: the local fact is authoritative for the
    // independent Windows context WSS while the manifest-authorized Pi branch
    // keeps the legacy HTTP readers current when available.
    if (url.pathname === '/pdf/api/context-sync') {
      return nativeContextSyncFetch(input, init, url, method, route);
    }
    if (url.pathname === '/pdf/api/active-reading') {
      return nativeActiveReadingFetch(input, init, url, method, route);
    }
    if (url.pathname === '/api/assistant/voice-page-text' && method === 'GET') {
      return nativeVoicePageText(url);
    }
    if (url.pathname === '/pdf/api/book-crop' &&
        (method === 'GET' || method === 'POST')) {
      return localBookCrop(input, init, url, method);
    }
    if (url.pathname === '/pdf/api/epub-search' && method === 'GET') {
      return localEPUBSearch(url);
    }
    if (url.pathname === '/pdf/api/page-overlay' && method === 'GET') {
      return nativePageOverlayFetch(input, init, url, route);
    }
    if (url.pathname === '/pdf/api/page-translate' && method === 'GET') {
      return nativePageTranslate(input, init, url);
    }
    if (route.owner === 'native') {
      // App-owned document-start bridges (for example bwNativeLocalNotes)
      // are already captured in originalFetch. A native route must use that
      // exact bridge and must never fall through to Pi.
      return originalFetch(input, init);
    }
    if (route.owner === 'local') {
      if (url.pathname === '/pdf/api/book-meta') {
        return localJSONRoute(function () {
          localFileQuery(url, ['file'], ['file'], 'BW_LOCAL_BOOK_META_REQUEST');
          return originalFetch(
            basePath + '/native-api/book-meta?book=' + encodeURIComponent(bookId),
            {
              method: 'GET', cache: 'no-store', credentials: 'omit',
              headers: { 'Accept': 'application/json' },
              signal: requestSignal(input, init)
            }
          );
        }, 'BW_LOCAL_BOOK_META_FAILED');
      }
      if (url.pathname === '/pdf/api/page-image') {
        try {
          localFileQuery(
            url,
            ['file', 'page', 'w', 'v', 'sharp'],
            ['file'],
            'BW_LOCAL_PAGE_IMAGE_REQUEST'
          );
          // Keep the original same-origin URL so direct <img> loads and fetch
          // prewarming share one browser-cache key. The loopback server verifies
          // the shell Referer/current opaque book and renders with PDFKit.
          return originalFetch(input, init);
        } catch (error) {
          return Promise.resolve(nativeInterfaceErrorResponse(error));
        }
      }
      var epub = handleEPUB(url);
      if (epub) return epub;
      var state = handleLocalState(
        input, init, url, method, mutationGatePassed
      );
      if (state) return state;
      return Promise.resolve(nativeInterfaceErrorResponse(new RuntimeError(
        '原生 Reader 接口已声明但缺少处理器：' + url.pathname,
        'BW_NATIVE_INTERFACE_HANDLER_MISSING',
        { path: url.pathname, owner: route.owner }
      )));
    }
    if (route.owner === 'pi') {
      if (nativeInterfaceSurface === 'pdf' && url.pathname === '/api/assistant/chat') {
        return nativePDFChatFetch(input, init, url, route);
      }
      if (nativeInterfaceSurface === 'pdf' && url.pathname === '/api/assistant/voice-tool') {
        return nativePDFVoiceToolFetch(input, init, url, route);
      }
      if (nativeInterfaceSurface === 'epub' && url.pathname === '/api/assistant/chat') {
        return nativeEPUBGenericChatFetch(input, init, url, route);
      }
      if (nativeInterfaceSurface === 'epub' && url.pathname === '/api/assistant/voice-tool') {
        return nativeEPUBGenericVoiceToolFetch(input, init, url, route);
      }
      if (nativeInterfaceSurface === 'epub' && url.pathname === '/pdf/api/epub-assistant') {
        return nativeEPUBAssistantFetch(input, init, url, route).catch(function (error) {
          var failure = nativePiFailure(error);
          return jsonResponse({ ok: false, code: failure.code, error: failure.message }, failure.status);
        });
      }
      if (nativeInterfaceSurface === 'epub' && url.pathname === '/pdf/api/epub-action') {
        return nativeEPUBActionFetch(input, init, url, route);
      }
      return nativePiFetch(input, init, route).catch(function (error) {
        var failure = nativePiFailure(error);
        return jsonResponse({
          ok: false,
          code: failure.code,
          error: failure.message
        }, failure.status);
      });
    }
    return originalFetch(input, init);
  }

  // Book pixels and native text already live behind the App's capability URL.
  // They must remain readable while IndexedDB-backed annotations/preferences
  // are opening or recovering.  Keep this list intentionally small: every
  // route here is read-only and its handler does not touch stores/router.
  var NATIVE_DOCUMENT_FAST_READS = new Set([
    '/pdf/api/book-meta',
    '/pdf/api/page-image',
    '/pdf/api/page-chars',
    '/pdf/api/page-text-status',
    '/pdf/api/search'
  ]);

  function nativeDocumentFastFetch(input, init) {
    var url = urlOf(input);
    var method = methodOf(input, init);
    if (!url || url.origin !== root.location.origin || method !== 'GET' ||
        !NATIVE_DOCUMENT_FAST_READS.has(url.pathname)) return null;
    return localFetch(input, init);
  }

  function installFetchBridge() {
    root.fetch = function (input, init) {
      var fast = nativeDocumentFastFetch(input, init);
      if (fast) return fast;
      return bootPromise.then(function () { return localFetch(input, init); });
    };
    if (root.navigator) {
      try {
        root.navigator.sendBeacon = function (url, data) {
           var parsed = urlOf(url);
           if (parsed && parsed.origin === root.location.origin && [
             '/pdf/api/reading-pos', '/pdf/api/ink', '/pdf/api/epub-ink',
             '/pdf/api/active-reading', '/pdf/api/read-dwell'
           ].indexOf(parsed.pathname) >= 0) {
            var body = data;
            if (typeof Blob !== 'undefined' && data instanceof Blob) {
              data.text().then(function (text) {
                // @interaction reading.position.save
                root.fetch(parsed.href, { method: 'POST', body: text, headers: { 'Content-Type': 'application/json' }, keepalive: true }).catch(function () {});
               });
             } else {
               // @interaction reading.position.save
               root.fetch(parsed.href, {
                 method: 'POST', body: body,
                 headers: parsed.pathname === '/pdf/api/active-reading'
                   ? { 'Content-Type': 'application/json' } : undefined,
                 keepalive: true
               }).catch(function () {});
             }
            return true;
          }
          return originalSendBeacon ? originalSendBeacon(url, data) : false;
        };
      } catch (_) {}
    }
  }

  var api = {
    contract: CONTRACT,
    owner: 'native-app',
    deviceId: deviceId,
    deviceFamilyId: deviceId,
    localBookId: bookId,
    ready: function () { return bootPromise; },
    savePDFHighlight: function (payload) {
      var allowed = new Set([
        'file', 'id', 'page', 'rects', 'color', 'text', 'note', 'kind',
        'sentence', 'body', 'page_w', 'page_h'
      ]);
      if (!payload || typeof payload !== 'object' || Array.isArray(payload) ||
          Object.keys(payload).some(function (key) { return !allowed.has(key); })) {
        return Promise.reject(new RuntimeError(
          '本机精确高亮参数无效', 'BW_LOCAL_HIGHLIGHT_DIRECT'
        ));
      }
      var body = clone(payload);
      return bootPromise.then(function () {
        return withNativePDFWriter('assistant-exact-highlight', function (lease) {
          assertNativePDFWriterLease(lease);
          // 精确工具携带稳定 mutation id，CAS 冲突可安全重试。不要排在普通
          // 高亮的共享 Promise 队列之后：旧 WebKit 写入若失联，队列会永久
          // 悬住并让每次语音高亮都只得到 20 秒回执超时。
          return persistLocalPDFHighlight(
            body, 'BW_LOCAL_HIGHLIGHT_DIRECT', true
          );
        });
      });
    },
    status: function () {
      return {
        contract: CONTRACT, owner: 'native-app', state: bootState,
        error: bootError ? { code: bootError.code || 'BW_LOCAL_RUNTIME', message: bootError.message } : null,
        localBookId: bookId, storageOrigin: root.location.origin,
        syncBound: !!syncControl, preferencesBound: !!preferences,
        interfaceManifest: nativeInterfaceManifest ? {
          contract: nativeInterfaceManifest.contract,
          surface: nativeInterfaceSurface,
          routeCount: nativeInterfaceManifest.routes.length
        } : null
      };
    },
    localStores: function () { return stores; },
    storageRouter: function () { return router; },
    storage: function () { return router; },
    preferenceStore: function () { return preferences; },
    bookUserState: bookUserStateAPI,
    dictionaryFallbackCache: dictionaryFallbackCacheAPI,
    publishPageContext: publishLocalPageContext,
    documentHost: function () {
      return root.RC && root.RC.documentHost && root.RC.documentHost.current
        ? root.RC.documentHost.current() : null;
    },
    piFetch: nativePiFetch,
    syncBootstrapContext: function () {
      return {
        contract: BOOTSTRAP_CONTRACT,
        deviceId: deviceId,
        deviceFamilyId: deviceId,
        ready: false, reason: 'BW_NATIVE_SYNC_BOOTSTRAP_UNAVAILABLE',
        piFetch: nativePiFetch
      };
    },
    bindSyncControl: function (control) {
      if (syncControl && syncControl !== control) {
        throw new RuntimeError('本机同步控制器已绑定', 'BW_NATIVE_SYNC_DUPLICATE');
      }
      if (!control || control.contract !== 'sync-conflict-control/1' || control.owner !== 'native-app' || typeof control.syncNow !== 'function') {
        throw new RuntimeError('本机同步控制器无效', 'BW_NATIVE_SYNC_CONTROL');
      }
      syncControl = control;
      return true;
    },
    syncControl: function () { return syncControl; },
    syncNow: function (request) {
      if (!request || request.contract !== SYNC_REQUEST_CONTRACT || !/^[A-Za-z0-9._:-]{1,160}$/.test(String(request.requestId || ''))) {
        return Promise.reject(new RuntimeError('Pi 同步请求无效', 'BW_NATIVE_SYNC_REQUEST'));
      }
      if (!syncControl) return Promise.reject(new RuntimeError('Pi 同步尚未准备好', 'BW_NATIVE_SYNC_BOOTSTRAP_UNAVAILABLE'));
      return syncControl.syncNow(request);
    }
  };
  runtimeRoot.nativeLocalRuntime = api;

  // The shell deliberately loads this bootstrap before the legacy Reader
  // scripts. Its storage dependencies are later synchronous scripts, so the
  // storage boot begins only after parsing completes. The immutable native
  // document surface is classified now, allowing its bounded read-only routes
  // to paint the book without waiting for annotation storage.
  var resolveBoot;
  var rejectBoot;
  var bootPromise = new Promise(function (resolve, reject) {
    resolveBoot = resolve;
    rejectBoot = reject;
  });
  root.__BW_READER_RUNTIME__ = api;
  try {
    nativeInterfaceManifest = nativeInterfaceManifestFromRoot();
    nativeInterfaceSurface = nativeInterfacePageSurface();
  } catch (error) {
    blockingFailure(error);
    rejectBoot(error);
  }
  installFetchBridge();

  function beginBoot() {
    if (bootState !== 'starting') return;
    try {
      if (typeof root.dlog === 'function') root.dlog('本机启动:连接本地状态');
      stores = createStores();
      router = createRouter(stores);
      // Real state reads below are the readiness proof. A destructive
      // write/read/delete probe on every book switch duplicated real work and
      // could itself queue behind the page being replaced.
      Promise.resolve().then(function () {
        return recoverNativePDFMutationOnBoot();
      }).then(function () {
        if (typeof root.dlog === 'function') root.dlog('本机启动:PDF 恢复检查已完成');
        return attachPreferenceStore();
      }).then(function () {
        bootState = 'ready';
        if (typeof root.dlog === 'function') root.dlog('本机启动:运行时已就绪');
        try {
          root.dispatchEvent(new CustomEvent('bw:native-local-runtime-ready', {
            detail: api.status()
          }));
        } catch (_) {}
        resolveBoot(api);
      }).catch(function (error) {
        if (typeof root.dlog === 'function') {
          root.dlog('本机启动失败:' + String(error && error.code || error), '#ff6b6b');
        }
        blockingFailure(error);
        rejectBoot(error);
      });
    } catch (error) {
      blockingFailure(error);
      rejectBoot(error);
    }
  }
  if (root.document && root.document.readyState === 'loading') {
    root.document.addEventListener('DOMContentLoaded', beginBoot, { once: true });
  } else {
    beginBoot();
  }

  root.addEventListener('pagehide', function () {
    blobURLs.forEach(function (url) { try { URL.revokeObjectURL(url); } catch (_) {} });
    blobURLs = [];
    epubSharedResourceBudget = null;
    epubActualTextBytes = 0;
    epubTextQueue = Promise.resolve();
    epubTextByPath = Object.create(null);
  });
})(typeof globalThis !== 'undefined' ? globalThis : window);
