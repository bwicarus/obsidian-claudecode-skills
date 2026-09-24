import Foundation
import UIKit
import WebKit

/// Native request, stream, recovery and completion owner. Compatibility
/// producers receive committed events and display projections only.
@MainActor
final class ReaderNativeAssistantStreamBridge: NSObject, WKScriptMessageHandlerWithReply {
    static let messageName = "bwNativeAssistantStream"
    private weak var webView: WKWebView?
    private let trustedBaseURL: URL
    private let gateway: ReaderNativeServerGateway
    private var epoch = UUID()
    private var tasks: [String: Task<Void, Never>] = [:]
    private var sequences: [String: Int] = [:]
    private var bookActionSequences: [String: Int] = [:]
    private var documents: [String: ReaderNativeAssistantDocumentSession] = [:]
    private var turns: [String: ReaderNativeAssistantTurn] = [:]
    private var watchers: [String: Task<Void, Never>] = [:]
    private var watcherKeys: [String: String] = [:]
    private var watchedEffectCounts: [String: Int] = [:]
    private var activeWaiters: [UUID: CheckedContinuation<Void, Error>] = [:]
    private var observer: NSObjectProtocol?
    private var history: ReaderNativeAssistantHistory?
    private var historyContext: UInt64?
    var beforeHistoryClear: ((String) async throws -> UUID)?
    var afterHistoryClear: ((String,UUID,Bool) -> Void)?
    var commitPDFEvents: ((ReaderNativeAssistantDocumentSession, [[String:Any]], Int) throws -> [String:Any])?
    var preparePDFBody: (([String:Any]) async throws -> [String:Any])?
    var prepareReaderPCContext: (([String:Any]) async throws -> [String:Any])?
    private var contextTask: Task<Void,Never>?
    var replyReference: ((String,String,Bool) throws -> [String:Any])?

    init(webView: WKWebView, trustedBaseURL: URL, gateway: ReaderNativeServerGateway) {
        self.webView = webView; self.trustedBaseURL = trustedBaseURL; self.gateway = gateway
        super.init()
        observer = NotificationCenter.default.addObserver(forName: UIApplication.didBecomeActiveNotification,
                                                          object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.resumeActiveWaiters() }
        }
    }

    deinit {
        contextTask?.cancel()
        if let observer { NotificationCenter.default.removeObserver(observer) }
        tasks.values.forEach { $0.cancel() }
        watchers.values.forEach { $0.cancel() }
        activeWaiters.values.forEach { $0.resume(throwing: CancellationError()) }
    }

    func invalidate() {
        contextTask?.cancel(); contextTask = nil
        documents.values.forEach { $0.close() }; documents.removeAll()
        let previousHistory = history
        history = nil; historyContext = nil
        Task { await previousHistory?.invalidate() }
        epoch = UUID(); tasks.values.forEach { $0.cancel() }; tasks.removeAll(); sequences.removeAll(); bookActionSequences.removeAll(); turns.removeAll()
        watchers.values.forEach { $0.cancel() }; watchers.removeAll(); watcherKeys.removeAll(); watchedEffectCounts.removeAll()
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
        if command["action"] as? String == "watchTask" {
            watchTask(command, id: id, surface: requestedSurface, replyHandler: replyHandler); return
        }
        if command["action"] as? String == "history" {
            performHistory(command, surface: requestedSurface, replyHandler: replyHandler)
            return
        }
        if command["action"] as? String == "pageContext" {
            guard requestedSurface == .pdf, let current = command["current"] as? [String:Any], let prepareReaderPCContext else {
                replyHandler(nil,"原生阅读状态尚未就绪"); return
            }
            let lease = epoch, context = gateway.contextRevision
            contextTask?.cancel()
            contextTask = Task { @MainActor [weak self] in
                do {
                    let value = try await prepareReaderPCContext(current)
                    guard let self, !Task.isCancelled, self.epoch == lease, self.gateway.contextRevision == context else { throw CancellationError() }
                    replyHandler(["ok":true,"context":value],nil)
                } catch { replyHandler(nil,error.localizedDescription) }
            }
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
              let body = command["body"] as? [String: Any], JSONSerialization.isValidJSONObject(body) else {
            replyHandler(nil, "对话已在进行或请求参数无效"); return
        }
        let lease = epoch
        let gatewayContext = gateway.contextRevision
        turns[id] = ReaderNativeAssistantTurn()
        tasks[id] = Task { @MainActor [weak self] in
            guard let self else { replyHandler(nil, "对话已关闭"); return }
            defer { if self.epoch == lease { self.tasks.removeValue(forKey: id); self.sequences.removeValue(forKey: id); self.bookActionSequences.removeValue(forKey: id); self.turns.removeValue(forKey: id) } }
            do {
                let prepared = try await self.beginDocumentSession(id: id, body: body, path: path, lease: lease)
                let stream = try ReaderNativeAssistantStream(initial: prepared, connect: { [weak self] body, response, chunk in
                    guard let self else { throw CancellationError() }
                    try await self.connect(body, path: path, surface: requestedSurface, lease: lease, gatewayContext: gatewayContext,
                                           onResponse: response, onChunk: chunk)
                }, deliver: { [weak self] events in
                    guard let self else { throw CancellationError() }
                    // Sequence advancement and consumer delivery share the UI
                    // actor; a rejected/unknown delivery never resumes effects.
                    try await self.deliver(events, id: id, lease: lease, gatewayContext: gatewayContext)
                })
                let result = try await stream.run()
                if result != .done, self.turns[id]?.answer.isEmpty == true {
                    let mode = body["assistant_mode"] as? String ?? "normal"
                    let route = try ReaderNativeAssistantHistory.route("/api/assistant/history" + (mode == "review" ? "?assistant_mode=review" : ""), operation:"read", mode:mode)
                    let service = self.historyService(surface:requestedSurface,lease:lease,context:gatewayContext)
                    if let recovered = try await service.recover(route,rid:body["rid"] as? String ?? "",turnID:body["turn_id"] as? String ?? "") {
                        guard self.epoch == lease, self.gateway.contextRevision == gatewayContext else { throw CancellationError() }
                        try self.turns[id]?.restore(recovered)
                    }
                }
                await self.endDocumentSession(id: id)
                try Task.checkCancellation()
                guard self.epoch == lease else { throw CancellationError() }
                try await self.finish(id:id,lease:lease,context:gatewayContext,aborted:false)
                replyHandler(["ok": true, "status": result.rawValue], nil)
            } catch {
                await self.endDocumentSession(id: id)
                let aborted = Task.isCancelled || self.epoch != lease
                do {
                    try await self.finish(id:id,lease:lease,context:gatewayContext,aborted:aborted,error:aborted ? nil : error.localizedDescription)
                    replyHandler(["ok":true,"status":aborted ? "aborted" : "failed"],nil)
                } catch { replyHandler(nil,error.localizedDescription) }
            }
        }
    }

    private func watchTask(_ command: [String: Any], id: String, surface: ReaderNativeInterfaceSurface,
                           replyHandler: @escaping (Any?, String?) -> Void) {
        do {
            guard let taskID = command["taskID"] as? String,
                  let rawKind = command["kind"] as? String, let kind = ReaderNativeTaskMonitor.Kind(rawValue: rawKind),
                  watchers.count < 32 else { throw ReaderNativeTaskMonitor.Failure(message: "后台任务追踪参数无效或任务过多") }
            let path = try ReaderNativeTaskMonitor.path(taskID: taskID), key = rawKind + ":" + taskID
            guard watcherKeys[key] == nil else { throw ReaderNativeTaskMonitor.Failure(message: "后台任务已在追踪") }
            // Do not evict an effect receipt then silently replay its actions.
            guard watchedEffectCounts[taskID] != nil || watchedEffectCounts.count < 1024 else {
                throw ReaderNativeTaskMonitor.Failure(message: "任务回执已满，请重新打开阅读器")
            }
            watcherKeys[key] = id
            let lease = epoch, context = gateway.contextRevision
            watchers[id] = Task { @MainActor [weak self] in
                guard let self else { replyHandler(nil, "任务追踪已关闭"); return }
                defer { if self.epoch == lease { self.watchers.removeValue(forKey: id); self.watcherKeys.removeValue(forKey: key); self.sequences.removeValue(forKey: id) } }
                do {
                    let monitor = ReaderNativeTaskMonitor(kind: kind, fetch: { [weak self] in
                        guard let self else { throw CancellationError() }
                        return try await self.fetchTask(path, surface: surface, lease: lease, context: context)
                    }, deliver: { [weak self] data in
                        guard let self else { throw CancellationError() }
                        try await self.deliverTask(data, id: id, taskID: taskID, lease: lease, context: context)
                    })
                    let outcome = try await monitor.run()
                    guard self.epoch == lease, self.gateway.contextRevision == context else { throw CancellationError() }
                    replyHandler(["ok": true, "status": outcome.rawValue], nil)
                } catch {
                    if Task.isCancelled || self.epoch != lease || self.gateway.contextRevision != context {
                        replyHandler(["ok": true, "status": "aborted"], nil)
                    } else { replyHandler(nil, error.localizedDescription) }
                }
            }
        } catch { replyHandler(nil, error.localizedDescription) }
    }

    private func fetchTask(_ path: String, surface: ReaderNativeInterfaceSurface, lease: UUID, context: UInt64) async throws -> Data {
        try await whenActive()
        guard epoch == lease, gateway.contextRevision == context else { throw CancellationError() }
        let response: ReaderNativeServerProxyBroker.DataResponse
        do { response = try await gateway.fetchData(path: path, method: "GET", body: Data(), surface: surface) }
        catch {
            try Task.checkCancellation()
            guard epoch == lease, gateway.contextRevision == context else { throw CancellationError() }
            if (error as NSError).domain == NSURLErrorDomain { throw error }
            throw ReaderNativeTaskMonitor.Failure(message: error.localizedDescription)
        }
        try Task.checkCancellation()
        guard epoch == lease, gateway.contextRevision == context else { throw CancellationError() }
        if response.status == 401 || response.status == 403 { throw ReaderNativeTaskMonitor.Failure(message: "后台任务查询未获授权") }
        if response.status >= 500 || response.status == 429 { throw URLError(.badServerResponse) }
        return response.data
    }

    private func deliverTask(_ data: Data, id: String, taskID: String, lease: UUID, context: UInt64) async throws {
        try Task.checkCancellation()
        guard epoch == lease, gateway.contextRevision == context, let webView,
              var snapshot = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw CancellationError() }
        if let actions = snapshot["client_actions"] as? [Any] {
            let count = watchedEffectCounts[taskID, default: 0]
            guard actions.count >= count else { throw ReaderNativeTaskMonitor.Failure(message: "后台任务的操作序列已改变，请核对结果") }
            snapshot["client_actions"] = actions.enumerated().map { $0.offset < count ? NSNull() : $0.element }
            // Reserve before the JS acknowledgement: an unknown delivery may
            // have executed its mutation and must not be replayed on reattach.
            watchedEffectCounts[taskID] = actions.count
        }
        let next = (sequences[id] ?? 0) + 1
        let result = try await webView.callAsyncJavaScript("const receipt = window.__bwNativeAssistantStream?.acceptTask(payload); await window.RC?.turnCard?.settle?.(); return receipt;",
            arguments: ["payload": ["id": id, "sequence": next, "snapshot": snapshot]], in: nil, contentWorld: .page)
        guard epoch == lease, gateway.contextRevision == context, let ack = result as? [String: Any],
              ack["ok"] as? Bool == true, ack["sequence"] as? Int == next else {
            throw ReaderNativeTaskMonitor.Failure(message: "任务更新未获确认，未重复执行操作")
        }
        sequences[id] = next
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
            let history = historyService(surface:surface,lease:lease,context:context)
            Task { @MainActor [weak self] in
                var clearToken: UUID?, cleared = false
                defer { if let clearToken { self?.afterHistoryClear?(mode,clearToken,cleared) } }
                do {
                    guard let self, self.epoch == lease, self.gateway.contextRevision == context else { throw CancellationError() }
                    if operation == "clear" {
                        clearToken = try await self.beforeHistoryClear?(mode)
                    }
                    let response = try await (operation == "read" ? history.read(route) : history.clear(route))
                    guard self.epoch == lease, self.gateway.contextRevision == context else { throw CancellationError() }
                    if operation == "clear", (200..<300).contains(response.status),
                       let result = try? JSONSerialization.jsonObject(with:response.body) as? [String:Any], result["ok"] as? Bool == true { cleared = true }
                    var reply: [String: Any] = ["ok": (200..<300).contains(response.status), "status": response.status,
                                                "body": String(decoding: response.body, as: UTF8.self)]
                    if let presentation = response.presentation { reply["presentation"] = String(decoding: presentation, as: UTF8.self) }
                    replyHandler(reply, nil)
                } catch { replyHandler(nil, error.localizedDescription) }
            }
        } catch { replyHandler(nil, error.localizedDescription) }
    }

    private func historyService(surface:ReaderNativeInterfaceSurface,lease:UUID,context:UInt64) -> ReaderNativeAssistantHistory {
        if history == nil || historyContext != context {
            let previous = history
            Task { await previous?.invalidate() }
            historyContext = context
            history = ReaderNativeAssistantHistory { [weak self] path,method,body in
                guard let self else { throw CancellationError() }
                return try await self.fetchHistory(path:path,method:method,body:body,surface:surface,lease:lease,context:context)
            }
        }
        return history!
    }

    private func finish(id:String,lease:UUID,context:UInt64,aborted:Bool,error:String? = nil) async throws {
        guard epoch == lease, gateway.contextRevision == context, let webView, let turn = turns[id] else { throw CancellationError() }
        var result = turn.completion(aborted:aborted,error:error)
        if let replyReference {
            result["replyRef"] = try replyReference(id,result["finalDisplayText"] as? String ?? "",true)
        }
        let ack = try await webView.callAsyncJavaScript("return window.__bwNativeAssistantStream?.acceptCompletion(id,result);",
            arguments:["id":id,"result":result],in:nil,contentWorld:.page) as? [String:Any]
        guard epoch == lease, gateway.contextRevision == context, ack?["ok"] as? Bool == true else {
            throw ReaderNativeAssistantStream.Failure("对话收尾状态已变化，未重复提交任务")
        }
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

    private func deliver(_ events: [ReaderNativeAssistantEvent], id: String, lease: UUID, gatewayContext: UInt64) async throws {
        try Task.checkCancellation()
        guard epoch == lease, gateway.contextRevision == gatewayContext, let webView, surface(webView.url) != nil else { throw CancellationError() }
        let next = (sequences[id] ?? 0) + 1
        guard var turn = turns[id] else { throw CancellationError() }
        let batch = try ReaderNativeAssistantEventBatch(events)
        var receipts: [ReaderNativeAssistantEvent] = []
        var nativeCommit: [String:Any]?
        if !batch.actions.isEmpty {
            // The document adapter has its own mutation sequence. Skipping a
            // text-only batch must not create a gap or advance a write receipt.
            let actionSequence = (bookActionSequences[id] ?? 0) + 1
            let actionEvents = batch.actions.map { ["name":$0.name,"data":$0.data] }
            let committed: Any?
            if let document = documents[id] {
                guard let commitPDFEvents else { throw ReaderNativeAssistantStream.Failure("原生 PDF 写入入口未准备好") }
                // Synchronous on the UI actor: navigation/cancellation cannot
                // interleave with the existing SQLite transaction owners.
                let result = try commitPDFEvents(document,actionEvents,actionSequence)
                nativeCommit = result
                committed = result
            } else {
                guard surface(webView.url) == .epub else { throw ReaderNativeAssistantStream.Failure("原生 PDF 会话已关闭") }
                committed = try await webView.callAsyncJavaScript(
                    "return await window.BWReaderRuntime?.nativeLocalRuntime?.commitAssistantEvents(id,sequence,events);",
                    arguments:["id":id,"sequence":actionSequence,"events":actionEvents], in:nil, contentWorld:.page)
            }
            guard epoch == lease, gateway.contextRevision == gatewayContext, let receipt = committed as? [String:Any], receipt["ok"] as? Bool == true,
                  receipt["sequence"] as? Int == actionSequence, let values = receipt["events"] as? [[String:Any]] else {
                throw ReaderNativeAssistantStream.Failure("本机改动未确认，未显示完成或重复执行")
            }
            receipts = try values.map { value in
                guard let name = value["name"] as? String, let data = value["data"] as? String else { throw ReaderNativeAssistantStream.Failure("助手改动回执不完整") }
                return .init(name:name,data:data)
            }
            bookActionSequences[id] = actionSequence
        }
        try Task.checkCancellation()
        let projected: [[String: Any]] = try batch.committed(receipts).map { event in
            var state = try turn.consume(event)
            if ["answer","error","done"].contains(event.name), !turn.sawTool, !turn.sawCLICard {
                guard let replyReference else { throw ReaderNativeAssistantStream.Failure("原生回复显示入口未准备好") }
                let final = event.name != "answer", content = ReaderNativeAssistantTurn.content(turn.answer)
                state["replyRef"] = try replyReference(id, final ? content.finalDisplayText : content.displayText, final)
            }
            return ["name": event.name, "data": event.data, "state":state]
        }
        var payload: [String:Any] = ["id":id,"sequence":next,"events":projected]
        if var nativeCommit {
            nativeCommit.removeValue(forKey:"events")
            payload["documentCommit"] = nativeCommit
        }
        let result = try await webView.callAsyncJavaScript(
            "const receipt = window.__bwNativeAssistantStream?.accept(payload); await window.RC?.turnCard?.settle?.(); return receipt;",
            arguments: ["payload": payload],
            in: nil, contentWorld: .page)
        guard epoch == lease, let ack = result as? [String: Any], ack["ok"] as? Bool == true,
              ack["sequence"] as? Int == next else { throw ReaderNativeAssistantStream.Failure("对话接收状态已改变，未重复执行事件") }
        sequences[id] = next
        turns[id] = turn
    }

    private func beginDocumentSession(id: String, body: [String:Any], path: String, lease: UUID) async throws -> Data {
        guard epoch == lease, let webView else { throw CancellationError() }
        let nativePDF = surface(webView.url) == .pdf
        if nativePDF && (commitPDFEvents == nil || preparePDFBody == nil) { throw ReaderNativeAssistantStream.Failure("原生 PDF 入口未准备好") }
        let response = try await webView.callAsyncJavaScript(
            "return await window.BWReaderRuntime?.nativeLocalRuntime?.beginAssistantSession(id,body,path,nativePDF);",
            arguments:["id":id,"body":body,"path":path,"nativePDF":nativePDF],in:nil,contentWorld:.page)
        try Task.checkCancellation()
        guard epoch == lease, let value = response as? [String:Any], value["ok"] as? Bool == true,
              var prepared = value["body"] as? [String:Any], prepared["rid"] as? String == body["rid"] as? String,
              prepared["turn_id"] as? String == body["turn_id"] as? String,
              JSONSerialization.isValidJSONObject(prepared) else { throw ReaderNativeAssistantStream.Failure("助手书籍上下文未准备好") }
        if nativePDF {
            guard value["commitOwner"] as? String == "swift", let preparePDFBody else {
                throw ReaderNativeAssistantStream.Failure("原生 PDF 写入接管未确认")
            }
            prepared = try await preparePDFBody(prepared)
            try Task.checkCancellation()
            guard epoch == lease, prepared["rid"] as? String == body["rid"] as? String,
                  prepared["turn_id"] as? String == body["turn_id"] as? String,
                  let context = prepared["context"] as? [String:Any], let authority = context["native_local_state"] as? [String:Any],
                  authority["file"] as? String == value["file"] as? String else { throw CancellationError() }
            documents[id] = try ReaderNativeAssistantDocumentSession(id:id,authority:authority)
        }
        let data = try JSONSerialization.data(withJSONObject:prepared)
        guard data.count <= 8 * 1024 * 1024 else { throw ReaderNativeAssistantStream.Failure("助手书籍上下文过大，未截断发送") }
        return data
    }

    private func endDocumentSession(id: String) async {
        documents.removeValue(forKey:id)?.close()
        guard let webView else { return }
        _ = try? await webView.callAsyncJavaScript("return window.BWReaderRuntime?.nativeLocalRuntime?.endAssistantSession(id);",
            arguments:["id":id],in:nil,contentWorld:.page)
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
      const active = new Map(), watching = new Map(), watchKeys = new Map();
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
        async pageContext(current) {
          current = {...current,page:Number(current.page)};
          const result = await handler.postMessage({version:1, action:'pageContext', id:crypto.randomUUID(), current});
          if (!result?.ok || result.context?.kind !== 'pdf' || result.context.file !== current.file || result.context.page !== current.page) throw new Error('原生阅读状态已改变');
          return result.context;
        },
        watchTask(kind, taskID, consume) {
          const key = kind + ':' + taskID;
          if (watchKeys.has(key)) return watchKeys.get(key);
          const id = crypto.randomUUID(), entry = {consume, sequence:0};
          watching.set(id, entry);
          const result = Promise.resolve().then(() => handler.postMessage({version:1, action:'watchTask', id, kind, taskID}))
            .then(reply => { if (!reply?.ok) throw new Error('任务追踪未完成'); return reply.status; })
            .finally(() => { watching.delete(id); watchKeys.delete(key); });
          watchKeys.set(key, result);
          return result;
        },
        acceptTask(payload) {
          const entry = watching.get(payload?.id);
          if (!entry || !Number.isSafeInteger(payload.sequence)) return {ok:false};
          if (payload.sequence === entry.sequence) return {ok:true,sequence:entry.sequence};
          if (payload.sequence !== entry.sequence + 1 || !payload.snapshot) return {ok:false};
          entry.sequence = payload.sequence;
          entry.consume(payload.snapshot);
          return {ok:true,sequence:entry.sequence};
        },
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
        },
        acceptCompletion(id, result) {
          const entry = active.get(id);
          if (!entry || !result || typeof result.answer !== 'string') return {ok:false};
          if (entry.completed) return {ok:true};
          entry.completed = true;
          entry.consume('native-completion', null, result);
          return {ok:true};
        }
      };
    })();
    """#
}
