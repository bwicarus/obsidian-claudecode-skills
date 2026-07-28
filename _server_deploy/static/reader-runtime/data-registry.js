/* data-registry.js — collection 归属的唯一白名单与渐进迁移门禁。
 *
 * 只有 status=ready 的 collection 才能进入 DataStore/扩展 provider。其余旧数据仍由
 * 原实现持有；这里用 pending 明确阻止“按前缀整包搬迁”造成的静默合并或功能丢失。
 * StorageRouter 与扩展后台不得再维护同名默认表；本文件缺失或版本不匹配时必须 fail closed。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.dataRegistry = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'data-registry/1';
  var SYNC_CONTRACT = 'sync-v3';
  var SYNC_CHANGE_CONTRACT = 'record-parent-state/1';
  var COLLECTIONS = {
    'user-settings': {
      scope: 'global', status: 'ready', provider: true, sync: true,
      recordSchema: 1,
      conflictPolicy: 'explicit',
      reason: '仅接收下方显式白名单中的语义设置'
    },
    'device-preferences': {
      scope: 'device', status: 'ready', provider: false,
      conflictPolicy: 'local-device',
      reason: '首屏和设备外观继续保留 localStorage 镜像'
    },
    'ui-session': {
      scope: 'device', status: 'ready', provider: false,
      conflictPolicy: 'local-device'
    },
    'query-cache': {
      scope: 'global', status: 'ready', provider: true,
      conflictPolicy: 'regenerate', derived: true,
      reason: '本地派生缓存；在 regenerate 冲突裁决器落地前不跨设备同步'
    },
    'translation-cache': {
      scope: 'global', status: 'ready', provider: true,
      conflictPolicy: 'regenerate', derived: true,
      reason: '本地派生缓存；在 regenerate 冲突裁决器落地前不跨设备同步'
    },
    'dictionary-cache': {
      scope: 'global', status: 'ready', provider: true,
      conflictPolicy: 'regenerate', derived: true,
      reason: '本地派生缓存；在 regenerate 冲突裁决器落地前不跨设备同步'
    },
    'vocabulary-state': {
      scope: 'global', status: 'ready', provider: true, sync: true,
      recordSchema: 1,
      conflictPolicy: 'explicit',
      reason: '只保存单词/词组掌握与词组收藏；词典笔记、释义和熟练度算法仍由原专用存储持有'
    },

    'credentials': {
      scope: 'global', status: 'pending', provider: false,
      reason: '凭据只由扩展网络服务使用，不通过通用 DataStore 返回给页面或 SyncGateway'
    },
    'assistant-action-prefs': {
      scope: 'global', status: 'pending', provider: false,
      reason: '现阶段只允许从服务端只读镜像，尚未切换权威来源'
    },
    'preference-profiles': {
      scope: 'global', status: 'pending', provider: false,
      reason: '服务端仍是权威来源，需先补 revision 与双写验证'
    },
    'conversation-threads': {
      scope: 'global', status: 'pending', provider: false,
      reason: 'PDF 全局历史与 EPUB 按书历史语义冲突，且缺稳定 threadId'
    },
    'conversation-messages': {
      scope: 'global', status: 'pending', provider: false,
      reason: '历史消息普遍缺稳定 messageId，不能安全合并'
    },
    'conversation-summaries': {
      scope: 'global', status: 'pending', provider: false,
      reason: '必须先绑定稳定 threadId'
    },
    'card-entities': {
      scope: 'global', status: 'pending', provider: false,
      reason: '现有卡片已用 cid/gid 保持唯一身份和跨宿主共享；待把 card_、c_、fcg_ 等有效旧编号无损映射到统一实体记录'
    },
    'card-states': {
      scope: 'global', status: 'pending', provider: false,
      reason: '状态已按 cid/gid 跨宿主联动，但现有数组状态与 entity PATCH 尚未映射到带 revision/tombstone 的统一记录'
    },
    'card-favorites': {
      scope: 'global', status: 'pending', provider: false,
      reason: '收藏宿主记录有自己的记录号并引用卡片 cid/gid；迁移必须保留引用关系，不能给同一张卡重编号'
    },
    'favorite-folders': {
      scope: 'global', status: 'pending', provider: false,
      reason: '文件夹有稳定编号，但尚未加入 revision/tombstone'
    },
    'favorite-memberships': {
      scope: 'global', status: 'pending', provider: false,
      reason: '收藏条目没有稳定 itemId，当前仅按位置字段判等'
    },
    'vocabulary': {
      scope: 'global', status: 'pending', provider: false,
      reason: 'Vault vocab 与 jp-vocab.json 仍是两个实时真源'
    },
    'annotation-metadata': {
      scope: 'global', status: 'pending', provider: false,
      reason: '必须先拆开通用语义实体与 PWA 文档锚点投影'
    },
    'cross-document-links': {
      scope: 'global', status: 'pending', provider: false,
      reason: '尚无稳定 DocumentId 与经过验证的实体格式'
    },

    'document-preferences': {
      scope: 'document', status: 'pending', provider: false,
      reason: '旧 pdf-* 同时混有缓存、布局和真正设置，禁止按前缀迁移'
    },
    'document-files': {
      scope: 'document', status: 'pending', provider: false,
      reason: 'Blob/ArrayBuffer 继续由 PWA AssetStore/CacheStorage 持有，不进入 JSON DataStore'
    },
    'document-cache': {
      scope: 'document', status: 'pending', provider: false,
      reason: '二进制与大对象缓存继续使用现有 PWA 专用缓存'
    },
    'anchors': {
      scope: 'document', status: 'pending', provider: false,
      reason: '需先建立不随 file_rel 改名变化的 DocumentId'
    },
    'geometry': {
      scope: 'document', status: 'pending', provider: false,
      reason: '格式私有几何继续由 DocumentHost 和现有 sidecar 持有'
    },
    'ink': {
      scope: 'document', status: 'pending', provider: false,
      reason: '现有 PDF/EPUB sidecar 保留，待稳定 DocumentId 后只接适配器'
    },
    'user-pages': {
      scope: 'document', status: 'pending', provider: false,
      reason: '现有 reader-userpages sidecar 保留'
    },
    'render-state': {
      scope: 'document', status: 'pending', provider: false,
      reason: '渲染状态不属于通用同步数据'
    },
    'reading-position': {
      scope: 'document', status: 'pending', provider: false,
      reason: '本地与服务端已有两套带 ts 仲裁的真源'
    },
    'document-highlights': {
      scope: 'document', status: 'pending', provider: false,
      reason: '先保持三种格式原 sidecar，不改写锚点'
    },
    'document-notes': {
      scope: 'document', status: 'ready', provider: false,
      conflictPolicy: 'explicit',
      reason: 'DocumentNoteRepository 数据契约已稳定；PWA 旧便签 UI 尚未迁移，宿主私有 anchor 只作不透明 envelope'
    },
    'placements': {
      scope: 'document', status: 'pending', provider: false,
      reason: '卡片实体编号与文档投影尚未彻底拆分'
    },
    'query-history': {
      scope: 'document', status: 'pending', provider: false,
      reason: 'pdf-qhist-* 是用户内容而不是可重建 query-cache'
    },
    'document-translations': {
      scope: 'document', status: 'pending', provider: false,
      reason: '页面句子投影不得与全局 translation-cache 合并'
    },

    /* 旧粗粒度 collection 只作阻断提示，不再作为新代码入口。 */
    'settings': {
      scope: 'global', status: 'pending', provider: false,
      reason: '请改用 user-settings / device-preferences / document-preferences'
    },
    'conversations': {
      scope: 'global', status: 'pending', provider: false,
      reason: '请先完成 thread/message 稳定编号'
    },
    'cards': {
      scope: 'global', status: 'pending', provider: false,
      reason: '卡片唯一身份已经确认；请先完成旧编号无损映射和状态记录拆分，再使用 card-entities/card-states'
    },
    'favorites': {
      scope: 'global', status: 'pending', provider: false,
      reason: '请拆为 favorite-folders/favorite-memberships'
    }
  };

  /*
   * PreferenceStore 唯一显式白名单。legacyKey 不匹配的键绝不因前缀相似而迁移；
   * 文档数据、位置、缓存、草稿、语音和旧模型键必须留在各自专用层。
   */
  var SETTING_MIGRATIONS = [
    { legacyKey: 'bw-set-target-langs', collection: 'user-settings', semanticKey: 'reading.target-languages', codec: 'json-string' },
    { legacyKey: 'rcWebTrStyle', collection: 'user-settings', semanticKey: 'translation.display-style', codec: 'string' },
    { legacyKey: 'eph-vocab-underline', collection: 'user-settings', semanticKey: 'vocabulary.underline', codec: 'boolean-string' },
    { legacyKey: 'eph-click-translate', collection: 'user-settings', semanticKey: 'vocabulary.click-translate', codec: 'boolean-string' },
    { legacyKey: 'eph-web-pretr', collection: 'user-settings', semanticKey: 'translation.pretranslate', codec: 'boolean-string' },
    { legacyKey: 'eph-grammar-view', collection: 'user-settings', semanticKey: 'grammar.view-mode', codec: 'string' },
    { legacyKey: 'pdf-grammar-view', collection: 'user-settings', semanticKey: 'grammar.view-mode.pdf', codec: 'string' },
    { legacyKey: 'pdf-vocab-underline', collection: 'user-settings', semanticKey: 'vocabulary.pdf-underline', codec: 'boolean-string' },
    { legacyKey: 'pdf-click-translate-unmastered', collection: 'user-settings', semanticKey: 'vocabulary.pdf-click-translate', codec: 'boolean-string' },
    { legacyKey: 'pdf-hl-colors', collection: 'user-settings', semanticKey: 'highlight.palette', codec: 'json-string' },
    { legacyKey: 'eph-hl-colors', collection: 'user-settings', semanticKey: 'highlight.palette.epub-web', codec: 'json-string' },
    { legacyKey: 'eph-fig-badge', collection: 'user-settings', semanticKey: 'figures.badge', codec: 'boolean-string' },
    { legacyKey: 'rc-prefer-image', collection: 'user-settings', semanticKey: 'assistant.prefer-image', codec: 'boolean-string' },
    { legacyKey: 'rc-prefer-video', collection: 'user-settings', semanticKey: 'assistant.prefer-video', codec: 'boolean-string' },
    { legacyKey: 'rc-prefer-book', collection: 'user-settings', semanticKey: 'assistant.prefer-book', codec: 'boolean-string' },
    { legacyKey: 'asst-followups-on', collection: 'user-settings', semanticKey: 'assistant.followups-visible', codec: 'boolean-string' },

    { legacyKey: 'eph-gp-floating', collection: 'device-preferences', semanticKey: 'sidebar.floating', codec: 'boolean-string' },
    { legacyKey: 'eph-gp-blur', collection: 'device-preferences', semanticKey: 'sidebar.blur', codec: 'number-string' },
    { legacyKey: 'ep-side-width', collection: 'device-preferences', semanticKey: 'sidebar.width', codec: 'number-string' },
    { legacyKey: 'ep-side-tab', collection: 'device-preferences', semanticKey: 'sidebar.active-tab', codec: 'string' },
    { legacyKey: 'ep-side-tabs-off', collection: 'device-preferences', semanticKey: 'sidebar.hidden-tabs', codec: 'json-string' },
    /*
     * PDF 的侧栏外观按排版模式分别记忆。这里故意登记成六个 semantic key，
     * 不能与上面的 EPUB/HTML/Web 通用侧栏键合并。
     */
    { legacyKey: 'pdf-gp-width-continuous', collection: 'device-preferences', semanticKey: 'sidebar.pdf.continuous.width', codec: 'number-string' },
    { legacyKey: 'pdf-gp-floating-continuous', collection: 'device-preferences', semanticKey: 'sidebar.pdf.continuous.floating', codec: 'boolean-string' },
    { legacyKey: 'pdf-gp-blur-continuous', collection: 'device-preferences', semanticKey: 'sidebar.pdf.continuous.blur', codec: 'number-string' },
    { legacyKey: 'pdf-gp-width-spread', collection: 'device-preferences', semanticKey: 'sidebar.pdf.spread.width', codec: 'number-string' },
    { legacyKey: 'pdf-gp-floating-spread', collection: 'device-preferences', semanticKey: 'sidebar.pdf.spread.floating', codec: 'boolean-string' },
    { legacyKey: 'pdf-gp-blur-spread', collection: 'device-preferences', semanticKey: 'sidebar.pdf.spread.blur', codec: 'number-string' },
    /* 三个宿主的设置页内容不同，分别记忆当前 tab，禁止互相覆盖。 */
    { legacyKey: 'eph-set-tab', collection: 'device-preferences', semanticKey: 'settings.active-tab.epub', codec: 'string' },
    { legacyKey: 'bw-set-tab', collection: 'device-preferences', semanticKey: 'settings.active-tab.web', codec: 'string' },
    { legacyKey: 'pdf-set-tab', collection: 'device-preferences', semanticKey: 'settings.active-tab.pdf', codec: 'string' },
    /* 共享便签组件的设备外观/触控偏好。 */
    { legacyKey: 'rc-note-opacity', collection: 'device-preferences', semanticKey: 'note.opacity', codec: 'number-string' },
    { legacyKey: 'rc-note-autocontrast', collection: 'device-preferences', semanticKey: 'note.auto-contrast', codec: 'boolean-string' },
    { legacyKey: 'rc-note-longpress', collection: 'device-preferences', semanticKey: 'note.long-press-ms', codec: 'number-string' },
    { legacyKey: 'rc-note-blur', collection: 'device-preferences', semanticKey: 'note.blur', codec: 'number-string' },
    { legacyKey: 'eph-debug', collection: 'device-preferences', semanticKey: 'reader.debug', codec: 'boolean-string' },
    { legacyKey: 'eph-hl-color', collection: 'device-preferences', semanticKey: 'highlight.active-color', codec: 'string' },
    { legacyKey: 'eph-ink-color', collection: 'device-preferences', semanticKey: 'ink.active-color', codec: 'string' },
    { legacyKey: 'eph-fs-mode', collection: 'device-preferences', semanticKey: 'reader.fullscreen-mode', codec: 'boolean-string' },
    { legacyKey: 'eph-fs', collection: 'device-preferences', semanticKey: 'reader.font-scale', codec: 'number-string' },
    { legacyKey: 'eph-th', collection: 'device-preferences', semanticKey: 'reader.theme', codec: 'string' },
    { legacyKey: 'eph-lh', collection: 'device-preferences', semanticKey: 'reader.line-height', codec: 'number-string' },
    { legacyKey: 'eph-mw', collection: 'device-preferences', semanticKey: 'reader.column-width', codec: 'number-string' },
    { legacyKey: 'html-fs-pct', collection: 'device-preferences', semanticKey: 'reader.html.font-scale', codec: 'number-string' },
    { legacyKey: 'html-lh', collection: 'device-preferences', semanticKey: 'reader.html.line-height', codec: 'number-string' },
    { legacyKey: 'html-th', collection: 'device-preferences', semanticKey: 'reader.html.theme', codec: 'string' },
    { legacyKey: 'html-hl-color', collection: 'device-preferences', semanticKey: 'highlight.html.active-color', codec: 'string' },
    { legacyKey: 'pdf-read-mode', collection: 'device-preferences', semanticKey: 'pdf.read-mode', codec: 'string' },
    { legacyKey: 'pdf-fullscreen', collection: 'device-preferences', semanticKey: 'pdf.fullscreen', codec: 'boolean-string' },
    { legacyKey: 'pdf-debug', collection: 'device-preferences', semanticKey: 'pdf.debug', codec: 'boolean-string' },
    { legacyKey: 'pdf-auto-orient', collection: 'device-preferences', semanticKey: 'pdf.auto-orient', codec: 'boolean-string' },
    { legacyKey: 'pdf-charbox', collection: 'device-preferences', semanticKey: 'pdf.character-boxes', codec: 'boolean-string' },
    { legacyKey: 'pdf-ruby', collection: 'device-preferences', semanticKey: 'pdf.ruby', codec: 'boolean-string' },
    { legacyKey: 'pdf-img-mode', collection: 'device-preferences', semanticKey: 'pdf.image-mode', codec: 'boolean-string' },
    { legacyKey: 'pdf-auto-prewarm', collection: 'device-preferences', semanticKey: 'pdf.auto-prewarm', codec: 'boolean-string' }
  ];

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function scopes() {
    var out = {};
    Object.keys(COLLECTIONS).forEach(function (name) {
      var item = COLLECTIONS[name];
      out[name] = {
        scope: item.scope,
        status: item.status,
        provider: item.provider === true,
        sync: item.sync === true,
        derived: item.derived === true,
        recordSchema: Number.isInteger(item.recordSchema)
          ? item.recordSchema
          : 0,
        conflictPolicy: item.conflictPolicy || '',
        reason: item.reason || ''
      };
    });
    return out;
  }
  function providerCollections() {
    return Object.keys(COLLECTIONS).filter(function (name) {
      return isProviderCollection(name);
    }).sort();
  }
  function syncCollections() {
    return Object.keys(COLLECTIONS).filter(function (name) {
      return isSyncCollection(name);
    }).sort();
  }
  function syncDescriptor() {
    return syncCollections().map(function (name) {
      var item = COLLECTIONS[name];
      var conflictPolicy = String(item.conflictPolicy || '').trim();
      var recordSchema = Number(item.recordSchema);
      if (
        !conflictPolicy ||
        !/^[A-Za-z0-9._-]+$/.test(name) ||
        !/^[A-Za-z0-9._-]+$/.test(conflictPolicy) ||
        !Number.isInteger(recordSchema) ||
        recordSchema < 1
      ) {
        throw new Error(
          '同步 collection 缺少 conflictPolicy/recordSchema：' + name
        );
      }
      return {
        name: name,
        conflictPolicy: conflictPolicy,
        derived: item.derived === true,
        recordSchema: recordSchema
      };
    });
  }
  function syncDigest() {
    return SYNC_CONTRACT + ':' + SYNC_CHANGE_CONTRACT + '|' +
      syncDescriptor().map(function (item) {
      return [
        item.name,
        item.conflictPolicy,
        item.derived ? '1' : '0',
        String(item.recordSchema)
      ].join(':');
    }).join('|');
  }
  function isProviderCollection(name) {
    var item = COLLECTIONS[String(name || '')];
    return !!(
      item &&
      item.scope === 'global' &&
      item.status === 'ready' &&
      item.provider === true
    );
  }
  function isSyncCollection(name) {
    var item = COLLECTIONS[String(name || '')];
    return !!(
      item &&
      item.scope === 'global' &&
      item.status === 'ready' &&
      item.provider === true &&
      item.sync === true
    );
  }
  function collection(name) {
    var item = COLLECTIONS[String(name || '')];
    return item ? clone(item) : null;
  }

  return {
    CONTRACT: CONTRACT,
    SYNC_CONTRACT: SYNC_CONTRACT,
    SYNC_CHANGE_CONTRACT: SYNC_CHANGE_CONTRACT,
    collections: function () { return clone(COLLECTIONS); },
    collection: collection,
    scopes: scopes,
    providerCollections: providerCollections,
    isProviderCollection: isProviderCollection,
    syncCollections: syncCollections,
    isSyncCollection: isSyncCollection,
    syncDescriptor: syncDescriptor,
    syncDigest: syncDigest,
    settingMigrations: function () { return clone(SETTING_MIGRATIONS); }
  };
});
