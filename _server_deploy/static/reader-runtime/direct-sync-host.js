/* direct-sync-host.js — page/content-script owner for WebRTC direct sync.
 *
 * RTC never runs in an MV3 service worker. This host consumes authenticated
 * signalling, creates a DataChannel in a page-capable context, and registers
 * it with the shared SyncRuntime only after both devices have a live server
 * baseline. The durable server lane remains unconditional in SyncCoordinator.
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.directSyncHost = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'direct-sync-host/1';
  var SIGNAL_CONTRACT = 'direct-signal/1';
  var REGISTRY_DIGEST_PREFIX = 'sync-v3:record-parent-state/1|';
  var MAX_SIGNAL_BATCH = 32;
  var MAX_OUTGOING_SIGNALS = 256;
  var MAX_PENDING_ICE = 64;

  function HostError(message, code, retryable, details) {
    this.name = 'DirectSyncHostError';
    this.message = String(message || '设备直连宿主失败');
    this.code = String(code || 'BW_DIRECT_HOST');
    this.retryable = !!retryable;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, HostError);
  }
  HostError.prototype = Object.create(Error.prototype);
  HostError.prototype.constructor = HostError;

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function safe(value, label, pattern) {
    value = String(value || '').trim();
    if (!value || value.length > 512 || pattern && !pattern.test(value)) {
      throw new HostError(label + ' 无效', 'BW_DIRECT_HOST_INVALID', false);
    }
    return value;
  }
  function listen(target, type, listener) {
    if (target && typeof target.addEventListener === 'function') {
      target.addEventListener(type, listener);
      return function () {
        try { target.removeEventListener(type, listener); } catch (_) {}
      };
    }
    var property = 'on' + type;
    var previous = target && target[property];
    var installed = function (event) {
      if (typeof previous === 'function') previous.call(target, event);
      listener(event);
    };
    if (target) target[property] = installed;
    return function () {
      if (target && target[property] === installed) target[property] = previous || null;
    };
  }
  function createSessionId(cryptoApi) {
    if (!cryptoApi || typeof cryptoApi.getRandomValues !== 'function') {
      throw new HostError(
        '缺少安全随机数，不能建立直连会话',
        'BW_DIRECT_HOST_CRYPTO',
        false
      );
    }
    var bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    return 'direct-session-v1-' + Array.prototype.map.call(bytes, function (byte) {
      return byte.toString(16).padStart(2, '0');
    }).join('');
  }

  function createDirectSyncHost(options) {
    options = options || {};
    var deviceId = safe(
      options.deviceId,
      'deviceId',
      /^[A-Za-z0-9._:-]{1,128}$/
    );
    var registryDigest = safe(options.registryDigest, 'registryDigest');
    var registry = options.registry ||
      root && root.BWReaderRuntime && root.BWReaderRuntime.dataRegistry;
    if (
      !registry ||
      registry.CONTRACT !== 'data-registry/1' ||
      registry.SYNC_CONTRACT !== 'sync-v3' ||
      registry.SYNC_CHANGE_CONTRACT !== 'record-parent-state/1' ||
      typeof registry.syncDigest !== 'function' ||
      registryDigest.indexOf(REGISTRY_DIGEST_PREFIX) !== 0 ||
      registryDigest !== String(registry.syncDigest() || '')
    ) {
      throw new HostError(
        'registryDigest 与 DataRegistry sync-v3 因果合同不匹配',
        'BW_DIRECT_HOST_REGISTRY',
        false
      );
    }
    var signalTransport = options.signalTransport;
    var runtime = options.syncRuntime;
    var store = options.store;
    var directApi = options.directProtocolApi;
    var gatewayApi = options.syncGatewayApi;
    var PeerConnection = options.RTCPeerConnection;
    var cryptoApi = options.crypto;
    var setTimer = options.setTimeout || (
      typeof setTimeout === 'function' ? setTimeout : null
    );
    var clearTimer = options.clearTimeout || (
      typeof clearTimeout === 'function' ? clearTimeout : null
    );
    var assertLease = typeof options.assertLease === 'function'
      ? options.assertLease
      : function () { return true; };
    var onStatus = typeof options.onStatus === 'function'
      ? options.onStatus
      : function () {};
    var externalRelay = options.relay || null;
    var externalRegisterPeer = typeof options.registerPeer === 'function'
      ? options.registerPeer
      : null;
    var externalRemovePeer = typeof options.removePeer === 'function'
      ? options.removePeer
      : null;
    var externalServerBaseline = typeof options.getServerBaseline === 'function'
      ? options.getServerBaseline
      : null;
    var externalScheduleServer = typeof options.scheduleServer === 'function'
      ? options.scheduleServer
      : null;
    var pollMs = Math.max(1000, Number(options.pollMs) || 3000);
    var retryMinMs = Math.max(1000, Number(options.retryMinMs) || 3000);
    var retryMaxMs = Math.max(retryMinMs, Number(options.retryMaxMs) || 30000);
    var negotiationTimeoutMs = Math.max(
      1000,
      Number(options.negotiationTimeoutMs) || 15000
    );
    var rtcConfiguration = {
      iceServers: Array.isArray(options.iceServers)
        ? clone(options.iceServers)
        : []
    };
    var storeRelay;
    var started = false;
    var paused = true;
    var destroyed = false;
    var generation = 0;
    var timer = null;
    var polling = null;
    var signalCursor = 0;
    var accountProof = '';
    var signalSequence = 0;
    var outgoing = [];
    var peers = new Map();
    var liveBaselinePeers = new Set();
    var baselinePeerCursors = new Map();
    var retryMs = retryMinMs;
    var lastError = null;
    var lastExchange = null;

    if (
      !signalTransport ||
      signalTransport.contract !== SIGNAL_CONTRACT ||
      typeof signalTransport.exchange !== 'function'
    ) {
      throw new HostError(
        '缺少 direct-signal/1 transport',
        'BW_DIRECT_HOST_DEPENDENCY',
        false
      );
    }
    if (!externalRegisterPeer && (
      !runtime ||
      typeof runtime.addPeer !== 'function' ||
      typeof runtime.removePeer !== 'function' ||
      typeof runtime.status !== 'function' ||
      typeof runtime.schedule !== 'function'
    )) {
      throw new HostError('SyncRuntime 无效', 'BW_DIRECT_HOST_DEPENDENCY', false);
    }
    if (externalRegisterPeer && (
      !externalRemovePeer ||
      !externalServerBaseline ||
      !externalScheduleServer
    )) {
      throw new HostError(
        '扩展直连桥接合同不完整',
        'BW_DIRECT_HOST_DEPENDENCY',
        false
      );
    }
    if (
      !directApi ||
      directApi.CONTRACT !== 'direct-sync/1' ||
      typeof directApi.createStoreRelay !== 'function' ||
      typeof directApi.createChannelTransport !== 'function'
    ) {
      throw new HostError(
        'DirectSyncProtocol 无效',
        'BW_DIRECT_HOST_DEPENDENCY',
        false
      );
    }
    if (
      !gatewayApi ||
      gatewayApi.CONTRACT !== 'sync-gateway/2' ||
      typeof gatewayApi.createSyncGateway !== 'function'
    ) {
      throw new HostError('SyncGateway 无效', 'BW_DIRECT_HOST_DEPENDENCY', false);
    }
    if (!PeerConnection || !setTimer || !clearTimer) {
      throw new HostError(
        '当前上下文不支持 WebRTC 直连',
        'BW_DIRECT_HOST_UNAVAILABLE',
        false
      );
    }
    storeRelay = externalRelay || directApi.createStoreRelay({
      store: store,
      registry: registry
    });
    if (!storeRelay || typeof storeRelay.exchange !== 'function') {
      throw new HostError(
        '直连 store relay 无效',
        'BW_DIRECT_HOST_DEPENDENCY',
        false
      );
    }

    function removeRuntimePeer(peerId, reason) {
      if (externalRemovePeer) {
        try {
          Promise.resolve(externalRemovePeer(peerId, reason)).catch(function () {});
        } catch (_) {}
        return;
      }
      try { runtime.removePeer(peerId); } catch (_) {}
    }
    function scheduleServer(reason) {
      if (externalScheduleServer) {
        return externalScheduleServer(reason);
      }
      return runtime.schedule(reason, 0);
    }
    function registerRuntimePeer(entry) {
      if (externalRegisterPeer) {
        return Promise.resolve(externalRegisterPeer({
          peerId: entry.peerId,
          sessionId: entry.sessionId,
          baselineLocalCursor: entry.baselineLocalCursor,
          baselineRemoteCursor: entry.baselineRemoteCursor
        }, entry.transport));
      }
      entry.gateway = gatewayApi.createSyncGateway({
        transport: entry.transport,
        deviceId: deviceId
      });
      runtime.addPeer(
        entry.peerId,
        entry.gateway,
        {
          baselineReady: true,
          baselineLocalCursor: entry.baselineLocalCursor,
          baselineRemoteCursor: entry.baselineRemoteCursor
        }
      );
      return Promise.resolve();
    }

    function emit(state, detail) {
      try {
        onStatus({
          contract: CONTRACT,
          state: String(state || ''),
          detail: clone(detail || {})
        });
      } catch (_) {}
    }
    function clearScheduled() {
      if (timer != null) clearTimer(timer);
      timer = null;
    }
    function fence(expectedGeneration) {
      if (
        destroyed ||
        paused ||
        expectedGeneration != null && expectedGeneration !== generation
      ) {
        throw new HostError(
          '直连所有权已暂停或变化',
          'BW_DIRECT_HOST_INACTIVE',
          false
        );
      }
      return Promise.resolve(assertLease()).then(function (valid) {
        if (valid === false) {
          throw new HostError(
            '账户 lease 已失效',
            'BW_DIRECT_HOST_LEASE',
            false
          );
        }
        if (
          destroyed ||
          paused ||
          expectedGeneration != null && expectedGeneration !== generation
        ) {
          throw new HostError(
            '直连所有权已暂停或变化',
            'BW_DIRECT_HOST_INACTIVE',
            false
          );
        }
        return true;
      });
    }
    function schedule(delay) {
      if (destroyed || paused) return false;
      clearScheduled();
      var expectedGeneration = generation;
      timer = setTimer(function () {
        timer = null;
        if (
          destroyed ||
          paused ||
          expectedGeneration !== generation
        ) return;
        poll().catch(function () {});
      }, Math.max(0, Number(delay) || 0));
      return true;
    }
    function signalId() {
      signalSequence += 1;
      return deviceId + ':' + Date.now().toString(36) + ':' +
        signalSequence.toString(36);
    }
    function queueSignal(peerId, sessionId, kind, payload) {
      if (outgoing.length >= MAX_OUTGOING_SIGNALS) {
        var overflow = new HostError(
          '直连信令队列超过上限，已关闭直连并保留服务端同步',
          'BW_DIRECT_SIGNAL_QUEUE_FULL',
          false
        );
        lastError = overflow;
        pause('signal-queue-overflow');
        emit('error', {
          code: overflow.code,
          error: overflow.message,
          retryable: false
        });
        return false;
      }
      outgoing.push({
        signalId: signalId(),
        toDeviceId: peerId,
        sessionId: sessionId,
        kind: kind,
        payload: payload == null ? null : clone(payload)
      });
      schedule(0);
    }
    function peerEntry(peerId) {
      return peers.get(String(peerId || '')) || null;
    }
    function closePeer(peerId, reason, notifyRemote) {
      peerId = String(peerId || '');
      var entry = peers.get(peerId);
      if (!entry) return false;
      peers.delete(peerId);
      if (entry.negotiationTimer != null) {
        clearTimer(entry.negotiationTimer);
        entry.negotiationTimer = null;
      }
      outgoing = outgoing.filter(function (signal) {
        return !(
          signal.toDeviceId === peerId &&
          signal.sessionId === entry.sessionId
        );
      });
      removeRuntimePeer(peerId, reason);
      if (notifyRemote && entry.sessionId) {
        queueSignal(peerId, entry.sessionId, 'bye', {
          reason: String(reason || 'closed').slice(0, 120)
        });
      }
      if (entry.transport && typeof entry.transport.close === 'function') {
        try { entry.transport.close(reason || 'peer-closed'); } catch (_) {}
      }
      if (entry.channel && typeof entry.channel.close === 'function') {
        try { entry.channel.close(); } catch (_) {}
      }
      if (entry.pc && typeof entry.pc.close === 'function') {
        try { entry.pc.close(); } catch (_) {}
      }
      emit('peer-closed', { peerId: peerId, reason: String(reason || '') });
      return true;
    }
    function armNegotiationTimeout(entry, phase) {
      if (!entry || entry.connected || peers.get(entry.peerId) !== entry) {
        return false;
      }
      if (entry.negotiationTimer != null) {
        clearTimer(entry.negotiationTimer);
      }
      entry.negotiationPhase = String(phase || 'negotiating');
      var expectedGeneration = generation;
      entry.negotiationTimer = setTimer(function () {
        entry.negotiationTimer = null;
        if (
          destroyed ||
          paused ||
          expectedGeneration !== generation ||
          entry.connected ||
          peers.get(entry.peerId) !== entry
        ) return;
        var error = new HostError(
          'WebRTC offer/answer 协商超时，已回收会话并准备重试',
          'BW_DIRECT_NEGOTIATION_TIMEOUT',
          true,
          {
            peerId: entry.peerId,
            sessionId: entry.sessionId,
            phase: entry.negotiationPhase
          }
        );
        lastError = error;
        closePeer(entry.peerId, 'rtc-negotiation-timeout', true);
        try {
          scheduleServer('direct-negotiation-timeout:' + entry.peerId);
        } catch (_) {}
        emit('negotiation-timeout', {
          peerId: entry.peerId,
          sessionId: entry.sessionId,
          phase: entry.negotiationPhase,
          timeoutMs: negotiationTimeoutMs,
          code: error.code
        });
        /*
         * The lexicographically smaller device recreates an offer on the next
         * poll. Responders send a bye so the remote offerer also tears down
         * its stale session and retries. Durable server sync remains active.
         */
        schedule(0);
      }, negotiationTimeoutMs);
      return true;
    }
    function resetBaseline(reason) {
      outgoing = [];
      Array.from(peers.keys()).forEach(function (peerId) {
        closePeer(peerId, reason || 'server-baseline-reset', false);
      });
      liveBaselinePeers = new Set();
      baselinePeerCursors = new Map();
      try {
        scheduleServer('direct-reset:' + String(reason || 'baseline'));
      } catch (_) {}
    }
    function isRecoverableSignalRace(error) {
      return [
        'BW_DIRECT_BASELINE_REQUIRED',
        'BW_DIRECT_TARGET_UNAVAILABLE',
        'BW_DIRECT_SERVER_CURSOR',
        'BW_DIRECT_SIGNAL_ID_REUSE'
      ].indexOf(String(error && error.code || '')) >= 0;
    }
    function isSignalContractViolation(error) {
      return [
        'BW_DIRECT_SIGNAL_INVALID',
        'BW_DIRECT_SIGNAL_CONTRACT'
      ].indexOf(String(error && error.code || '')) >= 0;
    }
    function drainIce(entry) {
      if (!entry.remoteDescriptionSet || !entry.pendingIce.length) {
        return Promise.resolve();
      }
      var candidates = entry.pendingIce.splice(0);
      return candidates.reduce(function (chain, candidate) {
        return chain.then(function () {
          return entry.pc.addIceCandidate(candidate);
        });
      }, Promise.resolve());
    }
    function attachChannel(entry, channel) {
      if (!entry || entry.channel) return;
      entry.channel = channel;
      function opened() {
        if (
          destroyed ||
          paused ||
          peers.get(entry.peerId) !== entry ||
          !liveBaselinePeers.has(entry.peerId)
        ) {
          closePeer(entry.peerId, 'baseline-lost-before-open', false);
          return;
        }
        if (entry.negotiationTimer != null) {
          clearTimer(entry.negotiationTimer);
          entry.negotiationTimer = null;
        }
        try {
          entry.transport = directApi.createChannelTransport({
            channel: channel,
            sessionId: entry.sessionId,
            accountProof: accountProof,
            registryDigest: registryDigest,
            relay: storeRelay
          });
          Promise.resolve(registerRuntimePeer(entry)).then(function () {
            if (
              destroyed ||
              paused ||
              peers.get(entry.peerId) !== entry ||
              !liveBaselinePeers.has(entry.peerId)
            ) {
              closePeer(entry.peerId, 'peer-registration-stale', false);
              return;
            }
            entry.connected = true;
            scheduleServer('direct-peer-open:' + entry.peerId);
            emit('peer-open', {
              peerId: entry.peerId,
              sessionId: entry.sessionId
            });
          }).catch(function (error) {
            closePeer(entry.peerId, 'peer-registration-failed', true);
            lastError = error;
            emit('error', {
              code: error && error.code || 'BW_DIRECT_HOST_REGISTER',
              error: String(error && error.message || error)
            });
          });
        } catch (error) {
          closePeer(entry.peerId, 'channel-attach-failed', true);
          lastError = error;
          emit('error', {
            code: error.code || 'BW_DIRECT_HOST_CHANNEL',
            error: String(error.message || error)
          });
        }
      }
      listen(channel, 'open', opened);
      listen(channel, 'close', function () {
        closePeer(entry.peerId, 'data-channel-closed', false);
      });
      listen(channel, 'error', function () {
        closePeer(entry.peerId, 'data-channel-error', true);
      });
      if (channel.readyState === 'open') opened();
    }
    function createPeer(peerId, sessionId, initiator) {
      peerId = safe(peerId, 'peerId', /^[A-Za-z0-9._:-]{1,128}$/);
      sessionId = safe(
        sessionId,
        'sessionId',
        /^[A-Za-z0-9._:-]{1,160}$/
      );
      closePeer(peerId, 'peer-session-replaced', false);
      var pc = new PeerConnection(clone(rtcConfiguration));
      var entry = {
        peerId: peerId,
        sessionId: sessionId,
        initiator: !!initiator,
        pc: pc,
        channel: null,
        transport: null,
        gateway: null,
        connected: false,
        baselineLocalCursor: Math.max(
          0,
          Number(lastExchange && lastExchange.localCursor) || 0
        ),
        baselineRemoteCursor: Math.max(
          0,
          Number(baselinePeerCursors.get(peerId)) || 0
        ),
        remoteDescriptionSet: false,
        pendingIce: [],
        negotiationTimer: null,
        negotiationPhase: ''
      };
      peers.set(peerId, entry);
      listen(pc, 'icecandidate', function (event) {
        if (!event || !event.candidate) return;
        var candidate = typeof event.candidate.toJSON === 'function'
          ? event.candidate.toJSON()
          : clone(event.candidate);
        queueSignal(peerId, sessionId, 'ice', candidate);
      });
      listen(pc, 'connectionstatechange', function () {
        if (['failed', 'closed', 'disconnected'].indexOf(pc.connectionState) >= 0) {
          closePeer(peerId, 'rtc-' + pc.connectionState, false);
        }
      });
      listen(pc, 'datachannel', function (event) {
        if (event && event.channel) attachChannel(entry, event.channel);
      });
      if (initiator) {
        attachChannel(entry, pc.createDataChannel('bw-reader-sync-v1', {
          ordered: true
        }));
      }
      return entry;
    }
    function beginOffer(peerId) {
      var entry = createPeer(
        peerId,
        createSessionId(cryptoApi),
        true
      );
      return Promise.resolve(entry.pc.createOffer()).then(function (offer) {
        return entry.pc.setLocalDescription(offer).then(function () {
          queueSignal(peerId, entry.sessionId, 'offer', clone(
            entry.pc.localDescription || offer
          ));
          armNegotiationTimeout(entry, 'await-answer');
        });
      }).catch(function (error) {
        closePeer(peerId, 'offer-failed', false);
        throw error;
      });
    }
    function acceptOffer(signal) {
      var peerId = signal.fromDeviceId;
      if (!liveBaselinePeers.has(peerId)) return Promise.resolve();
      /* The lexicographically smaller device is the only offerer. This
       * removes glare without a second negotiation protocol. */
      if (peerId > deviceId) return Promise.resolve();
      var current = peerEntry(peerId);
      if (current && current.sessionId === signal.sessionId) return Promise.resolve();
      var entry = createPeer(peerId, signal.sessionId, false);
      return Promise.resolve(entry.pc.setRemoteDescription(signal.payload))
        .then(function () {
          entry.remoteDescriptionSet = true;
          return drainIce(entry);
        }).then(function () {
          return entry.pc.createAnswer();
        }).then(function (answer) {
          return entry.pc.setLocalDescription(answer).then(function () {
            queueSignal(peerId, entry.sessionId, 'answer', clone(
              entry.pc.localDescription || answer
            ));
            armNegotiationTimeout(entry, 'await-channel-open');
          });
        }).catch(function (error) {
          closePeer(peerId, 'answer-failed', true);
          throw error;
        });
    }
    function acceptAnswer(signal) {
      var entry = peerEntry(signal.fromDeviceId);
      if (
        !entry ||
        !entry.initiator ||
        entry.sessionId !== signal.sessionId ||
        !liveBaselinePeers.has(signal.fromDeviceId)
      ) return Promise.resolve();
      return Promise.resolve(entry.pc.setRemoteDescription(signal.payload))
        .then(function () {
          entry.remoteDescriptionSet = true;
          return drainIce(entry).then(function () {
            armNegotiationTimeout(entry, 'await-channel-open');
          });
        });
    }
    function acceptIce(signal) {
      var entry = peerEntry(signal.fromDeviceId);
      if (
        !entry ||
        entry.sessionId !== signal.sessionId ||
        !liveBaselinePeers.has(signal.fromDeviceId)
      ) return Promise.resolve();
      if (!entry.remoteDescriptionSet) {
        if (entry.pendingIce.length >= MAX_PENDING_ICE) {
          closePeer(signal.fromDeviceId, 'pending-ice-overflow', true);
          throw new HostError(
            '远端 ICE 候选超过上限',
            'BW_DIRECT_SIGNAL_QUEUE_FULL',
            false
          );
        }
        entry.pendingIce.push(clone(signal.payload));
        return Promise.resolve();
      }
      return Promise.resolve(entry.pc.addIceCandidate(signal.payload));
    }
    function processSignals(signals) {
      return (signals || []).reduce(function (chain, signal) {
        return chain.then(function () {
          if (signal.kind === 'offer') return acceptOffer(signal);
          if (signal.kind === 'answer') return acceptAnswer(signal);
          if (signal.kind === 'ice') return acceptIce(signal);
          if (signal.kind === 'bye') {
            var entry = peerEntry(signal.fromDeviceId);
            if (entry && entry.sessionId === signal.sessionId) {
              closePeer(signal.fromDeviceId, 'remote-bye', false);
            }
          }
          return null;
        });
      }, Promise.resolve());
    }
    function serverBaseline() {
      if (externalServerBaseline) {
        return Promise.resolve(externalServerBaseline()).then(function (value) {
          value = value || {};
          return {
            localCursor: Math.max(0, Number(value.localCursor) || 0),
            cursor: Math.max(0, Number(value.serverCursor) || 0),
            ready: value.ready === true
          };
        });
      }
      return Promise.resolve(runtime.status()).then(function (status) {
        var result = status && status.lastResult;
        var checkpoint = status && status.coordinator &&
          status.coordinator.checkpoint ||
          result && result.checkpoint ||
          null;
        var server = checkpoint && checkpoint.server || {};
        var latest = result && result.server || {};
        return {
          localCursor: Math.max(0, Number(server.localCursor) || 0),
          cursor: Math.max(0, Number(server.remoteCursor) || 0),
          ready: status && status.paused !== true &&
            latest.ok === true &&
            latest.pendingLocal !== true &&
            (!Array.isArray(latest.conflicts) || latest.conflicts.length === 0)
        };
      });
    }
    function reconcilePeers(response) {
      liveBaselinePeers = new Set(
        (response.peers || []).filter(function (peer) {
          return peer.baselineReady === true && peer.deviceId !== deviceId;
        }).map(function (peer) { return peer.deviceId; })
      );
      baselinePeerCursors = new Map(
        (response.peers || []).filter(function (peer) {
          return peer.baselineReady === true && peer.deviceId !== deviceId;
        }).map(function (peer) {
          return [
            peer.deviceId,
            Math.max(0, Number(peer.baselineLocalCursor) || 0)
          ];
        })
      );
      Array.from(peers.keys()).forEach(function (peerId) {
        if (!liveBaselinePeers.has(peerId)) {
          closePeer(peerId, 'server-baseline-lost', false);
        }
      });
      var offers = [];
      liveBaselinePeers.forEach(function (peerId) {
        if (deviceId < peerId && !peers.has(peerId)) offers.push(peerId);
      });
      return offers.sort().reduce(function (chain, peerId) {
        return chain.then(function () { return beginOffer(peerId); });
      }, Promise.resolve());
    }
    function poll() {
      if (destroyed || paused) {
        return Promise.resolve({
          contract: CONTRACT,
          skipped: true,
          code: 'BW_DIRECT_HOST_INACTIVE'
        });
      }
      if (polling) return polling;
      clearScheduled();
      var expectedGeneration = generation;
      var batch = outgoing.slice(0, MAX_SIGNAL_BATCH);
      var baselineSnapshot = null;
      polling = fence(expectedGeneration).then(function () {
        return serverBaseline();
      }).then(function (baseline) {
        baselineSnapshot = baseline;
        var previousHead = Math.max(
          0,
          Number(lastExchange && lastExchange.headCursor) || 0
        );
        if (
          lastExchange &&
          previousHead !== baseline.cursor
        ) {
          /*
           * Offers, answers and ICE candidates are only valid for the server
           * baseline under which they were created.  If durable sync advances
           * first, discard the old mailbox batch and rebuild every RTC
           * session from the new common baseline instead of retrying a stale
           * signalId against a different head.
           */
          batch = [];
          resetBaseline('server-head-changed');
          emit('baseline-changed', {
            previousHeadCursor: previousHead,
            headCursor: baseline.cursor
          });
        }
        if (!baseline.ready) {
          batch = [];
          resetBaseline('local-server-baseline-not-ready');
        }
        return fence(expectedGeneration).then(function () {
          return signalTransport.exchange({
            localCursor: baseline.localCursor,
            serverCursor: baseline.cursor,
            serverReady: baseline.ready,
            signalCursor: signalCursor,
            signals: batch
          });
        });
      }).then(function (response) {
        return fence(expectedGeneration).then(function () {
          if (accountProof && accountProof !== response.accountProof) {
            /*
             * The proof is part of every live direct-sync identity.  Merely
             * stopping future signalling would leave already-open channels
             * authorized by the obsolete generation, so tear them all down
             * before surfacing the non-retryable fence error.
             */
            resetBaseline('account-proof-changed');
            throw new HostError(
              '账户直连证明在同一租约中变化',
              'BW_DIRECT_HOST_PROOF',
              false
            );
          }
          accountProof = response.accountProof;
          lastExchange = clone(response);
          lastExchange.localCursor = Math.max(
            0,
            Number(
              response.baselineLocalCursor == null
                ? baselineSnapshot && baselineSnapshot.localCursor
                : response.baselineLocalCursor
            ) || 0
          );
          var responseSignalCursor = Math.max(
            0,
            Number(response.signalCursor) || 0
          );
          /*
           * A normal mailbox response is monotonic from the client's point of
           * view. A reset is different: the server has explicitly discarded
           * the old mailbox window, so retaining a client-ahead cursor would
           * skip every new signal until the server catches up to that stale
           * number.
           */
          signalCursor = response.signalResetRequired
            ? responseSignalCursor
            : Math.max(signalCursor, responseSignalCursor);
          if (batch.length) {
            var sent = new Set(response.ackedSignalIds || []);
            outgoing = outgoing.filter(function (item) {
              return !sent.has(item.signalId);
            });
          }
          if (response.signalResetRequired) {
            resetBaseline('signal-mailbox-reset');
          }
          if (response.baselineReady !== true) {
            resetBaseline('server-baseline-not-ready');
            return null;
          }
          return reconcilePeers(response).then(function () {
            return processSignals(response.signals || []);
          });
        });
      }).then(function () {
        retryMs = retryMinMs;
        lastError = null;
        var baselineReady = !!(lastExchange && lastExchange.baselineReady);
        emit(baselineReady ? 'ready' : 'waiting-baseline', {
          peerCount: peers.size,
          baselinePeerCount: liveBaselinePeers.size,
          signalCursor: signalCursor
        });
        schedule(
          baselineReady && (
            outgoing.length || lastExchange && lastExchange.hasMore
          )
            ? 0
            : pollMs
        );
        return clone(lastExchange);
      }).catch(function (error) {
        var recoverableRace = isRecoverableSignalRace(error);
        var signalContractViolation = isSignalContractViolation(error);
        if (recoverableRace || signalContractViolation) {
          resetBaseline(
            (recoverableRace ? 'signal-race:' : 'signal-contract:') +
            String(error.code || 'unknown')
          );
        }
        lastError = error;
        emit('error', {
          code: error && error.code || 'BW_DIRECT_HOST',
          error: String(error && error.message || error),
          retryable: recoverableRace || error && error.retryable !== false
        });
        if (
          !destroyed &&
          !paused &&
          expectedGeneration === generation &&
          (recoverableRace || !error || error.retryable !== false)
        ) {
          schedule(recoverableRace ? pollMs : retryMs);
          if (!recoverableRace) {
            retryMs = Math.min(retryMaxMs, retryMs * 2);
          }
        }
        throw error;
      }).finally(function () {
        polling = null;
      });
      return polling;
    }
    function start(reason) {
      if (destroyed) {
        throw new HostError(
          '直连宿主已销毁',
          'BW_DIRECT_HOST_DESTROYED',
          false
        );
      }
      started = true;
      if (!paused) {
        schedule(0);
        return false;
      }
      paused = false;
      generation += 1;
      retryMs = retryMinMs;
      try { scheduleServer('direct-baseline:' + String(reason || 'start')); }
      catch (_) {}
      schedule(0);
      emit('starting', { reason: String(reason || '') });
      return true;
    }
    function pause(reason) {
      if (destroyed) return false;
      var changed = !paused;
      paused = true;
      generation += 1;
      clearScheduled();
      Array.from(peers.keys()).forEach(function (peerId) {
        closePeer(peerId, reason || 'host-paused', false);
      });
      /* Every queued signal belongs to one of the sessions just closed. */
      outgoing = [];
      liveBaselinePeers.clear();
      emit('paused', { reason: String(reason || '') });
      return changed;
    }
    function destroy(reason) {
      if (destroyed) return false;
      pause(reason || 'host-destroyed');
      destroyed = true;
      emit('destroyed', { reason: String(reason || '') });
      return true;
    }

    return {
      contract: CONTRACT,
      start: start,
      resume: start,
      pause: pause,
      destroy: destroy,
      poll: poll,
      status: function () {
        return {
          contract: CONTRACT,
          started: started,
          paused: paused,
          destroyed: destroyed,
          generation: generation,
          scheduled: timer != null,
          polling: !!polling,
          signalCursor: signalCursor,
          pendingSignals: outgoing.length,
          peers: Array.from(peers.keys()).sort(),
          baselinePeers: Array.from(liveBaselinePeers).sort(),
          lastExchange: clone(lastExchange),
          lastError: lastError ? {
            code: String(lastError.code || 'BW_DIRECT_HOST'),
            error: String(lastError.message || lastError),
            retryable: lastError.retryable !== false
          } : null,
          iceServerCount: rtcConfiguration.iceServers.length
        };
      }
    };
  }

  return {
    CONTRACT: CONTRACT,
    HostError: HostError,
    createDirectSyncHost: createDirectSyncHost
  };
});
