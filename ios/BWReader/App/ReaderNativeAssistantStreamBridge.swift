import Foundation
import UIKit
import WebKit

/// Transitional command adapter. URLSession, framing and reconnect cursors
/// are native-owned. The existing event reducer still receives structured
/// events until conversation actions/history are fully migrated.
@MainActor
final class ReaderNativeAssistantStreamBridge: NSObject, WKScriptMessageHandlerWithReply {
    static let messageName = "bwNativeAssistantStream"
    private weak var webView: WKWebView?
    private let trustedBaseURL: URL
    private let gateway: ReaderNativeServerGateway
    private var epoch = UUID()
    private var tasks: [String: Task<Void, Never>] = [:]
    private var sequences: [String: Int] = [:]
    private var turns: [String: ReaderNativeAssistantTurn] = [:]
    private var activeWaiters: [UUID: CheckedContinuation<Void, Error>] = [:]
    private var observer: NSObjectProtocol?
    private var history: ReaderNativeAssistantHistory?
    private var historyContext: UInt64?

    init(webView: WKWebView, trustedBaseURL: URL, gateway: ReaderNativeServerGateway) {
        self.webView = webView; self.trustedBaseURL = trustedBaseURL; self.gateway = gateway
        super.init()
        observer = NotificationCenter.default.addObserver(forName: UIApplication.didBecomeActiveNotification,
                                                          object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.resumeActiveWaiters() }
        }
    }

    deinit {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        tasks.values.forEach { $0.cancel() }
        activeWaiters.values.forEach { $0.resume(throwing: CancellationError()) }
    }

    func invalidate() {
        let previousHistory = history
        history = nil; historyContext = nil
        Task { await previousHistory?.invalidate() }
        epoch = UUID(); tasks.values.forEach { $0.cancel() }; tasks.removeAll(); sequences.removeAll(); turns.removeAll()
        let pending = activeWaiters; activeWaiters.removeAll()
        pending.values.forEach { $0.resume(throwing: CancellationError()) }
    }

    private func resumeActiveWaiters() {
        guard UIApplication.shared.applicationState == .active else { return }
        let pending = activeWaiters; activeWaiters.removeAll()
        pending.values.forEach { $0.resume() }
    }

    private func whenActive() async throws {
        try Task.checkCancellation()
        guard UIApplication.shared.applicationState != .active else { return }
        let id = UUID()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (waiter: CheckedContinuation<Void, Error>) in
                if Task.isCancelled { waiter.resume(throwing: CancellationError()) }
                else if UIApplication.shared.applicationState == .active { waiter.resume() }
                else { activeWaiters[id] = waiter }
            }
        } onCancel: { Task { @MainActor [weak self] in
            self?.activeWaiters.removeValue(forKey: id)?.resume(throwing: CancellationError())
        } }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard message.name == Self.messageName, message.frameInfo.isMainFrame,
              let webView, message.webView === webView,
              let requestedSurface = surface(webView.url), surface(message.frameInfo.request.url) == requestedSurface,
              documentURL(webView.url) == documentURL(message.frameInfo.request.url),
              let command = message.body as? [String: Any], command["version"] as? Int == 1,
              let id = command["id"] as? String, UUID(uuidString: id) != nil else {
            replyHandler(nil, "原生对话请求来源无效"); return
        }
        if command["action"] as? String == "cancel" {
            tasks[id]?.cancel(); replyHandler(["ok": true], nil); return
        }
        if command["action"] as? String == "history" {
            performHistory(command, surface: requestedSurface, replyHandler: replyHandler)
            return
        }
        if command["action"] as? String == "prepare" {
            do {
                guard tasks.isEmpty, let input = command["body"] as? [String: Any] else {
                    throw ReaderNativeAssistantRequest.Failure(message: "上一条对话仍在处理中")
                }
                replyHandler(["ok": true, "body": try ReaderNativeAssistantRequest(input).body], nil)
            } catch { replyHandler(nil, error.localizedDescription) }
            return
        }
        guard command["action"] as? String == "start", tasks.isEmpty,
              let path = command["path"] as? String,
              path == "/api/assistant/chat" || (requestedSurface == .epub && path == "/pdf/api/epub-assistant"),
              let body = command["body"] as? [String: Any], JSONSerialization.isValidJSONObject(body),
              let data = try? JSONSerialization.data(withJSONObject: body) else {
            replyHandler(nil, "对话已在进行或请求参数无效"); return
        }
        let lease = epoch
        let gatewayContext = gateway.contextRevision
        turns[id] = ReaderNativeAssistantTurn()
        tasks[id] = Task { @MainActor [weak self] in
            guard let self else { replyHandler(nil, "对话已关闭"); return }
            defer { if self.epoch == lease { self.tasks.removeValue(forKey: id); self.sequences.removeValue(forKey: id); self.turns.removeValue(forKey: id) } }
            do {
                let stream = try ReaderNativeAssistantStream(initial: data, connect: { [weak self] body, response, chunk in
                    guard let self else { throw CancellationError() }
                    try await self.connect(body, path: path, surface: requestedSurface, lease: lease, gatewayContext: gatewayContext,
                                           onResponse: response, onChunk: chunk)
                }, deliver: { [weak self] events in
                    guard let self else { throw CancellationError() }
                    // Sequence advancement and consumer delivery share the UI
                    // actor; a rejected/unknown delivery never resumes effects.
                    try await self.deliver(events, id: id, lease: lease)
                })
                let result = try await stream.run()
                try Task.checkCancellation()
                guard self.epoch == lease else { throw CancellationError() }
                replyHandler(["ok": true, "status": result.rawValue], nil)
            } catch {
                if Task.isCancelled || self.epoch != lease { replyHandler(["ok": true, "status": "aborted"], nil) }
                else { replyHandler(nil, error.localizedDescription) }
            }
        }
    }

    private func performHistory(_ command: [String: Any], surface: ReaderNativeInterfaceSurface,
                                replyHandler: @escaping (Any?, String?) -> Void) {
        do {
            guard let path = command["path"] as? String, let operation = command["operation"] as? String,
                  let mode = command["mode"] as? String else {
                throw ReaderNativeAssistantHistory.Failure(message: "历史请求参数缺失")
            }
            let route = try ReaderNativeAssistantHistory.route(path, operation: operation, mode: mode)
            let lease = epoch, context = gateway.contextRevision
            if history == nil || historyContext != context {
                let previous = history
                Task { await previous?.invalidate() }
                historyContext = context
                history = ReaderNativeAssistantHistory { [weak self] path, method, body in
                    guard let self else { throw CancellationError() }
                    return try await self.fetchHistory(path: path, method: method, body: body, surface: surface, lease: lease, context: context)
                }
            }
            let history = history!
            Task { @MainActor [weak self] in
                do {
                    let response = try await (operation == "read" ? history.read(route) : history.clear(route))
                    guard let self, self.epoch == lease, self.gateway.contextRevision == context else { throw CancellationError() }
                    var reply: [String: Any] = ["ok": (200..<300).contains(response.status), "status": response.status,
                                                "body": String(decoding: response.body, as: UTF8.self)]
                    if let presentation = response.presentation { reply["presentation"] = String(decoding: presentation, as: UTF8.self) }
                    replyHandler(reply, nil)
                } catch { replyHandler(nil, error.localizedDescription) }
            }
        } catch { replyHandler(nil, error.localizedDescription) }
    }

    private func fetchHistory(path: String, method: String, body: Data, surface: ReaderNativeInterfaceSurface,
                              lease: UUID, context: UInt64) async throws -> ReaderNativeAssistantHistory.Response {
        guard epoch == lease, gateway.contextRevision == context else { throw CancellationError() }
        let response = try await gateway.fetchData(path: path, method: method, body: body, surface: surface)
        try Task.checkCancellation()
        guard epoch == lease, gateway.contextRevision == context else { throw CancellationError() }
        return .init(status: response.status, body: response.data)
    }

    private func connect(_ body: Data, path: String, surface: ReaderNativeInterfaceSurface, lease: UUID, gatewayContext: UInt64,
                         onResponse: ReaderNativeAssistantStream.Response,
                         onChunk: ReaderNativeAssistantStream.Chunk) async throws {
        try await whenActive()
        guard epoch == lease else { throw CancellationError() }
        try await gateway.streamAssistant(path: path, body: body, surface: surface, expectedContext: gatewayContext,
                                          onResponse: onResponse, onChunk: onChunk)
    }

    private func deliver(_ events: [ReaderNativeAssistantEvent], id: String, lease: UUID) async throws {
        try Task.checkCancellation()
        guard epoch == lease, let webView, surface(webView.url) != nil else { throw CancellationError() }
        let next = (sequences[id] ?? 0) + 1
        guard var turn = turns[id] else { throw CancellationError() }
        let projected: [[String: Any]] = try events.map { event in
            ["name": event.name, "data": event.data, "state": try turn.consume(event)]
        }
        let result = try await webView.callAsyncJavaScript(
            "return window.__bwNativeAssistantStream?.accept(payload);",
            arguments: ["payload": ["id": id, "sequence": next,
                                    "events": projected]],
            in: nil, contentWorld: .page)
        guard epoch == lease, let ack = result as? [String: Any], ack["ok"] as? Bool == true,
              ack["sequence"] as? Int == next else { throw ReaderNativeAssistantStream.Failure("对话接收状态已改变，未重复执行事件") }
        sequences[id] = next
        turns[id] = turn
    }

    private func surface(_ url: URL?) -> ReaderNativeInterfaceSurface? {
        guard let url, url.scheme == trustedBaseURL.scheme, url.host == trustedBaseURL.host,
              url.port == trustedBaseURL.port, url.path.hasPrefix(trustedBaseURL.path) else { return nil }
        if url.path.hasSuffix("/shells/pdf.html") { return .pdf }
        if url.path.hasSuffix("/shells/epub.html") { return .epub }
        return nil
    }

    private func documentURL(_ url: URL?) -> URL? {
        guard let url, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        parts.fragment = nil; return parts.url
    }

    static let script = #"""
    (() => {
      if (window !== window.top || window.__bwNativeAssistantStream) return;
      const handler = window.webkit?.messageHandlers?.bwNativeAssistantStream;
      if (!handler) return;
      const active = new Map();
      window.__bwNativeAssistantHistory = {
        async request(path, operation, mode) {
          const result = await handler.postMessage({version:1, action:'history', id:crypto.randomUUID(), path, operation, mode});
          if (!result || !Number.isInteger(result.status) || typeof result.body !== 'string') throw new Error('原生历史未获确认');
          return {ok:result.ok === true, status:result.status, json:async () => {
            const body = JSON.parse(result.body);
            if (typeof result.presentation === 'string') {
              const plans = JSON.parse(result.presentation);
              if (!Array.isArray(body.messages) || !Array.isArray(plans) || plans.length !== body.messages.length) throw new Error('历史消息投影不完整');
              body.messages.forEach((message, index) => {
                if (message && typeof message === 'object' && !Array.isArray(message)) Object.defineProperty(message, '__bwNativeHistory', {value:plans[index], enumerable:false});
              });
            }
            return body;
          }};
        }
      };
      function abortError() { const e = new Error('对话已停止'); e.name = 'AbortError'; return e; }
      window.__bwNativeAssistantStream = {
        async prepare(body) {
          const result = await handler.postMessage({version: 1, action: 'prepare', id: crypto.randomUUID(), body});
          if (!result?.ok || !result.body?.rid || !result.body?.turn_id || !result.body?.context) throw new Error('对话上下文未准备好');
          return result.body;
        },
        async run(path, body, consume, signal) {
          if (signal?.aborted) throw abortError();
          const id = crypto.randomUUID(), entry = { consume, sequence: 0, cancelled: false };
          const cancel = () => {
            entry.cancelled = true;
            Promise.resolve(handler.postMessage({version: 1, action: 'cancel', id})).catch(() => {});
          };
          active.set(id, entry);
          signal?.addEventListener('abort', cancel, {once: true});
          try {
            const result = await handler.postMessage({version: 1, action: 'start', id, path,
              body: JSON.parse(JSON.stringify(body))});
            if (entry.cancelled || result?.status === 'aborted') throw abortError();
            if (!result?.ok) throw new Error('原生对话流未完成');
            return result.status;
          } finally {
            signal?.removeEventListener('abort', cancel);
            active.delete(id);
          }
        },
        accept(payload) {
          const entry = active.get(payload?.id);
          if (!entry || entry.cancelled || !Number.isSafeInteger(payload.sequence)) return {ok: false};
          if (payload.sequence === entry.sequence) return {ok: true, sequence: entry.sequence};
          if (payload.sequence !== entry.sequence + 1 || !Array.isArray(payload.events)) return {ok: false};
          // Consume once. If a reducer throws part-way through a batch, native
          // stops instead of replaying mutations whose outcome is unknown.
          entry.sequence = payload.sequence;
          for (const event of payload.events) {
            let value; try { value = JSON.parse(event.data); } catch (_) { value = event.data; }
            entry.consume(event.name, value, event.state);
          }
          return {ok: true, sequence: entry.sequence};
        }
      };
    })();
    """#
}
