/* direct-sync-protocol.js — bounded request/response protocol over RTCDataChannel.
 *
 * Signalling, peer selection and the durable server baseline are host concerns.
 * This module never sees an account token or arbitrary URL.  It only exchanges
 * validated sync-gateway/1 payloads after the host has established an
 * account-fenced channel.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.directSyncProtocol = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'direct-sync/1';
  var GATEWAY_CONTRACT = 'sync-gateway/2';
  var MAX_MESSAGE_BYTES = 1024 * 1024;
  // 18 KiB of binary data becomes a 24 KiB base64 field.  With the bounded
  // identity/envelope fields every wire frame remains below 32 KiB.
  var FRAME_PAYLOAD_BYTES = 18 * 1024;
  var MAX_FRAME_WIRE_BYTES = 32 * 1024;
  var MAX_FRAME_COUNT = Math.ceil(MAX_MESSAGE_BYTES / FRAME_PAYLOAD_BYTES);
  var MAX_CHANGE_COUNT = 2000;
  var DEFAULT_PENDING_REQUESTS = 32;
  var DEFAULT_INBOUND_REQUESTS = 16;
  var DEFAULT_INBOUND_ASSEMBLIES = 8;
  var DEFAULT_BUFFER_HIGH_WATER = 256 * 1024;
  var DEFAULT_BUFFER_LOW_WATER = 64 * 1024;

  function DirectSyncError(message, code, retryable, details) {
    this.name = 'DirectSyncError';
    this.message = String(message || '设备直连失败');
    this.code = String(code || 'BW_DIRECT_ERROR');
    this.retryable = !!retryable;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, DirectSyncError);
  }
  DirectSyncError.prototype = Object.create(Error.prototype);
  DirectSyncError.prototype.constructor = DirectSyncError;

  function own(value, key) {
    return Object.prototype.hasOwnProperty.call(value, key);
  }
  function isPlainObject(value) {
    if (!value || Object.prototype.toString.call(value) !== '[object Object]') {
      return false;
    }
    var prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }
  function clone(value) {
    if (value == null) return value;
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_) {
      throw new DirectSyncError(
        '直连数据不能序列化',
        'BW_DIRECT_INVALID',
        false
      );
    }
  }
  function boundedInteger(value, minimum, maximum, label) {
    if (
      typeof value !== 'number' ||
      !Number.isSafeInteger(value) ||
      value < minimum ||
      value > maximum
    ) {
      throw new DirectSyncError(
        label + ' 无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
    return value;
  }
  function boundedString(value, label, maximum, pattern) {
    if (
      typeof value !== 'string' ||
      !value ||
      value.length > maximum ||
      /[\u0000-\u001f\u007f]/.test(value) ||
      pattern && !pattern.test(value)
    ) {
      throw new DirectSyncError(
        label + ' 无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
    return value;
  }
  function safe(value, label, maximum, pattern) {
    value = String(value == null ? '' : value).trim();
    return boundedString(value, label, maximum || 512, pattern);
  }
  function assertObjectKeys(value, allowed, label) {
    if (!isPlainObject(value)) {
      throw new DirectSyncError(
        label + ' 必须是对象',
        'BW_DIRECT_INVALID',
        false
      );
    }
    Object.keys(value).forEach(function (key) {
      if (!allowed[key]) {
        throw new DirectSyncError(
          label + ' 包含未知字段：' + key,
          'BW_DIRECT_INVALID',
          false
        );
      }
    });
    return value;
  }
  function byteLength(text) {
    if (typeof TextEncoder !== 'undefined') {
      return new TextEncoder().encode(text).byteLength;
    }
    return unescape(encodeURIComponent(text)).length;
  }
  function utf8Encode(text) {
    if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(text);
    var encoded = unescape(encodeURIComponent(text));
    var bytes = new Uint8Array(encoded.length);
    for (var index = 0; index < encoded.length; index += 1) {
      bytes[index] = encoded.charCodeAt(index);
    }
    return bytes;
  }
  function utf8Decode(bytes) {
    if (typeof TextDecoder !== 'undefined') {
      try {
        return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      } catch (_) {
        throw new DirectSyncError(
          '直连消息 UTF-8 无效',
          'BW_DIRECT_INVALID',
          false
        );
      }
    }
    var encoded = '';
    for (var index = 0; index < bytes.length; index += 1) {
      encoded += String.fromCharCode(bytes[index]);
    }
    try {
      return decodeURIComponent(escape(encoded));
    } catch (_) {
      throw new DirectSyncError(
        '直连消息 UTF-8 无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
  }
  function bytesToBase64(bytes) {
    var binary = '';
    for (var index = 0; index < bytes.length; index += 1) {
      binary += String.fromCharCode(bytes[index]);
    }
    if (typeof btoa === 'function') return btoa(binary);
    if (typeof Buffer !== 'undefined') {
      return Buffer.from(bytes).toString('base64');
    }
    throw new DirectSyncError(
      '当前环境不支持 base64',
      'BW_DIRECT_UNAVAILABLE',
      false
    );
  }
  function base64ToBytes(text) {
    if (
      typeof text !== 'string' ||
      !text ||
      text.length > Math.ceil(FRAME_PAYLOAD_BYTES / 3) * 4 + 4 ||
      text.length % 4 !== 0 ||
      !/^[A-Za-z0-9+/]+={0,2}$/.test(text)
    ) {
      throw new DirectSyncError(
        '直连分帧内容无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
    var binary;
    try {
      if (typeof atob === 'function') binary = atob(text);
      else if (typeof Buffer !== 'undefined') {
        var buffer = Buffer.from(text, 'base64');
        // Buffer accepts several non-canonical encodings.  Re-encoding gives
        // us one canonical representation before accepting peer input.
        if (buffer.toString('base64') !== text) throw new Error('base64');
        return new Uint8Array(buffer);
      } else {
        throw new Error('base64 unavailable');
      }
    } catch (_) {
      throw new DirectSyncError(
        '直连分帧内容无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
    var bytes = new Uint8Array(binary.length);
    for (var index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
  }
  function serializeMessage(value) {
    var text;
    try {
      text = JSON.stringify(value);
    } catch (_) {
      throw new DirectSyncError(
        '直连消息不能序列化',
        'BW_DIRECT_INVALID',
        false
      );
    }
    if (typeof text !== 'string' || !text) {
      throw new DirectSyncError(
        '直连消息为空',
        'BW_DIRECT_INVALID',
        false
      );
    }
    var bytes = utf8Encode(text);
    if (bytes.byteLength > MAX_MESSAGE_BYTES) {
      throw new DirectSyncError(
        '直连消息超过 1 MiB 上限',
        'BW_DIRECT_TOO_LARGE',
        false
      );
    }
    return bytes;
  }
  function channelReady(channel) {
    return !!channel &&
      channel.readyState === 'open' &&
      typeof channel.send === 'function';
  }
  function listen(channel, listener) {
    if (typeof channel.addEventListener === 'function') {
      channel.addEventListener('message', listener);
      return function () {
        try { channel.removeEventListener('message', listener); } catch (_) {}
      };
    }
    var previous = channel.onmessage;
    var installed = function (event) {
      if (typeof previous === 'function') previous.call(channel, event);
      listener(event);
    };
    channel.onmessage = installed;
    return function () {
      // Only restore the previous owner when our exact wrapper is still
      // installed.  This avoids removing a later listener and fixes the old
      // comparison against `listener`, which could never match.
      if (channel.onmessage === installed) channel.onmessage = previous || null;
    };
  }

  function validateChange(change, label) {
    label = label || 'change';
    assertObjectKeys(change, {
      cursor: true,
      mutationId: true,
      operation: true,
      collection: true,
      record: true,
      // IndexedDB marks locally journaled direct imports with these two
      // internal booleans.  They are accepted as known local metadata but are
      // deliberately stripped from the wire representation below.
      imported: true,
      remote: true
    }, label);
    boundedInteger(change.cursor, 0, Number.MAX_SAFE_INTEGER, label + '.cursor');
    boundedString(change.mutationId, label + '.mutationId', 512);
    boundedString(
      change.operation,
      label + '.operation',
      8,
      /^(put|remove)$/
    );
    boundedString(
      change.collection,
      label + '.collection',
      128,
      /^[A-Za-z0-9._:-]+$/
    );
    if (!isPlainObject(change.record)) {
      throw new DirectSyncError(
        label + '.record 无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
    boundedString(
      change.record.id,
      label + '.record.id',
      512
    );
    if (own(change, 'imported') && typeof change.imported !== 'boolean' ||
        own(change, 'remote') && typeof change.remote !== 'boolean') {
      throw new DirectSyncError(
        label + ' 本地元数据无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
    return {
      cursor: change.cursor,
      mutationId: change.mutationId,
      operation: change.operation,
      collection: change.collection,
      record: clone(change.record)
    };
  }
  function validateChanges(changes, label) {
    if (!Array.isArray(changes) || changes.length > MAX_CHANGE_COUNT) {
      throw new DirectSyncError(
        label + ' 无效或超过上限',
        'BW_DIRECT_INVALID',
        false
      );
    }
    return changes.map(function (change, index) {
      return validateChange(change, label + '[' + index + ']');
    });
  }
  function validateGatewayRequest(input) {
    assertObjectKeys(input, {
      contract: true,
      direction: true,
      deviceId: true,
      cursor: true,
      limit: true,
      changes: true
    }, 'gateway request');
    if (input.contract !== GATEWAY_CONTRACT) {
      throw new DirectSyncError(
        'gateway contract 不匹配',
        'BW_DIRECT_CONTRACT',
        false
      );
    }
    boundedString(
      input.direction,
      'gateway direction',
      4,
      /^(push|pull)$/
    );
    boundedString(
      input.deviceId,
      'gateway deviceId',
      128,
      /^[A-Za-z0-9._:-]+$/
    );
    boundedInteger(
      input.cursor,
      0,
      Number.MAX_SAFE_INTEGER,
      'gateway cursor'
    );
    boundedInteger(input.limit, 1, MAX_CHANGE_COUNT, 'gateway limit');
    var changes = validateChanges(input.changes, 'gateway changes');
    if (input.direction === 'pull' && changes.length) {
      throw new DirectSyncError(
        'pull 请求不能携带变化',
        'BW_DIRECT_INVALID',
        false
      );
    }
    return {
      contract: GATEWAY_CONTRACT,
      direction: input.direction,
      deviceId: input.deviceId,
      cursor: input.cursor,
      limit: input.limit,
      changes: changes
    };
  }
  function validateGatewayResult(input) {
    assertObjectKeys(input, {
      contract: true,
      cursor: true,
      headCursor: true,
      oldestCursor: true,
      hasMore: true,
      resetRequired: true,
      ackedMutationIds: true,
      changes: true,
      conflicts: true
    }, 'gateway response');
    if (input.contract !== GATEWAY_CONTRACT) {
      throw new DirectSyncError(
        'gateway response contract 不匹配',
        'BW_DIRECT_CONTRACT',
        false
      );
    }
    ['cursor', 'headCursor', 'oldestCursor'].forEach(function (field) {
      boundedInteger(
        input[field],
        0,
        Number.MAX_SAFE_INTEGER,
        'gateway response.' + field
      );
    });
    if (typeof input.hasMore !== 'boolean' ||
        typeof input.resetRequired !== 'boolean') {
      throw new DirectSyncError(
        'gateway response 布尔字段无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
    if (
      !Array.isArray(input.ackedMutationIds) ||
      input.ackedMutationIds.length > MAX_CHANGE_COUNT
    ) {
      throw new DirectSyncError(
        'gateway response ACK 无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
    var acknowledged = input.ackedMutationIds.map(function (mutationId, index) {
      return boundedString(
        mutationId,
        'gateway response.ackedMutationIds[' + index + ']',
        512
      );
    });
    var changes = validateChanges(input.changes, 'gateway response.changes');
    if (
      !Array.isArray(input.conflicts) ||
      input.conflicts.length > MAX_CHANGE_COUNT
    ) {
      throw new DirectSyncError(
        'gateway response conflicts 无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
    return {
      contract: GATEWAY_CONTRACT,
      cursor: input.cursor,
      headCursor: input.headCursor,
      oldestCursor: input.oldestCursor,
      hasMore: input.hasMore,
      resetRequired: input.resetRequired,
      ackedMutationIds: acknowledged,
      changes: changes,
      conflicts: clone(input.conflicts)
    };
  }
  function validateRemoteError(input) {
    assertObjectKeys(input, {
      code: true,
      message: true,
      retryable: true
    }, 'remote error');
    return {
      code: boundedString(
        input.code,
        'remote error.code',
        96,
        /^[A-Z0-9_:-]+$/
      ),
      message: boundedString(input.message, 'remote error.message', 512),
      retryable: input.retryable === true
    };
  }
  function validateProtocolMessage(input, identity) {
    if (!isPlainObject(input) || input.contract !== CONTRACT) {
      throw new DirectSyncError(
        '直连协议不匹配',
        'BW_DIRECT_CONTRACT',
        false
      );
    }
    boundedString(
      input.type,
      'message.type',
      8,
      /^(REQUEST|RESPONSE)$/
    );
    var allowed = {
      contract: true,
      type: true,
      requestId: true,
      sessionId: true,
      accountProof: true,
      registryDigest: true
    };
    if (input.type === 'REQUEST') allowed.payload = true;
    else {
      allowed.ok = true;
      if (input.ok === true) allowed.result = true;
      else allowed.error = true;
    }
    assertObjectKeys(input, allowed, 'direct message');
    boundedString(
      input.requestId,
      'requestId',
      128,
      /^[A-Za-z0-9._:-]+$/
    );
    boundedString(input.sessionId, 'sessionId', 160);
    boundedString(input.accountProof, 'accountProof', 512);
    boundedString(input.registryDigest, 'registryDigest', 512);
    if (
      input.sessionId !== identity.sessionId ||
      input.accountProof !== identity.accountProof ||
      input.registryDigest !== identity.registryDigest
    ) {
      throw new DirectSyncError(
        '直连账户、会话或 registry 围栏不匹配',
        'BW_DIRECT_FENCE',
        false
      );
    }
    if (input.type === 'REQUEST') {
      input.payload = validateGatewayRequest(input.payload);
    } else {
      if (typeof input.ok !== 'boolean') {
        throw new DirectSyncError(
          'response.ok 无效',
          'BW_DIRECT_INVALID',
          false
        );
      }
      if (input.ok) input.result = validateGatewayResult(input.result);
      else input.error = validateRemoteError(input.error);
    }
    return input;
  }
  function validateFrame(raw, identity) {
    if (
      typeof raw !== 'string' ||
      !raw ||
      byteLength(raw) > MAX_FRAME_WIRE_BYTES
    ) {
      throw new DirectSyncError(
        '直连分帧无效或超过 32 KiB',
        'BW_DIRECT_TOO_LARGE',
        false
      );
    }
    var frame;
    try { frame = JSON.parse(raw); }
    catch (_) {
      throw new DirectSyncError(
        '直连分帧不是 JSON',
        'BW_DIRECT_INVALID',
        false
      );
    }
    assertObjectKeys(frame, {
      contract: true,
      type: true,
      messageId: true,
      frameIndex: true,
      frameCount: true,
      totalBytes: true,
      sessionId: true,
      accountProof: true,
      registryDigest: true,
      chunk: true
    }, 'direct frame');
    if (frame.contract !== CONTRACT || frame.type !== 'FRAME') {
      throw new DirectSyncError(
        '直连分帧合同不匹配',
        'BW_DIRECT_CONTRACT',
        false
      );
    }
    boundedString(
      frame.messageId,
      'messageId',
      128,
      /^[A-Za-z0-9._:-]+$/
    );
    boundedInteger(frame.frameCount, 1, MAX_FRAME_COUNT, 'frameCount');
    boundedInteger(
      frame.frameIndex,
      0,
      frame.frameCount - 1,
      'frameIndex'
    );
    boundedInteger(frame.totalBytes, 1, MAX_MESSAGE_BYTES, 'totalBytes');
    if (
      frame.frameCount !==
      Math.ceil(frame.totalBytes / FRAME_PAYLOAD_BYTES)
    ) {
      throw new DirectSyncError(
        '直连分帧数量与总长度不一致',
        'BW_DIRECT_INVALID',
        false
      );
    }
    boundedString(frame.sessionId, 'frame.sessionId', 160);
    boundedString(frame.accountProof, 'frame.accountProof', 512);
    boundedString(frame.registryDigest, 'frame.registryDigest', 512);
    if (
      frame.sessionId !== identity.sessionId ||
      frame.accountProof !== identity.accountProof ||
      frame.registryDigest !== identity.registryDigest
    ) {
      throw new DirectSyncError(
        '直连账户、会话或 registry 围栏不匹配',
        'BW_DIRECT_FENCE',
        false
      );
    }
    var bytes = base64ToBytes(frame.chunk);
    var expected = frame.frameIndex < frame.frameCount - 1
      ? FRAME_PAYLOAD_BYTES
      : frame.totalBytes -
        (frame.frameCount - 1) * FRAME_PAYLOAD_BYTES;
    if (bytes.byteLength !== expected) {
      throw new DirectSyncError(
        '直连分帧长度无效',
        'BW_DIRECT_INVALID',
        false
      );
    }
    frame.bytes = bytes;
    return frame;
  }
  function makeFrames(value, identity, messageId) {
    var bytes = serializeMessage(value);
    var count = Math.ceil(bytes.byteLength / FRAME_PAYLOAD_BYTES);
    var frames = [];
    for (var index = 0; index < count; index += 1) {
      var start = index * FRAME_PAYLOAD_BYTES;
      var end = Math.min(bytes.byteLength, start + FRAME_PAYLOAD_BYTES);
      var raw = JSON.stringify({
        contract: CONTRACT,
        type: 'FRAME',
        messageId: messageId,
        frameIndex: index,
        frameCount: count,
        totalBytes: bytes.byteLength,
        sessionId: identity.sessionId,
        accountProof: identity.accountProof,
        registryDigest: identity.registryDigest,
        chunk: bytesToBase64(bytes.subarray(start, end))
      });
      if (byteLength(raw) > MAX_FRAME_WIRE_BYTES) {
        throw new DirectSyncError(
          '直连分帧超过 32 KiB',
          'BW_DIRECT_TOO_LARGE',
          false
        );
      }
      frames.push(raw);
    }
    return frames;
  }

  function createStoreRelay(options) {
    options = options || {};
    var store = options.store;
    var registry = options.registry;
    if (
      !store ||
      typeof store.changes !== 'function' ||
      typeof store.applyChanges !== 'function' ||
      typeof store.status !== 'function'
    ) {
      throw new DirectSyncError('store 无效', 'BW_DIRECT_STORE', false);
    }
    if (!registry || typeof registry.isSyncCollection !== 'function') {
      throw new DirectSyncError('registry 无效', 'BW_DIRECT_REGISTRY', false);
    }
    function collection(change) {
      return String(change && (
        change.collection ||
        change.record && change.record.collection
      ) || '');
    }
    function validateCollections(changes) {
      changes.forEach(function (change) {
        if (!registry.isSyncCollection(collection(change))) {
          throw new DirectSyncError(
            '直连包含未开放 collection：' + collection(change),
            'BW_DIRECT_COLLECTION',
            false
          );
        }
      });
    }
    return {
      exchange: function (request) {
        try {
          request = validateGatewayRequest(request);
          validateCollections(request.changes);
        } catch (error) {
          return Promise.reject(error);
        }
        if (request.direction === 'push') {
          var incoming = request.changes;
          return Promise.resolve(store.applyChanges(
            incoming,
            { journal: true, tombstoneDominates: true }
          )).then(function (result) {
            result = result || {};
            var conflicts = Array.isArray(result.conflicts)
              ? result.conflicts
              : [];
            var conflictMutationIds = new Set(conflicts.map(function (item) {
              return String(item && item.mutationId || '');
            }));
            var hasUnscopedConflict = conflictMutationIds.has('');
            var acknowledged = incoming.filter(function (change) {
              return !hasUnscopedConflict &&
                !conflictMutationIds.has(String(change.mutationId || ''));
            }).map(function (change) {
              return String(change.mutationId || '');
            });
            return Promise.resolve(store.status()).then(function (status) {
              var head = Math.max(0, Number(status && status.cursor) || 0);
              return validateGatewayResult({
                contract: GATEWAY_CONTRACT,
                cursor: request.cursor,
                headCursor: head,
                oldestCursor: 0,
                hasMore: false,
                resetRequired: false,
                ackedMutationIds: acknowledged,
                changes: [],
                conflicts: conflicts
              });
            });
          });
        }
        return Promise.resolve(store.changes({
          after: request.cursor,
          limit: Math.max(1, Math.min(100, request.limit))
        })).then(function (batch) {
          batch = batch || {};
          var outgoing = validateChanges(
            (batch.changes || []).filter(function (change) {
              return registry.isSyncCollection(collection(change));
            }),
            'outgoing changes'
          );
          validateCollections(outgoing);
          return validateGatewayResult({
            contract: GATEWAY_CONTRACT,
            cursor: Math.max(
              Number(batch.nextCursor) || 0,
              request.cursor
            ),
            headCursor: Math.max(0, Number(batch.cursor) || 0),
            oldestCursor: Math.max(0, Number(batch.oldestCursor) || 0),
            hasMore: !!batch.hasMore,
            resetRequired: !!batch.resetRequired,
            ackedMutationIds: [],
            changes: outgoing,
            conflicts: []
          });
        });
      }
    };
  }

  function createChannelTransport(options) {
    options = options || {};
    var channel = options.channel;
    var identity = {
      sessionId: safe(
        options.sessionId,
        'sessionId',
        160,
        /^[A-Za-z0-9._:-]+$/
      ),
      accountProof: safe(
        options.accountProof,
        'accountProof',
        512,
        /^[A-Za-z0-9._:-]+$/
      ),
      registryDigest: safe(
        options.registryDigest,
        'registryDigest',
        512,
        /^[A-Za-z0-9._|:/-]+$/
      )
    };
    var relay = options.relay || null;
    var timeoutMs = Math.max(1000, Number(options.timeoutMs) || 15000);
    var reassemblyTimeoutMs = Math.max(
      50,
      Math.min(60000, Number(options.reassemblyTimeoutMs) || 10000)
    );
    var maxPendingRequests = Math.max(
      1,
      Math.min(64, Number(options.maxPendingRequests) ||
        DEFAULT_PENDING_REQUESTS)
    );
    var maxInboundRequests = Math.max(
      1,
      Math.min(32, Number(options.maxInboundRequests) ||
        DEFAULT_INBOUND_REQUESTS)
    );
    var maxInboundAssemblies = Math.max(
      1,
      Math.min(16, Number(options.maxInboundAssemblies) ||
        DEFAULT_INBOUND_ASSEMBLIES)
    );
    var highWater = Math.max(
      FRAME_PAYLOAD_BYTES,
      Math.min(
        4 * MAX_MESSAGE_BYTES,
        Number(options.bufferedAmountHighWater) ||
          DEFAULT_BUFFER_HIGH_WATER
      )
    );
    var lowWater = Math.max(
      0,
      Math.min(
        highWater,
        Number(options.bufferedAmountLowWater) ||
          DEFAULT_BUFFER_LOW_WATER
      )
    );
    var setTimer = options.setTimeout || (
      typeof setTimeout === 'function' ? setTimeout : null
    );
    var clearTimer = options.clearTimeout || (
      typeof clearTimeout === 'function' ? clearTimeout : null
    );
    if (!setTimer || !clearTimer) {
      throw new DirectSyncError(
        '当前环境缺少计时器',
        'BW_DIRECT_UNAVAILABLE',
        false
      );
    }
    if (!channel || typeof channel.send !== 'function') {
      throw new DirectSyncError(
        'RTCDataChannel 无效',
        'BW_DIRECT_OFFLINE',
        true
      );
    }
    try {
      if ('bufferedAmountLowThreshold' in channel) {
        channel.bufferedAmountLowThreshold = lowWater;
      }
    } catch (_) {}

    var pending = Object.create(null);
    var inbound = Object.create(null);
    var inboundCount = 0;
    var seenInbound = Object.create(null);
    var seenOrder = [];
    var assemblies = Object.create(null);
    var assemblyCount = 0;
    var requestSequence = 0;
    var messageSequence = 0;
    var closed = false;
    var lastError = null;
    var outboundQueue = Promise.resolve();

    function identifier(prefix) {
      messageSequence += 1;
      return prefix + '-' + Date.now().toString(36) + '-' +
        messageSequence.toString(36);
    }
    function waitForWritable() {
      if (closed || !channelReady(channel)) {
        return Promise.reject(new DirectSyncError(
          'RTCDataChannel 未连接',
          'BW_DIRECT_OFFLINE',
          true
        ));
      }
      var amount = Math.max(0, Number(channel.bufferedAmount) || 0);
      if (amount <= highWater) return Promise.resolve();
      return new Promise(function (resolve, reject) {
        var started = Date.now();
        var timer = null;
        var listening = false;
        function cleanup() {
          if (timer != null) clearTimer(timer);
          timer = null;
          if (listening && typeof channel.removeEventListener === 'function') {
            try {
              channel.removeEventListener('bufferedamountlow', check);
            } catch (_) {}
          }
          listening = false;
        }
        function check() {
          if (closed || !channelReady(channel)) {
            cleanup();
            reject(new DirectSyncError(
              'RTCDataChannel 未连接',
              'BW_DIRECT_OFFLINE',
              true
            ));
            return;
          }
          if ((Math.max(0, Number(channel.bufferedAmount) || 0)) <= lowWater) {
            cleanup();
            resolve();
            return;
          }
          if (Date.now() - started >= timeoutMs) {
            cleanup();
            reject(new DirectSyncError(
              'RTCDataChannel 背压等待超时',
              'BW_DIRECT_BACKPRESSURE',
              true
            ));
            return;
          }
          if (timer == null) timer = setTimer(function () {
            timer = null;
            check();
          }, 25);
        }
        if (typeof channel.addEventListener === 'function') {
          channel.addEventListener('bufferedamountlow', check);
          listening = true;
        }
        check();
      });
    }
    function sendMessage(value) {
      var messageId = identifier('direct-message');
      var frames;
      try { frames = makeFrames(value, identity, messageId); }
      catch (error) { return Promise.reject(error); }
      return frames.reduce(function (chain, raw) {
        return chain.then(function () {
          return waitForWritable().then(function () {
            if (closed || !channelReady(channel)) {
              throw new DirectSyncError(
                'RTCDataChannel 未连接',
                'BW_DIRECT_OFFLINE',
                true
              );
            }
            channel.send(raw);
          });
        });
      }, Promise.resolve());
    }
    function enqueueSend(value) {
      var run = outboundQueue.then(function () {
        return sendMessage(value);
      });
      outboundQueue = run.catch(function () {});
      return run;
    }
    function errorPayload(request, error) {
      var code = String(error && error.code || 'BW_DIRECT_REMOTE')
        .slice(0, 96);
      if (!/^[A-Z0-9_:-]+$/.test(code)) code = 'BW_DIRECT_REMOTE';
      return {
        contract: CONTRACT,
        type: 'RESPONSE',
        requestId: request.requestId,
        sessionId: identity.sessionId,
        accountProof: identity.accountProof,
        registryDigest: identity.registryDigest,
        ok: false,
        error: {
          code: code,
          message: String(error && error.message || error || '远端直连失败')
            .slice(0, 512),
          retryable: !!(error && error.retryable !== false)
        }
      };
    }
    function responseFor(request, result, error) {
      if (!error) {
        try { result = validateGatewayResult(result); }
        catch (validationError) { error = validationError; }
      }
      var payload = error ? errorPayload(request, error) : {
        contract: CONTRACT,
        type: 'RESPONSE',
        requestId: request.requestId,
        sessionId: identity.sessionId,
        accountProof: identity.accountProof,
        registryDigest: identity.registryDigest,
        ok: true,
        result: result
      };
      return enqueueSend(payload).catch(function (sendError) {
        // A peer must receive a small explicit failure instead of timing out
        // when a locally produced response exceeds the 1 MiB cap.
        if (!error && sendError && sendError.code === 'BW_DIRECT_TOO_LARGE') {
          return enqueueSend(errorPayload(request, sendError));
        }
        return Promise.reject(sendError);
      }).catch(function () {});
    }
    function rememberInbound(requestId) {
      seenInbound[requestId] = true;
      seenOrder.push(requestId);
      while (seenOrder.length > 128) {
        delete seenInbound[seenOrder.shift()];
      }
    }
    function handleProtocolMessage(message) {
      if (message.type === 'RESPONSE') {
        var waiter = pending[message.requestId];
        if (!waiter) return;
        if (waiter.timer != null) clearTimer(waiter.timer);
        delete pending[message.requestId];
        if (message.ok) waiter.resolve(clone(message.result));
        else {
          waiter.reject(new DirectSyncError(
            message.error.message,
            message.error.code,
            message.error.retryable
          ));
        }
        return;
      }
      if (!relay || typeof relay.exchange !== 'function') return;
      if (seenInbound[message.requestId]) {
        responseFor(message, null, new DirectSyncError(
          '重复的直连 requestId',
          'BW_DIRECT_DUPLICATE',
          false
        ));
        return;
      }
      rememberInbound(message.requestId);
      if (inboundCount >= maxInboundRequests) {
        responseFor(message, null, new DirectSyncError(
          '远端直连请求过多',
          'BW_DIRECT_BUSY',
          true
        ));
        return;
      }
      inbound[message.requestId] = true;
      inboundCount += 1;
      Promise.resolve().then(function () {
        return relay.exchange(clone(message.payload));
      }).then(
        function (result) { return responseFor(message, result, null); },
        function (error) { return responseFor(message, null, error); }
      ).then(function () {
        if (inbound[message.requestId]) {
          delete inbound[message.requestId];
          inboundCount -= 1;
        }
      }, function () {
        if (inbound[message.requestId]) {
          delete inbound[message.requestId];
          inboundCount -= 1;
        }
      });
    }
    function dropAssembly(messageId) {
      var assembly = assemblies[messageId];
      if (!assembly) return;
      if (assembly.timer != null) clearTimer(assembly.timer);
      delete assemblies[messageId];
      assemblyCount -= 1;
    }
    function consumeFrame(frame) {
      var assembly = assemblies[frame.messageId];
      if (!assembly) {
        if (assemblyCount >= maxInboundAssemblies) {
          throw new DirectSyncError(
            '直连重组队列已满',
            'BW_DIRECT_BUSY',
            true
          );
        }
        assembly = {
          frameCount: frame.frameCount,
          totalBytes: frame.totalBytes,
          chunks: new Array(frame.frameCount),
          received: 0,
          receivedBytes: 0,
          timer: null
        };
        assemblies[frame.messageId] = assembly;
        assemblyCount += 1;
        assembly.timer = setTimer(function () {
          dropAssembly(frame.messageId);
        }, reassemblyTimeoutMs);
      }
      if (
        assembly.frameCount !== frame.frameCount ||
        assembly.totalBytes !== frame.totalBytes
      ) {
        throw new DirectSyncError(
          '同一消息的分帧元数据不一致',
          'BW_DIRECT_INVALID',
          false
        );
      }
      if (assembly.chunks[frame.frameIndex]) {
        var previous = assembly.chunks[frame.frameIndex];
        if (
          previous.byteLength !== frame.bytes.byteLength ||
          previous.some(function (byte, index) {
            return byte !== frame.bytes[index];
          })
        ) {
          throw new DirectSyncError(
            '重复分帧内容不一致',
            'BW_DIRECT_INVALID',
            false
          );
        }
        return null;
      }
      assembly.chunks[frame.frameIndex] = frame.bytes;
      assembly.received += 1;
      assembly.receivedBytes += frame.bytes.byteLength;
      if (assembly.receivedBytes > assembly.totalBytes) {
        throw new DirectSyncError(
          '直连重组超过声明长度',
          'BW_DIRECT_TOO_LARGE',
          false
        );
      }
      if (assembly.received !== assembly.frameCount) return null;
      if (assembly.receivedBytes !== assembly.totalBytes) {
        throw new DirectSyncError(
          '直连重组长度不完整',
          'BW_DIRECT_INVALID',
          false
        );
      }
      var bytes = new Uint8Array(assembly.totalBytes);
      var offset = 0;
      assembly.chunks.forEach(function (chunk) {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
      });
      dropAssembly(frame.messageId);
      var value;
      try { value = JSON.parse(utf8Decode(bytes)); }
      catch (error) {
        if (error && error.name === 'DirectSyncError') throw error;
        throw new DirectSyncError(
          '直连消息不是 JSON',
          'BW_DIRECT_INVALID',
          false
        );
      }
      return validateProtocolMessage(value, identity);
    }
    function closeInternal(reason, code, shouldCloseChannel) {
      if (closed) return;
      closed = true;
      lastError = {
        code: String(code || 'BW_DIRECT_OFFLINE'),
        reason: String(reason || '直连关闭')
      };
      unlisten();
      Object.keys(assemblies).forEach(dropAssembly);
      Object.keys(pending).forEach(function (requestId) {
        if (pending[requestId].timer != null) {
          clearTimer(pending[requestId].timer);
        }
        pending[requestId].reject(new DirectSyncError(
          lastError.reason,
          lastError.code,
          lastError.code === 'BW_DIRECT_OFFLINE'
        ));
        delete pending[requestId];
      });
      if (shouldCloseChannel && typeof channel.close === 'function') {
        try { channel.close(); } catch (_) {}
      }
    }
    function onMessage(event) {
      try {
        var frame = validateFrame(event && event.data, identity);
        var message = consumeFrame(frame);
        if (message) handleProtocolMessage(message);
      } catch (error) {
        // This is a dedicated DataChannel.  A malformed or differently fenced
        // frame can only be a stale/wrong peer, so fail closed instead of
        // allowing it to consume reassembly memory indefinitely.
        closeInternal(
          String(error && error.message || error || '协议错误'),
          String(error && error.code || 'BW_DIRECT_INVALID'),
          true
        );
      }
    }
    var unlisten = listen(channel, onMessage);

    function exchange(payload) {
      if (closed) {
        return Promise.reject(new DirectSyncError(
          '直连已关闭',
          'BW_DIRECT_OFFLINE',
          true
        ));
      }
      try { payload = validateGatewayRequest(payload); }
      catch (error) { return Promise.reject(error); }
      if (Object.keys(pending).length >= maxPendingRequests) {
        return Promise.reject(new DirectSyncError(
          '等待中的直连请求过多',
          'BW_DIRECT_BUSY',
          true
        ));
      }
      requestSequence += 1;
      var requestId = 'direct-' + Date.now().toString(36) + '-' +
        requestSequence.toString(36);
      return new Promise(function (resolve, reject) {
        pending[requestId] = {
          resolve: resolve,
          reject: reject,
          timer: null
        };
        enqueueSend({
          contract: CONTRACT,
          type: 'REQUEST',
          requestId: requestId,
          sessionId: identity.sessionId,
          accountProof: identity.accountProof,
          registryDigest: identity.registryDigest,
          payload: clone(payload)
        }).then(function () {
          var waiter = pending[requestId];
          if (!waiter) return;
          waiter.timer = setTimer(function () {
            delete pending[requestId];
            reject(new DirectSyncError(
              '直连响应超时',
              'BW_DIRECT_TIMEOUT',
              true
            ));
          }, timeoutMs);
        }, function (error) {
          var waiter = pending[requestId];
          if (!waiter) return;
          delete pending[requestId];
          reject(error);
        });
      });
    }
    function close(reason) {
      closeInternal(
        String(reason || '直连关闭'),
        'BW_DIRECT_OFFLINE',
        false
      );
    }
    return {
      contract: CONTRACT,
      exchange: exchange,
      close: close,
      status: function () {
        return Promise.resolve({
          contract: CONTRACT,
          state: channelReady(channel) && !closed ? 'ready' : 'offline',
          sessionId: identity.sessionId,
          registryDigest: identity.registryDigest,
          pendingRequests: Object.keys(pending).length,
          inboundRequests: inboundCount,
          inboundAssemblies: assemblyCount,
          bufferedAmount: Math.max(0, Number(channel.bufferedAmount) || 0),
          error: clone(lastError)
        });
      }
    };
  }

  return {
    CONTRACT: CONTRACT,
    GATEWAY_CONTRACT: GATEWAY_CONTRACT,
    MAX_MESSAGE_BYTES: MAX_MESSAGE_BYTES,
    FRAME_PAYLOAD_BYTES: FRAME_PAYLOAD_BYTES,
    MAX_FRAME_WIRE_BYTES: MAX_FRAME_WIRE_BYTES,
    MAX_FRAME_COUNT: MAX_FRAME_COUNT,
    DirectSyncError: DirectSyncError,
    createStoreRelay: createStoreRelay,
    createChannelTransport: createChannelTransport
  };
});
