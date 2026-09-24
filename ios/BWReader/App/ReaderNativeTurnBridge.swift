import Foundation
import WebKit

@MainActor
final class ReaderNativeTurnBridge: NSObject, WKScriptMessageHandlerWithReply {
    static let messageName = "bwNativeTurns"
    private weak var webView: WKWebView?
    private let trustedBaseURL: URL
    private let gateway: ReaderNativeServerGateway
    private var session: String?
    private var sequence = 0
    private var store = ReaderNativeTurnStore()
    var onFailure: ((String) -> Void)?
    private var saves: [String:(id:UUID,task:Task<[String:Any],Error>)] = [:]
    private var clearing: [String:UUID] = [:]

    init(webView: WKWebView, trustedBaseURL: URL, gateway: ReaderNativeServerGateway) {
        self.webView = webView; self.trustedBaseURL = trustedBaseURL; self.gateway = gateway
        super.init()
    }
    deinit { saves.values.forEach { $0.task.cancel() } }
    func removeMedia(card: [String:Any], index: Int) throws { try store.removeMedia(card:card,index:index) }
    func replyReference(id: String, text: String, final: Bool) throws -> [String:Any] {
        guard let session else { throw ReaderNativeTurnStore.Failure(message:"对话消息源尚未就绪") }
        let tid = "native-reply:" + id
        var candidate = store
        var result = try candidate.apply(["action":"draft","tid":tid,"text":text,
            "itemId":"reply","origin":"native-stream","role":"assistant"])
        if final {
            result = try candidate.apply(["action":"freeze","tid":tid,"itemId":"reply","origin":"native-stream","role":"assistant"])
        }
        guard let turn = (result["turns"] as? [[String:Any]])?.first,
              let presentation = turn["presentation"] as? [String:Any], let revision = presentation["revision"] as? Int else {
            throw ReaderNativeTurnStore.Failure(message:"原生回复未提交")
        }
        store = candidate
        return ["session":session,"tid":tid,"revision":revision]
    }
    func conversationPayload(_ input: [String:Any]) throws -> [String:Any] {
        guard var batch = input["messageDelta"] as? [String:Any], let messages = batch["upserts"] as? [[String:Any]] else { return input }
        batch["upserts"] = try messages.map { message -> [String:Any] in
            if let reference = message["nativeTurnRef"] as? [String:Any] {
                guard let session, reference["session"] as? String == session else { throw CancellationError() }
            }
            return try store.conversationMessage(message)
        }
        var result = input; result["messageDelta"] = batch; return result
    }
    func invalidate() {
        saves.values.forEach { $0.task.cancel() }; saves.removeAll()
        session = nil; sequence = 0; store = ReaderNativeTurnStore()
        clearing.removeAll()
    }

    func beginClear(_ mode: String) async throws -> UUID {
        guard clearing[mode] == nil else { throw ReaderNativeTurnStore.Failure(message:"对话正在清空") }
        let token = UUID(), lease = session
        clearing[mode] = token
        // Clear cannot race an already accepted save and let its late response
        // recreate the old history. Unknown save results remain visible errors.
        do {
            for pending in saves.filter({ $0.key.hasPrefix(mode + ":") }).values { _ = try await pending.task.value }
            guard session == lease, clearing[mode] == token else { throw CancellationError() }
            return token
        } catch {
            if clearing[mode] == token { clearing.removeValue(forKey:mode) }
            throw error
        }
    }

    func endClear(_ mode: String, token: UUID, cleared: Bool) {
        guard clearing[mode] == token else { return }
        if cleared { store = ReaderNativeTurnStore() }
        clearing.removeValue(forKey:mode)
    }
    private func document(_ url: URL?) -> URL? {
        guard let url, url.scheme == trustedBaseURL.scheme, url.host == trustedBaseURL.host,
              url.port == trustedBaseURL.port, url.path.hasPrefix(trustedBaseURL.path),
              url.path.hasSuffix("/shells/pdf.html") || url.path.hasSuffix("/shells/epub.html"),
              var parts = URLComponents(url:url,resolvingAgainstBaseURL:false) else { return nil }
        parts.fragment = nil; return parts.url
    }
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard message.name == Self.messageName, message.frameInfo.isMainFrame,
              let webView, message.webView === webView, let current = document(webView.url),
              current == document(message.frameInfo.request.url), let body = message.body as? [String:Any],
              body["version"] as? Int == 1, let requested = body["session"] as? String, UUID(uuidString:requested) != nil else {
            replyHandler(nil,"原生轮次请求来源无效"); return
        }
        if body["action"] as? String == "start", session == nil {
            session = requested; replyHandler(["ok":true,"session":requested,"sequence":0],nil); return
        }
        if session == requested, body["action"] as? String == "failure", let message = body["error"] as? String {
            onFailure?(String(message.prefix(2000))); replyHandler(["ok":true],nil); return
        }
        if session == requested, ["persist","logVoice"].contains(body["action"] as? String ?? "") {
            persist(body,document:current,replyHandler:replyHandler); return
        }
        guard session == requested, body["action"] as? String == "apply",
              let next = body["sequence"] as? Int, next == sequence + 1,
              let commands = body["commands"] as? [[String:Any]], !commands.isEmpty, commands.count <= 1024 else {
            replyHandler(nil,"对话轮次已切换或顺序不一致"); return
        }
        do {
            var candidate = store, changed: [String:[String:Any]] = [:], removed = Set<String>(), persist = Set<String>()
            var result: [String:Any] = [:]
            for command in commands {
                result = try candidate.apply(command)
                for id in result["removed"] as? [String] ?? [] { removed.insert(id); changed.removeValue(forKey:id); persist.remove(id) }
                for turn in result["turns"] as? [[String:Any]] ?? [] {
                    guard let id = turn["id"] as? String else { continue }
                    changed[id] = turn; removed.remove(id)
                    if ["append","freeze","cli","operationState","rename"].contains(command["action"] as? String ?? ""), turn["historyReplay"] as? Bool != true { persist.insert(id) }
                }
            }
            result["turns"] = changed.keys.sorted().compactMap { changed[$0] }
            result["removed"] = Array(removed); result["persist"] = Array(persist)
            result["generation"] = commands.last?["generation"] ?? 0
            // Commit all commands together. Unknown/rejected delivery is never
            // silently retried under a new session or interpreted as empty history.
            store = candidate; sequence = next
            replyHandler(["ok":true,"session":requested,"sequence":sequence,"result":result],nil)
        } catch { onFailure?(error.localizedDescription); replyHandler(nil,error.localizedDescription) }
    }

    private func persist(_ request: [String:Any], document: URL, replyHandler: @escaping (Any?,String?) -> Void) {
        do {
            guard let tid = request["tid"] as? String, tid.utf16.count <= 2048,
                  let metadata = request["metadata"] as? [String:Any] else { throw ReaderNativeTurnStore.Failure(message:"轮次保存参数缺失") }
            let body: [String:Any]?
            let mode: String
            if request["action"] as? String == "logVoice" {
                body = try store.voicePayload(tid:tid,metadata:metadata)
                mode = body!["assistant_mode"] as! String
            } else {
                guard let requestedMode = metadata["mode"] as? String, let file = metadata["file"] as? String,
                      let page = metadata["page"] as? Int else { throw ReaderNativeTurnStore.Failure(message:"轮次保存参数缺失") }
                mode = requestedMode
                body = try store.historyPayload(tid:tid,mode:mode,file:file,page:page,absorb:metadata["absorb"] as? [String] ?? [])
            }
            guard let body else {
                replyHandler(["ok":true,"skipped":true],nil); return
            }
            guard clearing[mode] == nil else { throw ReaderNativeTurnStore.Failure(message:"正在清空对话，未追加旧轮次") }
            let bytes = try JSONSerialization.data(withJSONObject:body), lease = session, context = gateway.contextRevision
            let key = mode + ":" + tid, id = UUID(), previous = saves[key]?.task
            let surface: ReaderNativeInterfaceSurface = document.path.hasSuffix("/epub.html") ? .epub : .pdf
            let task = Task { @MainActor [weak self] () throws -> [String:Any] in
                // Preserve write order for the same turn; never let an older
                // network completion replace a newly frozen message.
                _ = try await previous?.value
                guard let self else { throw CancellationError() }
                for attempt in 0...1 {
                    try Task.checkCancellation()
                    guard self.session == lease, self.gateway.contextRevision == context else { throw CancellationError() }
                    let response = try await self.gateway.fetchData(path:"/api/assistant/log",method:"POST",body:bytes,surface:surface)
                    try Task.checkCancellation()
                    guard self.session == lease, self.gateway.contextRevision == context else { throw CancellationError() }
                    guard (200..<300).contains(response.status), let value = try JSONSerialization.jsonObject(with:response.data) as? [String:Any] else {
                        throw ReaderNativeTurnStore.Failure(message:"对话保存未获服务器确认")
                    }
                    if value["upserted"] as? Bool == false {
                        // Only an explicit not-applied response can be retried.
                        guard attempt == 0 else { throw ReaderNativeTurnStore.Failure(message:"对话保存尚未就绪") }
                        try await Task.sleep(nanoseconds:2_500_000_000); continue
                    }
                    guard value["ok"] as? Bool != false else { throw ReaderNativeTurnStore.Failure(message:value["error"] as? String ?? "对话保存失败") }
                    return value
                }
                throw ReaderNativeTurnStore.Failure(message:"对话保存尚未就绪")
            }
            saves[key] = (id,task)
            Task { @MainActor [weak self] in
                do { replyHandler(["ok":true,"result":try await task.value],nil) }
                catch {
                    if self?.session == lease, !(error is CancellationError) { self?.onFailure?(error.localizedDescription) }
                    replyHandler(nil,error.localizedDescription)
                }
                if self?.saves[key]?.id == id { self?.saves.removeValue(forKey:key) }
            }
        } catch { onFailure?(error.localizedDescription); replyHandler(nil,error.localizedDescription) }
    }

    static let script = #"""
    (() => {
      if (window !== window.top || window.__bwNativeTurns) return;
      const handler = window.webkit?.messageHandlers?.bwNativeTurns;
      if (!handler) return;
      const session = crypto.randomUUID();
      let sequence = 0, failure = null, pending = [], scheduled = false, accept = null;
      function failed(error) {
        // A rejected queue stays stopped. Already queued batches must not emit
        // another alert for the same failure, and WebKit/cross-realm errors
        // still carry their original message even without instanceof Error.
        if (failure) return;
        failure = error instanceof Error ? error : new Error(typeof error?.message === 'string' ? error.message : String(error));
        Promise.resolve(handler.postMessage({version:1,action:'failure',session,error:failure.message})).catch(() => {});
        window.dispatchEvent(new CustomEvent('rc:native-turn-error', {detail:{message:failure.message}}));
      }
      let queue = Promise.resolve(handler.postMessage({version:1,action:'start',session})).then(reply => {
        if (!reply?.ok || reply.session !== session || reply.sequence !== 0) throw new Error('原生轮次未就绪');
      }).catch(failed);
      function flush() {
        scheduled = false;
        if (!pending.length) return;
        const commands = pending.splice(0,1024);
        queue = queue.then(async () => {
          if (failure) throw failure;
          const next = sequence + 1;
          const reply = await handler.postMessage({version:1,action:'apply',session,sequence:next,commands});
          if (!reply?.ok || reply.session !== session || reply.sequence !== next || !reply.result) throw new Error('原生轮次更新未获确认');
          sequence = next;
          if (accept) accept(reply.result);
        }).catch(failed);
        if (pending.length) { scheduled = true; queueMicrotask(flush); }
      }
      window.__bwNativeTurns = {
        reference(presentation) {
          if (!presentation || typeof presentation.tid !== 'string' || !Number.isSafeInteger(presentation.revision)) return null;
          return {session,tid:presentation.tid,revision:presentation.revision};
        },
        connect(fn) { if (accept) throw new Error('轮次视图已连接'); accept = fn; },
        enqueue(command) {
          if (failure) throw failure;
          const value = JSON.parse(JSON.stringify(command));
          // Collapse only consecutive updates to the same still-open draft.
          // A freeze, tool or different speaker always remains an ordering fence.
          const prior = pending[pending.length-1];
          if (value.action === 'draft' && prior?.action === 'draft' &&
              ['tid','itemId','origin','role'].every(k => prior[k] === value[k])) pending[pending.length-1] = value;
          else pending.push(value);
          if (!scheduled) { scheduled = true; queueMicrotask(flush); }
        },
        async settle() {
          while (pending.length) flush();
          for (;;) { const waiting = queue; await waiting; if (failure) throw failure; if (waiting === queue && !pending.length) return; while (pending.length) flush(); }
        },
        async persist(tid, metadata) {
          await this.settle();
          const reply = await handler.postMessage({version:1,action:'persist',session,tid,metadata});
          if (!reply?.ok) throw new Error('对话保存未获确认');
          return reply.result || {ok:true,skipped:true};
        },
        async logVoice(tid, metadata) {
          await this.settle();
          const reply = await handler.postMessage({version:1,action:'logVoice',session,tid:String(tid || ''),metadata});
          if (!reply?.ok) throw new Error('语音轮次保存未获确认');
          return reply.result;
        }
      };
    })();
    """#
}
