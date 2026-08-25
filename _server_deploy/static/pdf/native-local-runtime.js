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
  var NATIVE_READER_UNDO_RESULT_CONTRACT = 'reader-native-undo-result/1';
  var LOCAL_PAGE_CARDS_CONTRACT = 'reader-local-page-cards/1';
  var LOCAL_PAGE_CARD_SOURCE_CONTRACT = 'reader-local-page-card-source/1';
  var LOCAL_PAGE_CARD_PROJECTION_CONTRACT = 'reader-local-page-card-projection/1';
  var NATIVE_PAGE_CARD_PROJECTION_CONTRACT = 'reader-native-page-card-projection/1';
  // Normal and long cards are complete in the page snapshot.  This is only a
  // corrupt-data/programming-error fence; the direct bridge still owns the
  // smaller total-message byte budget.
  var LOCAL_PAGE_CARD_CONTEXT_LIMIT = 100000;
  var LOCAL_PAGE_CARD_CONTEXT_MAX_WIRE_BYTES = 200 * 1024;
  var LOCAL_PAGE_CARD_CONTEXT_FRAGMENT = 2000;
  var LOCAL_PAGE_CARD_SNAPSHOT_CACHE_REVISIONS = 4;
  var LOCAL_PAGE_CARD_SNAPSHOT_CACHE_BYTES = 4 * 1024 * 1024;
  var nativePageCardSnapshotCache = new Map();
  var LOCAL_PAGE_CONTEXT_TEXT_LIMIT = 220000;
  var LOCAL_PAGE_CARD_REPLACEMENT_FORMAT =
    'application/vnd.bw-reader.card-replacement+json;version=1';
  var LOCAL_NOTES_CHANGED_CONTRACT = 'reader-local-notes-changed/1';
  var LOCAL_NOTES_CHANGED_EVENT = 'bw:native-document-notes-changed';
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
      return nativePDFRecoverPendingPageCardJournals(lease);
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

  var localNotesChangeRevision = 0;
  function announceLocalNotesChanged(source) {
    localNotesChangeRevision += 1;
    try {
      root.dispatchEvent(new CustomEvent(LOCAL_NOTES_CHANGED_EVENT, {
        detail: {
          contract: LOCAL_NOTES_CHANGED_CONTRACT,
          file: localFileRef(),
          revision: localNotesChangeRevision,
          source: String(source || 'write')
        }
      }));
    } catch (_) {}
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
          if (kind === 'document-notes-legacy') {
            // 只在 notes + 两个派生索引的事务确实提交后发信号。删除失败或 CAS
            // 仍未知时不发，旧 page.context 因而继续保留，不能提前把 CARD 擦掉。
            announceLocalNotesChanged('mutation');
          }
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

  // ---- 高亮集合门面：一条高亮一条记录 + 墓碑 + meta 序/CAS ----
  //
  // 两节点复制（references/reader-two-node-replication.md §8.5 前提 B）要求
  // 高亮以"独立记录 + 删除留墓碑"存储，事件才有可传播的形状。这里只拆
  // 存储记录层：HTTP 路由与所有消费方仍然拿到 (整册数组, 集合修订号)，
  // 由门面从 per-item 记录物化——命令出箱回放白名单因此零改动。
  //
  // 形状：
  //   条目记录  collection 'native-<kind>-items'
  //             id 'native-<kind>-item-v1:<bookId.length>:<bookId>:<itemId>'
  //             payload = 高亮对象；墓碑 payload = { id, deleted:true, time }
  //   meta 记录 kind '<kind>-split-meta'，payload = { order: [itemId…] }
  //             它的 rev 就是整个集合的修订号（每次写集合都随批 CAS +1，
  //             与旧的整册记录 rev 算术逐位一致，助手 undo 的
  //             expectedRevision 契约因此原样成立）；它的存在同时是
  //             启动迁移的完成标记。
  //   顺序保存在 meta.order 里——物化数组与拆分前逐位同序，
  //   user-state 摘要不因拆分而漂移。
  var HIGHLIGHT_SPLIT_KINDS = { 'document-highlights': true, 'epub-highlights': true };
  function highlightMetaKind(kind) { return kind + '-split-meta'; }
  function highlightItemsCollection(kind) { return 'native-' + kind + '-items'; }
  function highlightItemRecordId(kind, itemId) {
    return 'native-' + kind + '-item-v1:' + bookId.length + ':' + bookId + ':' + itemId;
  }

  function listHighlightItemRecords(kind) {
    var collection = highlightItemsCollection(kind);
    var seen = new Set();
    var output = [];
    function page(afterId) {
      var query = {
        documentId: bookId, includeDeleted: true,
        orderBy: 'id', limit: 1000
      };
      if (afterId != null) query.afterId = afterId;
      return stores.document.list(collection, query).then(function (records) {
        var added = 0;
        var lastId = afterId == null ? '' : String(afterId);
        (Array.isArray(records) ? records : []).forEach(function (record) {
          var value = record && record.value;
          if (!value || typeof value !== 'object') return;
          // 真实存储按 query.documentId 过滤；测试替身可能忽略 query，
          // 这里必须再筛一次，否则会读进别的书的条目。
          if (String(value.documentId || '') !== bookId) return;
          var recordId = String(value.id || '');
          if (!recordId || seen.has(recordId)) return;
          seen.add(recordId);
          added += 1;
          output.push({ value: value, rev: Number(record.rev || 0) });
          if (recordId > lastId) lastId = recordId;
        });
        // 终止判据两条都要：真实存储按页翻完（<limit），
        // 忽略分页参数的替身靠"没有新条目"终止。
        if (!added || (Array.isArray(records) && records.length < 1000)) {
          return output;
        }
        return page(lastId);
      });
    }
    return page(null);
  }

  function readHighlightCollection(kind, queryOptions) {
    return Promise.all([
      storedStateRecord(
        stores.document, highlightMetaKind(kind), 'documentId', bookId,
        null, queryOptions
      ),
      listHighlightItemRecords(kind)
    ]).then(function (values) {
      var meta = values[0];
      var order = meta.payload && Array.isArray(meta.payload.order)
        ? meta.payload.order.map(String) : [];
      var itemsById = Object.create(null);
      var tombstones = Object.create(null);
      values[1].forEach(function (record) {
        var payload = record.value.payload;
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
        var itemId = String(payload.id || '');
        if (!itemId) return;
        if (payload.deleted === true) {
          tombstones[itemId] = clone(payload);
          return;
        }
        itemsById[itemId] = clone(payload);
      });
      var used = new Set();
      var orderedIds = [];
      order.forEach(function (id) {
        if (itemsById[id] && !used.has(id)) { used.add(id); orderedIds.push(id); }
      });
      // meta.order 与条目记录理论上同批提交、不会分叉；这里仍把序里没有的
      // 存活条目按 id 排序补在末尾，宁可顺序退化也不静默丢数据。
      Object.keys(itemsById).sort().forEach(function (id) {
        if (!used.has(id)) { used.add(id); orderedIds.push(id); }
      });
      return {
        payload: orderedIds.map(function (id) { return clone(itemsById[id]); }),
        rev: meta.rev,
        state: { itemsById: itemsById, tombstones: tombstones, metaRev: meta.rev }
      };
    });
  }

  function highlightItemMutation(kind, payload, suffix) {
    var itemId = String(payload.id);
    return {
      operation: 'put',
      collection: highlightItemsCollection(kind),
      value: {
        id: highlightItemRecordId(kind, itemId),
        documentId: bookId,
        payload: clone(payload),
        updatedAt: Date.now()
      },
      options: { mutationId: 'native-' + kind + '-item-' + suffix + '-' + itemId }
    };
  }

  function highlightCollectionMutations(kind, state, nextItems, suffix) {
    if (!Array.isArray(nextItems)) {
      throw new RuntimeError('高亮集合必须是数组', 'BW_LOCAL_STATE_MUTATION');
    }
    var mutations = [];
    var nextIds = [];
    var seen = new Set();
    nextItems.forEach(function (item) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        throw new RuntimeError('高亮记录不是对象', 'BW_LOCAL_STATE_MUTATION');
      }
      var itemId = String(item.id || '');
      // deleted 是墓碑的保留字段；带着它的"存活"条目宁可当场拒绝，
      // 也不能静默存成一条读不回来的记录。
      if (!itemId || itemId.length > 200 || seen.has(itemId) || item.deleted === true) {
        throw new RuntimeError('高亮记录 id 无效或重复', 'BW_LOCAL_STATE_MUTATION');
      }
      seen.add(itemId);
      nextIds.push(itemId);
      var prior = state.itemsById[itemId];
      if (!prior || canonicalJSONString(prior) !== canonicalJSONString(item)) {
        mutations.push(highlightItemMutation(kind, item, suffix));
      }
    });
    Object.keys(state.itemsById).forEach(function (itemId) {
      if (!seen.has(itemId)) {
        mutations.push(highlightItemMutation(kind, {
          id: itemId, deleted: true, time: nowSeconds()
        }, suffix));
      }
    });
    // meta 永远随批写入并 CAS：它的 rev 就是集合修订号，
    // 每次写集合 +1 的算术必须与旧的整册记录逐位一致。
    mutations.push(stateRecordMutation(
      highlightMetaKind(kind), { order: nextIds }, suffix + '-meta', state.metaRev
    ));
    return mutations;
  }

  function mutateHighlightCollectionNow(kind, mutator, batchOptions) {
    var attempts = 0;
    function attempt() {
      attempts += 1;
      return readHighlightCollection(kind, batchOptions).then(function (current) {
        var outcome = mutator(clone(current.payload));
        if (!outcome || !Object.prototype.hasOwnProperty.call(outcome, 'payload')) {
          throw new RuntimeError(
            '本机文档状态修改器响应无效', 'BW_LOCAL_STATE_MUTATION'
          );
        }
        var mutations = highlightCollectionMutations(
          kind, current.state, outcome.payload, randomHex(12)
        );
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

  function mutateHighlightCollection(kind, mutator, batchOptions) {
    return serializeLocalStateMutation('document', kind, function () {
      return mutateHighlightCollectionNow(kind, mutator, batchOptions);
    });
  }

  // 启动迁移：整册数组 → per-item 记录。meta 记录的存在即完成标记；
  // 单批原子提交（要么全成要么全无），mutationId 固定所以重试即重放；
  // legacy 整册记录刻意保留不清（迁移一半崩了重启续跑，数据不丢）。
  function migrateHighlightSplitOnBoot() {
    return Object.keys(HIGHLIGHT_SPLIT_KINDS).reduce(function (chain, kind) {
      return chain.then(function () {
        return storedStateRecord(
          stores.document, highlightMetaKind(kind), 'documentId', bookId, null
        ).then(function (meta) {
          if (meta.rev > 0) return null;
          return readState(kind, []).then(function (legacy) {
            var items = Array.isArray(legacy) ? legacy : [];
            // 空集合不写 meta：首写时以 ifRev=0 创建（rev 从 1 起），
            // 修订号算术与旧的整册记录逐位一致（旧测试断言 rev===1）。
            // 代价只是无高亮的书每次开书多两次 miss 读。
            if (!items.length) return null;
            var mutations = [];
            var order = [];
            var seen = new Set();
            items.forEach(function (item) {
              if (!item || typeof item !== 'object' || Array.isArray(item)) return;
              var itemId = String(item.id || '');
              if (!itemId || itemId.length > 200 || seen.has(itemId) ||
                  item.deleted === true) return;
              seen.add(itemId);
              order.push(itemId);
              mutations.push({
                operation: 'put',
                collection: highlightItemsCollection(kind),
                value: {
                  id: highlightItemRecordId(kind, itemId),
                  documentId: bookId,
                  payload: clone(item),
                  updatedAt: Date.now()
                },
                options: { mutationId: 'hl-split:' + kind + ':' + bookId + ':' + itemId }
              });
            });
            if (order.length !== items.length && typeof root.dlog === 'function') {
              root.dlog(
                '高亮拆分迁移丢弃了 ' + (items.length - order.length) +
                  ' 条无 id 条目:' + kind,
                '#ff6b6b'
              );
            }
            mutations.push({
              operation: 'put',
              collection: 'native-' + highlightMetaKind(kind),
              value: {
                id: stateId(highlightMetaKind(kind)),
                documentId: bookId,
                payload: { order: order },
                updatedAt: Date.now()
              },
              options: {
                mutationId: 'hl-split:' + kind + ':' + bookId + ':meta',
                ifRev: 0
              }
            });
            return stores.document.batch(mutations);
          });
        });
      });
    }, Promise.resolve());
  }

  // ---- 两节点复制：App 侧命令出箱（规格步骤 3）----
  //
  // 用户在 App 上的高亮写入本地立刻生效（绝不等网络），随后把"操作"入队，
  // 服务端可达时经 Direct 桥推送（rc-computer-voice 暴露的
  // __BW_REPLICATION_PUSH__，一帧一命令）。信封 replication-command/1 的
  // 权威定义在 Windows 侧 replication_command_ledger.py —— 这里是发送端
  // 副本（contract-sites: replication-command-envelope）。
  //
  // 身份（前提 A 的 App 半边）：App 铸 replicationBookId（内容无关、铸后
  // 不变），与配对公告 op(/replication/pair) **同一批原子落库** —— 链接
  // 记录与公告要么都在要么都不在。POST 的 body 是**已落库的完整条目**
  // （发送端约定，见规格 §9）；投递 at-least-once，服务端按 mutationId
  // 幂等，所以推成功才删队列条目（先投后删）。
  var REPLICATION_LINK_KIND = 'replication-link';
  var REPLICATION_OUTBOX_COLLECTION = 'native-replication-outbox';
  var REPLICATION_PAIR_URL = '/replication/pair';
  var REPLICATION_RESYNC_URL = '/replication/resync';
  var REPLICATION_DRAIN_INTERVAL_MS = 30000;
  var replicationSeqCounter = 0;
  var replicationDraining = false;
  var replicationPushMissingLogged = false;

  function replicationEligible() {
    // 欢迎书不是用户内容；其 id 也过不了对端的 peerBookId 闸。
    return bookId !== 'localbook-welcome';
  }

  function buildReplicationEnvelope(replicationBookId, url, method, body) {
    return {
      contract: 'replication-command/1',
      deviceId: deviceId,
      replicationBookId: replicationBookId,
      actor: 'user',
      op: {
        mutationId: 'mut-v2-' + randomHex(16),
        url: url,
        method: method,
        body: clone(body)
      }
    };
  }

  function replicationOutboxItemMutation(envelope, suffix) {
    replicationSeqCounter += 1;
    // 字典序 ≈ 入队序：毫秒时间戳 + 实例内单调计数。投递按这个序。
    var sequence = 'ro-' +
      Date.now().toString(16).padStart(12, '0') + '-' +
      replicationSeqCounter.toString(16).padStart(6, '0');
    return {
      operation: 'put',
      collection: REPLICATION_OUTBOX_COLLECTION,
      value: {
        id: bookId + ':' + sequence,
        documentId: bookId,
        payload: { envelope: clone(envelope) },
        updatedAt: Date.now()
      },
      options: { mutationId: 'native-replication-outbox-' + suffix }
    };
  }

  // 全文件 contentSha256:两节点复制的内容会合材料(App 重装重铸 repbook
  // 后,服务端靠它把新身份接回旧数据)。任何失败都折成 null —— 会合材料
  // 是增强,v1 公告本来就不带;它坏了不该拦配对本身。
  function fetchReplicationContentSha() {
    return Promise.resolve().then(function () {
      return nativePageTextRequest('book-identity', {});
    }).then(function (reply) {
      var sha = reply && reply.contentSha256;
      return typeof sha === 'string' && /^[a-f0-9]{64}$/.test(sha)
        ? sha : null;
    }).catch(function () { return null; });
  }

  function enqueueReplicationCommand(url, method, body) {
    if (!replicationEligible()) return Promise.resolve(false);
    return serializeLocalStateMutation('document', 'replication-outbox', function () {
      return storedStateRecord(
        stores.document, REPLICATION_LINK_KIND, 'documentId', bookId, null
      ).then(function (link) {
        if (link.payload && link.payload.replicationBookId) {
          return link.payload.replicationBookId;
        }
        var minted = 'repbook-' + randomHex(16);
        var displayName = String(
          (root.document && root.document.title) || bookId
        ).slice(0, 512) || bookId;
        var suffix = randomHex(8);
        return fetchReplicationContentSha().then(function (contentSha) {
          var pairBody = {
            peerBookId: bookId,
            replicationBookId: minted,
            displayName: displayName
          };
          // 缺席而不是 null 占位:对端 body 键集校验按可选字段处理。
          if (contentSha) pairBody.contentSha256 = contentSha;
          return stores.document.batch([
            stateRecordMutation(
              REPLICATION_LINK_KIND,
              { replicationBookId: minted, pairedAt: nowSeconds() },
              suffix + '-link',
              link.rev
            ),
            replicationOutboxItemMutation(
              buildReplicationEnvelope(minted, REPLICATION_PAIR_URL, 'POST', pairBody),
              suffix + '-pair'
            )
          ]);
        }).then(function () { return minted; });
      }).then(function (replicationBookId) {
        var envelope = buildReplicationEnvelope(replicationBookId, url, method, body);
        // 超单帧的信封由传输层分片（rc-computer-voice 的 chunk 协议）；
        // 这里只拦真正超账本层上限（6MB）的命令 —— 留 1MB 余量提前拒
        // 并出声，否则命令会在队列里每轮被对端拒、永远赖着不走。
        if (utf8(JSON.stringify(envelope)).byteLength > 5 * 1024 * 1024) {
          throw new RuntimeError(
            '复制命令超过信封上限', 'BW_REPLICATION_ENVELOPE_TOO_LARGE'
          );
        }
        return stores.document.batch([
          replicationOutboxItemMutation(envelope, randomHex(8))
        ]);
      });
    }).then(function () {
      scheduleReplicationDrain(500);
      return true;
    }).catch(function (error) {
      // 入队失败绝不能影响已经完成的本地写 —— 但必须出声，
      // 静默丢队列就是静默分叉。
      if (typeof root.dlog === 'function') {
        root.dlog('复制命令入队失败:' + String(error && error.code || error), '#ff6b6b');
      }
      return false;
    });
  }

  function listReplicationOutbox() {
    var seen = new Set();
    var output = [];
    function page(afterId) {
      var query = { documentId: bookId, orderBy: 'id', limit: 1000 };
      if (afterId != null) query.afterId = afterId;
      return stores.document.list(REPLICATION_OUTBOX_COLLECTION, query)
        .then(function (records) {
          var added = 0;
          var lastId = afterId == null ? '' : String(afterId);
          (Array.isArray(records) ? records : []).forEach(function (record) {
            var value = record && record.value;
            if (!value || String(value.documentId || '') !== bookId) return;
            var recordId = String(value.id || '');
            if (!recordId || seen.has(recordId)) return;
            seen.add(recordId);
            added += 1;
            output.push(value);
            if (recordId > lastId) lastId = recordId;
          });
          if (!added || (Array.isArray(records) && records.length < 1000)) {
            output.sort(function (a, b) {
              return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
            });
            return output;
          }
          return page(lastId);
        });
    }
    return page(null);
  }

  var replicationDrainHadBacklog = false;
  function drainReplicationOutbox() {
    if (replicationDraining || !replicationEligible()) return Promise.resolve();
    replicationDraining = true;
    replicationDrainHadBacklog = false;
    return listReplicationOutbox().then(function (records) {
      if (!records.length) return null;
      replicationDrainHadBacklog = true;
      var push = root.__BW_REPLICATION_PUSH__;
      if (typeof push !== 'function') {
        if (!replicationPushMissingLogged && typeof root.dlog === 'function') {
          replicationPushMissingLogged = true;
          root.dlog('复制推送通道未接线,命令留在队列', '#ffb020');
        }
        return null;
      }
      var envelopes = records.map(function (record) {
        return clone(record.payload.envelope);
      });
      return Promise.resolve(push(envelopes)).then(function (results) {
        var accepted = new Set();
        (Array.isArray(results) ? results : []).forEach(function (item) {
          if (item && item.outcome === 'accepted') {
            accepted.add(String(item.mutationId || ''));
          }
        });
        // 服务端已持久（fsync）确认的才删；其余留队下轮重投，
        // 重复投递由服务端 mutationId 幂等吸收。
        var settled = records.filter(function (record) {
          return accepted.has(String(
            record.payload.envelope.op.mutationId || ''
          ));
        });
        if (settled.length === records.length) {
          replicationDrainHadBacklog = false;
        }
        var chain = Promise.resolve();
        settled.forEach(function (record) {
          chain = chain.then(function () {
            return stores.document.remove(
              REPLICATION_OUTBOX_COLLECTION, record.id
            );
          });
        });
        return chain;
      });
    }).catch(function (error) {
      replicationDrainHadBacklog = true;
      if (typeof root.dlog === 'function') {
        root.dlog('复制推送失败,下轮重试:' + String(error && error.code || error), '#ffb020');
      }
    }).then(function () {
      replicationDraining = false;
      // 没有常驻 interval：队列仍有积压才重排下一轮。
      // 队列空则彻底静默（也让测试进程能自然退出）。
      if (replicationDrainHadBacklog) {
        scheduleReplicationDrain(REPLICATION_DRAIN_INTERVAL_MS);
      } else {
        maybeReconcileReplication();
      }
    });
  }

  var replicationDrainTimer = null;
  function scheduleReplicationDrain(delayMs) {
    if (replicationDrainTimer != null) return;
    var timer = root.setTimeout(function () {
      replicationDrainTimer = null;
      drainReplicationOutbox();
    }, delayMs);
    replicationDrainTimer = timer;
    // Node（vm 测试环境）里的 Timeout 对象可 unref —— 挂起的重试兜底
    // 不该阻止进程退出；浏览器里 setTimeout 返回数字，此调用自然无害。
    if (timer && typeof timer.unref === 'function') timer.unref();
  }

  // 墨迹静置后同步（规格 §3：一定时间不变动才同步，粒度 = 一页笔画）。
  // 每页一个防抖定时器：再写重置；到点取**当时最新**整页笔画入队。
  var REPLICATION_INK_SETTLE_MS = 60000;
  var replicationInkTimers = Object.create(null);
  function scheduleInkReplication(kind, key) {
    if (!replicationEligible()) return;
    var timerKey = kind + ':' + key;
    if (replicationInkTimers[timerKey] != null) {
      root.clearTimeout(replicationInkTimers[timerKey]);
    }
    var timer = root.setTimeout(function () {
      delete replicationInkTimers[timerKey];
      readState(kind, {}).then(function (map) {
        var strokes = map && Array.isArray(map[key]) ? map[key] : [];
        var body = { file: localFileRef(), strokes: clone(strokes) };
        if (kind === 'epub-ink') {
          body.idx = /^u_/.test(key) ? key : Number(key);
        } else {
          body.page = Number(key);
        }
        return enqueueReplicationCommand(
          kind === 'epub-ink' ? '/pdf/api/epub-ink' : '/pdf/api/ink',
          'POST', body
        );
      }).catch(function (error) {
        if (typeof root.dlog === 'function') {
          root.dlog('墨迹复制入队失败:' + String(error && error.code || error), '#ffb020');
        }
      });
    }, REPLICATION_INK_SETTLE_MS);
    replicationInkTimers[timerKey] = timer;
    if (timer && typeof timer.unref === 'function') timer.unref();
  }

  // ---- 对账（规格 §6：命令复制的必需品）----
  //
  // 队列排空后比对两端每域摘要（本端物化数组的 canonical sha256 vs
  // Windows 摄取线程导出的摘要视图）。不一致必须出声，并入队一条
  // /replication/resync 整域重同步命令（全量 upsert + 差集墓碑，幂等）
  // 让对端收敛 —— 这就是规格说的"允许一次显式的整域重同步"。
  var REPLICATION_RECONCILE_MIN_INTERVAL_MS = 5 * 60 * 1000;
  var REPLICATION_IDLE_RECONCILE_MS = 10 * 60 * 1000;
  function scheduleReplicationIdleReconcile() {
    var timer = root.setTimeout(function () {
      Promise.resolve(maybeReconcileReplication())
        .catch(function () {})
        .then(function () { scheduleReplicationIdleReconcile(); });
    }, REPLICATION_IDLE_RECONCILE_MS);
    if (timer && typeof timer.unref === 'function') timer.unref();
  }
  var REPLICATION_RESYNC_COOLDOWN_MS = 30 * 60 * 1000;
  // read() 返回本域的物化数组（对账输入）。高亮走门面（order→存活条目），
  // 便签仍是整册数组存储、原序即物化序 —— 两端规则一致即可比。
  var REPLICATION_DOMAINS = [
    {
      domain: 'pdf-highlights',
      read: function () {
        return readHighlightCollection('document-highlights').then(function (read) {
          return read.payload;
        });
      }
    },
    {
      domain: 'epub-highlights',
      read: function () {
        return readHighlightCollection('epub-highlights').then(function (read) {
          return read.payload;
        });
      }
    },
    {
      domain: 'document-notes',
      read: function () {
        return readState('document-notes-legacy', []).then(function (items) {
          return Array.isArray(items) ? items : [];
        });
      }
    },
    {
      domain: 'user-pages',
      read: function () {
        return readState('user-pages', []).then(function (items) {
          return Array.isArray(items) ? items : [];
        });
      }
    },
    // 墨迹物化：map → [{id, strokes}] 按键字典序 —— 两端排序规则必须
    // 逐位一致（Python 端 ink 域同为 sorted(items)），否则摘要永远对不上。
    {
      domain: 'pdf-ink',
      read: function () { return replicationInkMaterialize('ink'); }
    },
    {
      domain: 'epub-ink',
      read: function () { return replicationInkMaterialize('epub-ink'); }
    }
  ];

  function replicationInkMaterialize(kind) {
    return readState(kind, {}).then(function (map) {
      if (!map || typeof map !== 'object' || Array.isArray(map)) return [];
      return Object.keys(map).sort().map(function (key) {
        return { id: key, strokes: clone(map[key]) };
      });
    });
  }
  var replicationLastReconcileMs = 0;
  var replicationLastResyncMs = Object.create(null);
  var replicationReconciling = false;

  function maybeReconcileReplication() {
    if (replicationReconciling || !replicationEligible()) return Promise.resolve();
    var now = Date.now();
    if (now - replicationLastReconcileMs < REPLICATION_RECONCILE_MIN_INTERVAL_MS) {
      return Promise.resolve();
    }
    var query = root.__BW_REPLICATION_DIGESTS__;
    if (typeof query !== 'function') return Promise.resolve();
    replicationReconciling = true;
    replicationLastReconcileMs = now;
    return storedStateRecord(
      stores.document, REPLICATION_LINK_KIND, 'documentId', bookId, null
    ).then(function (link) {
      if (!link.payload || !link.payload.replicationBookId) return null;
      return Promise.all(
        [Promise.resolve(query(link.payload.replicationBookId))].concat(
          REPLICATION_DOMAINS.map(function (spec) { return spec.read(); })
        )
      ).then(function (values) {
        var view = values[0];
        var domains = view && view.domains && typeof view.domains === 'object'
          ? view.domains : {};
        var chain = Promise.resolve();
        REPLICATION_DOMAINS.forEach(function (spec, index) {
          chain = chain.then(function () {
            var payload = values[index + 1];
            return sha256Hex(canonicalJSONString(payload)).then(function (localDigest) {
              var remote = domains[spec.domain];
              var remoteDigest = remote && typeof remote.digest === 'string'
                ? remote.digest : null;
              if (remoteDigest === null) {
                // 对端还没有这个域：本端也为空即视为一致；
                // 本端非空则按不一致走重同步（首次搬运也走同一条路）。
                if (!payload.length) return null;
              } else if (remoteDigest === localDigest) {
                return null;
              }
              var lastResync = Number(replicationLastResyncMs[spec.domain] || 0);
              if (now - lastResync < REPLICATION_RESYNC_COOLDOWN_MS) return null;
              replicationLastResyncMs[spec.domain] = now;
              if (typeof root.dlog === 'function') {
                root.dlog(
                  '复制对账不一致:' + spec.domain + ',触发整域重同步(' +
                    payload.length + '条)',
                  '#ffb020'
                );
              }
              return enqueueReplicationCommand(REPLICATION_RESYNC_URL, 'POST', {
                domain: spec.domain,
                items: clone(payload)
              });
            });
          });
        });
        return chain;
      });
    }).catch(function (error) {
      if (typeof root.dlog === 'function') {
        root.dlog('复制对账失败:' + String(error && error.code || error), '#ffb020');
      }
    }).then(function () { replicationReconciling = false; });
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
    ]).then(function () {
      announceLocalNotesChanged('replace');
      return clone(notes);
    });
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
    var directKinds = USER_STATE_RECORD_KINDS.filter(function (kind) {
      return !HIGHLIGHT_SPLIT_KINDS[kind];
    });
    return Promise.all([
      stores.document.getMany(directKinds.map(function (kind) {
        return { collection: 'native-' + kind, id: stateId(kind) };
      })),
      readHighlightCollection('document-highlights'),
      readHighlightCollection('epub-highlights')
    ]).then(function (values) {
      var output = Object.create(null);
      directKinds.forEach(function (kind, index) {
        output[kind] = values[0][index] || null;
      });
      // 高亮已拆 per-item：合成与旧整册记录同形的 {value:{payload}, rev}，
      // recordPayload / userStateDomainRevision 因此零改动；state 供导入端
      // 用门面 diff 出条目级写入与墓碑。
      [['document-highlights', values[1]], ['epub-highlights', values[2]]]
        .forEach(function (entry) {
          output[entry[0]] = {
            value: {
              id: stateId(entry[0]), documentId: bookId,
              payload: entry[1].payload, updatedAt: 0
            },
            rev: entry[1].rev,
            state: entry[1].state
          };
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
        var mutations = [];
        Array.from(kinds).forEach(function (kind) {
          if (HIGHLIGHT_SPLIT_KINDS[kind]) {
            // 导入是权威整域覆盖：门面 diff 出条目级 put 与墓碑，
            // suffix 来自 transactionId，重试即按 mutationId 重放。
            mutations.push.apply(mutations, highlightCollectionMutations(
              kind, records[kind].state, payloads[kind], suffix + '-' + kind
            ));
            return;
          }
          mutations.push(stateRecordMutation(
            kind,
            payloads[kind],
            suffix + '-' + kind,
            Number(records[kind] && records[kind].rev || 0)
          ));
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
    if (root.__BW_NATIVE_COMPUTER_VOICE__ === true) {
      try {
        return !!(root.RC && root.RC.ctxSync &&
          typeof root.RC.ctxSync._serverSnapshotEnabled === 'function' &&
          root.RC.ctxSync._serverSnapshotEnabled());
      } catch (_) { return false; }
    }
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
    var opaqueEvent = event;
    if (event.type === 'page.context') {
      var pageContext = event.page_context;
      var pageText = pageContext && pageContext.text;
      if (!pageContext || typeof pageContext !== 'object' ||
          Array.isArray(pageContext) || typeof pageText !== 'string' ||
          pageText.length > LOCAL_PAGE_CONTEXT_TEXT_LIMIT ||
          /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(pageText)) {
        throw outgoingRequestError(
          'page.context.text 无效或过长',
          'BW_LOCAL_OUTGOING_JOURNAL_CORRUPT', 400
        );
      }
      // page.context.text is the one intentionally large opaque field: it can
      // contain complete inline CARD records.  Validate it against its own
      // bounded contract, then keep the generic 8192-character fence for every
      // other string in this event and all other outgoing event types.
      opaqueEvent = Object.assign({}, event, {
        page_context: Object.assign({}, pageContext, { text: '' })
      });
    }
    validateOpaqueJSON(opaqueEvent, 'BW_LOCAL_OUTGOING_JOURNAL_CORRUPT');
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
    if (typeof value.text !== 'string' ||
        value.text.length > LOCAL_PAGE_CONTEXT_TEXT_LIMIT ||
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
      'textAuthority', 'layout'
    ]),
    status: new Set(['progress']),
    search: new Set(['matches', 'total', 'pages', 'incomplete']),
    'ocr-selection': new Set(['page', 'text', 'cv', 'persisted', 'textAuthority']),
    'reocr-page': new Set(['page', 'chars', 'cv', 'textAuthority']),
    'clear-reocr-page': new Set(['page', 'cleared', 'cv', 'textAuthority']),
    'book-identity': new Set(['contentSha256'])
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
    'c', 'x0', 'y0', 'x1', 'y1', 'w', 'bk', 'sp', 'b', 'fml', 'flx',
    'line', 'vertical'
  ]);
  var PAGE_TEXT_FURIGANA_KEYS = new Set(['x0', 'y0', 'x1', 'y1', 'rt', 'wd', 'ctx']);
  var PAGE_TEXT_FORMULA_KEYS = new Set([
    'id', 'x0', 'y0', 'x1', 'y1', 'state', 'latex', 'multiline', 'error'
  ]);
  var PAGE_LAYOUT_KEYS = new Set([
    'schema', 'textSource', 'layoutSource', 'mode', 'readingDirection',
    'confidence', 'gridColumns', 'gridRows', 'regions', 'tables'
  ]);
  var PAGE_LAYOUT_REGION_KEYS = new Set([
    'id', 'kind', 'order', 'bounds', 'ranges', 'gridRow', 'gridColumn',
    'rowSpan', 'columnSpan', 'vertical', 'tableId', 'row', 'column'
  ]);
  var PAGE_LAYOUT_TABLE_KEYS = new Set([
    'id', 'rows', 'columns', 'xEdges', 'yEdges'
  ]);
  var PAGE_LAYOUT_TEXT_SOURCES = new Set(['vision', 'unavailable']);
  var PAGE_LAYOUT_SOURCES = new Set(['manga', 'ruled-table', 'vision']);
  var PAGE_LAYOUT_MODES = new Set(['manga', 'table', 'vision', 'fallback']);
  var PAGE_LAYOUT_DIRECTIONS = new Set(['ltr', 'rtl']);
  var PAGE_LAYOUT_CONFIDENCE = new Set(['high', 'low', 'fallback']);
  var PAGE_LAYOUT_REGION_KINDS = new Set([
    'manga-region', 'vision-supplement', 'table-cell', 'vision-block'
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
          (item.line != null &&
            (!Number.isInteger(Number(item.line)) || Number(item.line) < 0)) ||
          (item.vertical != null && typeof item.vertical !== 'boolean') ||
          (item.flx != null && typeof item.flx !== 'string')) {
        dropped += 1;
        continue;
      }
      var normalized = {
        c: c,
        x0: x0, y0: y0, x1: x1, y1: y1,
        w: Number.isInteger(Number(item.w)) ? Number(item.w) : -1,
        bk: Number.isInteger(Number(item.bk)) ? Number(item.bk) : -1,
        sp: !!item.sp,
        fml: !!item.fml,
        flx: item.fml ? String(item.flx || '').slice(0, 4000) : ''
      };
      if (item.line != null) normalized.line = Number(item.line);
      if (item.vertical != null) normalized.vertical = item.vertical;
      out.push(normalized);
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

  function pageLayoutInteger(value, minimum, maximum) {
    return typeof value === 'number' && Number.isSafeInteger(value) &&
      value >= minimum && value <= maximum
      ? value : null;
  }

  function normalizePageLayout(raw, chars, pageWidth, pageHeight) {
    if (raw == null) return null;
    function invalid() {
      throw new RuntimeError('原生页面布局响应无效', 'BW_PAGE_TEXT_BRIDGE_RESPONSE');
    }
    function exactKeys(value, allowed) {
      return value && typeof value === 'object' && !Array.isArray(value) &&
        !Object.keys(value).some(function (key) { return !allowed.has(key); }) &&
        Array.from(allowed).every(function (key) {
          return Object.prototype.hasOwnProperty.call(value, key);
        });
    }
    function boundedBox(value) {
      if (!Array.isArray(value) || value.length !== 4) return null;
      if (!value.every(function (item) {
        return typeof item === 'number' && Number.isFinite(item);
      })) return null;
      var box = value.slice();
      if (box[0] < 0 || box[1] < 0 ||
          box[2] < box[0] || box[3] < box[1] ||
          box[2] > pageWidth || box[3] > pageHeight) return null;
      return box;
    }
    function edges(value, expected, maximum) {
      if (!Array.isArray(value) || value.length !== expected) return null;
      if (!value.every(function (item) {
        return typeof item === 'number' && Number.isFinite(item);
      })) return null;
      var out = value.slice();
      for (var index = 0; index < out.length; index += 1) {
        if (!Number.isFinite(out[index]) || out[index] < 0 ||
            out[index] > maximum || (index && out[index] <= out[index - 1])) {
          return null;
        }
      }
      return out;
    }
    if (!exactKeys(raw, PAGE_LAYOUT_KEYS) ||
        raw.schema !== 'reader-page-layout/1' ||
        !PAGE_LAYOUT_TEXT_SOURCES.has(raw.textSource) ||
        !PAGE_LAYOUT_SOURCES.has(raw.layoutSource) ||
        !PAGE_LAYOUT_MODES.has(raw.mode) ||
        !PAGE_LAYOUT_DIRECTIONS.has(raw.readingDirection) ||
        !PAGE_LAYOUT_CONFIDENCE.has(raw.confidence)) invalid();
    var gridColumns = pageLayoutInteger(raw.gridColumns, 1, 8);
    var gridRows = pageLayoutInteger(raw.gridRows, 0, 4096);
    if (gridColumns === null || gridRows === null ||
        !Array.isArray(raw.regions) || raw.regions.length > 4096 ||
        !Array.isArray(raw.tables) || raw.tables.length > 64) invalid();
    if (raw.textSource === 'unavailable') {
      if (raw.mode !== 'fallback' || raw.confidence !== 'fallback' ||
          raw.layoutSource !== 'vision' || raw.regions.length ||
          raw.tables.length || gridRows !== 0) invalid();
      return {
        schema: raw.schema, textSource: raw.textSource,
        layoutSource: raw.layoutSource, mode: raw.mode,
        readingDirection: raw.readingDirection, confidence: raw.confidence,
        gridColumns: gridColumns, gridRows: gridRows, regions: [], tables: []
      };
    }
    if (raw.mode === 'fallback' || raw.confidence === 'fallback' ||
        (raw.mode === 'manga' &&
          (raw.layoutSource !== 'manga' || gridColumns !== 4)) ||
        (raw.mode === 'table' && raw.layoutSource !== 'ruled-table') ||
        (raw.mode === 'vision' && raw.layoutSource !== 'vision') ||
        !raw.regions.length || !chars.length || gridRows < 1) invalid();

    var tablesById = Object.create(null);
    var totalTableCells = 0;
    var tables = raw.tables.map(function (item) {
      if (!exactKeys(item, PAGE_LAYOUT_TABLE_KEYS)) invalid();
      var id = pageLayoutInteger(item.id, 0, 1000000);
      var rows = pageLayoutInteger(item.rows, 1, 4096);
      var columns = item.columns;
      if (id === null || rows === null || typeof columns !== 'number' ||
          !Number.isSafeInteger(columns) ||
          columns < 2 || columns > 4096 || tablesById[id]) invalid();
      totalTableCells += rows * columns;
      if (totalTableCells > 16384) invalid();
      var xEdges = edges(item.xEdges, columns + 1, pageWidth);
      var yEdges = edges(item.yEdges, rows + 1, pageHeight);
      if (!xEdges || !yEdges) invalid();
      var normalized = {
        id: id, rows: rows, columns: columns,
        xEdges: xEdges, yEdges: yEdges
      };
      tablesById[id] = normalized;
      return normalized;
    });
    if ((raw.mode === 'table') !== (tables.length > 0)) invalid();

    var covered = new Uint8Array(chars.length);
    var regionIds = Object.create(null);
    var regionOrders = Object.create(null);
    var rangeCount = 0;
    var regions = raw.regions.map(function (item) {
      if (!exactKeys(item, PAGE_LAYOUT_REGION_KEYS) ||
          !PAGE_LAYOUT_REGION_KINDS.has(item.kind) ||
          typeof item.vertical !== 'boolean') invalid();
      var id = pageLayoutInteger(item.id, 0, 1000000);
      var order = pageLayoutInteger(item.order, 0, 1000000);
      var gridRow = pageLayoutInteger(item.gridRow, 0, 4095);
      var gridColumn = pageLayoutInteger(item.gridColumn, 0, 7);
      var rowSpan = pageLayoutInteger(item.rowSpan, 1, 4096);
      var columnSpan = pageLayoutInteger(item.columnSpan, 1, 8);
      var bounds = boundedBox(item.bounds);
      if (id === null || order === null || gridRow === null || gridColumn === null ||
          rowSpan === null || columnSpan === null || !bounds || regionIds[id] ||
          regionOrders[order] || gridRow + rowSpan > gridRows ||
          gridColumn + columnSpan > gridColumns || !Array.isArray(item.ranges) ||
          !item.ranges.length || item.ranges.length > chars.length) invalid();
      regionIds[id] = true;
      regionOrders[order] = true;
      var previousEnd = -1;
      var ranges = item.ranges.map(function (range) {
        rangeCount += 1;
        if (rangeCount > Math.max(chars.length, 1) ||
            !Array.isArray(range) || range.length !== 2) invalid();
        var start = pageLayoutInteger(range[0], 0, chars.length - 1);
        var end = pageLayoutInteger(range[1], 0, chars.length - 1);
        if (start === null || end === null || end < start || start <= previousEnd) invalid();
        previousEnd = end;
        for (var cursor = start; cursor <= end; cursor += 1) {
          if (covered[cursor]) invalid();
          covered[cursor] = 1;
        }
        return [start, end];
      });
      var tableId = item.tableId === null
        ? null : pageLayoutInteger(item.tableId, 0, 1000000);
      var row = item.row === null ? null : pageLayoutInteger(item.row, 0, 4095);
      var column = item.column === null ? null : pageLayoutInteger(item.column, 0, 4095);
      if (item.kind === 'table-cell') {
        var table = tableId === null ? null : tablesById[tableId];
        if (!table || row === null || column === null ||
            row + rowSpan > table.rows || column + columnSpan > table.columns) invalid();
      } else if (tableId !== null || row !== null || column !== null) {
        invalid();
      }
      return {
        id: id, kind: item.kind, order: order, bounds: bounds, ranges: ranges,
        gridRow: gridRow, gridColumn: gridColumn,
        rowSpan: rowSpan, columnSpan: columnSpan, vertical: item.vertical,
        tableId: tableId, row: row, column: column
      };
    });
    for (var charIndex = 0; charIndex < covered.length; charIndex += 1) {
      if (covered[charIndex] !== 1) invalid();
    }
    return {
      schema: raw.schema, textSource: raw.textSource,
      layoutSource: raw.layoutSource, mode: raw.mode,
      readingDirection: raw.readingDirection, confidence: raw.confidence,
      gridColumns: gridColumns, gridRows: gridRows,
      regions: regions, tables: tables
    };
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
      layout: null,
      layoutFallback: false,
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
        layout: null,
        layoutFallback: false,
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
    var layout = null;
    var layoutFallback = false;
    if (raw.layout != null) {
      try {
        layout = normalizePageLayout(raw.layout, chars, pageWidth, pageHeight);
      } catch (_) {
        // Spatial metadata is model-facing enrichment. A malformed optional
        // layout must never erase an otherwise valid/selectable Vision layer.
        layoutFallback = true;
      }
    }
    chars = applyFormulaRegions(chars, formulaRegions);
    furigana = furiganaOutsideFormulaRegions(furigana, formulaRegions);
    if (layout && layout.textSource === 'vision' && formulaRegions.some(function (region) {
      return region.state === 'ready';
    })) {
      // Ready formula replacement removes and inserts provider characters, so
      // the persisted Vision ranges no longer address this result array. Keep
      // the final character layer, but fail closed on spatial projection.
      layout = null;
      layoutFallback = true;
    }
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
          layout: null,
          layoutFallback: layoutFallback,
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
      chars: chars, layout: layout, layoutFallback: layoutFallback,
      furigana: furigana,
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
      layout: null,
      layoutFallback: !!nativeResult.layoutFallback,
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
          layout: null,
          layoutFallback: false,
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

  // 把一页正文切成 [{from,to,text}]，序号是**字符层里的真实下标**。
  //
  //   为什么本机要有这个（用户 2026-08-19）：卡片可以绑到正文的一段字上
  //   （bind.kind='page-chars' 要 from/to），而用户明确说过「选中后要求出卡应该
  //   不是常态，而是自动化操作」—— 也就是助手该自己定位。可它此前拿到的只有
  //   `searchableText()` 拼出来的一整段纯文本，序号在那一步就丢了，于是只能
  //   反过来要求用户先选中。
  //
  //   按 w（词组）聚合而非逐字：一页几百个字符逐字发既臃肿又难用，而 w 是有意义
  //   的词边界（日文经 fugashi 分词，2026-08-19 起生效）。
  //
  //   ⚠ 空白（sp）不单独成段，但它**占序号** —— 序号必须与 chars 数组的真实下标
  //     一致，否则卡片会绑到偏掉的位置上。
  // ⚠ 每条 segment 带 `block`：正文里印的 [NN] 与它一一对应。
  //   块归属**本来就在数据里**（chars 的 `bk`，PyMuPDF rawdict 的块序号，
  //   服务端提取时就算好了）—— 此前只是没往上带。这是同一个模式第三次出现：
  //   生成端算出来，在给助手的边界上丢掉。
  //   bk 本身可能不连续（跳号、分栏后重编），所以转成从 1 起的连号再印，
  //   否则助手看到的 [NN] 跟内部 bk 对不上。
  function pageTextSegments(chars) {
    var segments = [];
    var currentWord = null;
    var start = 0;
    var buffer = [];
    var last = 0;
    var blockNo = Object.create(null);   // bk → 从 1 起的连号
    var blockSeq = 0;
    var curBk = null;
    function numberFor(bk) {
      var key = String(bk);
      if (!blockNo[key]) { blockSeq += 1; blockNo[key] = blockSeq; }
      return blockNo[key];
    }
    function flush(end) {
      if (!buffer.length) return;
      var text = buffer.join('').trim();
      if (text) {
        segments.push({
          from: start, to: end, text: text.slice(0, 120),
          block: curBk === null ? 0 : numberFor(curBk)
        });
      }
    }
    for (var index = 0; index < (chars || []).length; index += 1) {
      var item = chars[index];
      if (!item || item.sp) continue;
      var value = String(item.c == null ? '' : item.c);
      if (!value.trim()) continue;
      var word = Number.isInteger(Number(item.w)) ? Number(item.w) : -1;
      var bk = item.bk === undefined || item.bk === null ? null : item.bk;
      // 换块也要断段：一个词组不可能跨两个版面块。
      if (currentWord === null || word !== currentWord || word === -1 ||
          String(bk) !== String(curBk)) {
        flush(last);
        currentWord = word;
        curBk = bk;
        start = index;
        buffer = [];
      }
      buffer.push(value);
      last = index;
    }
    flush(last);
    // 一页几百个词组全发出去对提示词是负担；截断，宁可少给也不给一大堆。
    return segments.slice(0, 400);
  }

  var PAGE_TEXT_MAX_MATCHES = 8;

  // 把**字符**按块折成 `[NN] 正文` 的行。
  //
  // ⚠ 必须从 chars 拼，**不能从 segments 拼** —— 每条 segment 的 text 是
  //   截断到 120 字的预览，拿它拼会把整页正文悄悄缩成一串摘要。
  //   （既有契约测试抓到过这一点：1500 字的页变成了 125 字。）
  //
  // 助手要能说「第 3 块的这句话」,前提是它**看得见**块编号。此前只有
  // 漫画/表格版面印 [NN],普通书页只有一整段平文本 —— 于是「第 3 块」
  // 这句话在普通页上根本说不出口。
  //
  // ⚠ 行首的 [NN] 是**块地址**,不是字符下标。要下标只能用 segments。
  //   两套编号长得一样,这是 ANCHOR_MAP 那个陷阱的同一形态。
  function blockLines(chars, limit) {
    var lines = [];
    var cur = null, seq = 0;
    var no = Object.create(null);
    for (var i = 0; i < (chars || []).length; i += 1) {
      var item = chars[i];
      if (!item) continue;
      var value = String(item.c == null ? '' : item.c);
      if (!value) continue;
      var bk = item.bk === undefined || item.bk === null ? '' : String(item.bk);
      if (!cur || cur.bk !== bk) {
        if (cur) lines.push(cur);
        if (!no[bk]) { seq += 1; no[bk] = seq; }
        cur = { bk: bk, n: no[bk], buf: '' };
      }
      cur.buf += value;
    }
    if (cur) lines.push(cur);
    var out = [];
    for (var j = 0; j < lines.length; j += 1) {
      var body = lines[j].buf.replace(/\s+/g, ' ').trim();
      if (!body) continue;
      var n2 = lines[j].n;
      out.push('[' + (n2 < 10 ? '0' : '') + n2 + '] ' + body);
    }
    var text = out.join('\n');
    return limit && text.length > limit ? text.slice(0, limit) : text;
  }

  // 按助手给的原文把整页收窄成"覆盖那句话的几条"。
  //
  // ⚠ 在**字符层**上找，不是在 searchableText 上找 —— 后者是给人读的投影，
  //   它的位置换算不回 chars 下标。这正是 ANCHOR_MAP 那个陷阱的同一形态。
  // ⚠ 跳过空白与 sp：它们**占序号但不成段**，不跳过就永远匹配不上跨词的句子。
  function narrowPageSegments(result, segments, needle) {
    var chars = (result && result.chars) || [];
    var matches = [];
    for (var start = 0; start < chars.length; start += 1) {
      if (matches.length >= PAGE_TEXT_MAX_MATCHES) break;
      var cursor = start;
      var taken = 0;
      while (cursor < chars.length && taken < needle.length) {
        var item = chars[cursor];
        var value = String((item && item.c) == null ? '' : item.c);
        if (!value || (item && item.sp) || !value.trim()) { cursor += 1; continue; }
        if (value !== needle.charAt(taken)) break;
        taken += 1;
        cursor += 1;
      }
      if (taken === needle.length) matches.push({ from: start, to: cursor - 1 });
    }
    if (!matches.length) return { matches: [], matchCount: 0, segments: [] };
    var keep = [];
    for (var i = 0; i < segments.length; i += 1) {
      var seg = segments[i];
      for (var m = 0; m < matches.length; m += 1) {
        if (seg.to >= matches[m].from && seg.from <= matches[m].to) {
          keep.push(seg);
          break;
        }
      }
    }
    return { matches: matches, matchCount: matches.length, segments: keep };
  }

  function nativeVoicePageText(url) {
    var code = 'BW_LOCAL_VOICE_PAGE_TEXT';
    return localJSONRoute(function () {
      // ⚠ 第 2 个是**允许**表、第 3 个是**必需**表。contains 是可选的，
      //   只进允许表；进了必需表会让不带它的老调用当场被拒。
      //   （这张精确参数表是 2026-08-19 咬过的五处之一。）
      localFileQuery(
        url, ['file', 'page', 'contains'], ['file', 'page'], code
      );
      var page = strictInteger(
        url.searchParams.get('page'), 1, 10000000, 'page', code
      );
      var needle = String(url.searchParams.get('contains') || '').trim();
      if (nativeInterfaceSurface === 'epub') {
        return loadEPUB().then(function (epub) {
          var index = page - 1;
          if (index >= epub.spine.length) {
            throw outgoingRequestError('EPUB 章节越界', code, 400);
          }
          return epubSectionVisibleText(epub, index);
        }).then(function (text) {
          return {
            ok: true,
            text: String(text || '').slice(0, 1500),
            // EPUB 这条走的是可见文本，没有字符层，也就没有可用的序号。
            // 给空数组而不是省略字段：省略会让调用方分不清"这个表面没有这项能力"
            // 和"这一页恰好没有内容"。
            segments: [],
            // 传了 contains 也一样没有序号可给。**明确说出来** ——
            // 静默忽略会让助手以为"筛过了，就这些"，然后据此下结论。
            matchCount: needle ? 0 : undefined,
            containsUnsupported: needle ? true : undefined
          };
        });
      }
      return pageTextForPage(page).then(function (result) {
        var all = pageTextSegments(result && result.chars);
        // 助手据此自己挑段落绑卡片，不必要求用户先选中。
        //
        // contains 给了就只回覆盖那句话的几条。旧的内联 ANCHOR_MAP 正是
        // segments 的同一份数据（370 条 / 5718 字符）—— 整页发回去等于把它
        // 换个地方重来一遍。助手明明知道自己要钉哪句话。
        var narrowed = needle
          ? narrowPageSegments(result, all, needle) : null;
        // 正文按块折行并印上 [NN],助手才说得出「第 3 块」。
        // 认不出块时(bk 缺失)退回原来的平文本 —— 宁可没有编号,
        // 也不要印一串全是 [00] 的假编号。
        var hasBlocks = all.some(function (x) { return x.block > 0; });
        return {
          ok: true,
          text: hasBlocks
            ? blockLines(result && result.chars, 1500)
            : searchableText(result).slice(0, 1500),
          blocks: hasBlocks,
          segments: narrowed ? narrowed.segments : all,
          // matchCount > 1 = 这句话在本页出现多次，光凭它定位不了。
          // 只回第一处会静默锚到用户没在看的那一段。
          matches: narrowed ? narrowed.matches : undefined,
          matchCount: narrowed ? narrowed.matchCount : undefined
        };
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

  // 助手画完高亮后，让界面出现那张卡片条。
  //
  // 迁移前这条反馈由 Pi 的 client_actions 带回（runActions → _assistEdit → hlcard：
  // 原文｜↗跳转｜↩撤销⇄↪重做）。App 本地化后写入不再经 Pi，反馈随之消失，
  // 用户看到的是"功能没了"。这里在**本地确实落库之后**补发同一个调用，渲染器一行不改。
  //
  // 只覆盖助手路径：调用点在 savePDFHighlight(assistant-exact-highlight)，专供助手；
  // 手动划线走 /pdf/api/highlights 的普通写入，不经这里。基线时手动高亮从不产生
  // 卡片条，若一并覆盖，用户每划一次线就多一张卡 —— 那是新增噪声，不是恢复。
  var announcedAssistantHighlights = new Set();
  function announceAssistantHighlight(body, saved) {
    try {
      if (!saved || saved.ok !== true) return;
      var id = String(
        (saved.highlight && saved.highlight.id) || saved.id || body.id || ''
      );
      if (!id) return;
      // 同一条只报一次：CAS 冲突会让写入重试，重试不该再刷出一张卡片条。
      if (announcedAssistantHighlights.has(id)) return;
      announcedAssistantHighlights.add(id);
      if (typeof root._assistEdit !== 'function') return;
      var page = Number(body.page) || 0;
      var display = page;
      try {
        if (typeof root._dispPage === 'function') {
          display = Number(root._dispPage(page)) || page;
        }
      } catch (e) {}
      var savedHighlight = saved.highlight && typeof saved.highlight === 'object'
        ? saved.highlight : null;
      var sourceRects = savedHighlight && savedHighlight.rects != null
        ? savedHighlight.rects : body.rects;
      var rects = Array.isArray(sourceRects) ? sourceRects.map(function (rect) {
        return Array.isArray(rect) ? rect.slice() : rect;
      }) : [];
      root._assistEdit({
        type: 'highlight',
        file: String(body.file || ''),
        items: [{
          id: id,
          text: String(body.text || ''),
          color: String(body.color || ''),
          pdf_page: page,
          disp_page: display,
          rects: rects
        }]
      });
    } catch (e) {
      // 反馈失败不能影响已经完成的写入：高亮已经存下了。
    }
  }

  function normalizedLocalPDFHighlight(body, code) {
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
    return highlight;
  }

  function assistantHighlightFingerprint(surface, highlight) {
    var fields = surface === 'epub'
      ? ['id', 'cfi', 'anchor', 'text', 'color', 'note', 'sentence', 'body', 'kind']
      : ['id', 'page', 'rects', 'color', 'text', 'note', 'kind', 'sentence',
        'body', 'page_w', 'page_h'];
    var value = { surface: surface };
    fields.forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(highlight, key)) {
        value[key] = clone(highlight[key]);
      }
    });
    return canonicalJSONString(value);
  }

  function persistLocalPDFHighlight(body, code, independent) {
    var highlight = normalizedLocalPDFHighlight(body, code);
    var mutate = independent ? mutateHighlightCollectionNow : mutateHighlightCollection;
    return mutate('document-highlights', function (items) {
      items = storedList(items, code).filter(function (item) {
        return !item || item.id !== highlight.id;
      });
      items.push(highlight);
      return localStateMutationResult(items, {
        ok: true, id: highlight.id, highlight: highlight
      });
      // 上界与 independent 无关：普通高亮写入同样会挂住 object store，
      // 一旦挂住，后面连精确高亮也进不来。independent 只决定走哪个入口。
    }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS }).then(function (value) {
      // 本地已落库，事件入复制队列（POST 带完整条目 —— 发送端约定）。
      enqueueReplicationCommand(
        '/pdf/api/highlights', 'POST',
        Object.assign({ file: localFileRef() }, clone(value.highlight))
      );
      return value;
    });
  }

  // Direct reader_highlight_text must create the highlight and its undo entry in
  // the same bounded IndexedDB transaction. Writing the highlight first and the
  // stack second would leave a visible-but-not-undoable mutation after a crash.
  function persistAssistantPDFHighlight(body, code) {
    if (typeof body.id !== 'string' || !/^c_[a-f0-9]{8,32}$/.test(body.id)) {
      return Promise.reject(outgoingRequestError(
        '助手高亮缺少稳定 mutation id', code, 400
      ));
    }
    var highlight = normalizedLocalPDFHighlight(body, code);
    var creationID = 'direct-highlight:' + highlight.id;
    var fingerprint = assistantHighlightFingerprint('pdf', highlight);
    var bound = { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS };
    return serializeLocalStateMutation('document', 'pdf-assistant-bundle', function () {
      return Promise.all([
        readHighlightCollection('document-highlights', bound),
        storedStateRecord(
          stores.document, 'pdf-assistant-undo', 'documentId', bookId, [], bound
        ),
        storedStateRecord(
          stores.document, 'pdf-assistant-ops', 'documentId', bookId, [], bound
        )
      ]).then(function (records) {
        var highlights = storedList(clone(records[0].payload), code);
        var undo = storedList(clone(records[1].payload), code);
        var receipts = storedList(clone(records[2].payload), code);
        var priorReceipt = receipts.find(function (item) {
          return item && String(item.id || '') === creationID;
        });
        var existing = highlights.find(function (item) {
          return item && String(item.id || '') === highlight.id;
        });
        if (priorReceipt) {
          if (!existing || priorReceipt.fingerprint !== fingerprint ||
              assistantHighlightFingerprint('pdf', existing) !== fingerprint) {
            throw new RuntimeError(
              '助手高亮 mutation id 已用于不同内容',
              'BW_NATIVE_PDF_ASSISTANT_CONFLICT'
            );
          }
          return { ok: true, id: existing.id, highlight: clone(existing), replayed: true };
        }
        if (existing) {
          throw new RuntimeError(
            '助手高亮 id 已存在但没有匹配回执',
            'BW_NATIVE_PDF_ASSISTANT_CONFLICT'
          );
        }
        highlights.push(highlight);
        undo.push({
          id: creationID,
          kind: 'highlight-create',
          targetKind: 'document-highlights',
          expectedRevision: records[0].rev + 1,
          ids: [highlight.id],
          ts: nowSeconds()
        });
        receipts.push({
          id: creationID, kind: 'highlight', fingerprint: fingerprint,
          ts: nowSeconds()
        });
        undo = undo.slice(-80);
        receipts = boundedAssistantOperationReceipts(receipts);
        var suffix = randomHex(12);
        return stores.document.batch(highlightCollectionMutations(
          'document-highlights', records[0].state, highlights, suffix + '-highlights'
        ).concat([
          stateRecordMutation(
            'pdf-assistant-undo', undo, suffix + '-undo', records[1].rev
          ),
          stateRecordMutation(
            'pdf-assistant-ops', receipts, suffix + '-ops', records[2].rev
          )
        ]), bound).then(function () {
          return { ok: true, id: highlight.id, highlight: clone(highlight), replayed: false };
        });
      });
    });
  }

  function localPDFHighlights(input, init, url, method) {
    var code = 'BW_LOCAL_HIGHLIGHTS';
    if (method === 'GET') {
      return localJSONRoute(function () {
        localFileQuery(url, ['file'], ['file'], code);
        return readHighlightCollection('document-highlights', {
          transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS
        }).then(function (read) {
          var items = storedList(read.payload, code).slice().sort(function (a, b) {
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
          return mutateHighlightCollection('document-highlights', function (items) {
            items = storedList(items, code);
            var before = items.length;
            items = items.filter(function (item) { return item && item.id !== request.id; });
            if (items.length === before) {
              throw outgoingRequestError('未找到高亮', code, 404);
            }
            return localStateMutationResult(items, { ok: true });
          }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS }).then(function (value) {
            enqueueReplicationCommand('/pdf/api/highlights', 'DELETE', {
              file: localFileRef(), id: request.id
            });
            return value;
          });
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
        return mutateHighlightCollection('document-highlights', function (items) {
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
        }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS }).then(function (value) {
          enqueueReplicationCommand(
            '/pdf/api/highlights', 'PATCH',
            Object.assign({}, clone(body), { file: localFileRef(), id: id })
          );
          return value;
        });
      });
    }, code);
  }

  function localEPUBHighlights(input, init, url, method) {
    var code = 'BW_LOCAL_EPUB_HIGHLIGHTS';
    if (method === 'GET') {
      return localJSONRoute(function () {
        localFileQuery(url, ['file'], ['file'], code);
        return readHighlightCollection('epub-highlights', {
          transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS
        }).then(function (read) {
          return { ok: true, highlights: storedList(read.payload, code) };
        });
      }, code);
    }
    if (method === 'DELETE') {
      return localJSONRoute(function () {
        return deleteRecordRequest(input, init, url, code).then(function (request) {
          return mutateHighlightCollection('epub-highlights', function (items) {
            items = storedList(items, code);
            var before = items.length;
            items = items.filter(function (item) { return item && item.id !== request.id; });
            if (items.length === before) throw outgoingRequestError('未找到高亮', code, 404);
            return localStateMutationResult(items, { ok: true });
          }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS }).then(function (value) {
            enqueueReplicationCommand('/pdf/api/epub-highlights', 'DELETE', {
              file: localFileRef(), id: request.id
            });
            return value;
          });
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
          var highlight = normalizedLocalEPUBHighlight(body, code);
          if (typeof body.id === 'string' && /^c_[a-f0-9]{8,32}$/.test(body.id)) {
            return persistAssistantEPUBHighlight(highlight, code);
          }
          return mutateHighlightCollection('epub-highlights', function (items) {
            items = storedList(items, code).filter(function (item) {
              return !item || item.id !== highlight.id;
            });
            items.push(highlight);
            return localStateMutationResult(items, {
              ok: true, id: highlight.id, highlight: highlight
            });
          }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS }).then(function (value) {
            enqueueReplicationCommand(
              '/pdf/api/epub-highlights', 'POST',
              Object.assign({ file: localFileRef() }, clone(value.highlight))
            );
            return value;
          });
        }
        var id = localRecordId(body.id, code);
        return mutateHighlightCollection('epub-highlights', function (items) {
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
        }, { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS }).then(function (value) {
          enqueueReplicationCommand(
            '/pdf/api/epub-highlights', 'PATCH',
            Object.assign({}, clone(body), { file: localFileRef(), id: id })
          );
          return value;
        });
      });
    }, code);
  }

  function normalizedLocalEPUBHighlight(body, code) {
    var cfi = boundedLocalString(body.cfi, 8192, '', code, 'EPUB CFI', true);
    var anchor = body.anchor == null ? null : boundedCanonicalJSON(
      body.anchor, 64 * 1024, code, 'EPUB 高亮锚点'
    );
    if (!cfi && (!anchor || typeof anchor !== 'object' || Array.isArray(anchor))) {
      throw outgoingRequestError('缺少 cfi/anchor', code, 400);
    }
    var clientId = typeof body.id === 'string' && /^c_[a-f0-9]{8,32}$/.test(body.id)
      ? body.id : '';
    return {
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
  }

  function nativeEPUBDirectHighlightAction(highlight, creationID) {
    return {
      id: creationID,
      kind: 'epub_highlight',
      title: '高亮:1处',
      detail: '· ' + String(highlight.text || '').slice(0, 120),
      undo: {
        op: 'hl_delete', file: localFileRef(), ids: [highlight.id]
      },
      redo: {
        op: 'hl_create', file: localFileRef(), items: [clone(highlight)]
      },
      state: 'done',
      ts: Number(highlight.time) || nowSeconds()
    };
  }

  function persistAssistantEPUBHighlight(highlight, code) {
    var creationID = 'direct-highlight:' + highlight.id;
    var fingerprint = assistantHighlightFingerprint('epub', highlight);
    var bound = { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS };
    return serializeLocalStateMutation('document', 'epub-assistant-bundle', function () {
      return Promise.all([
        readHighlightCollection('epub-highlights', bound),
        storedStateRecord(
          stores.document, 'epub-assistant-undo', 'documentId', bookId, [], bound
        ),
        storedStateRecord(
          stores.document, 'epub-assistant-ops', 'documentId', bookId, [], bound
        )
      ]).then(function (records) {
        var highlights = storedList(clone(records[0].payload), code);
        var stack = storedList(clone(records[1].payload), code);
        var receipts = storedList(clone(records[2].payload), code);
        var priorReceipt = receipts.find(function (item) {
          return item && String(item.id || '') === creationID;
        });
        var existing = highlights.find(function (item) {
          return item && String(item.id || '') === highlight.id;
        });
        if (priorReceipt) {
          if (!existing || priorReceipt.fingerprint !== fingerprint ||
              assistantHighlightFingerprint('epub', existing) !== fingerprint) {
            throw new RuntimeError(
              '助手 EPUB 高亮 mutation id 已用于不同内容',
              'BW_NATIVE_EPUB_UNDO_CONFLICT'
            );
          }
          return {
            ok: true,
            id: existing.id,
            highlight: clone(existing),
            action: nativeEPUBDirectHighlightAction(existing, creationID),
            replayed: true
          };
        }
        if (existing) {
          throw new RuntimeError(
            '助手 EPUB 高亮 id 已存在但没有匹配回执',
            'BW_NATIVE_EPUB_UNDO_CONFLICT'
          );
        }
        highlights.push(highlight);
        var action = nativeEPUBDirectHighlightAction(highlight, creationID);
        stack.push(nativeEPUBStackEntry(
          action,
          { kind: 'epub-highlights', operation: 'hl_create' },
          records[0].rev + 1
        ));
        stack = stack.slice(-80);
        receipts.push({
          id: creationID, kind: 'highlight', fingerprint: fingerprint,
          ts: nowSeconds()
        });
        receipts = receipts.slice(-160);
        var suffix = randomHex(12);
        return stores.document.batch(highlightCollectionMutations(
          'epub-highlights', records[0].state, highlights, suffix + '-highlights'
        ).concat([
          stateRecordMutation(
            'epub-assistant-undo', stack, suffix + '-undo', records[1].rev
          ),
          stateRecordMutation(
            'epub-assistant-ops', receipts, suffix + '-ops', records[2].rev
          )
        ]), bound).then(function () {
          return {
            ok: true,
            id: highlight.id,
            highlight: clone(highlight),
            action: clone(action),
            replayed: false
          };
        });
      });
    });
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
          }).then(function (value) {
            enqueueReplicationCommand('/pdf/api/notes', 'DELETE', {
              file: localFileRef(), id: request.id
            });
            return value;
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
          }).then(function (value) {
            enqueueReplicationCommand(
              '/pdf/api/notes', 'POST',
              Object.assign({ file: localFileRef() }, clone(value.note))
            );
            return value;
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
        }).then(function (value) {
          enqueueReplicationCommand(
            '/pdf/api/notes', 'PATCH',
            Object.assign({}, clone(body), { file: localFileRef(), id: id })
          );
          return value;
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
          }).then(function (value) {
            enqueueReplicationCommand('/pdf/api/userpages', 'DELETE', {
              file: localFileRef(), id: request.id
            });
            return value;
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
          }).then(function (value) {
            enqueueReplicationCommand(
              '/pdf/api/userpages', 'POST',
              Object.assign({ file: localFileRef() }, clone(value.page))
            );
            return value;
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
        }).then(function (value) {
          enqueueReplicationCommand(
            '/pdf/api/userpages', 'PATCH',
            Object.assign({}, clone(body), { file: localFileRef(), id: id })
          );
          return value;
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
        }).then(function (value) {
          scheduleInkReplication(kind, key);
          return value;
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

  // 一次性存量修复(2026-08-25 实锤):插入页迁移曾只搬壳锚 anchor.page、
  // 漏了词锚 card/html.bind.page(migrateNativePDFNotes 已补),已插过页的书
  // 里词锚停在移位前的页号,词框在错误的页找文本,绑不上就藏 ——
  // 用户看到'绑定的卡都消失了'。绑定创建时词锚与壳锚同页,所以
  // anchor.kind==='pdf' 且 bind.page !== anchor.page 的都是这次错位,
  // 直接对齐。幂等:对齐后条件不再命中,之后每次开书只是一次探测读。
  // 修复只写本地;Windows 副本靠周期对账(摘要变化 → resync)自动收敛。
  function noteBindPageDrifted(note) {
    if (!note || typeof note !== 'object') return false;
    var anchor = note.anchor;
    if (!anchor || anchor.kind !== 'pdf' ||
        !Number.isInteger(anchor.page)) return false;
    return ['card', 'html'].some(function (slot) {
      var bind = note[slot] && note[slot].bind;
      return !!bind && bind.kind === 'page-chars' &&
        Number.isInteger(Number(bind.page)) &&
        Number(bind.page) !== anchor.page;
    });
  }
  function repairNoteBindPageDrift() {
    if (nativeInterfaceSurface !== 'pdf') return Promise.resolve(null);
    return readState('document-notes-legacy', []).then(function (probe) {
      if (!Array.isArray(probe) || !probe.some(noteBindPageDrifted)) {
        return null;
      }
      return mutateDocumentState('document-notes-legacy', [], function (items) {
        items = Array.isArray(items) ? items : [];
        items.forEach(function (note) {
          if (!noteBindPageDrifted(note)) return;
          ['card', 'html'].forEach(function (slot) {
            var bind = note[slot] && note[slot].bind;
            if (!bind || bind.kind !== 'page-chars' ||
                !Number.isInteger(Number(bind.page)) ||
                Number(bind.page) === note.anchor.page) return;
            bind.page = note.anchor.page;
          });
        });
        return localStateMutationResult(items, { ok: true });
      });
    }).catch(function (error) {
      // 修复失败不拦启动(卡片照旧隐身而不是书打不开),但必须留痕。
      // 留痕本身也不许抛:启动链上这个 catch 是最后一道,它再抛就是
      // bootState 永远不 ready、整本书打不开 —— 比没修复糟一个量级。
      try {
        enqueueReplicationCommand('/replication/diagnostic', 'POST', {
          kind: 'note-bind-repair-error',
          error: String(error && error.message || error).slice(0, 2000),
          at: nowSeconds()
        });
      } catch (_) {}
      return null;
    });
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
      // 词锚(card/html.bind.page)必须与壳锚同步移位(2026-08-25 实锤:
      // 只搬 anchor.page 的话,插入页后词框在错误的页找文本,绑不上就
      // 藏起来,用户看到的是'绑定的卡都消失了')。字符下标 from/to 属于
      // 该页自身的字符层,页内容未变,只映射页号。被删页上的词锚降级成
      // 自由卡(去掉 bind),便签本体与内容保留 —— 锚没了不等于笔记没了。
      ['card', 'html'].forEach(function (slot) {
        var payload = note && note[slot];
        var bind = payload && payload.bind;
        if (!bind || !Number.isInteger(Number(bind.page))) return;
        var mappedBind = nativePDFPageMap(
          Number(bind.page), plan.operation, plan.pivotPage
        );
        if (mappedBind == null) delete payload.bind;
        else bind.page = mappedBind;
      });
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
      if (HIGHLIGHT_SPLIT_KINDS[kind]) {
        // journal 契约不变：before[kind] 仍是 {payload, rev}，
        // 只是 payload 由门面物化、rev 是集合 meta 的修订号。
        return readHighlightCollection(kind).then(function (read) {
          return { kind: kind, payload: read.payload, rev: read.rev };
        });
      }
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
      // ⚠ 真机 IndexedDB 的 remove 留**墓碑**（rev 继续存在）：上一次
      // mutation 收尾清掉 journal 后，物理记录仍在且 rev>0 —— 这里若用
      // ifRev:0（要求记录不存在）,同一本书的第二次改页必然版本冲突
      // （2026-08-25 真机实锤）。CAS 基线必须取**含墓碑**的当前 rev；
      // 并发首写保护不受影响（两个并发写仍会有一个 CAS 失败）。
      return stores.document.get(
        'native-' + NATIVE_PDF_MUTATION_JOURNAL_KIND,
        stateId(NATIVE_PDF_MUTATION_JOURNAL_KIND),
        { includeDeleted: true }
      ).then(function (tombstoned) {
        return stores.document.batch([
          stateRecordMutation(
            NATIVE_PDF_MUTATION_JOURNAL_KIND,
            transaction.journal,
            randomHex(12),
            Number(tombstoned && tombstoned.rev || 0)
          )
        ]);
      });
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
      if (HIGHLIGHT_SPLIT_KINDS[kind]) {
        return readHighlightCollection(kind).then(function (read) {
          return {
            kind: kind,
            record: { payload: read.payload, rev: read.rev },
            hlState: read.state
          };
        });
      }
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
          if (HIGHLIGHT_SPLIT_KINDS[item.kind]) {
            mutations.push.apply(mutations, highlightCollectionMutations(
              item.kind, item.hlState, wanted, suffix + '-' + item.kind
            ));
          } else {
            mutations.push(stateRecordMutation(
              item.kind, wanted, suffix + '-' + item.kind, item.record.rev
            ));
          }
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
        // 诊断出口（silent-failure 规则5：无控制台设备上沉默=不可诊断）。
        // 改页失败的错误文本只在 UI 横幅一闪而过 —— 经复制通道送到
        // 服务端持久留痕，远程就能看到真机到底失败在哪一步。
        enqueueReplicationCommand('/replication/diagnostic', 'POST', {
          kind: 'pdf-mutation-error',
          operation: String((plan && plan.operation) || ''),
          error: failures.join('；').slice(0, 4000),
          at: nowSeconds()
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

  function nativePDFContextPages(context) {
    var output = [];
    function add(value) {
      value = Number(value);
      if (Number.isSafeInteger(value) && value >= 1 && value <= 10000000 &&
          output.indexOf(value) < 0) output.push(value);
    }
    context = context && typeof context === 'object' && !Array.isArray(context)
      ? context : {};
    if (Array.isArray(context.pages)) context.pages.slice(0, 12).forEach(add);
    add(context.page);
    return output;
  }

  function nativePDFPageCardProjection(context, notesRevision) {
    var pages = nativePDFContextPages(context);
    var computerVoice = root.RC && root.RC.computerVoice;
    if (!pages.length || !computerVoice ||
        typeof computerVoice.pageCards !== 'function') return Promise.resolve(null);
    return Promise.all(pages.map(function (page) {
      return Promise.resolve(computerVoice.pageCards(page)).then(function (value) {
        if (!value || value.contract !== LOCAL_PAGE_CARD_PROJECTION_CONTRACT ||
            Number(value.page) !== page || Number(value.revision) !== Number(notesRevision) ||
            !Array.isArray(value.cards)) {
          throw new RuntimeError(
            '页面卡片精确序号投影与权威便签版本不一致',
            'BW_NATIVE_PDF_PAGE_CARDS_PROJECTION'
          );
        }
        return {
          page: page,
          cards: value.cards.map(function (card) {
            if (!card || typeof card !== 'object' || Array.isArray(card) ||
                typeof card.id !== 'string' || !card.id ||
                (card.kind !== 'anki' && card.kind !== 'card') ||
                typeof card.label !== 'string' || typeof card.text !== 'string' ||
                typeof card.unbound !== 'boolean') {
              throw new RuntimeError(
                '页面卡片精确序号投影条目无效',
                'BW_NATIVE_PDF_PAGE_CARDS_PROJECTION'
              );
            }
            if (card.unbound === true) {
              if (card.number !== null || card.bind !== null) {
                throw new RuntimeError(
                  '自由卡片精确序号投影无效',
                  'BW_NATIVE_PDF_PAGE_CARDS_PROJECTION'
                );
              }
            } else if (!Number.isSafeInteger(card.number) || card.number < 1 ||
                       !card.bind || card.bind.kind !== 'page-chars' ||
                       Number(card.bind.page) !== page) {
              throw new RuntimeError(
                '锚定卡片精确序号投影无效',
                'BW_NATIVE_PDF_PAGE_CARDS_PROJECTION'
              );
            }
            return {
              id: card.id,
              number: card.number,
              kind: card.kind,
              label: card.label,
              text: card.text,
              bind: card.bind == null ? null : clone(card.bind),
              unbound: card.unbound
            };
          })
        };
      });
    })).then(function (rows) {
      var projectedPages = {};
      rows.forEach(function (row) { projectedPages[String(row.page)] = row.cards; });
      return {
        contract: NATIVE_PAGE_CARD_PROJECTION_CONTRACT,
        revision: Number(notesRevision),
        pages: projectedPages
      };
    }).catch(function () {
      // Geometry is optional for reading but mandatory for number-based writes.
      // Omit the whole projection rather than forwarding a partial/stale map.
      return null;
    });
  }

  function nativePDFAuthoritySnapshot(context) {
    return Promise.all([
      readHighlightCollection('document-highlights'),
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
      return nativePDFPageCardProjection(context, records[1].rev).then(function (projection) {
        if (projection) snapshot.page_cards = projection;
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
    return bodyJSON(input, init).then(function (body) {
        assertNativePDFWriterLease(writerLease);
        if (!body || typeof body !== 'object' || Array.isArray(body)) {
          throw new RuntimeError(
            'PDF 助手请求体无效', 'BW_NATIVE_PDF_ASSISTANT_BODY'
          );
        }
        var context = body[contextKey];
        if (!context || typeof context !== 'object' || Array.isArray(context)) {
          context = {};
        }
        return Promise.all([
          nativePDFAssistantContext(context),
          nativePDFAuthoritySnapshot(context)
        ]).then(function (values) {
          assertNativePDFWriterLease(writerLease);
          var suppliedContext = values[0];
          var snapshot = values[1];
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
    var valid = descriptor && (descriptor.kind === 'page-card-edit' ||
      descriptor.kind === 'page-card-delete')
      ? /^(?:npdf|pcard)_[0-9a-f]{24}$/.test(value)
      : /^npdf_[0-9a-f]{24}$/.test(value);
    if (!valid) {
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
    if (data.type === 'page-card' && data.op === 'edit') {
      return { kind: 'page-card-edit', data: data };
    }
    if (data.type === 'page-card' && data.op === 'delete') {
      return { kind: 'page-card-delete', data: data };
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
    if (descriptor.kind === 'page-card-edit' ||
        descriptor.kind === 'page-card-delete') {
      // before/after may each be multiple MiB and are trusted-runtime journal
      // material, not UI state. Keep only stable display metadata in the page.
      output.args[0].item = { id: String(descriptor.data.expected_id || '') };
    }
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

  function nativePDFRememberPageCardSnapshots(revision, notes, cardIDs) {
    revision = Number(revision);
    if (!Number.isSafeInteger(revision) || revision < 0 ||
        !Array.isArray(notes) || !Array.isArray(cardIDs)) return;
    var entry = nativePageCardSnapshotCache.get(revision);
    if (!entry) entry = { bytes: 0, cards: new Map() };
    var wanted = new Set(cardIDs.map(function (id) { return String(id || ''); }));
    for (var index = 0; index < notes.length; index += 1) {
      var note = notes[index];
      var id = String(note && (note.id || note.noteId) || '');
      if (!wanted.has(id) || entry.cards.has(id)) continue;
      try {
        var encoded = canonicalJSONString(
          nativePDFNoteSnapshot(note, id, 'BW_LOCAL_PAGE_CARDS')
        );
        var size = utf8(encoded).byteLength;
        if (size > LOCAL_PAGE_CARD_SNAPSHOT_CACHE_BYTES ||
            entry.bytes + size > LOCAL_PAGE_CARD_SNAPSHOT_CACHE_BYTES) continue;
        entry.cards.set(id, encoded);
        entry.bytes += size;
      } catch (_) {}
    }
    nativePageCardSnapshotCache.set(revision, entry);
    while (nativePageCardSnapshotCache.size >
        LOCAL_PAGE_CARD_SNAPSHOT_CACHE_REVISIONS) {
      nativePageCardSnapshotCache.delete(
        nativePageCardSnapshotCache.keys().next().value
      );
    }
  }

  function nativePDFCachedPageCardUnchanged(revision, note, id, code) {
    var entry = nativePageCardSnapshotCache.get(Number(revision));
    var expected = entry && entry.cards.get(String(id || ''));
    if (!expected) return false;
    try {
      return expected === canonicalJSONString(
        nativePDFNoteSnapshot(note, id, code)
      );
    } catch (_) {
      return false;
    }
  }

  function nativePDFPageCardBind(note, code) {
    var payload = note && note.card ? note.card : (note && note.html ? note.html : null);
    var bind = payload && payload.bind;
    if (!bind || typeof bind !== 'object' || Array.isArray(bind) ||
        bind.kind !== 'page-chars') {
      throw outgoingRequestError('页面卡片不是字符锚定卡片', code, 409);
    }
    return {
      kind: 'page-chars',
      page: strictInteger(bind.page, 1, 10000000, 'page', code),
      from: strictInteger(bind.from, 0, 1000000, 'from', code),
      to: strictInteger(bind.to, 0, 1000000, 'to', code),
      text: boundedLocalString(bind.text, 200, '', code, '锚定词', false)
    };
  }

  function nativePDFPageCardOptionalBind(note, code) {
    var payload = note && note.card ? note.card : (note && note.html ? note.html : null);
    if (!payload || payload.bind == null) return null;
    return nativePDFPageCardBind(note, code);
  }

  function nativePDFPageCardIdentity(note) {
    var value = clone(note);
    delete value.updated;
    if (value.card) {
      value.card = clone(value.card);
      delete value.card.cards;
      delete value.card.contextText;
      delete value.card.context_text;
    }
    if (value.html) {
      value.html = clone(value.html);
      delete value.html.content;
      delete value.html.contextText;
      delete value.html.context_text;
    }
    return canonicalJSONString(value);
  }

  function nativePDFPageCardCanonicalID(card, code) {
    if (!card || typeof card !== 'object' || Array.isArray(card)) return null;
    var ids = [];
    ['id', 'cid', 'gid'].forEach(function (field) {
      if (!Object.prototype.hasOwnProperty.call(card, field) || card[field] == null ||
          card[field] === '') return;
      if (typeof card[field] !== 'string') {
        throw outgoingRequestError('学习卡语义身份无效', code, 409);
      }
      ids.push(card[field]);
    });
    if (!ids.length) return null;
    if (ids.some(function (id) { return id !== ids[0]; })) {
      throw outgoingRequestError('学习卡 id/cid/gid 不一致', code, 409);
    }
    return /^card_[a-f0-9]{4,64}$/.test(ids[0]) ? ids[0] : null;
  }

  function nativePDFPageCardCards(value, code) {
    if (!Array.isArray(value) || value.length < 1 || value.length > 12) {
      throw outgoingRequestError('学习卡替换必须包含 1 到 12 张卡片', code, 400);
    }
    return value.map(function (card) {
      if (!card || typeof card !== 'object' || Array.isArray(card)) {
        throw outgoingRequestError('学习卡替换条目无效', code, 400);
      }
      var keys = Object.keys(card).sort().join(',');
      if (card.type === 'basic' && keys === 'back,front,type') {
        var front = boundedLocalString(
          card.front, LOCAL_PAGE_CARD_CONTEXT_LIMIT, '', code, '卡片正面', false
        );
        var back = boundedLocalString(
          card.back, LOCAL_PAGE_CARD_CONTEXT_LIMIT, '', code, '卡片背面', false
        );
        if (!front.trim() || !back.trim()) {
          throw outgoingRequestError('basic 卡正反面不能为空', code, 400);
        }
        return { type: 'basic', front: front, back: back };
      }
      if (card.type === 'cloze' && keys === 'cloze,type') {
        var cloze = boundedLocalString(
          card.cloze, LOCAL_PAGE_CARD_CONTEXT_LIMIT, '', code, '挖空卡正文', false
        );
        if (!/\{\{c[1-9][0-9]*::[\s\S]+?\}\}/.test(cloze)) {
          throw outgoingRequestError('cloze 卡缺少有效挖空', code, 400);
        }
        return { type: 'cloze', cloze: cloze };
      }
      throw outgoingRequestError('学习卡只能完整替换 strict basic/cloze 内容', code, 400);
    });
  }

  function nativePDFPageCardContextText(cards) {
    var rows = (Array.isArray(cards) ? cards : []).map(function (card) {
      if (card && card.type === 'cloze') {
        return localContextPlainText(card.cloze, LOCAL_PAGE_CARD_CONTEXT_LIMIT);
      }
      var front = localContextPlainText(
        card && card.front, LOCAL_PAGE_CARD_CONTEXT_LIMIT
      );
      var back = localContextPlainText(
        card && card.back, LOCAL_PAGE_CARD_CONTEXT_LIMIT
      );
      return [front, back].filter(Boolean).join(' / ');
    }).filter(Boolean);
    return localContextPlainText(
      rows.join('\n'), LOCAL_PAGE_CARD_CONTEXT_LIMIT
    );
  }

  function nativePDFPageCardHTML(value, code) {
    value = boundedLocalString(
      value, LOCAL_PAGE_CARD_CONTEXT_LIMIT, '', code, '卡片内容', false
    );
    if (!value.trim() || !root.DOMPurify ||
        typeof root.DOMPurify.sanitize !== 'function' ||
        typeof root.DOMParser !== 'function') {
      throw outgoingRequestError('卡片 HTML 净化器不可用或内容为空', code, 400);
    }
    if (typeof root.DOMPurify.removeAllHooks === 'function') {
      root.DOMPurify.removeAllHooks();
    }
    var sanitized = String(root.DOMPurify.sanitize(value, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'form'],
      FORBID_ATTR: ['srcdoc']
    }) || '');
    if (!sanitized.trim()) {
      throw outgoingRequestError('卡片 HTML 净化后为空', code, 400);
    }
    var parsed = new root.DOMParser().parseFromString(sanitized, 'text/html');
    var contextText = localContextPlainText(
      parsed && parsed.body ? parsed.body.textContent || '' : '',
      LOCAL_PAGE_CARD_CONTEXT_LIMIT
    );
    return { content: sanitized, contextText: contextText };
  }

  function nativePDFPageCardPlan(data, code) {
    var commonKeys = [
      'type', 'op', 'native_operation_id', 'file', 'page', 'number',
      'expected_id', 'expected_revision', 'item'
    ];
    if (!exactKeys(data, commonKeys) || data.type !== 'page-card' ||
        (data.op !== 'edit' && data.op !== 'delete')) {
      throw outgoingRequestError('页面卡片动作合同无效', code, 400);
    }
    requireLocalFile(data.file, code);
    var id = localRecordId(data.expected_id, code);
    var page = strictInteger(data.page, 1, 10000000, 'page', code);
    var number = data.number == null
      ? null : strictInteger(data.number, 1, 1000000, 'number', code);
    var expectedRevision = strictInteger(
      data.expected_revision, 0, Number.MAX_SAFE_INTEGER, 'expected_revision', code
    );
    var item = data.item;
    var itemKeys = data.op === 'edit' ? ['id', 'before', 'after'] : ['id', 'before'];
    if (!exactKeys(item, itemKeys) || localRecordId(item.id, code) !== id) {
      throw outgoingRequestError('页面卡片动作项目无效', code, 400);
    }
    var before = nativePDFNoteSnapshot(item.before, id, code);
    if (before.id !== id || (!!before.card === !!before.html) || before.video) {
      throw outgoingRequestError('页面卡片修改前快照无效', code, 400);
    }
    var bind = nativePDFPageCardOptionalBind(before, code);
    var anchorPage = before.anchor && before.anchor.kind === 'pdf'
      ? Number(before.anchor.page) : null;
    if ((bind && (bind.page !== page || bind.to < bind.from)) ||
        (!bind && anchorPage !== page) || (number != null && !bind)) {
      throw outgoingRequestError('页面卡片页锚不匹配', code, 409);
    }
    var after = null;
    var replacementCards = null;
    if (data.op === 'edit') {
      after = nativePDFNoteSnapshot(item.after, id, code);
      if (after.id !== id || nativePDFPageCardIdentity(before) !==
          nativePDFPageCardIdentity(after) || (!!after.card === !!before.card) === false) {
        throw outgoingRequestError('页面卡片修改越过了内容字段边界', code, 409);
      }
      var afterBind = nativePDFPageCardOptionalBind(after, code);
      if (canonicalJSONString(afterBind) !== canonicalJSONString(bind)) {
        throw outgoingRequestError('页面卡片修改不得改变锚点', code, 409);
      }
      if (before.card) {
        replacementCards = nativePDFPageCardCards(after.card.cards, code);
        after.card.cards = clone(replacementCards);
        // Remote contextText is not authority. Derive the future AI context
        // from the exact validated faces that will be persisted.
        after.card.contextText = nativePDFPageCardContextText(replacementCards);
      } else {
        var safeHTML = nativePDFPageCardHTML(after.html.content, code);
        after.html.content = safeHTML.content;
        // AI-supplied contextText is not an authority; derive it from the same
        // sanitized DOM that will actually render.
        after.html.contextText = safeHTML.contextText;
      }
    }
    return {
      id: id, page: page, number: number,
      expectedRevision: expectedRevision,
      before: before, after: after,
      replacementCards: replacementCards,
      canonicalId: before.card ? nativePDFPageCardCanonicalID(before.card, code) : null
    };
  }

  function nativePDFPageCardFingerprint(plan, operationID, kind) {
    return canonicalJSONString({
      id: operationID, kind: kind, placementId: plan.id,
      page: plan.page, number: plan.number,
      before: plan.before, after: plan.after
    });
  }

  function nativePDFPageCardWireCards(cards) {
    return (Array.isArray(cards) ? cards : []).map(function (card) {
      return card && card.type === 'cloze'
        ? { type: 'cloze', cloze: String(card.cloze || '') }
        : {
            type: 'basic',
            front: String(card && card.front || ''),
            back: String(card && card.back || '')
          };
    });
  }

  function nativePDFPageCardRequestFingerprint(plan, kind, numberSpecified) {
    var replacement = null;
    if (kind === 'page-card-edit' && plan.after && plan.after.card) {
      replacement = { cards: nativePDFPageCardWireCards(plan.after.card.cards) };
    } else if (kind === 'page-card-edit' && plan.after && plan.after.html) {
      replacement = { content: String(plan.after.html.content || '') };
    }
    return canonicalJSONString({
      operation: kind === 'page-card-delete' ? 'delete' : 'edit',
      expectedId: plan.id,
      expectedRevision: plan.expectedRevision,
      numberSpecified: numberSpecified === true,
      number: numberSpecified === true ? plan.number : null,
      replacement: replacement
    });
  }

  function nativePDFDirectPageCardRequestFingerprint(input, code) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) {
      throw outgoingRequestError('页面卡片修改参数无效', code, 400);
    }
    var operation = input.operation;
    var keys = operation === 'edit'
      ? ['operation', 'operationId', 'expectedId', 'expectedRevision', 'replacement']
      : ['operation', 'operationId', 'expectedId', 'expectedRevision'];
    if (Object.prototype.hasOwnProperty.call(input, 'number')) keys.push('number');
    if ((operation !== 'edit' && operation !== 'delete') || !exactKeys(input, keys) ||
        !/^pcard_[0-9a-f]{24}$/.test(String(input.operationId || ''))) {
      throw outgoingRequestError('页面卡片修改合同无效', code, 400);
    }
    var replacement = null;
    if (operation === 'edit') {
      if (exactKeys(input.replacement, ['cards'])) {
        replacement = {
          cards: nativePDFPageCardWireCards(
            nativePDFPageCardCards(input.replacement.cards, code)
          )
        };
      } else if (exactKeys(input.replacement, ['content'])) {
        replacement = {
          content: nativePDFPageCardHTML(input.replacement.content, code).content
        };
      } else {
        throw outgoingRequestError('页面卡片替换内容无效', code, 400);
      }
    }
    return canonicalJSONString({
      operation: operation,
      expectedId: localRecordId(input.expectedId, code),
      expectedRevision: strictInteger(
        input.expectedRevision, 0, Number.MAX_SAFE_INTEGER, 'expectedRevision', code
      ),
      numberSpecified: Object.prototype.hasOwnProperty.call(input, 'number'),
      number: Object.prototype.hasOwnProperty.call(input, 'number')
        ? strictInteger(input.number, 1, 1000000, 'number', code) : null,
      replacement: replacement
    });
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

  function nativePDFRecordSet(queryOptions) {
    var specs = [
      ['document-highlights', []],
      ['document-notes-legacy', []],
      ['card-placements', []],
      ['entity-references', []],
      ['pdf-assistant-undo', []],
      ['pdf-assistant-ops', []]
    ];
    return Promise.all(specs.map(function (spec) {
      if (HIGHLIGHT_SPLIT_KINDS[spec[0]]) {
        return readHighlightCollection(spec[0], queryOptions);
      }
      return storedStateRecord(
        stores.document, spec[0], 'documentId', bookId, spec[1], queryOptions
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

  function nativePDFExpectedRevisions(state) {
    return state && state.revisions && typeof state.revisions === 'object'
      ? state.revisions : (state || {});
  }

  function nativePDFAssertPageCardReference(state, plan, stableIDOnly) {
    var projection = state && state.page_cards;
    if (!projection) return;
    var revisionMatches = Number(projection.revision) ===
      Number(plan.expectedRevision);
    var rows = projection.contract === NATIVE_PAGE_CARD_PROJECTION_CONTRACT &&
      (stableIDOnly || plan.number == null || revisionMatches) && projection.pages &&
      Array.isArray(projection.pages[String(plan.page)])
      ? projection.pages[String(plan.page)] : null;
    if (!rows) nativePDFRevisionConflict('页面卡片投影');
    var row = rows.find(function (candidate) {
      return candidate && String(candidate.id || '') === plan.id;
    });
    if (!row || (!stableIDOnly && plan.number != null &&
        (row.unbound === true || Number(row.number) !== Number(plan.number)))) {
      nativePDFRevisionConflict(stableIDOnly || plan.number == null
        ? '页面卡片稳定 ID' : '页面卡片当前序号');
    }
  }

  function nativePDFPageCardCanonicalRepository() {
    var repository = runtimeRoot.cardRepository;
    if (!repository || typeof repository.load !== 'function' ||
        typeof repository.replaceContent !== 'function') return null;
    return repository;
  }

  function nativePDFPageCardSemanticCards(existing, replacement, code) {
    if (!Array.isArray(existing) || existing.length !== replacement.length) {
      throw outgoingRequestError('学习卡批内数量不得改变', code, 409);
    }
    return replacement.map(function (face, index) {
      var old = existing[index] && typeof existing[index] === 'object'
        ? existing[index] : {};
      var merged = clone(face);
      ['deck', 'tags', 'reason'].forEach(function (field) {
        if (Object.prototype.hasOwnProperty.call(old, field)) {
          merged[field] = clone(old[field]);
        }
      });
      return merged;
    });
  }

  function nativePDFPageCardCanonicalIntent(plan, operationID, code) {
    if (!plan.replacementCards || !plan.canonicalId) return Promise.resolve(null);
    var repository = nativePDFPageCardCanonicalRepository();
    if (!repository) {
      return Promise.reject(new RuntimeError(
        '统一学习卡仓库尚未准备好', 'BW_NATIVE_PDF_PAGE_CARD_ENTITY'
      ));
    }
    return repository.load(plan.canonicalId).then(function (record) {
      if (!record || record.deleted || !Array.isArray(record.cards) ||
          !Number.isSafeInteger(Number(record.entityRev))) {
        throw outgoingRequestError('统一学习卡实体不存在', code, 409);
      }
      if (!plan.before.card || !Array.isArray(plan.before.card.cards) ||
          canonicalJSONString(plan.before.card.cards) !==
          canonicalJSONString(record.cards)) {
        nativePDFRevisionConflict('统一学习卡与页面副本内容');
      }
      return {
        id: plan.canonicalId,
        beforeCards: clone(record.cards),
        afterCards: nativePDFPageCardSemanticCards(
          record.cards, plan.replacementCards, code
        ),
        beforeEntityRev: Number(record.entityRev),
        lastEntityRev: null,
        mutationId: operationID + ':entity:apply'
      };
    });
  }

  function nativePDFPageCardApplyCanonical(entity, code) {
    if (!entity) return Promise.resolve(null);
    var repository = nativePDFPageCardCanonicalRepository();
    if (!repository) {
      return Promise.reject(new RuntimeError(
        '统一学习卡仓库尚未准备好', 'BW_NATIVE_PDF_PAGE_CARD_ENTITY'
      ));
    }
    return repository.load(entity.id).then(function (record) {
      if (!record || record.deleted || !Array.isArray(record.cards)) {
        throw outgoingRequestError('统一学习卡实体不存在', code, 409);
      }
      var current = canonicalJSONString(record.cards);
      var before = canonicalJSONString(entity.beforeCards);
      var after = canonicalJSONString(entity.afterCards);
      if (current === after) {
        entity.lastEntityRev = Number(record.entityRev);
        return clone(entity);
      }
      if (current !== before || Number(record.entityRev) !== Number(entity.beforeEntityRev)) {
        nativePDFRevisionConflict('统一学习卡内容');
      }
      return repository.replaceContent(entity.id, entity.afterCards, {
        ifEntityRev: Number(record.entityRev), mutationId: entity.mutationId
      }).then(function (updated) {
        entity.lastEntityRev = Number(updated.entityRev);
        return clone(entity);
      });
    });
  }

  function nativePDFPageCardRecordSet(queryOptions) {
    var specs = [
      ['document-notes-legacy', []],
      ['card-placements', []],
      ['entity-references', []],
      ['pdf-assistant-ops', []]
    ];
    return Promise.all(specs.map(function (spec) {
      return storedStateRecord(
        stores.document, spec[0], 'documentId', bookId, spec[1], queryOptions
      );
    })).then(function (records) { return { specs: specs, records: records }; });
  }

  function nativePDFPageCardJournalValue(
    plan, operationID, kind, fingerprint, requestFingerprint, entity
  ) {
    return {
      id: operationID,
      contract: 'reader-native-page-card-action/1',
      kind: kind,
      state: 'preparing',
      fingerprint: fingerprint,
      requestFingerprint: requestFingerprint,
      placementId: plan.id,
      page: plan.page,
      number: plan.number,
      expectedRevision: plan.expectedRevision,
      before: clone(plan.before),
      after: clone(plan.after),
      beforeIndex: null,
      entity: clone(entity),
      transition: 0,
      ts: nowSeconds()
    };
  }

  function boundedAssistantOperationReceipts(value) {
    var receipts = Array.isArray(value) ? value : [];
    var keep = new Set();
    var byteCount = 2;
    var maxCount = 160;
    var maxBytes = 4 * 1024 * 1024;
    function sizeOf(item) {
      try { return utf8(JSON.stringify(item)).byteLength + 1; }
      catch (_) { return maxBytes + 1; }
    }
    // An interrupted page-card saga is recovery authority. It must never be
    // discarded merely because newer, already-complete receipts arrived.
    receipts.forEach(function (item, index) {
      if (item && item.contract === 'reader-native-page-card-action/1' &&
          (item.state === 'preparing' || item.pending)) {
        keep.add(index);
        byteCount += sizeOf(item);
      }
    });
    for (var index = receipts.length - 1;
         index >= 0 && keep.size < maxCount; index -= 1) {
      if (keep.has(index)) continue;
      var itemSize = sizeOf(receipts[index]);
      if (byteCount + itemSize > maxBytes) continue;
      keep.add(index);
      byteCount += itemSize;
    }
    return receipts.filter(function (_, index) { return keep.has(index); });
  }

  function nativePDFCommitPageCardAction(actions, actionIndex, expectedState, writerLease) {
    assertNativePDFWriterLease(writerLease);
    var action = actions[actionIndex];
    var descriptor = nativePDFActionDescriptor(action);
    var code = 'BW_NATIVE_PDF_ASSISTANT_ACTION';
    var plan = nativePDFPageCardPlan(descriptor.data, code);
    var operationID = nativePDFOperationID(action, descriptor);
    var fingerprint = nativePDFPageCardFingerprint(plan, operationID, descriptor.kind);
    var expectedRevisions = nativePDFExpectedRevisions(expectedState);
    var stableIDOnly = plan.number == null ||
      !!(expectedState && expectedState.page_card_stable_id === true);
    var requestFingerprint = nativePDFPageCardRequestFingerprint(
      plan, descriptor.kind, !stableIDOnly
    );
    var bound = { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS };
    return serializeLocalStateMutation('document', 'pdf-assistant-bundle', function () {
      assertNativePDFWriterLease(writerLease);
      return nativePDFPageCardRecordSet(bound).then(function (recordSet) {
        var notes = storedList(clone(recordSet.records[0].payload), code);
        var receipts = storedList(clone(recordSet.records[3].payload), code);
        var prior = receipts.find(function (item) {
          return item && String(item.id || '') === operationID;
        });
        if (prior) {
          if (prior.contract !== 'reader-native-page-card-action/1' ||
              prior.fingerprint !== fingerprint ||
              prior.requestFingerprint !== requestFingerprint) {
            nativePDFRevisionConflict('页面卡片操作编号');
          }
          if (prior.state === 'done') {
            var replayOutput = actions.map(clone);
            replayOutput[actionIndex] = nativePDFSanitizeAction(action, descriptor);
            return {
              actions: replayOutput,
              revisions: clone(expectedRevisions),
              replayed: true,
              receipt: clone(prior)
            };
          }
          if (prior.state === 'undone') {
            throw outgoingRequestError(
              '这次页面卡片操作已经撤销，不能按原请求伪装成已执行', code, 409
            );
          }
          if (prior.state !== 'preparing') {
            throw outgoingRequestError('页面卡片操作日志状态无效', code, 409);
          }
          return null;
        }
        var listRevisionChanged =
          Number(expectedRevisions.notes) !== Number(plan.expectedRevision) ||
          Number(recordSet.records[0].rev) !== Number(plan.expectedRevision);
        // Visible numbers are list-owned and can never be rebased.  Stable-id
        // operations may cross an unrelated list revision only when the exact
        // target snapshot below still matches; canonical learning-card content
        // keeps its independent entityRev CAS in the following intent/apply.
        if (listRevisionChanged && !stableIDOnly) {
          nativePDFRevisionConflict('页面卡片列表');
        }
        nativePDFAssertPageCardReference(expectedState, plan, stableIDOnly);
        var noteIndex = notes.findIndex(function (note) {
          return note && String(note.id || '') === plan.id;
        });
        if (noteIndex < 0 || canonicalJSONString(
          nativePDFNoteSnapshot(notes[noteIndex], plan.id, code)
        ) !== canonicalJSONString(plan.before)) {
          nativePDFRevisionConflict('页面卡片内容');
        }
        return nativePDFPageCardCanonicalIntent(plan, operationID, code).then(function (entityIntent) {
          var journal = nativePDFPageCardJournalValue(
            plan, operationID, descriptor.kind, fingerprint,
            requestFingerprint, entityIntent
          );
          if (entityIntent && journal.after && journal.after.card) {
            journal.after.card.cards = clone(entityIntent.afterCards);
          }
          journal.beforeIndex = noteIndex;
          receipts.push(journal);
          receipts = boundedAssistantOperationReceipts(receipts);
          return stores.document.batch([
            stateRecordMutation(
              'pdf-assistant-ops', receipts, randomHex(12) + '-page-card-intent',
              recordSet.records[3].rev
            )
          ], bound).then(function () { return null; });
        });
      }).then(function (early) {
        if (early) return early;
        return nativePDFPageCardRecordSet(bound).then(function (recordSet) {
          var receipts = storedList(clone(recordSet.records[3].payload), code);
          var journal = receipts.find(function (item) {
            return item && String(item.id || '') === operationID;
          });
          if (!journal || journal.state !== 'preparing' ||
              journal.fingerprint !== fingerprint) {
            nativePDFRevisionConflict('页面卡片操作日志');
          }
          return nativePDFPageCardApplyCanonical(journal.entity, code).then(function (entity) {
            journal.entity = clone(entity);
            return nativePDFPageCardRecordSet(bound).then(function (currentSet) {
              var currentNotes = storedList(clone(currentSet.records[0].payload), code);
              var currentReceipts = storedList(clone(currentSet.records[3].payload), code);
              var currentJournalIndex = currentReceipts.findIndex(function (item) {
                return item && String(item.id || '') === operationID;
              });
              if (currentJournalIndex < 0 ||
                  currentReceipts[currentJournalIndex].state !== 'preparing' ||
                  currentReceipts[currentJournalIndex].fingerprint !== fingerprint) {
                nativePDFRevisionConflict('页面卡片操作日志');
              }
              var noteIndex = currentNotes.findIndex(function (note) {
                return note && String(note.id || '') === journal.placementId;
              });
              if (noteIndex < 0 || canonicalJSONString(
                nativePDFNoteSnapshot(currentNotes[noteIndex], journal.placementId, code)
              ) !== canonicalJSONString(journal.before)) {
                nativePDFRevisionConflict('页面卡片内容');
              }
              if (descriptor.kind === 'page-card-delete') currentNotes.splice(noteIndex, 1);
              else currentNotes[noteIndex] = clone(journal.after);
              journal = clone(currentReceipts[currentJournalIndex]);
              journal.entity = clone(entity);
              journal.state = 'done';
              journal.updatedAt = Date.now();
              currentReceipts[currentJournalIndex] = journal;
              var placements = deriveCardPlacements(currentNotes);
              var references = deriveEntityReferences(placements);
              var suffix = randomHex(12);
              return stores.document.batch([
                stateRecordMutation(
                  'document-notes-legacy', currentNotes, suffix + '-page-card-notes',
                  currentSet.records[0].rev
                ),
                stateRecordMutation(
                  'card-placements', placements, suffix + '-page-card-placements',
                  currentSet.records[1].rev
                ),
                stateRecordMutation(
                  'entity-references', references, suffix + '-page-card-references',
                  currentSet.records[2].rev
                ),
                stateRecordMutation(
                  'pdf-assistant-ops', currentReceipts, suffix + '-page-card-ops',
                  currentSet.records[3].rev
                )
              ], bound).then(function () {
                announceLocalNotesChanged('page-card');
                var output = actions.map(clone);
                output[actionIndex] = nativePDFSanitizeAction(action, descriptor);
                return storedStateRecord(
                  stores.document, 'document-notes-legacy', 'documentId', bookId, [], bound
                ).then(function (updatedNotes) {
                  return {
                    actions: output,
                    revisions: Object.assign({}, expectedRevisions, {
                      notes: updatedNotes.rev
                    }),
                    replayed: false,
                    receipt: clone(journal)
                  };
                });
              });
            });
          });
        });
      });
    });
  }

  function nativePDFPageCardProjectionState(localProjection) {
    var page = Number(localProjection && localProjection.page);
    var revision = Number(localProjection && localProjection.revision);
    if (!localProjection || localProjection.contract !==
        LOCAL_PAGE_CARD_PROJECTION_CONTRACT ||
        !Number.isSafeInteger(page) || page < 1 ||
        !Number.isSafeInteger(revision) || revision < 0 ||
        !Array.isArray(localProjection.cards)) {
      throw new RuntimeError(
        '页面卡片精确序号暂不可用', 'BW_NATIVE_PDF_PAGE_CARDS_PROJECTION'
      );
    }
    var rows = localProjection.cards.map(function (card) {
      return {
        id: String(card && card.id || ''),
        number: card && card.number == null ? null : Number(card.number),
        kind: String(card && card.kind || ''),
        label: String(card && card.label || ''),
        text: String(card && card.text || ''),
        bind: card && card.bind == null ? null : clone(card.bind),
        unbound: !!(card && card.unbound)
      };
    });
    var pages = {};
    pages[String(page)] = rows;
    return {
      contract: NATIVE_PAGE_CARD_PROJECTION_CONTRACT,
      revision: revision,
      pages: pages
    };
  }

  function nativePDFDirectPageCardData(input, projection, note, code) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) {
      throw outgoingRequestError('页面卡片修改参数无效', code, 400);
    }
    var operation = input.operation;
    var keys = operation === 'edit'
      ? ['operation', 'operationId', 'expectedId', 'expectedRevision', 'replacement']
      : ['operation', 'operationId', 'expectedId', 'expectedRevision'];
    var hasNumber = Object.prototype.hasOwnProperty.call(input, 'number');
    if (hasNumber) keys.push('number');
    if ((operation !== 'edit' && operation !== 'delete') || !exactKeys(input, keys) ||
        !/^pcard_[0-9a-f]{24}$/.test(String(input.operationId || ''))) {
      throw outgoingRequestError('页面卡片修改合同无效', code, 400);
    }
    var requestedNumber = hasNumber
      ? strictInteger(input.number, 1, 1000000, 'number', code) : null;
    var expectedId = localRecordId(input.expectedId, code);
    var expectedRevision = strictInteger(
      input.expectedRevision, 0, Number.MAX_SAFE_INTEGER, 'expectedRevision', code
    );
    if (Number(projection.revision) !== expectedRevision &&
        (hasNumber || !nativePDFCachedPageCardUnchanged(
          expectedRevision, note, expectedId, code
        ))) {
      nativePDFRevisionConflict('页面卡片列表');
    }
    var row = projection.cards.find(function (candidate) {
      if (!candidate) return false;
      return requestedNumber == null
        ? String(candidate.id || '') === expectedId
        : Number(candidate.number) === requestedNumber;
    });
    if (!row || String(row.id || '') !== expectedId ||
        (requestedNumber != null && row.unbound === true)) {
      nativePDFRevisionConflict(requestedNumber == null
        ? '页面卡片稳定 ID' : '页面卡片当前序号');
    }
    var number = row.unbound === true || row.number == null
      ? null : strictInteger(row.number, 1, 1000000, 'number', code);
    var before = nativePDFNoteSnapshot(note, expectedId, code);
    var after = null;
    if (operation === 'edit') {
      if (!input.replacement || typeof input.replacement !== 'object' ||
          Array.isArray(input.replacement)) {
        throw outgoingRequestError('页面卡片替换内容无效', code, 400);
      }
      after = clone(before);
      if (exactKeys(input.replacement, ['cards']) && before.card) {
        after.card.cards = nativePDFPageCardCards(input.replacement.cards, code);
        after.card.contextText = nativePDFPageCardContextText(after.card.cards);
      } else if (exactKeys(input.replacement, ['content']) && before.html) {
        after.html.content = boundedLocalString(
          input.replacement.content, LOCAL_PAGE_CARD_CONTEXT_LIMIT,
          '', code, '卡片内容', false
        );
        after.html.contextText = '';
      } else {
        throw outgoingRequestError('替换内容与卡片类型不匹配', code, 400);
      }
      after.updated = nowSeconds();
    }
    var data = {
      type: 'page-card', op: operation,
      native_operation_id: String(input.operationId),
      file: localFileRef(), page: Number(projection.page), number: number,
      expected_id: expectedId, expected_revision: expectedRevision,
      item: { id: expectedId, before: before }
    };
    if (after) data.item.after = after;
    return data;
  }

  function nativeReaderPageCardMutate(input) {
    var code = 'BW_NATIVE_READER_PAGE_CARD';
    return bootPromise.then(function () {
      if (nativeInterfaceSurface !== 'pdf') {
        throw new RuntimeError(
          '当前界面不是 PDF 页面卡片宿主', 'BW_NATIVE_READER_PAGE_CARD_SURFACE'
        );
      }
      var computerVoice = root.RC && root.RC.computerVoice;
      if (!computerVoice || typeof computerVoice.pageCards !== 'function') {
        throw new RuntimeError(
          '当前页卡片精确序号不可用', 'BW_NATIVE_PDF_PAGE_CARDS_PROJECTION'
        );
      }
      return withNativePDFWriter('reader-page-card', function (writerLease) {
        var requestFingerprint = nativePDFDirectPageCardRequestFingerprint(input, code);
        return storedStateRecord(
          stores.document, 'pdf-assistant-ops', 'documentId', bookId, [],
          { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS }
        ).then(function (opsRecord) {
          var prior = storedList(opsRecord.payload, code).find(function (item) {
            return item && String(item.id || '') === String(input.operationId || '');
          });
          if (!prior) return null;
          if (prior.contract !== 'reader-native-page-card-action/1' ||
              prior.requestFingerprint !== requestFingerprint) {
            nativePDFRevisionConflict('页面卡片操作编号');
          }
          if (prior.state === 'done') {
            return {
              ok: true,
              operationId: String(input.operationId),
              operation: String(input.operation),
              page: Number(prior.page),
              number: prior.number == null ? null : Number(prior.number),
              id: String(prior.placementId),
              replayed: true
            };
          }
          if (prior.state === 'undone') {
            throw outgoingRequestError(
              '这次页面卡片操作已经撤销，不能按原请求伪装成已执行', code, 409
            );
          }
          return null;
        }).then(function (replayed) {
          if (replayed) return replayed;
          return Promise.resolve(computerVoice.pageCards()).then(function (projection) {
          return storedStateRecord(
            stores.document, 'document-notes-legacy', 'documentId', bookId, [],
            { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS }
          ).then(function (notesRecord) {
            if (Number(notesRecord.rev) !== Number(projection.revision)) {
              nativePDFRevisionConflict('页面卡片列表');
            }
            var expectedId = localRecordId(input && input.expectedId, code);
            var note = storedList(notesRecord.payload, code).find(function (candidate) {
              return candidate && String(candidate.id || '') === expectedId;
            });
            if (!note) nativePDFRevisionConflict('页面卡片内容');
            var data = nativePDFDirectPageCardData(
              input, projection, note, code
            );
            var action = { fn: '_assistEdit', args: [data] };
            var expectedState = {
              revisions: { notes: Number(notesRecord.rev) },
              page_cards: nativePDFPageCardProjectionState(projection),
              page_card_stable_id: !Object.prototype.hasOwnProperty.call(
                input, 'number'
              )
            };
            return nativePDFCommitActions(
              [action], expectedState, writerLease
            ).then(function (committed) {
              var sanitized = committed.actions[0];
              if (committed.replayed !== true && sanitized &&
                  sanitized.fn === '_assistEdit' &&
                  Array.isArray(sanitized.args) && typeof root._assistEdit === 'function') {
                root._assistEdit(clone(sanitized.args[0]));
              }
              return {
                ok: true,
                operationId: String(input.operationId),
                operation: String(input.operation),
                page: Number(projection.page),
                number: data.number == null ? null : Number(data.number),
                id: expectedId,
                replayed: committed.replayed === true
              };
            });
          });
        });
        });
      });
    });
  }

  function nativePDFPageCardTransitionCanonical(
    entity, targetState, operationID, transition, code
  ) {
    if (!entity) return Promise.resolve(null);
    var repository = nativePDFPageCardCanonicalRepository();
    if (!repository) {
      return Promise.reject(new RuntimeError(
        '统一学习卡仓库尚未准备好', 'BW_NATIVE_PDF_PAGE_CARD_ENTITY'
      ));
    }
    var targetCards = targetState === 'done' ? entity.afterCards : entity.beforeCards;
    var otherCards = targetState === 'done' ? entity.beforeCards : entity.afterCards;
    return repository.load(entity.id).then(function (record) {
      if (!record || record.deleted || !Array.isArray(record.cards)) {
        throw outgoingRequestError('统一学习卡实体不存在', code, 409);
      }
      var current = canonicalJSONString(record.cards);
      if (current === canonicalJSONString(targetCards)) {
        entity.lastEntityRev = Number(record.entityRev);
        return clone(entity);
      }
      if (current !== canonicalJSONString(otherCards)) {
        nativePDFRevisionConflict('统一学习卡内容');
      }
      return repository.replaceContent(entity.id, targetCards, {
        ifEntityRev: Number(record.entityRev),
        mutationId: operationID + ':t' + String(transition) + ':entity'
      }).then(function (updated) {
        entity.lastEntityRev = Number(updated.entityRev);
        return clone(entity);
      });
    });
  }

  function nativePDFRecoverPendingPageCardJournals(writerLease) {
    if (nativeInterfaceSurface !== 'pdf' || !stores || !stores.document) {
      return Promise.resolve(false);
    }
    var code = 'BW_NATIVE_PDF_PAGE_CARD_RECOVERY';
    var bound = { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS };
    return serializeLocalStateMutation('document', 'pdf-assistant-bundle', function () {
      var recovered = false;

      function sameNote(note, snapshot, placementID) {
        if (!note || !snapshot) return false;
        return canonicalJSONString(
          nativePDFNoteSnapshot(note, placementID, code)
        ) === canonicalJSONString(snapshot);
      }

      function placementPhase(notes, placementID, kind, targetState, before, after) {
        var noteIndex = notes.findIndex(function (note) {
          return note && String(note.id || '') === placementID;
        });
        if (kind === 'page-card-delete') {
          if (targetState === 'done') {
            if (noteIndex < 0) return { phase: 'target', index: noteIndex };
            return sameNote(notes[noteIndex], before, placementID)
              ? { phase: 'source', index: noteIndex }
              : { phase: 'conflict', index: noteIndex };
          }
          if (noteIndex < 0) return { phase: 'source', index: noteIndex };
          return sameNote(notes[noteIndex], before, placementID)
            ? { phase: 'target', index: noteIndex }
            : { phase: 'conflict', index: noteIndex };
        }
        if (noteIndex < 0) return { phase: 'conflict', index: noteIndex };
        var target = targetState === 'done' ? after : before;
        var source = targetState === 'done' ? before : after;
        if (sameNote(notes[noteIndex], target, placementID)) {
          return { phase: 'target', index: noteIndex };
        }
        if (sameNote(notes[noteIndex], source, placementID)) {
          return { phase: 'source', index: noteIndex };
        }
        return { phase: 'conflict', index: noteIndex };
      }

      function terminalizeConflict(
        recordSet, receipts, journalIndex, journal, operationID, sourceState
      ) {
        journal.state = 'conflicted';
        journal.pending = null;
        journal.recoveryError = 'placement-content-conflict';
        journal.updatedAt = Date.now();
        receipts[journalIndex] = journal;
        return stores.document.batch([stateRecordMutation(
          'pdf-assistant-ops', boundedAssistantOperationReceipts(receipts),
          randomHex(12) + '-page-card-recovery-conflict', recordSet.records[3].rev
        )], bound).then(function () {
          recovered = true;
          try {
            if (typeof root._toast === 'function') {
              root._toast('一项卡片操作与较新的页面内容冲突，已停止自动恢复');
            }
          } catch (_) {}
          // If the prior process had already changed the entity, restore the
          // saga's source face with an entity CAS. The journal is terminalized
          // first, so even a failed compensation cannot lock every later PDF
          // write behind an eternal pending record.
          return nativePDFPageCardTransitionCanonical(
            clone(journal.entity), sourceState, operationID,
            Number(journal.transition || 0) + 1000000, code
          ).catch(function () { return null; }).then(function () { return next(); });
        });
      }

      function next() {
        assertNativePDFWriterLease(writerLease);
        return nativePDFPageCardRecordSet(bound).then(function (recordSet) {
          var receipts = storedList(clone(recordSet.records[3].payload), code);
          var journalIndex = receipts.findIndex(function (item) {
            return item && item.contract === 'reader-native-page-card-action/1' &&
              (item.state === 'preparing' || item.pending);
          });
          if (journalIndex < 0) return recovered;

          var journal = clone(receipts[journalIndex]);
          var operationID = String(journal.id || '');
          var placementID = localRecordId(journal.placementId, code);
          var kind = String(journal.kind || '');
          if (!/^(?:npdf|pcard)_[0-9a-f]{24}$/.test(operationID) ||
              (kind !== 'page-card-edit' && kind !== 'page-card-delete')) {
            throw outgoingRequestError('页面卡片恢复日志无效', code, 409);
          }
          var before = nativePDFNoteSnapshot(journal.before, placementID, code);
          var after = kind === 'page-card-edit'
            ? nativePDFNoteSnapshot(journal.after, placementID, code) : null;
          var preparing = journal.state === 'preparing';
          var targetState = preparing ? 'done'
            : String(journal.pending && journal.pending.target || '');
          var sourceState = targetState === 'done' ? 'undone' : 'done';
          if ((targetState !== 'done' && targetState !== 'undone') ||
              (!preparing && journal.state !== sourceState)) {
            throw outgoingRequestError('页面卡片恢复状态无效', code, 409);
          }
          var initialNotes = storedList(clone(recordSet.records[0].payload), code);
          var initialPhase = placementPhase(
            initialNotes, placementID, kind, targetState, before, after
          );
          if (initialPhase.phase === 'conflict') {
            return terminalizeConflict(
              recordSet, receipts, journalIndex, journal, operationID, sourceState
            );
          }
          var canonical = preparing
            ? nativePDFPageCardApplyCanonical(clone(journal.entity), code)
            : nativePDFPageCardTransitionCanonical(
                clone(journal.entity), targetState, operationID,
                Number(journal.transition || 0), code
              );

          return canonical.then(function (entity) {
            assertNativePDFWriterLease(writerLease);
            return nativePDFPageCardRecordSet(bound).then(function (currentSet) {
              var notes = storedList(clone(currentSet.records[0].payload), code);
              var currentReceipts = storedList(clone(currentSet.records[3].payload), code);
              var currentIndex = currentReceipts.findIndex(function (item) {
                return item && String(item.id || '') === operationID;
              });
              if (currentIndex < 0) {
                nativePDFRevisionConflict('页面卡片恢复日志');
              }
              var currentJournal = clone(currentReceipts[currentIndex]);
              var stillPending = preparing
                ? currentJournal.state === 'preparing'
                : currentJournal.pending &&
                  currentJournal.pending.target === targetState &&
                  currentJournal.state === sourceState;
              if (!stillPending) nativePDFRevisionConflict('页面卡片恢复日志');

              var currentPhase = placementPhase(
                notes, placementID, kind, targetState, before, after
              );
              if (currentPhase.phase === 'conflict') {
                return terminalizeConflict(
                  currentSet, currentReceipts, currentIndex, currentJournal,
                  operationID, sourceState
                );
              }
              var noteIndex = currentPhase.index;
              var changed = false;
              if (currentPhase.phase === 'source') {
                if (kind === 'page-card-delete') {
                  if (targetState === 'done') {
                    notes.splice(noteIndex, 1);
                  } else {
                    var insertAt = Math.max(0, Math.min(
                      notes.length, Number(currentJournal.beforeIndex) || 0
                    ));
                    notes.splice(insertAt, 0, clone(before));
                  }
                } else {
                  notes[noteIndex] = clone(targetState === 'done' ? after : before);
                }
                changed = true;
              }

              currentJournal.entity = clone(entity);
              currentJournal.state = targetState;
              currentJournal.pending = null;
              currentJournal.updatedAt = Date.now();
              currentReceipts[currentIndex] = currentJournal;
              var suffix = randomHex(12);
              var mutations = [stateRecordMutation(
                'pdf-assistant-ops', boundedAssistantOperationReceipts(currentReceipts),
                suffix + '-page-card-recovery-ops', currentSet.records[3].rev
              )];
              if (changed) {
                var placements = deriveCardPlacements(notes);
                var references = deriveEntityReferences(placements);
                mutations.unshift(
                  stateRecordMutation(
                    'document-notes-legacy', notes,
                    suffix + '-page-card-recovery-notes', currentSet.records[0].rev
                  ),
                  stateRecordMutation(
                    'card-placements', placements,
                    suffix + '-page-card-recovery-placements', currentSet.records[1].rev
                  ),
                  stateRecordMutation(
                    'entity-references', references,
                    suffix + '-page-card-recovery-references', currentSet.records[2].rev
                  )
                );
              }
              return stores.document.batch(mutations, bound).then(function () {
                recovered = true;
                if (changed) {
                  announceLocalNotesChanged('page-card-recovery');
                  try { if (typeof root.notesReload === 'function') root.notesReload(); } catch (_) {}
                }
                return next();
              });
            });
          });
        });
      }

      return next();
    });
  }

  function nativeReaderPageCardAction(input) {
    var code = 'BW_NATIVE_READER_PAGE_CARD_ACTION';
    if (!exactKeys(input, ['operationId', 'action']) ||
        !/^(?:npdf|pcard)_[0-9a-f]{24}$/.test(String(input.operationId || '')) ||
        (input.action !== 'undo' && input.action !== 'redo')) {
      return Promise.reject(outgoingRequestError(
        '页面卡片撤销参数无效', code, 400
      ));
    }
    var operationID = String(input.operationId);
    var targetState = input.action === 'undo' ? 'undone' : 'done';
    var sourceState = input.action === 'undo' ? 'done' : 'undone';
    var bound = { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS };
    return bootPromise.then(function () {
      if (nativeInterfaceSurface !== 'pdf') {
        throw new RuntimeError(
          '当前界面不是 PDF 页面卡片宿主', 'BW_NATIVE_READER_PAGE_CARD_SURFACE'
        );
      }
      return withNativePDFWriter('reader-page-card-action', function (writerLease) {
        return serializeLocalStateMutation('document', 'pdf-assistant-bundle', function () {
          assertNativePDFWriterLease(writerLease);
          return nativePDFPageCardRecordSet(bound).then(function (recordSet) {
            var receipts = storedList(clone(recordSet.records[3].payload), code);
            var journalIndex = receipts.findIndex(function (item) {
              return item && String(item.id || '') === operationID;
            });
            if (journalIndex < 0 || receipts[journalIndex].contract !==
                'reader-native-page-card-action/1') {
              throw outgoingRequestError('页面卡片操作记录已失效', code, 404);
            }
            var journal = clone(receipts[journalIndex]);
            if (journal.state === targetState && !journal.pending) {
              return {
                ok: true, operationId: operationID, action: input.action,
                state: targetState, replayed: true,
                page: journal.page, number: journal.number, id: journal.placementId
              };
            }
            if (journal.pending) {
              if (journal.pending.target !== targetState) {
                nativePDFRevisionConflict('页面卡片撤销状态');
              }
            } else {
              if (journal.state !== sourceState) {
                nativePDFRevisionConflict('页面卡片撤销状态');
              }
              journal.transition = Number(journal.transition || 0) + 1;
              journal.pending = { target: targetState, step: 'intent' };
              journal.updatedAt = Date.now();
              receipts[journalIndex] = journal;
              return stores.document.batch([
                stateRecordMutation(
                  'pdf-assistant-ops', receipts,
                  randomHex(12) + '-page-card-transition-intent',
                  recordSet.records[3].rev
                )
              ], bound).then(function () { return null; });
            }
            return null;
          }).then(function (early) {
            if (early) return early;
            return nativePDFPageCardRecordSet(bound).then(function (intentSet) {
              var intentReceipts = storedList(clone(intentSet.records[3].payload), code);
              var intentIndex = intentReceipts.findIndex(function (item) {
                return item && String(item.id || '') === operationID;
              });
              var journal = intentIndex >= 0 ? clone(intentReceipts[intentIndex]) : null;
              if (!journal || !journal.pending ||
                  journal.pending.target !== targetState) {
                nativePDFRevisionConflict('页面卡片撤销日志');
              }
              return nativePDFPageCardTransitionCanonical(
                journal.entity, targetState, operationID,
                Number(journal.transition || 0), code
              ).then(function (entity) {
                return nativePDFPageCardRecordSet(bound).then(function (currentSet) {
                  var notes = storedList(clone(currentSet.records[0].payload), code);
                  var currentReceipts = storedList(clone(currentSet.records[3].payload), code);
                  var currentIndex = currentReceipts.findIndex(function (item) {
                    return item && String(item.id || '') === operationID;
                  });
                  var currentJournal = currentIndex >= 0
                    ? clone(currentReceipts[currentIndex]) : null;
                  if (!currentJournal || !currentJournal.pending ||
                      currentJournal.pending.target !== targetState) {
                    nativePDFRevisionConflict('页面卡片撤销日志');
                  }
                  var noteIndex = notes.findIndex(function (note) {
                    return note && String(note.id || '') === currentJournal.placementId;
                  });
                  var kind = String(currentJournal.kind || '');
                  if (targetState === 'undone') {
                    if (kind === 'page-card-delete') {
                      if (noteIndex >= 0) nativePDFRevisionConflict('页面卡片内容');
                      var insertAt = Math.max(0, Math.min(
                        notes.length, Number(currentJournal.beforeIndex) || 0
                      ));
                      notes.splice(insertAt, 0, clone(currentJournal.before));
                    } else {
                      if (noteIndex < 0 || canonicalJSONString(
                        nativePDFNoteSnapshot(notes[noteIndex], currentJournal.placementId, code)
                      ) !== canonicalJSONString(currentJournal.after)) {
                        nativePDFRevisionConflict('页面卡片内容');
                      }
                      notes[noteIndex] = clone(currentJournal.before);
                    }
                  } else if (kind === 'page-card-delete') {
                    if (noteIndex < 0 || canonicalJSONString(
                      nativePDFNoteSnapshot(notes[noteIndex], currentJournal.placementId, code)
                    ) !== canonicalJSONString(currentJournal.before)) {
                      nativePDFRevisionConflict('页面卡片内容');
                    }
                    notes.splice(noteIndex, 1);
                  } else {
                    if (noteIndex < 0 || canonicalJSONString(
                      nativePDFNoteSnapshot(notes[noteIndex], currentJournal.placementId, code)
                    ) !== canonicalJSONString(currentJournal.before)) {
                      nativePDFRevisionConflict('页面卡片内容');
                    }
                    notes[noteIndex] = clone(currentJournal.after);
                  }
                  currentJournal.entity = clone(entity);
                  currentJournal.state = targetState;
                  currentJournal.pending = null;
                  currentJournal.updatedAt = Date.now();
                  currentReceipts[currentIndex] = currentJournal;
                  var placements = deriveCardPlacements(notes);
                  var references = deriveEntityReferences(placements);
                  var suffix = randomHex(12);
                  return stores.document.batch([
                    stateRecordMutation(
                      'document-notes-legacy', notes, suffix + '-page-card-transition-notes',
                      currentSet.records[0].rev
                    ),
                    stateRecordMutation(
                      'card-placements', placements,
                      suffix + '-page-card-transition-placements',
                      currentSet.records[1].rev
                    ),
                    stateRecordMutation(
                      'entity-references', references,
                      suffix + '-page-card-transition-references',
                      currentSet.records[2].rev
                    ),
                    stateRecordMutation(
                      'pdf-assistant-ops', currentReceipts,
                      suffix + '-page-card-transition-ops',
                      currentSet.records[3].rev
                    )
                  ], bound).then(function () {
                    announceLocalNotesChanged('page-card-' + input.action);
                    return {
                      ok: true, operationId: operationID, action: input.action,
                      state: targetState, replayed: false,
                      page: currentJournal.page, number: currentJournal.number,
                      id: currentJournal.placementId
                    };
                  });
                });
              });
            });
          });
        });
      });
    });
  }

  function nativePDFCommitActions(actions, expectedState, writerLease) {
    assertNativePDFWriterLease(writerLease);
    if (!Array.isArray(actions)) {
      return Promise.reject(new RuntimeError(
        'PDF 助手动作列表无效', 'BW_NATIVE_PDF_ASSISTANT_ACTION'
      ));
    }
    var descriptors = actions.map(nativePDFActionDescriptor);
    var pageCardIndexes = [];
    descriptors.forEach(function (descriptor, index) {
      if (descriptor && (descriptor.kind === 'page-card-edit' ||
          descriptor.kind === 'page-card-delete')) pageCardIndexes.push(index);
    });
    if (pageCardIndexes.length) {
      if (pageCardIndexes.length !== 1 || descriptors.some(function (descriptor, index) {
        return index !== pageCardIndexes[0] && !!descriptor;
      })) {
        return Promise.reject(new RuntimeError(
          '一次响应只能提交一个页面卡片动作', 'BW_NATIVE_PDF_ASSISTANT_ACTION'
        ));
      }
      return nativePDFCommitPageCardAction(
        actions, pageCardIndexes[0], expectedState, writerLease
      );
    }
    var expectedRevisions = nativePDFExpectedRevisions(expectedState);
    var hasLocal = descriptors.some(function (descriptor) { return !!descriptor; });
    if (!hasLocal) {
      return Promise.resolve({ actions: clone(actions), revisions: clone(expectedRevisions) });
    }
    var bound = { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS };
    return serializeLocalStateMutation('document', 'pdf-assistant-bundle', function () {
      assertNativePDFWriterLease(writerLease);
      return nativePDFRecordSet(bound).then(function (recordSet) {
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
        var replayed = false;
        var lastReceipt = null;

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
            replayed = true;
            lastReceipt = clone(received.get(operationID));
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
              targetKind: 'document-highlights',
              expectedRevision: recordSet.records[0].rev + 1,
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
              targetKind: 'document-notes-legacy',
              expectedRevision: recordSet.records[1].rev + 1,
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
              targetKind: 'document-notes-legacy',
              expectedRevision: recordSet.records[1].rev + 1,
              ts: nowSeconds()
            });
            touchedNotes = true;
            touchedUndo = true;
          } else if (descriptor.kind === 'undo') {
            var last = undo.length ? undo[undo.length - 1] : null;
            if (!last || typeof last !== 'object') {
              throw new RuntimeError(
                '没有可撤销的 PDF 本机书籍改动',
                'BW_NATIVE_PDF_UNDO_EMPTY'
              );
            }
            var currentTargetRevision = last.targetKind === 'document-highlights'
              ? recordSet.records[0].rev
              : (last.targetKind === 'document-notes-legacy'
                ? recordSet.records[1].rev : null);
            if (last.expectedRevision != null &&
                Number(last.expectedRevision) !== Number(currentTargetRevision)) {
              nativePDFRevisionConflict('最近的本机改动');
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
            var previousTarget = null;
            for (var undoIndex = undo.length - 1; undoIndex >= 0; undoIndex -= 1) {
              if (undo[undoIndex] && undo[undoIndex].targetKind === last.targetKind) {
                previousTarget = undo[undoIndex];
                break;
              }
            }
            if (previousTarget && currentTargetRevision != null) {
              previousTarget.expectedRevision = currentTargetRevision + 1;
            }
            touchedUndo = true;
          }

          var receipt = { id: operationID, kind: descriptor.kind, ts: nowSeconds() };
          if (descriptor.kind === 'undo' && last) {
            receipt.undone = {
              kind: String(last.kind || ''),
              id: String(last.id || '')
            };
            receipt.remaining = undo.length;
          }
          receipts.push(receipt);
          received.set(operationID, receipt);
          lastReceipt = clone(receipt);
          touchedReceipts = true;
          output.push(nativePDFSanitizeAction(action, descriptor));
        });

        undo = undo.slice(-80);
        receipts = boundedAssistantOperationReceipts(receipts);
        var suffix = randomHex(12);
        var mutations = [];
        if (touchedHighlights) {
          mutations.push.apply(mutations, highlightCollectionMutations(
            'document-highlights', recordSet.records[0].state, highlights,
            suffix + '-highlights'
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
          return {
            actions: output,
            revisions: clone(expectedRevisions),
            replayed: replayed,
            receipt: lastReceipt
          };
        }
        assertNativePDFWriterLease(writerLease);
        return stores.document.batch(mutations, bound).then(function () {
          assertNativePDFWriterLease(writerLease);
          return Promise.all([
            readHighlightCollection('document-highlights', bound),
            storedStateRecord(
              stores.document, 'document-notes-legacy', 'documentId', bookId, [], bound
            )
          ]).then(function (updated) {
            return {
              actions: output,
              replayed: replayed,
              receipt: lastReceipt,
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

  function nativePDFUndoLast(operationID) {
    operationID = String(operationID || '');
    if (!/^npdf_[0-9a-f]{24}$/.test(operationID)) {
      return Promise.reject(new RuntimeError(
        'PDF 撤销操作编号无效', 'BW_NATIVE_PDF_ASSISTANT_ACTION'
      ));
    }
    var bound = { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS };
    return withNativePDFWriter('assistant-undo-last', function (lease) {
      return Promise.all([
        readHighlightCollection('document-highlights', bound),
        storedStateRecord(
          stores.document, 'document-notes-legacy', 'documentId', bookId, [], bound
        ),
        storedStateRecord(
          stores.document, 'pdf-assistant-undo', 'documentId', bookId, [], bound
        )
      ]).then(function (records) {
        var stack = storedList(
          clone(records[2].payload), 'BW_NATIVE_PDF_ASSISTANT_ACTION'
        );
        return nativePDFCommitActions([{
          fn: '_nativePDFUndoLast', args: [operationID]
        }], {
          highlights: records[0].rev,
          notes: records[1].rev
        }, lease).then(function (committed) {
          return {
            contract: NATIVE_READER_UNDO_RESULT_CONTRACT,
            ok: true,
            surface: 'pdf',
            operationId: operationID,
            replayed: committed.replayed === true,
            undone: committed.receipt && committed.receipt.undone
              ? clone(committed.receipt.undone) : null,
            remaining: Number(
              committed.receipt && committed.receipt.remaining != null
                ? committed.receipt.remaining
                : Math.max(0, stack.length - 1)
            )
          };
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

  function nativePDFCommittedSSE(response, initialState, writerLease) {
    if (!response.ok || !response.body || typeof ReadableStream === 'undefined') {
      releaseNativePDFWriterLease(writerLease);
      return Promise.resolve(response);
    }
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var encoder = new TextEncoder();
    var buffer = '';
    var authority = clone(initialState);
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
            actions, authority, writerLease
          ).then(function (result) {
            authority.revisions = result.revisions;
            // A successful page-card write changes numbering; no later action
            // in this response may reuse the pre-write geometry projection.
            if (result.receipt && result.receipt.contract ===
                'reader-native-page-card-action/1') delete authority.page_cards;
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
      return nativePDFRecoverPendingPageCardJournals(writerLease);
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
          response, request.snapshot, writerLease
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
            actions, request.snapshot, writerLease
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
      readHighlightCollection('epub-highlights'),
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

  function nativeEPUBRecordSet(kind, queryOptions, includeAssistant) {
    var kinds = kind === 'document-notes-legacy'
      ? ['document-notes-legacy', 'card-placements', 'entity-references']
      : [kind];
    var targetCount = kinds.length;
    if (includeAssistant) {
      kinds = kinds.concat(['epub-assistant-undo']);
    }
    return Promise.all(kinds.map(function (item) {
      if (HIGHLIGHT_SPLIT_KINDS[item]) {
        return readHighlightCollection(item, queryOptions);
      }
      return storedStateRecord(
        stores.document, item, 'documentId', bookId, [], queryOptions
      );
    })).then(function (records) {
      return { kinds: kinds, records: records, targetCount: targetCount };
    });
  }

  function nativeEPUBRecordMutations(recordSet, payload, suffix, revisionDelta) {
    var mutations = [];
    recordSet.kinds.slice(0, recordSet.targetCount).forEach(function (kind, index) {
      var value;
      if (kind === recordSet.kinds[0]) value = payload;
      else if (kind === 'card-placements') value = deriveCardPlacements(payload);
      else value = deriveEntityReferences(deriveCardPlacements(payload));
      if (HIGHLIGHT_SPLIT_KINDS[kind]) {
        // 现役调用点 revisionDelta 恒为 0；门面用 meta rev 做同一套 CAS。
        mutations.push.apply(mutations, highlightCollectionMutations(
          kind, recordSet.records[index].state, value, suffix + '-' + index
        ));
        return;
      }
      mutations.push(stateRecordMutation(
        kind, value, suffix + '-' + index,
        recordSet.records[index].rev + (revisionDelta || 0)
      ));
    });
    return mutations;
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

  function nativeEPUBStackEntry(action, descriptor, expectedRevision) {
    return {
      id: String(action && action.id || ''),
      kind: String(action && action.kind || descriptor.operation || ''),
      targetKind: descriptor.kind,
      action: clone(action),
      expectedRevision: expectedRevision,
      ts: nowSeconds()
    };
  }

  function nativeEPUBPreviousTarget(stack, targetKind) {
    for (var index = stack.length - 1; index >= 0; index -= 1) {
      if (stack[index] && stack[index].targetKind === targetKind) return stack[index];
    }
    return null;
  }

  function nativeEPUBStackAfterAction(
    stack, nextAction, descriptor, requestedOp, currentRevision
  ) {
    stack = storedList(clone(stack), 'BW_NATIVE_EPUB_ACTION_STATE');
    var actionID = String(nextAction && nextAction.id || '');
    var existingIndex = -1;
    stack.forEach(function (item, index) {
      if (item && String(item.id || '') === actionID) existingIndex = index;
    });
    if (requestedOp === 'undo') {
      if (existingIndex >= 0) {
        var existing = stack[existingIndex];
        if (existing.targetKind !== descriptor.kind ||
            Number(existing.expectedRevision) !== Number(currentRevision)) {
          throw new RuntimeError(
            'EPUB 最近动作之后的内容已经变化',
            'BW_NATIVE_EPUB_UNDO_CONFLICT'
          );
        }
        stack.splice(existingIndex, 1);
        var previous = nativeEPUBPreviousTarget(stack, descriptor.kind);
        if (previous) previous.expectedRevision = currentRevision + 1;
      }
      return stack.slice(-80);
    }
    if (existingIndex >= 0) stack.splice(existingIndex, 1);
    stack.push(nativeEPUBStackEntry(
      nextAction, descriptor, currentRevision + 1
    ));
    return stack.slice(-80);
  }

  function nativeEPUBActionTransaction(action, requestedOp, metadataTask) {
    var descriptor = nativeEPUBActionOperation(action, requestedOp);
    if (!descriptor) {
      return Promise.reject(new RuntimeError(
        '不是本机 EPUB action', 'BW_NATIVE_EPUB_ACTION_NOT_LOCAL'
      ));
    }
    // A forward assistant action is allowed into the authoritative recent-action
    // stack only when it already carries a usable inverse snapshot.  Applying a
    // redo-only action and merely omitting it from the stack would leave a local
    // mutation that the promised no-argument undo can never reverse; storing the
    // malformed entry is worse because it permanently poisons the stack head.
    if (requestedOp !== 'undo') {
      var undoDescriptor = nativeEPUBActionOperation(action, 'undo');
      if (!undoDescriptor || undoDescriptor.invalidFile ||
          undoDescriptor.kind !== descriptor.kind) {
        return Promise.reject(new RuntimeError(
          'EPUB action 缺少同书同类型的撤销快照',
          'BW_NATIVE_EPUB_ACTION_BODY'
        ));
      }
    }
    // 队列里只放本地的读与写，而且两者都有界。
    //
    // 所有 EPUB assistant action 与无参撤销共享一个本机 bundle 队列；普通 CRUD
    // 依靠同批 CAS 冲突保护。Pi metadata 永远在队列外等待。
    var bound = { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS };
    return serializeLocalStateMutation('document', 'epub-assistant-bundle', function () {
      return nativeEPUBRecordSet(descriptor.kind, bound, true).then(function (recordSet) {
        var before = clone(recordSet.records[0].payload);
        var nextAction = clone(action);
        var nextDescriptor = nativeEPUBActionOperation(nextAction, requestedOp);
        var stackRecord = recordSet.records[recordSet.targetCount];
        var stack = storedList(
          clone(stackRecord.payload), 'BW_NATIVE_EPUB_ACTION_STATE'
        );
        var next = nativeEPUBApplyActionPayload(
          before, nextDescriptor, nextAction, requestedOp
        );
        nextAction.state = requestedOp === 'undo' ? 'undone' : 'done';
        stack = nativeEPUBStackAfterAction(
          stack, nextAction, nextDescriptor, requestedOp,
          recordSet.records[0].rev
        );
        var suffix = randomHex(12);
        var mutations = nativeEPUBRecordMutations(
          recordSet, next, suffix + '-commit', 0
        );
        mutations.push(stateRecordMutation(
          'epub-assistant-undo', stack, suffix + '-undo', stackRecord.rev
        ));
        return stores.document.batch(
          mutations,
          bound
        ).then(function () { return nextAction; });
      });
    }).then(function (nextAction) {
      return nativeEPUBActionMetadata(nextAction, metadataTask);
    });
  }

  function nativeEPUBUndoLast(operationID) {
    operationID = String(operationID || '');
    if (!/^epub_[0-9a-f]{24}$/.test(operationID)) {
      return Promise.reject(new RuntimeError(
        'EPUB 撤销操作编号无效', 'BW_NATIVE_EPUB_UNDO_OPERATION'
      ));
    }
    var bound = { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS };
    return serializeLocalStateMutation(
      'document', 'epub-assistant-bundle', function () {
        return Promise.all([
          storedStateRecord(
            stores.document, 'epub-assistant-undo',
            'documentId', bookId, [], bound
          ),
          storedStateRecord(
            stores.document, 'epub-assistant-ops',
            'documentId', bookId, [], bound
          )
        ]).then(function (assistantRecords) {
          var stackRecord = assistantRecords[0];
          var opsRecord = assistantRecords[1];
          var stack = storedList(
            clone(stackRecord.payload), 'BW_NATIVE_EPUB_UNDO_STATE'
          );
          var receipts = storedList(
            clone(opsRecord.payload), 'BW_NATIVE_EPUB_UNDO_STATE'
          );
          var replay = receipts.find(function (item) {
            return item && String(item.id || '') === operationID;
          });
          if (replay) {
            return {
              contract: NATIVE_READER_UNDO_RESULT_CONTRACT,
              ok: true,
              surface: 'epub',
              operationId: operationID,
              replayed: true,
              undone: clone(replay.undone),
              remaining: Number(replay.remaining || 0)
            };
          }
          var last = stack.length ? stack[stack.length - 1] : null;
          if (!last || typeof last !== 'object' || Array.isArray(last)) {
            throw new RuntimeError(
              '没有可撤销的 EPUB 本机书籍改动',
              'BW_NATIVE_EPUB_UNDO_EMPTY'
            );
          }
          var action = clone(last.action);
          var descriptor = nativeEPUBActionOperation(action, 'undo');
          if (!descriptor || descriptor.kind !== last.targetKind) {
            throw new RuntimeError(
              'EPUB 最近动作记录损坏', 'BW_NATIVE_EPUB_UNDO_STATE'
            );
          }
          return nativeEPUBRecordSet(descriptor.kind, bound, false)
            .then(function (recordSet) {
              if (Number(recordSet.records[0].rev) !== Number(last.expectedRevision)) {
                throw new RuntimeError(
                  'EPUB 最近动作之后的内容已经变化',
                  'BW_NATIVE_EPUB_UNDO_CONFLICT'
                );
              }
              var next = nativeEPUBApplyActionPayload(
                clone(recordSet.records[0].payload),
                descriptor,
                action,
                'undo'
              );
              stack.pop();
              var previous = nativeEPUBPreviousTarget(stack, descriptor.kind);
              if (previous) {
                previous.expectedRevision = recordSet.records[0].rev + 1;
              }
              var undone = {
                kind: String(last.kind || ''),
                id: String(last.id || '')
              };
              var receipt = {
                id: operationID,
                undone: clone(undone),
                remaining: stack.length,
                ts: nowSeconds()
              };
              receipts.push(receipt);
              receipts = receipts.slice(-160);
              var suffix = randomHex(12);
              var mutations = nativeEPUBRecordMutations(
                recordSet, next, suffix + '-target', 0
              );
              mutations.push(
                stateRecordMutation(
                  'epub-assistant-undo', stack.slice(-80),
                  suffix + '-undo', stackRecord.rev
                ),
                stateRecordMutation(
                  'epub-assistant-ops', receipts,
                  suffix + '-ops', opsRecord.rev
                )
              );
              return stores.document.batch(mutations, bound).then(function () {
                return {
                  contract: NATIVE_READER_UNDO_RESULT_CONTRACT,
                  ok: true,
                  surface: 'epub',
                  operationId: operationID,
                  replayed: false,
                  undone: undone,
                  remaining: stack.length
                };
              });
            });
        });
      }
    ).catch(function (error) {
      if (error && error.code === 'BW_DATA_CONFLICT') {
        throw new RuntimeError(
          'EPUB 最近动作在提交前发生变化',
          'BW_NATIVE_EPUB_UNDO_CONFLICT'
        );
      }
      throw error;
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

  // The authoritative mutation has already committed before this helper runs.
  // Refreshing the projection must therefore be best-effort: a renderer bug or
  // a page that is still mounting must never turn a completed undo into an
  // outcome-unknown mutation that the caller might retry.
  function nativeReaderRefreshAfterUndo(result) {
    var refresh = null;
    var assistant = null;
    var lookupError = null;
    try {
      var adapter = root.RC && typeof root.RC.adapter === 'function'
        ? root.RC.adapter() : null;
      assistant = adapter && adapter._host && adapter._host.asst
        ? adapter._host.asst : null;
      if (assistant && typeof assistant.reloadHighlights === 'function') {
        refresh = function () { return assistant.reloadHighlights(); };
      } else if (result && result.surface === 'pdf' &&
          typeof root._reloadHighlights === 'function') {
        // PDF boots the legacy renderer before every shared adapter path is
        // guaranteed to be mounted.  Its long-standing window hook is the
        // narrow fallback; EPUB has no equivalent global.
        refresh = function () { return root._reloadHighlights(); };
      }
    } catch (error) {
      lookupError = error;
    }
    var work = lookupError
      ? Promise.reject(lookupError)
      : (refresh ? Promise.resolve().then(refresh) : Promise.resolve());
    var notesRefresh = assistant && typeof assistant.notesReload === 'function'
      ? function () { return assistant.notesReload(); }
      : (result && result.surface === 'pdf' && typeof root.notesReload === 'function'
        ? function () { return root.notesReload(); } : null);
    if (notesRefresh) {
      work = Promise.all([
        work,
        Promise.resolve().then(notesRefresh)
      ]);
    }
    return work.then(function () {
      return result;
    }, function (error) {
      if (typeof root.dlog === 'function') {
        try {
          root.dlog(
            '本机撤销已完成，但页面刷新失败: ' +
            String(error && error.message || error || 'unknown')
          );
        } catch (_) {}
      }
      return result;
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

  var NATIVE_PAGE_OVERLAY_EVENT = 'bw:native-page-overlay-enrichment';
  var NATIVE_PAGE_OVERLAY_CONTRACT = 'reader-native-page-overlay-enrichment/1';
  var NATIVE_PAGE_OVERLAY_CACHE_KIND = 'page-overlay-enrichment-cache-v1';
  var NATIVE_PAGE_OVERLAY_CACHE_PAGES = 24;
  var NATIVE_PAGE_OVERLAY_CACHE_BYTES = 2 * 1024 * 1024;
  var nativePageOverlaySequence = 0;
  var nativePageOverlayLatest = Object.create(null);

  function nativePageOverlayLocalRevision(payload) {
    var revision = String(payload && payload.cv || '').trim();
    if (!revision || revision.length > 512 || /[\u0000-\u001f\u007f]/.test(revision)) {
      return '';
    }
    return revision;
  }

  function normalizedNativePageOverlay(remote, page, localRevision) {
    if (!remote || typeof remote !== 'object' || Array.isArray(remote) ||
        remote.ok !== true || !nativePageOverlayLocalRevision({ cv: localRevision })) {
      return null;
    }
    var payload = {
      page: page,
      localRevision: localRevision,
      vocab_marks: Array.isArray(remote.vocab_marks) ? clone(remote.vocab_marks) : [],
      vocab_sentences: Array.isArray(remote.vocab_sentences)
        ? clone(remote.vocab_sentences) : [],
      mastered_furi: Array.isArray(remote.mastered_furi)
        ? clone(remote.mastered_furi) : [],
      offset: remote.offset && typeof remote.offset === 'object' &&
        !Array.isArray(remote.offset) ? clone(remote.offset) : null,
      cv: String(remote.cv || ''),
      savedAt: Date.now()
    };
    if (payload.vocab_marks.length > 8000 ||
        payload.vocab_sentences.length > 2000 ||
        payload.mastered_furi.length > 8000) return null;
    var encoded;
    try { encoded = JSON.stringify(payload); } catch (_) { return null; }
    if (encoded.length > NATIVE_PAGE_OVERLAY_CACHE_BYTES) return null;
    payload.byteSize = encoded.length;
    return payload;
  }

  function nativePageOverlayCacheEntry(value, page, localRevision) {
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        Number(value.page) !== page || !Array.isArray(value.vocab_marks) ||
        !Array.isArray(value.vocab_sentences) || !Array.isArray(value.mastered_furi) ||
        value.localRevision !== localRevision ||
        !Number.isFinite(value.savedAt) || value.savedAt < 0) return null;
    return normalizedNativePageOverlay({
      ok: true,
      vocab_marks: value.vocab_marks,
      vocab_sentences: value.vocab_sentences,
      mastered_furi: value.mastered_furi,
      offset: value.offset,
      cv: value.cv
    }, page, localRevision);
  }

  function readNativePageOverlayCache(page, localRevision) {
    return Promise.resolve(bootPromise).then(function () {
      return readState(NATIVE_PAGE_OVERLAY_CACHE_KIND, []);
    }).then(function (items) {
      if (!Array.isArray(items)) return null;
      for (var index = 0; index < items.length; index += 1) {
        var entry = nativePageOverlayCacheEntry(items[index], page, localRevision);
        if (entry) {
          entry.savedAt = Number(items[index].savedAt);
          return entry;
        }
      }
      return null;
    }).catch(function () { return null; });
  }

  function writeNativePageOverlayCache(entry, expectedGeneration) {
    var lease = null;
    return Promise.resolve().then(function () {
      lease = acquireNativePDFWriterLease('page-overlay-cache');
      if (nativeInterfaceSurface === 'pdf' &&
          (!lease || lease.generation !== expectedGeneration)) {
        throw outgoingRequestError(
          '页面叠层缓存已跨过改页边界', 'BW_NATIVE_OVERLAY_STALE', 409
        );
      }
      assertNativePDFWriterLease(lease);
      return bootPromise;
    }).then(function () {
      return mutateDocumentState(
        NATIVE_PAGE_OVERLAY_CACHE_KIND, [], function (items) {
          items = Array.isArray(items) ? items.filter(function (item) {
            return item && Number(item.page) !== entry.page;
          }) : [];
          items.unshift(clone(entry));
          var bytes = 0;
          items = items.filter(function (item, index) {
            if (index >= NATIVE_PAGE_OVERLAY_CACHE_PAGES) return false;
            var size = Number(item && item.byteSize || 0);
            if (!Number.isFinite(size) || size < 0 || bytes + size > NATIVE_PAGE_OVERLAY_CACHE_BYTES) {
              return false;
            }
            bytes += size;
            return true;
          });
          return localStateMutationResult(items, true);
        },
        { transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS }
      );
    }).then(function () {
      assertNativePDFWriterLease(lease);
      releaseNativePDFWriterLease(lease);
      return true;
    }, function () {
      releaseNativePDFWriterLease(lease);
      return false;
    });
  }

  function announceNativePageOverlay(url, entry, source) {
    try {
      root.dispatchEvent(new CustomEvent(NATIVE_PAGE_OVERLAY_EVENT, {
        detail: {
          contract: NATIVE_PAGE_OVERLAY_CONTRACT,
          file: String(url.searchParams.get('file') || ''),
          page: entry.page,
          localRevision: entry.localRevision,
          source: source,
          savedAt: entry.savedAt,
          vocab_marks: clone(entry.vocab_marks),
          vocab_sentences: clone(entry.vocab_sentences),
          mastered_furi: clone(entry.mastered_furi),
          offset: clone(entry.offset),
          cv: entry.cv
        }
      }));
    } catch (_) {}
  }

  function nativePageOverlayFetch(input, init, url, route) {
    var page = Number(url.searchParams.get('page') || 0);
    var key = String(url.searchParams.get('file') || '') + '|' + String(page);
    var sequence = ++nativePageOverlaySequence;
    var writerGeneration = nativePDFWriterGeneration;
    nativePageOverlayLatest[key] = sequence;
    var remoteArrived = false;
    var current = function () {
      return nativePageOverlayLatest[key] === sequence &&
        nativePDFWriterGeneration === writerGeneration &&
        nativePDFWriterAccepting;
    };
    var local = localPageOverlay(url);
    var localRevision = local.then(function (response) {
      if (!response || !response.ok) return '';
      return response.clone().json().then(nativePageOverlayLocalRevision);
    }).catch(function () { return ''; });
    var remote = nativePiFetch(input, init, route).then(function (response) {
      if (!response || !response.ok) return null;
      return response.clone().json().catch(function () { return null; });
    }).catch(function () { return null; });

    // Cache and Pi enrichment are deliberately detached from the fetch result.
    // Local text/formula data can paint immediately; cached vocabulary follows
    // from IndexedDB and Pi revalidates it without adding a network floor.
    localRevision.then(function (revision) {
      if (!revision || !current()) return null;
      return readNativePageOverlayCache(page, revision);
    }).then(function (entry) {
      if (entry && current() && !remoteArrived) {
        announceNativePageOverlay(url, entry, 'cache');
      }
    });
    Promise.all([localRevision, remote]).then(function (values) {
      var revision = values[0];
      var entry = normalizedNativePageOverlay(values[1], page, revision);
      if (!entry || !current()) return;
      remoteArrived = true;
      announceNativePageOverlay(url, entry, 'pi');
      writeNativePageOverlayCache(entry, writerGeneration);
    }).catch(function () {});

    return local;
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
    '/pdf/api/review-event': Object.freeze(['POST']),
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

  // 助手创建便签的受信入口。
  //
  // 位置不由调用方给。桥接那侧只知道"用户想记一条"，不知道此刻在哪一页、哪一节；
  // 让它传坐标等于把一个它没有的事实编进协议，写错了还会把便签落到别的书上。
  // 这里用受信的当前界面与页码自己构造锚点，调用方只负责内容。
  //
  // 走本地 /pdf/api/notes 而不是另写一份持久化：校验、事务边界与状态迁移都在那条
  // 路上，绕开它就等于让同一种数据有两套写法。
  function nativeReaderCreateNote(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    var text = String(payload.text == null ? '' : payload.text);
    if (!text.trim()) {
      return Promise.reject(new RuntimeError(
        '便签内容为空', 'BW_READER_NOTE_TEXT'
      ));
    }
    if (text.length > 4000) {
      return Promise.reject(new RuntimeError(
        '便签内容过长', 'BW_READER_NOTE_TEXT'
      ));
    }
    var surface = nativeInterfaceSurface;
    if (surface !== 'pdf' && surface !== 'epub') {
      return Promise.reject(new RuntimeError(
        '当前阅读界面不支持便签', 'BW_READER_NOTE_SURFACE'
      ));
    }
    return bootPromise.then(function () {
      return readState('reading-position', null);
    }).then(function (position) {
      var page = Number(position && position.page) || 1;
      var body = {
        file: localFileRef(),
        anchor: surface === 'epub'
          ? { kind: 'epub', section: page, x: 0.12, y: 0.12 }
          : { kind: 'pdf', page: page, x: 0.12, y: 0.12 },
        text: text,
        color: '#ffffff'
      };
      // @interaction document.note.create
      return root.fetch(localBasePath() + '/pdf/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }).then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok || !data || data.ok !== true) {
            throw new RuntimeError(
              String((data && data.error) || '便签未能保存'),
              String((data && data.code) || 'BW_READER_NOTE_FAILED')
            );
          }
          return { ok: true, surface: surface, page: page, id: data.id || null };
        });
      });
    });
  }

  // 改的是已有便签的内容，不是它的位置。助手能重写一段文字，但不该把用户
  // 贴在某处的便签挪走或改色 —— 那是用户对页面的布置，不在"帮我改写"的语义里。
  // 所以这里只发 text；PATCH 未提及的字段本地会原样保留。
  function nativeReaderEditNote(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    var id = String(payload.id == null ? '' : payload.id);
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(id)) {
      return Promise.reject(new RuntimeError(
        '便签编号无效', 'BW_READER_NOTE_ID'
      ));
    }
    var text = String(payload.text == null ? '' : payload.text);
    if (!text.trim()) {
      return Promise.reject(new RuntimeError(
        '便签内容为空', 'BW_READER_NOTE_TEXT'
      ));
    }
    if (text.length > 4000) {
      return Promise.reject(new RuntimeError(
        '便签内容过长', 'BW_READER_NOTE_TEXT'
      ));
    }
    var surface = nativeInterfaceSurface;
    if (surface !== 'pdf' && surface !== 'epub') {
      return Promise.reject(new RuntimeError(
        '当前阅读界面不支持便签', 'BW_READER_NOTE_SURFACE'
      ));
    }
    return bootPromise.then(function () {
      // @interaction document.note.update
      return root.fetch(localBasePath() + '/pdf/api/notes', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: localFileRef(), id: id, text: text })
      });
    }).then(function (response) {
      return response.json().then(function (data) {
        // 便签不存在时本地回 404。这跟"写失败"是两回事：前者重试多少次都
        // 一样，后者可能只是这一刻不成。不合并，调用方才知道该不该再来。
        if (response.status === 404) {
          throw new RuntimeError('未找到该便签', 'BW_READER_NOTE_MISSING');
        }
        if (!response.ok || !data || data.ok !== true) {
          throw new RuntimeError(
            String((data && data.error) || '便签未能保存'),
            String((data && data.code) || 'BW_READER_NOTE_FAILED')
          );
        }
        return { ok: true, surface: surface, id: id };
      });
    });
  }

  // 助手读高亮：直接读本机那份，不经 Pi。
  //
  // 只回助手用得上的字段。rects 是渲染几何，占掉的体积能顶好几条正文，而助手
  // 拿它什么也做不了 —— 它要的是"划了什么、在哪一页、什么颜色"，以及能拿去
  // 撤销的那个 id。
  //
  // 装不下时截断并把 truncated 报上去。一个被悄悄截短的列表跟完整的长得一样，
  // 助手会据此说"你一共划了 12 条" —— 那比不回答更糟。
  function nativeReaderHighlights(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    var surface = nativeInterfaceSurface;
    if (surface !== 'pdf' && surface !== 'epub') {
      return Promise.reject(new RuntimeError(
        '当前阅读界面没有本机高亮', 'BW_READER_QUERY_SURFACE'
      ));
    }
    var wantPage = null;
    if (payload.page !== undefined && payload.page !== null) {
      wantPage = payload.page;
      if (typeof wantPage !== 'number' ||
          !Number.isInteger(wantPage) || wantPage < 1) {
        return Promise.reject(new RuntimeError(
          '页码无效', 'BW_READER_QUERY_PARAMS'
        ));
      }
    }
    var contains = payload.contains == null ? '' : String(payload.contains);
    if (contains.length > 256) {
      return Promise.reject(new RuntimeError(
        '过滤文字过长', 'BW_READER_QUERY_PARAMS'
      ));
    }
    var needle = contains.trim().toLowerCase();
    var kind = surface === 'epub' ? 'epub-highlights' : 'document-highlights';
    return bootPromise.then(function () {
      return readHighlightCollection(kind, {
        transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS
      });
    }).then(function (read) {
      var items = Array.isArray(read.payload) ? read.payload : [];
      var matched = [];
      for (var index = 0; index < items.length; index += 1) {
        var item = items[index];
        if (!item || typeof item !== 'object') continue;
        var page = Number(
          item.page != null ? item.page : item.section
        );
        if (!Number.isFinite(page)) page = null;
        if (wantPage !== null && page !== wantPage) continue;
        var text = String(item.text == null ? '' : item.text);
        if (needle && text.toLowerCase().indexOf(needle) < 0) continue;
        matched.push({
          id: String(item.id == null ? '' : item.id),
          page: page,
          color: String(item.color == null ? '' : item.color),
          text: text.length > 600 ? text.slice(0, 600) : text
        });
      }
      matched.sort(function (a, b) {
        return (a.page || 0) - (b.page || 0);
      });
      // 逐条累加真实序列化长度，装不下就停 —— 估一个"平均每条多少字节"
      // 在长引文面前必然失准，而失准的方向恰好是超限被整个丢掉。
      var budget = 32 * 1024;
      var used = 0;
      var kept = [];
      var truncated = false;
      for (var m = 0; m < matched.length; m += 1) {
        var size = JSON.stringify(matched[m]).length + 1;
        if (used + size > budget) { truncated = true; break; }
        used += size;
        kept.push(matched[m]);
      }
      return {
        ok: true,
        surface: surface,
        highlights: kept,
        matched: matched.length,
        returned: kept.length,
        truncated: truncated
      };
    });
  }

  // 助手读便签。跟高亮同一套取舍：只回内容与位置，几何和笔迹不回。
  function nativeReaderNotes(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    var surface = nativeInterfaceSurface;
    if (surface !== 'pdf' && surface !== 'epub') {
      return Promise.reject(new RuntimeError(
        '当前阅读界面没有本机便签', 'BW_READER_QUERY_SURFACE'
      ));
    }
    var wantPage = null;
    if (payload.page !== undefined && payload.page !== null) {
      wantPage = payload.page;
      if (typeof wantPage !== 'number' ||
          !Number.isInteger(wantPage) || wantPage < 1) {
        return Promise.reject(new RuntimeError(
          '页码无效', 'BW_READER_QUERY_PARAMS'
        ));
      }
    }
    var contains = payload.contains == null ? '' : String(payload.contains);
    if (contains.length > 256) {
      return Promise.reject(new RuntimeError(
        '过滤文字过长', 'BW_READER_QUERY_PARAMS'
      ));
    }
    var needle = contains.trim().toLowerCase();
    return bootPromise.then(function () {
      return readState('document-notes-legacy', []);
    }).then(function (stored) {
      var items = Array.isArray(stored) ? stored : [];
      var matched = [];
      for (var index = 0; index < items.length; index += 1) {
        var item = items[index];
        if (!item || typeof item !== 'object') continue;
        var anchor = item.anchor && typeof item.anchor === 'object'
          ? item.anchor : {};
        var page = Number(
          anchor.page != null ? anchor.page : anchor.section
        );
        if (!Number.isFinite(page)) page = null;
        if (wantPage !== null && page !== wantPage) continue;
        var text = String(item.text == null ? '' : item.text);
        if (needle && text.toLowerCase().indexOf(needle) < 0) continue;
        matched.push({
          id: String(item.id == null ? '' : item.id),
          page: page,
          text: text.length > 1000 ? text.slice(0, 1000) : text
        });
      }
      matched.sort(function (a, b) { return (a.page || 0) - (b.page || 0); });
      var budget = 32 * 1024;
      var used = 0;
      var kept = [];
      var truncated = false;
      for (var m = 0; m < matched.length; m += 1) {
        var size = JSON.stringify(matched[m]).length + 1;
        if (used + size > budget) { truncated = true; break; }
        used += size;
        kept.push(matched[m]);
      }
      return {
        ok: true, surface: surface, notes: kept,
        matched: matched.length, returned: kept.length, truncated: truncated
      };
    });
  }

  function localContextPlainText(value, maximum) {
    value = String(value == null ? '' : value)
      // Renderer controls are not card meaning.  Strip the whole control
      // element before removing ordinary markup so labels such as the image
      // card's “×” never leak into AI snapshots.
      .replace(/<(button|script|style|noscript|template)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/&nbsp;|&#160;/gi, ' ')
      .replace(/&amp;/gi, '&')
      .replace(/&lt;/gi, '<')
      .replace(/&gt;/gi, '>')
      .replace(/&quot;/gi, '"')
      .replace(/&#39;|&apos;/gi, "'")
      .replace(/\s+/g, ' ')
      .trim();
    return value.slice(0, maximum);
  }

  function localContextCardBody(note, kind, payload) {
    var contextText = localContextPlainText(
      payload.contextText || payload.context_text || '', LOCAL_PAGE_CARD_CONTEXT_LIMIT
    );
    if (contextText) return contextText;
    if (kind === 'anki') {
      var rows = [];
      var cards = Array.isArray(payload.cards) ? payload.cards : [];
      for (var index = 0; index < cards.length; index += 1) {
        var card = cards[index];
        if (!card || typeof card !== 'object' || Array.isArray(card)) continue;
        var parts = [];
        // Read the same durable learning-card aliases that the visible card
        // renderer accepts. Older notes commonly persisted q/a rather than
        // front/back; omitting them produced a formally valid but empty CARD.
        var frontField = card.front != null ? 'front'
          : (card.question != null ? 'question'
            : (card.q != null ? 'q' : (card.cloze != null ? 'cloze' : 'text')));
        var front = localContextPlainText(
          card[frontField], LOCAL_PAGE_CARD_CONTEXT_LIMIT
        );
        var back = localContextPlainText(
          card.back != null ? card.back
            : (card.answer != null ? card.answer : card.a),
          LOCAL_PAGE_CARD_CONTEXT_LIMIT
        );
        if (front) parts.push(front);
        if (back) parts.push(back);
        if (frontField !== 'cloze') {
          var cloze = localContextPlainText(
            card.cloze, LOCAL_PAGE_CARD_CONTEXT_LIMIT
          );
          if (cloze) parts.push(cloze);
        }
        if (parts.length) rows.push(parts.join(' / '));
        if (rows.join('\n').length >= LOCAL_PAGE_CARD_CONTEXT_LIMIT) break;
      }
      return localContextPlainText(
        rows.join('\n') || payload.text || note.text || '',
        LOCAL_PAGE_CARD_CONTEXT_LIMIT
      );
    }
    return localContextPlainText(
      payload.text || payload.content || note.text || '',
      LOCAL_PAGE_CARD_CONTEXT_LIMIT
    );
  }

  function localPageCardReplacementFace(card, fields) {
    card = card && typeof card === 'object' && !Array.isArray(card) ? card : {};
    for (var index = 0; index < fields.length; index += 1) {
      var field = fields[index];
      if (card[field] != null) return String(card[field]);
    }
    return '';
  }

  // This is deliberately the exact shape accepted by reader_page_card_edit,
  // not the richer persistence/source shape.  Learning-card metadata omitted
  // here (deck/tags/reason) is preserved by the authoritative edit transaction.
  function localPageCardReplacement(note, kind, payload) {
    if (kind === 'anki') {
      var cards = (Array.isArray(payload.cards) ? payload.cards : []).map(
        function (card) {
          card = card && typeof card === 'object' && !Array.isArray(card)
            ? card : {};
          var hasBasicFace = card.front != null || card.question != null ||
            card.q != null || card.text != null || card.back != null ||
            card.answer != null || card.a != null;
          if (card.type === 'cloze' || (card.type == null && card.cloze != null &&
              !hasBasicFace)) {
            return { type: 'cloze', cloze: String(card.cloze || '') };
          }
          return {
            type: 'basic',
            front: localPageCardReplacementFace(
              card, ['front', 'question', 'q', 'text']
            ),
            back: localPageCardReplacementFace(card, ['back', 'answer', 'a'])
          };
        }
      );
      return { replacement: 'cards', value: { cards: cards } };
    }
    return {
      replacement: 'content',
      value: {
        content: String(payload.content == null
          ? (payload.text == null ? note.text || '' : payload.text)
          : payload.content)
      }
    };
  }

  function localPageCardContextContent(note, kind, payload) {
    var replacement = localPageCardReplacement(note, kind, payload);
    var encoded = canonicalJSONString(replacement.value);
    // Account for the marker escape plus the surrounding JSON transport.  A
    // pathological backslash-heavy value can be far larger on the wire than
    // its UTF-16 length even before the 256 KiB direct-message hard limit.
    var markerEscaped = encoded
      .replace(/\\/g, '\\\\')
      .replace(/⟦/g, '\\⟦')
      .replace(/⟧/g, '\\⟧');
    var wireBytes = utf8(JSON.stringify(markerEscaped)).byteLength;
    var truncated = encoded.length > LOCAL_PAGE_CARD_CONTEXT_LIMIT ||
      wireBytes > LOCAL_PAGE_CARD_CONTEXT_MAX_WIRE_BYTES;
    var content = encoded;
    if (truncated) {
      var head = encoded.slice(0, LOCAL_PAGE_CARD_CONTEXT_FRAGMENT);
      var tail = encoded.slice(-LOCAL_PAGE_CARD_CONTEXT_FRAGMENT);
      content = '【卡片 replacement JSON 异常超大，已安全截断；原始长度=' +
        encoded.length + ' UTF-16 字符】\n【头部片段】' + head +
        '\n【尾部片段】' + tail;
    }
    return {
      content: content,
      contentLength: encoded.length,
      contentFormat: LOCAL_PAGE_CARD_REPLACEMENT_FORMAT,
      replacement: replacement.replacement,
      contentTruncated: truncated
    };
  }

  // page.context 只拿生成内联 CARD 所需的最小投影。来源始终是 App-owned
  // document-notes 权威记录，不从可见 DOM、pgbind-layer 或组件内存反推。
  function nativePageContextCards(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    if (Object.keys(payload).some(function (key) { return key !== 'page'; }) ||
        typeof payload.page !== 'number' || !Number.isInteger(payload.page) ||
        payload.page < 1) {
      return Promise.reject(new RuntimeError(
        '本地页面卡片页码无效', 'BW_LOCAL_PAGE_CARDS_PARAMS'
      ));
    }
    var page = payload.page;
    return bootPromise.then(function () {
      return storedStateRecord(
        stores.document, 'document-notes-legacy', 'documentId', bookId, []
      );
    }).then(function (record) {
      var notes = Array.isArray(record.payload) ? record.payload : [];
      var cards = [];
      for (var index = 0; index < notes.length; index += 1) {
        var note = notes[index];
        if (!note || typeof note !== 'object' || Array.isArray(note)) continue;
        var kind = note.card && typeof note.card === 'object' && !Array.isArray(note.card)
          ? 'anki'
          : (note.html && typeof note.html === 'object' && !Array.isArray(note.html)
            ? 'card' : '');
        if (!kind) continue;
        var data = kind === 'anki' ? note.card : note.html;
        var bind = data.bind;
        var hasPageBind = !!(bind && typeof bind === 'object' &&
          !Array.isArray(bind) && bind.kind === 'page-chars');
        if (hasPageBind && Number(bind.page) !== page) continue;
        var from = Number(bind && bind.from), to = Number(bind && bind.to);
        var bound = !!(hasPageBind && Number(bind.page) === page &&
          Number.isInteger(from) && Number.isInteger(to) && from >= 0 &&
          to >= from && to <= 1000000);
        var anchor = note.anchor;
        var unbound = !bound && !!(anchor && typeof anchor === 'object' &&
          !Array.isArray(anchor) && anchor.kind === 'pdf' &&
          Number(anchor.page) === page);
        if (!bound && !unbound) continue;
        var id = localRecordId(note.id || note.noteId, 'BW_LOCAL_PAGE_CARDS');
        var anchorLabel = bound ? localContextPlainText(bind.text || '', 120) : '';
        var cardLabel = localContextPlainText(
          data.label || data.title || data.gid || data.cid || id, 120
        );
        var contextContent = localPageCardContextContent(note, kind, data);
        var projected = {
          id: id,
          kind: kind,
          label: anchorLabel || cardLabel,
          text: localContextCardBody(note, kind, data),
          contextContent: contextContent.content,
          contentLength: contextContent.contentLength,
          contentFormat: contextContent.contentFormat,
          replacement: contextContent.replacement,
          contentTruncated: contextContent.contentTruncated
        };
        if (bound) {
          projected.bind = {
            kind: 'page-chars', page: page, from: from, to: to,
            text: localContextPlainText(bind.text || '', 200)
          };
        } else {
          // 历史手动 placement 没有字符 bind。它属于哪一页可以由权威
          // PDF anchor 确认，但不能据此猜正文词或伪造页内序号。
          projected.bind = null;
          projected.number = null;
          projected.unbound = true;
        }
        cards.push(projected);
        // 超过这个数量就不能保证有界读取，也不能截掉尾部后继续声称序号完整。
        if (cards.length > 2000) {
          throw new RuntimeError(
            '单页锚定卡片过多，无法安全编号', 'BW_LOCAL_PAGE_CARDS_LIMIT'
          );
        }
      }
      nativePDFRememberPageCardSnapshots(
        record.rev, notes, cards.map(function (card) { return card.id; })
      );
      return {
        contract: LOCAL_PAGE_CARDS_CONTRACT,
        page: page,
        revision: Number(record.rev) || 0,
        cards: cards
      };
    });
  }

  // Complete, stable source for one page placement.  This is intentionally a
  // separate capability from pageContextCards: the latter is a small list used
  // on every context refresh, while this source may be read in bounded chunks
  // by an explicit assistant tool.
  function nativePageCardSource(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    if (Object.keys(payload).sort().join(',') !== 'id,page' ||
        typeof payload.page !== 'number' || !Number.isInteger(payload.page) ||
        payload.page < 1) {
      return Promise.reject(new RuntimeError(
        '本地页面卡片详情参数无效', 'BW_LOCAL_PAGE_CARD_SOURCE_PARAMS'
      ));
    }
    var page = payload.page;
    var id;
    try { id = localRecordId(payload.id, 'BW_LOCAL_PAGE_CARD_SOURCE'); }
    catch (error) { return Promise.reject(error); }
    return bootPromise.then(function () {
      return storedStateRecord(
        stores.document, 'document-notes-legacy', 'documentId', bookId, []
      );
    }).then(function (record) {
      var notes = Array.isArray(record.payload) ? record.payload : [];
      var note = null;
      for (var index = 0; index < notes.length; index += 1) {
        var candidate = notes[index];
        if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) continue;
        if (String(candidate.id || candidate.noteId || '') === id) {
          note = candidate;
          break;
        }
      }
      if (!note) {
        throw new RuntimeError(
          '页面卡片不存在', 'BW_LOCAL_PAGE_CARD_SOURCE_NOT_FOUND'
        );
      }
      var kind = note.card && typeof note.card === 'object' && !Array.isArray(note.card)
        ? 'anki'
        : (note.html && typeof note.html === 'object' && !Array.isArray(note.html)
          ? 'card' : '');
      if (!kind) {
        throw new RuntimeError(
          '目标不是页面卡片', 'BW_LOCAL_PAGE_CARD_SOURCE_NOT_CARD'
        );
      }
      var data = kind === 'anki' ? note.card : note.html;
      var bind = data.bind;
      var boundHere = !!(bind && typeof bind === 'object' && !Array.isArray(bind) &&
        bind.kind === 'page-chars' && Number(bind.page) === page);
      var anchor = note.anchor;
      var freeHere = !boundHere && !!(anchor && typeof anchor === 'object' &&
        !Array.isArray(anchor) && anchor.kind === 'pdf' &&
        Number(anchor.page) === page);
      if (!boundHere && !freeHere) {
        throw new RuntimeError(
          '页面卡片不属于当前页', 'BW_LOCAL_PAGE_CARD_SOURCE_PAGE'
        );
      }
      var source;
      if (kind === 'anki') {
        source = {
          kind: 'anki',
          cards: clone(Array.isArray(data.cards) ? data.cards : [])
        };
      } else {
        var rawContent = String(data.content == null
          ? (data.text == null ? note.text || '' : data.text)
          : data.content);
        var contextText = String(
          data.contextText == null ? (data.context_text || '') : data.contextText
        );
        if (!contextText.trim()) {
          contextText = localContextPlainText(
            rawContent, LOCAL_PAGE_CARD_CONTEXT_LIMIT
          );
        }
        source = {
          kind: 'card',
          isHtml: data.isHtml === true,
          type: String(data.type || '').slice(0, 256),
          category: String(data.category || '').slice(0, 128),
          contextText: contextText.slice(0, LOCAL_PAGE_CARD_CONTEXT_LIMIT),
          content: rawContent
        };
      }
      var encoded = canonicalJSONString(source);
      if (utf8(encoded).byteLength > 3 * 1024 * 1024) {
        throw new RuntimeError(
          '页面卡片详情过大', 'BW_LOCAL_PAGE_CARD_SOURCE_LIMIT'
        );
      }
      return {
        contract: LOCAL_PAGE_CARD_SOURCE_CONTRACT,
        page: page,
        revision: Number(record.rev) || 0,
        id: id,
        kind: kind,
        content: encoded
      };
    });
  }

  // 助手在这本书里搜。复用本机既有的全文检索，不另起一套。
  //
  // incomplete 与 truncated 是两件事，都要报：前者是"有些页没能搜到"（文本层
  // 还没就绪、原生检索没答上），后者是"搜到的太多装不下"。合并成一个标志，
  // 助手就会把"半本书里没有"说成"这本书里没有"。
  function nativeReaderSearch(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    var surface = nativeInterfaceSurface;
    if (surface !== 'pdf' && surface !== 'epub') {
      return Promise.reject(new RuntimeError(
        '当前阅读界面不支持全文搜索', 'BW_READER_QUERY_SURFACE'
      ));
    }
    var query = String(payload.query == null ? '' : payload.query).trim();
    if (!query || query.length > 256) {
      return Promise.reject(new RuntimeError(
        '搜索文字无效', 'BW_READER_QUERY_PARAMS'
      ));
    }
    var limit = payload.limit == null ? 50 : payload.limit;
    if (typeof limit !== 'number' || !Number.isInteger(limit) ||
        limit < 1 || limit > 200) {
      return Promise.reject(new RuntimeError(
        '搜索条数无效', 'BW_READER_QUERY_PARAMS'
      ));
    }
    return bootPromise.then(function () {
      return searchPageText(query, limit);
    }).then(function (found) {
      var matches = found && Array.isArray(found.matches) ? found.matches : [];
      var budget = 32 * 1024;
      var used = 0;
      var kept = [];
      var truncated = false;
      for (var m = 0; m < matches.length; m += 1) {
        var item = matches[m] && typeof matches[m] === 'object'
          ? matches[m] : {};
        var snippet = String(item.text == null ? '' : item.text);
        var row = {
          page: Number.isFinite(Number(item.page)) ? Number(item.page) : null,
          text: snippet.length > 300 ? snippet.slice(0, 300) : snippet
        };
        var size = JSON.stringify(row).length + 1;
        if (used + size > budget) { truncated = true; break; }
        used += size;
        kept.push(row);
      }
      return {
        ok: true, surface: surface, query: query, matches: kept,
        matched: matches.length, returned: kept.length,
        truncated: truncated,
        incomplete: found && found.incomplete === true
      };
    });
  }

  // 助手读目录。/pdf/api/toc 由 App 原生提供，离线一样在 —— 走 originalFetch
  // 那条既有路径，不在这里另抄一份解析。
  //
  // 只对 PDF：EPUB 的章节结构走它自己的 manifest，这里不去猜一个等价物。
  function nativeReaderTableOfContents() {
    if (nativeInterfaceSurface !== 'pdf') {
      return Promise.reject(new RuntimeError(
        '当前阅读界面没有本机目录', 'BW_READER_QUERY_SURFACE'
      ));
    }
    return bootPromise.then(function () {
      // @interaction document.toc.read
      return root.fetch(
        localBasePath() + '/pdf/api/toc?file='
          + encodeURIComponent(localFileRef()) + '&entries=1'
      );
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok || !data) {
          throw new RuntimeError('目录读取失败', 'BW_READER_QUERY_TOC');
        }
        var entries = Array.isArray(data.entries) ? data.entries : [];
        var budget = 32 * 1024;
        var used = 0;
        var kept = [];
        var truncated = false;
        for (var index = 0; index < entries.length; index += 1) {
          var entry = entries[index] && typeof entries[index] === 'object'
            ? entries[index] : {};
          var title = String(entry.title == null ? '' : entry.title);
          var row = {
            title: title.length > 200 ? title.slice(0, 200) : title,
            page: Number.isFinite(Number(entry.page))
              ? Number(entry.page) : null,
            level: Number.isFinite(Number(entry.level))
              ? Number(entry.level) : 1
          };
          var size = JSON.stringify(row).length + 1;
          if (used + size > budget) { truncated = true; break; }
          used += size;
          kept.push(row);
        }
        // 空目录是真事实，不是失败：这本书可能就没建过目录。助手该照实说，
        // 而不是当成读不到。
        return {
          ok: true, entries: kept, matched: entries.length,
          returned: kept.length, truncated: truncated
        };
      });
    });
  }

  // 助手读某一页的正文。复用给语音助手用的那条本机接口，不重复实现取文逻辑
  // —— 两份取文迟早会在振假名、栏序或 OCR 回退上各走各的。
  function nativeReaderPageText(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    var surface = nativeInterfaceSurface;
    if (surface !== 'pdf' && surface !== 'epub') {
      return Promise.reject(new RuntimeError(
        '当前阅读界面没有本机正文', 'BW_READER_QUERY_SURFACE'
      ));
    }
    // 只认真正的数字。放行 '3' 这样的字符串等于替上游把类型错误吞掉 ——
    // 受信入口的价值正在于它不依赖上游校验过。
    var page = payload.page;
    if (typeof page !== 'number' || !Number.isInteger(page) || page < 1) {
      return Promise.reject(new RuntimeError(
        '页码无效', 'BW_READER_QUERY_PARAMS'
      ));
    }
    return bootPromise.then(function () {
      // @interaction document.page-text.read
      return root.fetch(
        // ⚠ 别在这里加查询参数：本地那条（nativeVoicePageText）用的是**精确参数
        //   白名单** localFileQuery(url, ['file','page'], …)，多一个参数就整条拒 ——
        //   那会把原本能用的页文本读取一起弄坏。segments 由本地实现无条件给出。
        localBasePath() + '/api/assistant/voice-page-text?file='
          + encodeURIComponent(localFileRef()) + '&page=' + page
      );
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok || !data || data.ok !== true) {
          throw new RuntimeError(
            String((data && data.error) || '正文读取失败'),
            String((data && data.code) || 'BW_READER_QUERY_PAGE_TEXT')
          );
        }
        var text = String(data.text == null ? '' : data.text);
        var segments = [];
        if (Array.isArray(data.segments)) {
          for (var i = 0; i < data.segments.length && i < 400; i += 1) {
            var seg = data.segments[i];
            if (!seg || typeof seg !== 'object') continue;
            var from = Number(seg.from), to = Number(seg.to);
            if (!Number.isInteger(from) || !Number.isInteger(to) ||
                from < 0 || to < from) continue;
            segments.push({
              from: from, to: to,
              text: String(seg.text == null ? '' : seg.text).slice(0, 120)
            });
          }
        }
        return {
          ok: true, surface: surface, page: page, text: text,
          // 这条接口本身就截到 1500 字符。不说出来，助手会把半页当整页读。
          truncated: text.length >= 1500,
          // 每段的 from/to 是**字符层里的真实下标**，可以直接填进卡片的 bind。
          segments: segments
        };
      });
    });
  }

  // 助手把一段内容存成一篇本机笔记（区别于贴在页面上的便签）。
  //
  // 书和页仍由 App 自己填 —— 桥接不知道你在读什么。标题给了就用，没给就由
  // App 按书名和时间生成：让助手编一个标题，出来的会是它以为你在读的那本书。
  function nativeReaderMakeNote(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    var text = String(payload.text == null ? '' : payload.text);
    if (!text.trim()) {
      return Promise.reject(new RuntimeError(
        '笔记内容为空', 'BW_READER_NOTE_TEXT'
      ));
    }
    if (text.length > 240000) {
      return Promise.reject(new RuntimeError(
        '笔记内容过长', 'BW_READER_NOTE_TEXT'
      ));
    }
    var title = String(payload.title == null ? '' : payload.title).trim();
    if (title.length > 240) {
      return Promise.reject(new RuntimeError(
        '笔记标题过长', 'BW_READER_NOTE_TITLE'
      ));
    }
    var surface = nativeInterfaceSurface;
    if (surface !== 'pdf' && surface !== 'epub') {
      return Promise.reject(new RuntimeError(
        '当前阅读界面不支持存笔记', 'BW_READER_NOTE_SURFACE'
      ));
    }
    var file = localFileRef();
    return bootPromise.then(function () {
      return readState('reading-position', null);
    }).then(function (position) {
      var page = Number(position && position.page);
      if (!Number.isFinite(page) || page < 0) page = 0;
      if (!title) {
        var book = String(file).split(/[\\/]/).pop()
          .replace(/\.[^.]+$/, '').trim();
        title = ('阅读笔记 · ' + (book || '当前书') + ' · 第 '
          + Math.trunc(page) + ' 页');
      }
      // @interaction knowledge.note.create
      return root.fetch(localBasePath() + '/pdf/api/to-note', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: title.slice(0, 240),
          text: text,
          file: String(file).slice(0, 8000),
          page: Math.trunc(page)
        })
      });
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok || !data || data.ok !== true) {
          throw new RuntimeError(
            String((data && data.error) || '笔记未能保存'),
            String((data && data.code) || 'BW_READER_NOTE_FAILED')
          );
        }
        return {
          ok: true, surface: surface, title: title,
          note_path: String(data.note_path || '')
        };
      });
    });
  }

  // 助手查词。词典在 Pi 上，本机没有 —— 离线时这里会明确失败，不会假装查过。
  //
  // 走 prewarm=1：助手代查不等于用户遇到了这个生词。不加这个参数，AI 每查一次
  // 就会给这个词记一次曝光、建一篇生词笔记，用户的生词统计会被助手的动作污染。
  function nativeReaderLookupWord(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    var surface = nativeInterfaceSurface;
    if (surface !== 'pdf' && surface !== 'epub') {
      return Promise.reject(new RuntimeError(
        '当前阅读界面不支持查词', 'BW_READER_QUERY_SURFACE'
      ));
    }
    var word = String(payload.word == null ? '' : payload.word).trim();
    if (!word || word.length > 128) {
      return Promise.reject(new RuntimeError(
        '查询词无效', 'BW_READER_QUERY_PARAMS'
      ));
    }
    return bootPromise.then(function () {
      // @interaction dictionary.quick.read
      return root.fetch(
        localBasePath() + '/pdf/api/dict-quick?word='
          + encodeURIComponent(word)
          + '&file=' + encodeURIComponent(localFileRef())
          + '&prewarm=1'
      );
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok || !data || data.ok !== true) {
          throw new RuntimeError(
            String((data && data.error) || '查词失败'),
            String((data && data.code) || 'BW_READER_QUERY_LOOKUP')
          );
        }
        var senses = Array.isArray(data.senses) ? data.senses : [];
        return {
          ok: true,
          word: word,
          lemma: String(data.lemma == null ? '' : data.lemma),
          translation: String(data.translation == null ? '' : data.translation)
            .slice(0, 2000),
          phonetic: String(
            data.phonetic_us == null ? (data.phonetic || '') : data.phonetic_us
          ).slice(0, 120),
          senses: senses.slice(0, 12).map(function (sense) {
            return String(sense == null ? '' : sense).slice(0, 300);
          }),
          truncated: senses.length > 12
        };
      });
    });
  }

  function projectNativeReaderVocabulary(word, mark, japanese) {
    var mastered = mark === 'known';
    var state = root.BWReaderRuntime && root.BWReaderRuntime.vocabularyState;
    if (state && state.CONTRACT === 'vocabulary-state/1' &&
        typeof state.setMastered === 'function') {
      try {
        state.setMastered({
          kind: 'word', language: japanese ? 'ja' : 'en',
          lemma: word, word: word, surface: word, forms: []
        }, mastered, { source: 'reader-query' });
      } catch (_) {}
    }
    // Keep the legacy PDF projection in step during the migration. This call is
    // synchronous and repaints only loaded pages containing the word.
    try {
      if (typeof root.applyVocabLocalOverride === 'function') {
        root.applyVocabLocalOverride(word, mastered, {
          word: word, surface: word, forms: [], jp: japanese
        });
      }
    } catch (_) {}
  }

  // 助手把一个词标成已掌握/生词。Pi 仍是兼容词库的确认端；确认成功后同一调用
  // 立即写 App 本地 vocabulary-state，让当前页在工具返回时就完成热更新。
  function nativeReaderMarkVocabulary(input) {
    var payload = input && typeof input === 'object' && !Array.isArray(input)
      ? input : {};
    var surface = nativeInterfaceSurface;
    if (surface !== 'pdf' && surface !== 'epub') {
      return Promise.reject(new RuntimeError(
        '当前阅读界面不支持标记生词', 'BW_READER_VOCAB_SURFACE'
      ));
    }
    var word = String(payload.word == null ? '' : payload.word).trim();
    if (!word || word.length > 128) {
      return Promise.reject(new RuntimeError(
        '生词无效', 'BW_READER_VOCAB_WORD'
      ));
    }
    var mark = String(payload.mark == null ? '' : payload.mark);
    if (mark !== 'known' && mark !== 'unknown') {
      return Promise.reject(new RuntimeError(
        '标记取值无效', 'BW_READER_VOCAB_MARK'
      ));
    }
    var japanese = /[\u3040-\u30ff\u3400-\u9fff]/.test(word);
    var endpoint = japanese ? '/pdf/api/jp-vocab-mark' : '/pdf/api/vocab-mark';
    return bootPromise.then(function () {
      // @interaction vocabulary.mastery.set
      return root.fetch(localBasePath() + endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ word: word, mark: mark })
      });
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok || !data || data.ok !== true) {
          throw new RuntimeError(
            String((data && data.error) || '生词标记未保存'),
            String((data && data.code) || 'BW_READER_VOCAB_FAILED')
          );
        }
        projectNativeReaderVocabulary(word, mark, japanese);
        return {
          ok: true, word: word, mark: mark,
          language: japanese ? 'ja' : 'en', localProjected: true
        };
      });
    });
  }

  function nativeReaderUndoLast(operationID) {
    operationID = String(operationID || '');
    if (!/^rundo_[0-9a-f]{24}$/.test(operationID)) {
      return Promise.reject(new RuntimeError(
        'Reader 撤销操作编号无效', 'BW_NATIVE_READER_UNDO_OPERATION'
      ));
    }
    var suffix = operationID.slice(6);
    return bootPromise.then(function () {
      var task;
      if (nativeInterfaceSurface === 'pdf') {
        task = nativePDFUndoLast('npdf_' + suffix);
      } else if (nativeInterfaceSurface === 'epub') {
        task = nativeEPUBUndoLast('epub_' + suffix);
      } else {
        throw new RuntimeError(
          '当前宿主不支持本机书籍撤销',
          'BW_NATIVE_READER_UNDO_SURFACE'
        );
      }
      return Promise.resolve(task).then(function (result) {
        result = clone(result);
        result.operationId = operationID;
        return nativeReaderRefreshAfterUndo(result);
      });
    });
  }

  var api = {
    contract: CONTRACT,
    owner: 'native-app',
    deviceId: deviceId,
    deviceFamilyId: deviceId,
    localBookId: bookId,
    ready: function () { return bootPromise; },
    undoLast: nativeReaderUndoLast,
    pageCardMutate: nativeReaderPageCardMutate,
    pageCardAction: nativeReaderPageCardAction,
    createNote: nativeReaderCreateNote,
    editNote: nativeReaderEditNote,
    highlights: nativeReaderHighlights,
    notes: nativeReaderNotes,
    search: nativeReaderSearch,
    toc: nativeReaderTableOfContents,
    pageText: nativeReaderPageText,
    makeNote: nativeReaderMakeNote,
    lookupWord: nativeReaderLookupWord,
    markVocabulary: nativeReaderMarkVocabulary,
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
          return persistAssistantPDFHighlight(
            body, 'BW_LOCAL_HIGHLIGHT_DIRECT'
          ).then(function (saved) {
            // 失败与未知走不到这里：它只挂在成功分支上。
            announceAssistantHighlight(body, saved);
            return saved;
          });
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
    pageContextCards: nativePageContextCards,
    pageCardSource: nativePageCardSource,
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
  root._nativeReaderUndoLast = function (operationID) {
    return api.undoLast(operationID);
  };
  root._nativeReaderPageCardMutate = function (input) {
    return api.pageCardMutate(input);
  };
  root._nativeReaderPageCardAction = function (input) {
    return api.pageCardAction(input);
  };
  // 与撤销同一形态的受信入口：经 api 转一道，调用方拿不到内部实现，
  // 也无法绕过其中的界面与内容校验。
  root._nativeReaderCreateNote = function (input) {
    return api.createNote(input);
  };
  root._nativeReaderEditNote = function (input) {
    return api.editNote(input);
  };
  root._nativeReaderHighlights = function (input) {
    return api.highlights(input);
  };
  root._nativeReaderNotes = function (input) {
    return api.notes(input);
  };
  root._nativeReaderSearch = function (input) {
    return api.search(input);
  };
  root._nativeReaderToc = function () {
    return api.toc();
  };
  root._nativeReaderPageText = function (input) {
    return api.pageText(input);
  };
  root._nativeReaderMakeNote = function (input) {
    return api.makeNote(input);
  };
  root._nativeReaderLookupWord = function (input) {
    return api.lookupWord(input);
  };
  root._nativeReaderMarkVocabulary = function (input) {
    return api.markVocabulary(input);
  };

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
        // 高亮拆分迁移必须先于 PDF 改页恢复：恢复用门面读当前状态做比对，
        // 旧 journal 里的整册数组要能与迁移后的物化结果逐位相等。
        return migrateHighlightSplitOnBoot();
      }).then(function () {
        return recoverNativePDFMutationOnBoot();
      }).then(function () {
        // 词锚错位修复要在 PDF 恢复之后:恢复可能回滚 journal,把 notes
        // 换回另一份;对着恢复前的数据修等于白修。
        return repairNoteBindPageDrift();
      }).then(function () {
        if (typeof root.dlog === 'function') root.dlog('本机启动:PDF 恢复检查已完成');
        return attachPreferenceStore();
      }).then(function () {
        bootState = 'ready';
        if (typeof root.dlog === 'function') root.dlog('本机启动:运行时已就绪');
        // 复制出箱：开书先冲一次积压。之后的节奏是"入队即触发 +
        // 有积压才按 30s 重排"，队列空时没有阻塞性的常驻定时器。
        // 另挂一条**周期对账**链（unref，不阻进程退出）：此前对账只在
        // 队列排空后触发 —— 没有命令活动时永不对账，走 PDF mutation
        // 等不入队路径的改动（真实插入页）只能靠它兜底收敛。
        if (replicationEligible()) {
          scheduleReplicationDrain(2000);
          scheduleReplicationIdleReconcile();
        }
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
