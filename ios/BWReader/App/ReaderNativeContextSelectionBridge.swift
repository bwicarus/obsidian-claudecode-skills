import Foundation
import WebKit

@MainActor
final class ReaderNativeContextSelectionBridge: NSObject, WKScriptMessageHandlerWithReply {
    static let messageName = "bwNativeContextSelection"
    private weak var webView: WKWebView?
    private let trustedBaseURL: URL
    private var session: String?
    private var sequence = 0
    private var state = ReaderNativeContextSelection()
    private var expiry: Task<Void, Never>?

    init(webView: WKWebView, trustedBaseURL: URL) {
        self.webView = webView; self.trustedBaseURL = trustedBaseURL
        super.init()
    }
    deinit { expiry?.cancel() }
    func invalidate() {
        expiry?.cancel(); expiry = nil; session = nil; sequence = 0
        state = ReaderNativeContextSelection()
    }
    func selectReview(_ id: String, cardKey: String, on: Bool, validate: () throws -> Void) async throws -> [[String: Any]] {
        guard let session, let webView, let current = document(webView.url) else {
            throw ReaderNativeContextSelection.Failure(message: "复习上下文尚未就绪")
        }
        // Existing message producers may still have queued registrations. Drain
        // that ordered channel before acting on the native graph, rather than
        // rereading a DOM node or reconstructing an answer from its label.
        let ready = try await webView.callAsyncJavaScript(
            "if (!window.BWReaderRuntime?.contextSelections?.settle) return false; await window.BWReaderRuntime.contextSelections.settle(); return true;",
            arguments: [:], in: nil, contentWorld: .page)
        guard ready as? Bool == true, self.session == session, document(webView.url) == current else {
            throw ReaderNativeContextSelection.Failure(message: "复习上下文已切换")
        }
        try validate()
        try state.selectReview(id, cardKey: cardKey, on: on, now: ProcessInfo.processInfo.systemUptime)
        scheduleExpiry()
        let accepted = try await webView.callAsyncJavaScript(
            "return window.__bwNativeContextSelections?.accept(payload) === true;",
            arguments: ["payload": ["session": session, "state": state.projection]], in: nil, contentWorld: .page)
        guard accepted as? Bool == true, self.session == session, document(webView.url) == current else {
            throw ReaderNativeContextSelection.Failure(message: "选中状态已更新，界面需重新读取")
        }
        try validate()
        return state.reviewPairs(cardKey: cardKey)
    }
    private func document(_ url: URL?) -> URL? {
        guard let url, url.scheme == trustedBaseURL.scheme, url.host == trustedBaseURL.host,
              url.port == trustedBaseURL.port, url.path.hasPrefix(trustedBaseURL.path),
              url.path.hasSuffix("/shells/pdf.html") || url.path.hasSuffix("/shells/epub.html"),
              var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        parts.fragment = nil; return parts.url
    }
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard message.name == Self.messageName, message.frameInfo.isMainFrame,
              let webView, message.webView === webView, let current = document(webView.url),
              current == document(message.frameInfo.request.url),
              let command = message.body as? [String: Any], command["version"] as? Int == 1,
              let requestedSession = command["session"] as? String, UUID(uuidString: requestedSession) != nil else {
            replyHandler(nil, "原生上下文请求来源无效"); return
        }
        if command["action"] as? String == "start", session == nil {
            session = requestedSession
            replyHandler(["ok": true, "session": requestedSession, "sequence": sequence, "state": state.projection], nil)
            return
        }
        guard session == requestedSession, let next = command["sequence"] as? Int, next == sequence + 1 else {
            replyHandler(nil, "上下文已切换或更新顺序不一致"); return
        }
        do {
            let now = ProcessInfo.processInfo.systemUptime
            var media: [String: Any]?
            if command["action"] as? String == "mutate", let value = command["value"] as? [String: Any] {
                try state.apply(value, now: now)
            } else if command["action"] as? String == "media", let value = command["value"] as? [String: Any],
                      let card = value["card"] as? [String: Any], let index = value["index"] as? Int,
                      let action = value["action"] as? String {
                // Uses the same ordered queue as text/card selections. Commit
                // the entire deselect/select operation, or leave it untouched.
                var candidate = state
                candidate.expire(now: now)
                let changes = try ReaderNativeMediaArtifact.selectionCommands(card: card, index: index,
                    action: action, selected: candidate.projection["selected"] as? [String] ?? [])
                for change in changes { try candidate.apply(change, now: now) }
                state = candidate
                media = ["cid": card["cid"] ?? "", "index": index, "action": action]
            } else if command["action"] as? String == "read" {
                state.expire(now: now)
            } else { throw ReaderNativeContextSelection.Failure(message: "上下文操作无效") }
            sequence = next; scheduleExpiry()
            var reply: [String: Any] = ["ok": true, "session": requestedSession, "sequence": sequence, "state": state.projection]
            if let media { reply["media"] = media }
            replyHandler(reply, nil)
        } catch {
            if command["action"] as? String == "media" {
                // A definite rejected media operation did not mutate the graph.
                // Keep the ordered channel usable; only uncertain transport
                // failures stop it, not an item that disappeared before a tap.
                sequence = next
                replyHandler(["ok": true, "session": requestedSession, "sequence": sequence,
                    "state": state.projection, "media": ["error": error.localizedDescription]], nil)
            } else { replyHandler(nil, error.localizedDescription) }
        }
    }
    private func scheduleExpiry() {
        expiry?.cancel(); expiry = nil
        guard let deadline = state.nextDeadline, let lease = session else { return }
        let delay = max(0, deadline - ProcessInfo.processInfo.systemUptime)
        expiry = Task { @MainActor [weak self] in
            do { try await Task.sleep(nanoseconds: UInt64(min(delay, 3600) * 1_000_000_000)) }
            catch { return }
            guard let self, self.session == lease, let webView = self.webView, self.document(webView.url) != nil else { return }
            let changed = self.state.expire(now: ProcessInfo.processInfo.systemUptime)
            self.scheduleExpiry()
            if changed {
                // A new document rejects this callback by its random session ID.
                _ = try? await webView.callAsyncJavaScript(
                    "return window.__bwNativeContextSelections?.accept(payload);",
                    arguments: ["payload": ["session": lease, "state": self.state.projection]],
                    in: nil, contentWorld: .page)
            }
        }
    }

    static let script = #"""
    (() => {
      if (window !== window.top || window.__bwNativeContextSelections) return;
      const handler = window.webkit?.messageHandlers?.bwNativeContextSelection;
      if (!handler) return;
      const session = crypto.randomUUID();
      let sequence = 0, pending = 0, error = null, current = null, registry = null;
      let publish = () => {};
      function receive(result) {
        if (result?.session !== session || !Number.isSafeInteger(result.state?.revision) ||
            !Array.isArray(result.state?.selected) || result.state?.snapshot?.contract !== 'context-selection/1' ||
            !Array.isArray(result.state?.snapshot?.items)) throw new Error('原生上下文未获确认');
        if (!current || result.state.revision >= current.revision) current = JSON.parse(JSON.stringify(result.state));
        if (!pending && !error) publish();
      }
      function failed(reason) {
        error = reason instanceof Error ? reason : new Error(String(reason));
        window.dispatchEvent(new CustomEvent('rc:context-selection-error', {detail:{message:error.message}}));
      }
      let queue = Promise.resolve().then(() => handler.postMessage({version:1, action:'start', session})).then(result => {
        if (!result?.ok || result.sequence !== 0) throw new Error('原生上下文未就绪');
        receive(result);
      }).catch(failed);
      function enqueue(action, value) {
        pending++;
        const frozen = value == null ? null : JSON.parse(JSON.stringify(value));
        let receipt;
        queue = queue.then(async () => {
          if (error) throw error;
          const next = ++sequence;
          const result = await handler.postMessage({version:1, action, session, sequence:next, value:frozen});
          if (!result?.ok || result.sequence !== next) throw new Error('原生上下文更新未获确认');
          receive(result);
          receipt = result;
        }).catch(failed).finally(() => { pending--; if (!pending && !error) publish(); });
        return queue.then(() => receipt);
      }
      window.__bwNativeContextSelections = {
        accept(payload) { if (payload?.session !== session) return false; receive(payload); return true; },
        async media(card, index, action) {
          const result = await enqueue('media', {card, index, action});
          if (error) throw error;
          if (result?.media?.error) throw new Error(result.media.error);
          if (result?.media?.cid !== card.cid || result.media.index !== index || result.media.action !== action)
            throw new Error('媒体操作未获确认');
          return result.media;
        },
        createRegistry(api) {
          if (registry) return registry;
          // Synchronous legacy callers retain a disposable optimistic projection.
          // It has no timer or persistence. Swift owns expiry and request snapshots.
          const mirror = api.createRegistry({expireMs:0}), listeners = [];
          let projectedRevision = -1, buffering = 0;
          const events = [];
          const emit = event => listeners.slice().forEach(fn => { try { fn(event); } catch (_) {} });
          const flush = () => { if (!buffering) while (events.length) emit(events.shift()); };
          mirror.subscribe(event => { events.push(event); flush(); });
          publish = () => {
            if (!current || projectedRevision === current.revision) return;
            projectedRevision = current.revision;
            const selected = new Set(current.selected);
            buffering++;
            // Swift can create a media selection without any web node. Include
            // covered children, so releasing a selected parent reveals them.
            for (const record of current.selectedRecords || []) mirror.upsert(record);
            for (const id of rawSelected) if (!selected.has(id)) mirror.deselect(id);
            for (const id of selected) mirror.select(id);
            rawSelected = selected;
            events.push({type:'native', id:'', version:mirror.version()});
            buffering--; flush();
          };
          let rawSelected = new Set();
          registry = Object.assign({}, mirror, {
            expireMs: () => 300000,
            subscribe(fn) {
              if (typeof fn !== 'function') throw new Error('subscribe 需要函数');
              listeners.push(fn);
              return () => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i,1); };
            },
            async settle() {
              // Include expiry at request time even if WebKit delayed a timer callback.
              enqueue('read');
              let observed;
              do { observed = queue; await observed; } while (observed !== queue);
              if (error) throw error;
              return registry.snapshot();
            },
            reviewPairs(cardKey) {
              if (error) throw error;
              if (pending || !current) return [];
              const pairs = current.reviewPairs || {};
              return Object.prototype.hasOwnProperty.call(pairs, cardKey) ? JSON.parse(JSON.stringify(pairs[cardKey])) : [];
            },
            snapshot(options = {}) {
              if (error) throw error;
              if (pending || !current) return mirror.snapshot(options);
              const limit = (v, fallback) => v == null || !Number.isFinite(Number(v)) ? fallback : Math.max(0, Math.floor(Number(v)));
              const textLimit = limit(options.maxText, 2500);
              const count = limit(options.limit ?? options.maxItems, Number.MAX_SAFE_INTEGER);
              return {contract:'context-selection/1', items:current.snapshot.items.slice(0,count).map(item => {
                const value = JSON.parse(JSON.stringify(item)); value.text = value.text.slice(0,textLimit); return value;
              })};
            },
            serialize(options) { return api.stableStringify(registry.snapshot(options)); },
            toLegacy(options) {
              const snapshot = registry.snapshot(options), labels = [], map = {};
              snapshot.items.forEach(item => {
                const base = item.label || item.id; let label = base, n = 2;
                while (Object.prototype.hasOwnProperty.call(map,label)) label = base + '·' + n++;
                labels.push(label); Object.defineProperty(map,label,{value:item.text,enumerable:true,configurable:true});
              });
              return {labels,map,items:snapshot.items,serialized:api.stableStringify(snapshot)};
            }
          });
          for (const operation of ['upsert','select','toggle','deselect','remove','clear']) {
            registry[operation] = function(input,on) {
              if (error) throw error;
              buffering++;
              try {
              const before = mirror.version(), result = mirror[operation](input,on);
              if (mirror.version() === before) return result;
              const object = input && typeof input === 'object';
              const id = operation === 'clear' ? '' : String(object ? input.id : input).trim();
              const value = {operation,id};
              if (operation === 'upsert' || object) {
                value.record = mirror.get(id);
                if (Object.prototype.hasOwnProperty.call(input,'selected')) value.selected = !!input.selected;
              }
              if (operation === 'select') value.on = on !== false;
              if (operation === 'clear') rawSelected.clear();
              else if (mirror.isSelected(id)) rawSelected.add(id); else rawSelected.delete(id);
              enqueue('mutate', value);
              return result;
              } finally { buffering--; flush(); }
            };
          }
          if (current && !pending) publish();
          return registry;
        }
      };
    })();
    """#
}
