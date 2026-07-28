// WebRTC must live in a page-capable content-script context. The background
// remains the only owner of token, namespace, Vault and SyncCoordinator.
(() => {
  "use strict";
  if (window.top !== window) return;
  const PROTOCOL = "bw-reader-direct-host/1";
  const runtime = globalThis.BWReaderRuntime || {};
  const Signals = runtime.directSyncSignalTransport;
  const Host = runtime.directSyncHost;
  const Direct = runtime.directSyncProtocol;
  const Gateway = runtime.syncGateway;
  if (
    typeof RTCPeerConnection !== "function" ||
    !Signals?.createDirectSignalTransport ||
    !Host?.createDirectSyncHost ||
    !Direct?.createChannelTransport ||
    !Gateway?.createSyncGateway
  ) return;

  const pending = new Map();
  const peerTransports = new Map();
  const RECONNECT_BASE_MS = 500;
  const RECONNECT_MAX_MS = 30_000;
  let port = null;
  let reconnectTimer = 0;
  let reconnectAttempt = 0;
  let sequence = 0;
  let host = null;
  let generation = 0;
  let closed = false;

  function bridgeError(message, code, retryable = false) {
    return Object.assign(new Error(String(message || "设备直连桥接失败")), {
      code: String(code || "BW_DIRECT_BRIDGE"),
      retryable: !!retryable,
    });
  }
  function rejectPending(message, code, retryable = true) {
    for (const waiter of pending.values()) {
      clearTimeout(waiter.timer);
      waiter.reject(bridgeError(message, code, retryable));
    }
    pending.clear();
  }
  function stopHost(reason) {
    generation += 1;
    const previousHost = host;
    host = null;
    peerTransports.clear();
    rejectPending(
      "扩展后台直连宿主已失效",
      "BW_DIRECT_HOST_INACTIVE",
      true,
    );
    if (previousHost) {
      try {
        previousHost.destroy(String(reason || "content-host-stopped"));
      } catch (_) {}
    }
  }
  function clearReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = 0;
  }
  function scheduleReconnect() {
    if (closed || port || reconnectTimer) return;
    reconnectAttempt += 1;
    const delay = Math.min(
      RECONNECT_MAX_MS,
      RECONNECT_BASE_MS * Math.pow(2, Math.max(0, reconnectAttempt - 1)),
    );
    reconnectTimer = setTimeout(() => {
      reconnectTimer = 0;
      connect();
    }, delay);
  }
  function losePort(currentPort, reason, disconnect = false) {
    if (closed || port !== currentPort) return;
    port = null;
    stopHost(reason || "background-disconnected");
    if (disconnect) {
      try { currentPort.disconnect(); } catch (_) {}
    }
    scheduleReconnect();
  }
  function post(message, expectedPort = port) {
    if (closed || !expectedPort || port !== expectedPort) {
      throw bridgeError(
        "扩展后台已断开",
        "BW_DIRECT_HOST_INACTIVE",
        true,
      );
    }
    try {
      expectedPort.postMessage(message);
    } catch (_) {
      losePort(expectedPort, "background-post-failed", true);
      throw bridgeError(
        "无法向扩展后台发送直连消息",
        "BW_DIRECT_HOST_INACTIVE",
        true,
      );
    }
  }
  function call(operation, payload, timeoutMs = 30_000) {
    const expectedPort = port;
    if (closed || !expectedPort) return Promise.reject(bridgeError(
      "扩展后台已断开",
      "BW_DIRECT_HOST_INACTIVE",
      true,
    ));
    const id = `direct-content-${Date.now().toString(36)}-${(++sequence).toString(36)}`;
    const expectedGeneration = generation;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(bridgeError(
          "扩展后台直连调用超时",
          "BW_DIRECT_TIMEOUT",
          true,
        ));
      }, timeoutMs);
      pending.set(id, {
        timer,
        generation: expectedGeneration,
        port: expectedPort,
        resolve,
        reject,
      });
      try {
        post({
          protocol: PROTOCOL,
          type: "CALL",
          id,
          operation,
          payload: payload || {},
        }, expectedPort);
      } catch (error) {
        clearTimeout(timer);
        pending.delete(id);
        reject(error);
      }
    });
  }
  function answerDirectCall(currentPort, message) {
    const id = String(message.id || "");
    const payload = message.payload || {};
    const peerId = String(payload.peerId || "");
    const sessionId = String(payload.sessionId || "");
    const binding = peerTransports.get(peerId);
    const expectedGeneration = generation;
    const answer = (result) => {
      if (
        closed ||
        port !== currentPort ||
        expectedGeneration !== generation
      ) return;
      try {
        post({
          protocol: PROTOCOL,
          type: "DIRECT_RESULT",
          id,
          payload: result,
        }, currentPort);
      } catch (_) {}
    };
    if (!id || !binding || binding.sessionId !== sessionId) {
      answer({
        ok: false,
        code: "BW_DIRECT_PEER_INACTIVE",
        error: "RTC peer 会话已失效",
        retryable: true,
      });
      return;
    }
    Promise.resolve(binding.transport.exchange(payload.request)).then(
      (result) => {
        answer({ ok: true, result });
      },
      (error) => {
        answer({
          ok: false,
          code: String(error?.code || "BW_DIRECT_REMOTE"),
          error: String(error?.message || error),
          retryable: error?.retryable !== false,
        });
      },
    );
  }
  function startHost(currentPort, configuration) {
    if (closed || port !== currentPort) return;
    stopHost("configuration-replaced");
    const expectedGeneration = generation;
    const deviceId = String(configuration?.deviceId || "");
    const registryDigest = String(configuration?.registryDigest || "");
    const callForHost = (operation, payload) => {
      if (
        closed ||
        port !== currentPort ||
        expectedGeneration !== generation
      ) {
        return Promise.reject(bridgeError(
          "RTC 内容宿主代际已变化",
          "BW_DIRECT_HOST_INACTIVE",
          true,
        ));
      }
      return call(operation, payload);
    };
    const signalTransport = Signals.createDirectSignalTransport({
      deviceId,
      registryDigest,
      exchange(request) {
        return callForHost("SIGNAL_EXCHANGE", { request });
      },
    });
    const relay = {
      exchange(request) {
        return callForHost("STORE_EXCHANGE", { request });
      },
    };
    host = Host.createDirectSyncHost({
      deviceId,
      registryDigest,
      signalTransport,
      relay,
      registerPeer(metadata, transport) {
        if (expectedGeneration !== generation) {
          throw bridgeError(
            "RTC 内容宿主代际已变化",
            "BW_DIRECT_HOST_INACTIVE",
          );
        }
        peerTransports.set(metadata.peerId, {
          sessionId: metadata.sessionId,
          transport,
        });
        return callForHost("PEER_READY", metadata).catch((error) => {
          peerTransports.delete(metadata.peerId);
          throw error;
        });
      },
      removePeer(peerId, reason) {
        peerTransports.delete(String(peerId || ""));
        if (expectedGeneration !== generation) return Promise.resolve();
        return callForHost("PEER_CLOSED", {
          peerId,
          reason: String(reason || ""),
        }).catch(() => {});
      },
      getServerBaseline() {
        return callForHost("BASELINE_STATUS", {});
      },
      scheduleServer(reason) {
        if (expectedGeneration !== generation) return Promise.resolve();
        return callForHost("SERVER_SCHEDULE", {
          reason: String(reason || ""),
        }).catch(() => {});
      },
      directProtocolApi: Direct,
      syncGatewayApi: Gateway,
      RTCPeerConnection,
      crypto: globalThis.crypto,
      iceServers: Array.isArray(configuration?.iceServers)
        ? configuration.iceServers
        : [],
      assertLease() {
        return !closed &&
          port === currentPort &&
          expectedGeneration === generation;
      },
    });
    host.start("extension-background-ready");
  }

  function handlePortMessage(currentPort, message) {
    if (closed || port !== currentPort) return;
    if (!message || message.protocol !== PROTOCOL) return;
    reconnectAttempt = 0;
    if (message.type === "RESULT" && message.id) {
      const waiter = pending.get(String(message.id));
      if (!waiter) return;
      clearTimeout(waiter.timer);
      pending.delete(String(message.id));
      if (
        waiter.port !== currentPort ||
        waiter.generation !== generation
      ) {
        waiter.reject(bridgeError(
          "直连调用结果已过期",
          "BW_DIRECT_HOST_INACTIVE",
        ));
      } else if (message.payload?.ok === true) {
        waiter.resolve(message.payload.result);
      } else {
        waiter.reject(bridgeError(
          message.payload?.error,
          message.payload?.code,
          message.payload?.retryable !== false,
        ));
      }
      return;
    }
    if (message.type === "DIRECT_CALL") {
      answerDirectCall(currentPort, message);
      return;
    }
    if (message.type === "READY") {
      try {
        startHost(currentPort, message.payload || {});
      } catch (_) {
        losePort(currentPort, "background-ready-invalid", true);
      }
      return;
    }
    if (message.type === "REVOKE") {
      stopHost(message.payload?.reason || "background-revoked");
      return;
    }
    if (message.type === "STANDBY") {
      if (host) stopHost(message.payload?.reason || "standby");
      return;
    }
    if (message.type === "ERROR") {
      stopHost(message.payload?.code || "background-error");
    }
  }
  function connect() {
    if (closed || port || reconnectTimer) return;
    clearReconnect();
    let currentPort;
    try {
      currentPort = chrome.runtime.connect({ name: "bw-reader-direct-host" });
      port = currentPort;
    } catch (_) {
      port = null;
      scheduleReconnect();
      return;
    }
    currentPort.onMessage.addListener((message) => {
      handlePortMessage(currentPort, message);
    });
    currentPort.onDisconnect.addListener(() => {
      if (closed || port !== currentPort) return;
      void chrome.runtime.lastError;
      losePort(currentPort, "background-disconnected");
    });
  }
  function dispose() {
    if (closed) return;
    closed = true;
    clearReconnect();
    const currentPort = port;
    port = null;
    stopHost("page-unloaded");
    if (currentPort) {
      try { currentPort.disconnect(); } catch (_) {}
    }
  }

  connect();
  window.addEventListener("pagehide", dispose, { once: true });
})();
