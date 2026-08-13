/* interaction-policy.js — reader 用户交互到网络端点的唯一策略清单。
 *
 * 这份文件只描述“用户何时应看见本地结果、网络回执如何处理”，不发请求，也不
 * 改写业务状态。PWA、扩展、静态审计和合约测试必须引用同一份清单，避免把普通
 * 状态切换重新实现成“等待服务器确认后才更新画面”。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.interactionPolicy = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'interaction-policy/1';
  var VALID_UI = {
    'local-immediate': true,
    'background': true,
    'cache-first': true,
    'network-first': true,
    'remote-required': true
  };
  var VALID_ACK = { 'reconcile': true, 'accept-result': true, 'none': true };
  var VALID_OFFLINE = {
    'queue': true,
    'retain-local': true,
    'cache-only': true,
    'fallback': true,
    'unavailable': true,
    'drop': true
  };
  var VALID_SYNC = { 'outbox': true, 'direct': true, 'none': true };
  var VALID_SERVICE_WORKER = {
    'none': true,
    'private-cache-first': true,
    'private-swr': true,
    'private-network-fallback': true,
    'public-cache-first': true
  };

  function transport(value) {
    value = value || {};
    return {
      outbox: value.outbox === true,
      extensionBridge: value.extensionBridge === true,
      serviceWorker: typeof value.serviceWorker === 'string'
        ? value.serviceWorker
        : 'none'
    };
  }

  function match(path, methods, params) {
    var result = { path: path, methods: methods };
    if (params) result.params = params;
    return result;
  }

  function localMutation(id, path, methods, local, options) {
    options = options || {};
    return {
      id: id,
      matches: [match(path, methods, options.params)],
      surfaces: options.surfaces || ['pwa', 'extension'],
      kind: options.kind || 'mutation',
      ui: 'local-immediate',
      localEffectMs: 50,
      local: local,
      ack: 'reconcile',
      offline: 'queue',
      sync: 'outbox',
      transport: transport(options.transport),
      reason: options.reason || ''
    };
  }

  function backgroundMutation(id, path, methods, options) {
    options = options || {};
    return {
      id: id,
      matches: [match(path, methods)],
      surfaces: options.surfaces || ['pwa', 'extension'],
      kind: options.kind || 'mutation',
      ui: 'background',
      local: options.local || null,
      ack: 'none',
      offline: options.offline || 'queue',
      sync: options.sync || 'outbox',
      transport: transport(options.transport),
      reason: options.reason || ''
    };
  }

  function networkMutation(id, path, methods, options) {
    options = options || {};
    return {
      id: id,
      matches: [match(path, methods, options.params)],
      surfaces: options.surfaces || ['pwa', 'extension'],
      kind: options.kind || 'mutation',
      ui: 'network-first',
      local: options.local || null,
      ack: 'accept-result',
      offline: options.offline || 'fallback',
      sync: options.sync || 'direct',
      transport: transport(options.transport),
      reason: options.reason || ''
    };
  }

  function remoteRequired(id, paths, methods, reason, options) {
    options = options || {};
    return {
      id: id,
      matches: paths.map(function (path) { return match(path, methods); }),
      surfaces: options.surfaces || ['pwa', 'extension'],
      kind: options.kind || 'command',
      ui: 'remote-required',
      local: options.local || null,
      ack: 'accept-result',
      offline: 'unavailable',
      sync: 'direct',
      transport: transport(options.transport),
      reason: reason
    };
  }

  function cachedRead(id, paths, strategy, options) {
    options = options || {};
    return {
      id: id,
      matches: paths.map(function (path) { return match(path, ['GET']); }),
      surfaces: options.surfaces || ['pwa', 'extension'],
      kind: 'query',
      ui: 'cache-first',
      local: { cache: strategy, privacy: options.privacy || 'account-private' },
      ack: 'reconcile',
      offline: 'cache-only',
      sync: 'direct',
      transport: transport(options.transport),
      reason: options.reason || ''
    };
  }

  function networkRead(id, paths, options) {
    options = options || {};
    var methods = options.methods || ['GET'];
    return {
      id: id,
      matches: paths.map(function (path) { return match(path, methods); }),
      surfaces: options.surfaces || ['pwa', 'extension'],
      kind: 'query',
      ui: 'network-first',
      local: options.local || null,
      ack: 'accept-result',
      offline: 'fallback',
      sync: 'direct',
      transport: transport(options.transport),
      reason: options.reason || ''
    };
  }

  var POLICIES = [
    /* 首批 local-first 门禁：掌握、收藏、标注、便签与复习答题。 */
    localMutation(
      'vocabulary.mastery.set',
      '/pdf/api/vocab-mark',
      ['POST'],
      { collection: 'vocabulary-state', projection: 'word.mastered' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'vocabulary.jp-mastery.set',
      '/pdf/api/jp-vocab-mark',
      ['POST'],
      { collection: 'vocabulary-state', projection: 'word.mastered' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'phrase.mastery.set',
      '/pdf/api/phrase-mark',
      ['POST'],
      { collection: 'vocabulary-state', projection: 'phrase.mastered' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'phrase.favorite.add',
      '/pdf/api/phrases',
      ['POST'],
      { collection: 'vocabulary-state', projection: 'phrase.favorite' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'phrase.favorite.remove',
      '/pdf/api/phrases',
      ['DELETE'],
      { collection: 'vocabulary-state', projection: 'phrase.favorite' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'document.highlight.create',
      '/pdf/api/highlights',
      ['POST'],
      { collection: 'document-highlights', projection: 'active-document' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'document.highlight.update',
      '/pdf/api/highlights',
      ['PATCH'],
      { collection: 'document-highlights', projection: 'active-document' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'document.highlight.remove',
      '/pdf/api/highlights',
      ['DELETE'],
      { collection: 'document-highlights', projection: 'active-document' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'document.note.create',
      '/pdf/api/notes',
      ['POST'],
      { collection: 'document-notes', projection: 'active-document' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'document.note.update',
      '/pdf/api/notes',
      ['PATCH'],
      { collection: 'document-notes', projection: 'active-document' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'document.note.remove',
      '/pdf/api/notes',
      ['DELETE'],
      { collection: 'document-notes', projection: 'active-document' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    localMutation(
      'review.answer.submit',
      '/pdf/api/review-answer',
      ['POST'],
      { collection: 'card-states', projection: 'review-queue-local-state' },
      { transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' } }
    ),
    remoteRequired(
      'assistant.model-preference.set',
      ['/api/assistant/action-pref'],
      ['POST'],
      '模型与 Fast 预设是账户级服务端配置；只有服务端持久化成功后才显示保存完成。'
    ),

    /* 同属 command outbox，但不应阻塞当前交互。 */
    backgroundMutation(
      'reading.position.save',
      '/pdf/api/reading-pos',
      ['POST'],
      {
        local: { collection: 'reading-position', projection: 'active-document' },
        transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' }
      }
    ),
    backgroundMutation(
      'learning.read-dwell.report',
      '/pdf/api/read-dwell',
      ['POST'],
      {
        kind: 'telemetry',
        offline: 'drop',
        sync: 'none',
        transport: { outbox: false, extensionBridge: true, serviceWorker: 'none' },
        reason: '阅读停留时间是易失增量；发送失败时丢弃，不能在恢复网络后冒充当前活动。'
      }
    ),
    /* 双向上下文同步(2026-07-26):上报「此刻活动的文档」。
       ⚠ 故意**不进 outbox 队列**(offline: 'retain-local' + sync: 'none'):这条数据的全部价值
       在于「此刻」,离线攒着、恢复网络后补发一条 20 分钟前的位置,会带着新鲜的服务端 ts 落库,
       正好重新制造它要修的那个 bug(拿旧状态冒充当前)。丢掉比补发更正确——下一次翻页或
       60s 心跳自然会带上真实的当前状态。 */
    /* 前端渲染完某一轮后的回执。离线不补发:回执迟到毫无意义(桥接那一轮早已结束),
       补发只会把"当时没渲染出来"污染成"渲染成功了"。 */
    /* 出向上下文:焦点上报。离线不补发 —— 迟到的焦点会把"早已取消的对象"复活成当前。 */
    backgroundMutation(
      'context.focus.report',
      '/pdf/api/outgoing/focus',
      ['POST'],
      {
        offline: 'retain-local',
        sync: 'none',
        transport: { outbox: false, extensionBridge: true, serviceWorker: 'none' },
        reason: '当前焦点对象的建立/替换/取消;仅在总开关开启时发生'
      }
    ),
    /* 绘图版本查询:只读,内容摘要 + 静默 1s 才给稳定版本。 */
    networkRead(
      'context.drawing.revision',
      ['/pdf/api/outgoing/drawing', '/pdf/api/outgoing/state'],
      { reason: '当前页绘图是否已稳定及其版本引用;未稳定不返回引用' }
    ),
    /* Windows 消费的不可变事件日志:游标轮询,只读。 */
    networkRead(
      'context.outgoing.journal',
      ['/pdf/api/outgoing/journal'],
      { reason: '跨机消费的出向事件日志;游标增量、损坏 fail-closed' }
    ),
    backgroundMutation(
      'assistant.turn.ack',
      '/pdf/api/turn-ack',
      ['POST'],
      {
        offline: 'retain-local',
        sync: 'none',
        transport: { outbox: false, extensionBridge: true, serviceWorker: 'none' },
        reason: '侧栏渲染回执:让桥接把「已写库」与「前端已画出」分开报'
      }
    ),
    backgroundMutation(
      'context.active.report',
      '/pdf/api/active-reading',
      ['POST'],
      {
        local: { collection: 'reader-active', projection: 'active-document' },
        offline: 'retain-local',
        sync: 'none',
        transport: { outbox: false, extensionBridge: true, serviceWorker: 'none' },
        reason: '当前活动文档上报;仅在「双向上下文同步」开关开启时发生,过期数据不补发'
      }
    ),
    /* 同一把总开关同时管两个方向(前端上报 + Pi→Windows 快照推送),所以它必须真的到达服务端:
       写不成就得回滚 UI,不能让前端以为关了、后台还在推。故 remote-required + direct。 */
    remoteRequired(
      'context.sync.toggle',
      ['/pdf/api/context-sync'],
      ['POST'],
      '双向上下文同步总开关:跨端唯一真值,必须服务端确认',
      { kind: 'command' }
    ),
    remoteRequired(
      'context.sync.read',
      ['/pdf/api/context-sync'],
      ['GET'],
      '读取双向上下文同步与交付模式的服务端真值',
      { kind: 'query' }
    ),
    networkRead(
      'card.repository.bootstrap',
      ['/pdf/api/card-repository/bootstrap'],
      {
        reason: '用户显式执行 Pi 同步时，一次性读取账户旧卡快照并原子导入 App 本地卡库'
      }
    ),
    /*
     * 电脑客户端的设备状态、一次性配对、启动命令和短期 WebRTC 信令均为
     * 易失远端状态：禁止离线排队或本地乐观成功，任一请求失败即明确不可用。
     */
    remoteRequired(
      'computer-voice.bridge.request',
      ['/api/reader/computer-voice/*'],
      ['GET', 'POST'],
      '电脑客户端桥接状态与一次性命令必须由当前认证会话实时确认',
      { kind: 'command' }
    ),
    backgroundMutation(
      'learning.lookup.report',
      '/pdf/api/lookup-event',
      ['POST'],
      {
        kind: 'telemetry',
        offline: 'queue',
        transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' }
      }
    ),
    localMutation(
      'anki.cards.enqueue',
      '/pdf/api/anki-add-cards',
      ['POST'],
      { collection: 'card-states', projection: 'pending-anki-commit' },
      {
        transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' },
        reason: '本地先显示 pending；真正写入 AnkiConnect 仍需服务器执行'
      }
    ),
    remoteRequired(
      'anki.draft.verify',
      ['/pdf/api/anki-draft'],
      ['POST'],
      '精确来源与当前书身份必须由服务端验证；成功只交付可编辑草稿，不写入 Anki',
      { kind: 'command' }
    ),
    localMutation(
      'entity.state.update',
      '/pdf/api/entity/{id}',
      ['PATCH'],
      { collection: 'card-states', projection: 'stable-card-identity' },
      {
        params: {
          id: { pattern: '[A-Za-z0-9_-]{1,160}' }
        },
        transport: { outbox: true, extensionBridge: true, serviceWorker: 'none' }
      }
    ),

    /* 计算/授权/结构变更没有等价的纯本地执行器，必须明确等待远端结果。 */
    remoteRequired(
      'ai.translate.compute',
      [
        '/pdf/api/translate',
        '/pdf/api/translate-sentence',
        '/pdf/api/epub-translate-section',
        '/pdf/api/web-translate'
      ],
      ['POST'],
      '译文生成需要服务器上的模型或翻译后端',
      {
        kind: 'inference',
        transport: { extensionBridge: true, serviceWorker: 'none' }
      }
    ),
    remoteRequired(
      'ai.explain.compute',
      ['/pdf/api/explain'],
      ['POST'],
      '解释生成需要服务器上的模型后端',
      {
        kind: 'inference',
        transport: { extensionBridge: true, serviceWorker: 'none' }
      }
    ),
    remoteRequired(
      'ai.grammar.compute',
      ['/pdf/api/grammar-analyze', '/pdf/api/grammar-stream'],
      ['POST'],
      '语法分析需要服务器解析器或模型后端',
      {
        kind: 'inference',
        transport: { extensionBridge: true, serviceWorker: 'none' }
      }
    ),
    remoteRequired(
      'ai.assistant.respond',
      ['/api/assistant/chat', '/api/assistant/voice-tool', '/pdf/api/epub-assistant'],
      ['POST'],
      'AI 回答需要服务器上的会话与模型执行器',
      {
        kind: 'inference',
        transport: { extensionBridge: true, serviceWorker: 'none' }
      }
    ),
    remoteRequired(
      'learning.snippets.enqueue',
      ['/pdf/api/snippets-to-async'],
      ['POST'],
      '笔记与 Anki 后台任务必须由服务器接收并返回任务标识；失败时调用方恢复本地草稿。',
      {
        kind: 'command',
        transport: { extensionBridge: true, serviceWorker: 'none' }
      }
    ),
    networkMutation(
      'document.epub-action.commit',
      '/pdf/api/epub-action',
      ['POST'],
      {
        local: { collection: 'epub-actions', projection: 'active-document' },
        transport: { extensionBridge: true, serviceWorker: 'none' },
        reason: '由当前运行时选择 App 本地原子存储或 PWA 服务端，并在该后端确认后更新动作卡。'
      }
    ),
    remoteRequired(
      'document.pdf-structure.mutate',
      ['/pdf/api/pdf-insert-page'],
      ['POST', 'PATCH', 'DELETE'],
      '修改 PDF 文件结构需要 PWA 服务器持有的文档执行器',
      { transport: { serviceWorker: 'none' } }
    ),
    remoteRequired(
      'document.ocr.rebuild',
      ['/pdf/api/reocr-page', '/pdf/api/reocr-page/clear'],
      ['POST'],
      'OCR 重建需要服务器上的文件与 OCR 运行时',
      { transport: { serviceWorker: 'none' } }
    ),
    remoteRequired(
      'document.toc.build',
      ['/pdf/api/build-toc'],
      ['POST'],
      '目录生成需要服务器读取完整书籍并运行分析',
      { transport: { serviceWorker: 'none' } }
    ),
    remoteRequired(
      'provider.authorize',
      ['/api/reader/provider-authorize', '/api/reader/token-owner'],
      ['POST'],
      '凭据所有权与 provider 授权只能由服务器确认',
      {
        kind: 'authorization',
        transport: { serviceWorker: 'none' }
      }
    ),

    /* 已有私有缓存语义的读操作：先用缓存，再后台更新。 */
    cachedRead(
      'document.page-image.read',
      ['/pdf/api/page-image'],
      'private-cache-first',
      { transport: { serviceWorker: 'private-cache-first' } }
    ),
    cachedRead(
      'document.page-chars.read',
      ['/pdf/api/page-chars'],
      'private-cache-first',
      { transport: { serviceWorker: 'private-cache-first' } }
    ),
    cachedRead(
      'dictionary.quick.read',
      ['/pdf/api/dict-quick'],
      'private-stale-while-revalidate',
      {
        transport: {
          extensionBridge: true,
          serviceWorker: 'private-swr'
        }
      }
    ),
    networkRead(
      'dictionary.jp.read',
      ['/pdf/api/dict-jp'],
      {
        transport: { extensionBridge: true, serviceWorker: 'none' },
        reason: '读取日语词典与永久缓存；不可用时由调用方显示失败或退回通用词典。'
      }
    ),
    cachedRead(
      'document.page-figures.read',
      ['/pdf/api/page-figures'],
      'private-stale-while-revalidate',
      { transport: { serviceWorker: 'private-swr' } }
    ),
    cachedRead(
      'document.book-meta.read',
      ['/pdf/api/book-meta'],
      'private-stale-while-revalidate',
      { transport: { serviceWorker: 'private-swr' } }
    ),
    cachedRead(
      'document.epub-manifest.read',
      ['/pdf/api/epub-manifest'],
      'private-stale-while-revalidate',
      { transport: { serviceWorker: 'private-swr' } }
    ),
    cachedRead(
      'document.epub-section.read',
      ['/pdf/api/epub-section'],
      'private-stale-while-revalidate',
      { transport: { serviceWorker: 'private-swr' } }
    ),
    cachedRead(
      'reader.shell.read',
      ['/static/*'],
      'public-shell-cache-first',
      {
        privacy: 'public-shell',
        surfaces: ['pwa'],
        transport: { serviceWorker: 'public-cache-first' }
      }
    ),

    /*
     * 用户拥有的阅读状态必须先读本地快照，再让远端快照与 dirty/outbox overlay
     * 对账；晚到 GET 不得覆盖尚未同步的本地 mutation。
     */
    cachedRead(
      'review.queue.read',
      ['/pdf/api/review-queue'],
      'local-snapshot-with-dirty-overlay',
      {
        transport: {
          extensionBridge: true,
          serviceWorker: 'private-network-fallback'
        }
      }
    ),
    networkRead(
      'review.candidates.read',
      ['/pdf/api/review-queue'],
      {
        methods: ['POST'],
        transport: {
          extensionBridge: true,
          serviceWorker: 'none'
        },
        reason: '正文只放在 POST body；失败时由调用方退回 GET 到期快照。'
      }
    ),
    cachedRead(
      'document.highlights.read',
      ['/pdf/api/highlights'],
      'local-snapshot-with-dirty-overlay',
      {
        transport: {
          extensionBridge: true,
          serviceWorker: 'private-network-fallback'
        }
      }
    ),
    cachedRead(
      'document.notes.read',
      ['/pdf/api/notes'],
      'local-snapshot-with-dirty-overlay',
      {
        transport: {
          extensionBridge: true,
          serviceWorker: 'private-network-fallback'
        }
      }
    ),
    cachedRead(
      'vocabulary.mastery-map.read',
      ['/pdf/api/vocab-mastery-map'],
      'local-snapshot-with-dirty-overlay',
      {
        transport: {
          extensionBridge: true,
          serviceWorker: 'private-network-fallback'
        }
      }
    ),
    cachedRead(
      'phrase.state.read',
      ['/pdf/api/phrases', '/pdf/api/phrase-mark'],
      'local-snapshot-with-dirty-overlay',
      {
        transport: {
          extensionBridge: true,
          serviceWorker: 'private-network-fallback'
        }
      }
    ),

    /* 纯派生页面 overlay 尚无可靠本地生成器，维持 network-first + 离线旧镜像。 */
    networkRead(
      'document.page-overlay.read',
      ['/pdf/api/page-overlay'],
      { transport: { serviceWorker: 'private-network-fallback' } }
    ),
  ];

  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function pathMatches(item, pathname) {
    var pattern = item.path;
    if (pattern.slice(-1) === '*') {
      return pathname.indexOf(pattern.slice(0, -1)) === 0;
    }
    var escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      .replace(/\\\{([A-Za-z][A-Za-z0-9_]*)\\\}/g, function (_, name) {
        var rule = item.params && item.params[name];
        return rule && typeof rule.pattern === 'string'
          ? '(?:' + rule.pattern + ')'
          : '[^/]+';
      });
    return new RegExp('^' + escaped + '$').test(pathname);
  }

  function pathnameOf(value) {
    value = String(value || '');
    try { return new URL(value, 'https://reader.invalid').pathname; }
    catch (_) { return value.split(/[?#]/, 1)[0]; }
  }

  function findPolicy(url, method) {
    var pathname = pathnameOf(url);
    method = String(method || 'GET').toUpperCase();
    for (var i = 0; i < POLICIES.length; i++) {
      for (var j = 0; j < POLICIES[i].matches.length; j++) {
        var item = POLICIES[i].matches[j];
        if (item.methods.indexOf(method) >= 0 && pathMatches(item, pathname)) {
          return clone(POLICIES[i]);
        }
      }
    }
    return null;
  }

  function validate(definitions) {
    definitions = definitions || POLICIES;
    var errors = [];
    var ids = Object.create(null);
    var routeKeys = Object.create(null);
    definitions.forEach(function (policy, index) {
      var at = 'policies[' + index + ']';
      if (!policy || typeof policy !== 'object') {
        errors.push(at + ' must be an object');
        return;
      }
      if (!/^[a-z][a-z0-9.-]+$/.test(String(policy.id || ''))) {
        errors.push(at + '.id is invalid');
      } else if (ids[policy.id]) {
        errors.push(at + '.id is duplicated');
      } else {
        ids[policy.id] = true;
      }
      if (!Array.isArray(policy.matches) || !policy.matches.length) {
        errors.push(at + '.matches must not be empty');
      } else {
        policy.matches.forEach(function (item, matchIndex) {
          var mat = at + '.matches[' + matchIndex + ']';
          if (!item || typeof item.path !== 'string' || item.path.charAt(0) !== '/') {
            errors.push(mat + '.path must be an absolute path');
            return;
          }
          if (!Array.isArray(item.methods) || !item.methods.length) {
            errors.push(mat + '.methods must not be empty');
            return;
          }
          var parameterNames = [];
          item.path.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, function (_, name) {
            parameterNames.push(name);
            return _;
          });
          if (
            item.params != null &&
            (!item.params || typeof item.params !== 'object' || Array.isArray(item.params))
          ) {
            errors.push(mat + '.params must be an object');
          } else if (item.params) {
            Object.keys(item.params).forEach(function (name) {
              var rule = item.params[name];
              if (parameterNames.indexOf(name) < 0) {
                errors.push(mat + '.params.' + name + ' has no path placeholder');
                return;
              }
              if (
                !rule ||
                typeof rule !== 'object' ||
                typeof rule.pattern !== 'string' ||
                !rule.pattern ||
                rule.pattern.length > 200
              ) {
                errors.push(mat + '.params.' + name + '.pattern is invalid');
                return;
              }
              try { new RegExp('^(?:' + rule.pattern + ')$'); }
              catch (_) { errors.push(mat + '.params.' + name + '.pattern is invalid'); }
            });
          }
          item.methods.forEach(function (method) {
            if (!/^(GET|POST|PUT|PATCH|DELETE)$/.test(method)) {
              errors.push(mat + ' contains invalid method ' + method);
              return;
            }
            var key = item.path + ' ' + method;
            if (routeKeys[key]) errors.push(mat + ' duplicates ' + key);
            else routeKeys[key] = policy.id;
          });
        });
      }
      if (!VALID_UI[policy.ui]) errors.push(at + '.ui is invalid');
      if (!VALID_ACK[policy.ack]) errors.push(at + '.ack is invalid');
      if (!VALID_OFFLINE[policy.offline]) errors.push(at + '.offline is invalid');
      if (!VALID_SYNC[policy.sync]) errors.push(at + '.sync is invalid');
      if (!Array.isArray(policy.surfaces) || !policy.surfaces.length) {
        errors.push(at + '.surfaces must not be empty');
      }
      if (
        policy.ui === 'local-immediate' &&
        (!Number.isFinite(policy.localEffectMs) || policy.localEffectMs < 0 || policy.localEffectMs > 50)
      ) {
        errors.push(at + '.localEffectMs must be between 0 and 50');
      }
      if (policy.ui === 'remote-required' && !String(policy.reason || '').trim()) {
        errors.push(at + '.reason is required for remote-required');
      }
      if (!policy.transport || typeof policy.transport !== 'object') {
        errors.push(at + '.transport is required');
      } else {
        if (typeof policy.transport.outbox !== 'boolean') {
          errors.push(at + '.transport.outbox must be boolean');
        }
        if (typeof policy.transport.extensionBridge !== 'boolean') {
          errors.push(at + '.transport.extensionBridge must be boolean');
        }
        if (!VALID_SERVICE_WORKER[policy.transport.serviceWorker]) {
          errors.push(at + '.transport.serviceWorker is invalid');
        }
      }
      if (Object.prototype.hasOwnProperty.call(policy, 'allow')) {
        errors.push(at + '.allow is deprecated; use transport');
      }
      if (policy.sync === 'outbox' && !(policy.transport && policy.transport.outbox === true)) {
        errors.push(at + ' outbox sync must be allowed explicitly');
      }
      if (policy.sync !== 'outbox' && policy.transport && policy.transport.outbox === true) {
        errors.push(at + ' must not allow outbox without outbox sync');
      }
      if (
        policy.transport &&
        policy.transport.extensionBridge === true &&
        (
          !Array.isArray(policy.surfaces) ||
          policy.surfaces.indexOf('extension') < 0
        )
      ) {
        errors.push(at + ' extensionBridge requires the extension surface');
      }
      if (
        policy.transport &&
        policy.transport.serviceWorker !== 'none' &&
        Array.isArray(policy.matches) &&
        policy.matches.some(function (item) {
          return (
            Array.isArray(item.methods) &&
            item.methods.some(function (method) { return method !== 'GET'; })
          );
        })
      ) {
        errors.push(at + ' serviceWorker caching only supports GET');
      }
    });
    return { contract: CONTRACT, ok: errors.length === 0, errors: errors };
  }

  var validation = validate(POLICIES);
  if (!validation.ok) {
    throw new Error('Invalid interaction policy: ' + validation.errors.join('; '));
  }

  return Object.freeze({
    CONTRACT: CONTRACT,
    policies: function () { return clone(POLICIES); },
    policy: function (id) {
      id = String(id || '');
      for (var i = 0; i < POLICIES.length; i++) {
        if (POLICIES[i].id === id) return clone(POLICIES[i]);
      }
      return null;
    },
    match: findPolicy,
    validate: function (definitions) { return validate(definitions ? clone(definitions) : POLICIES); }
  });
});
