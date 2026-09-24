import Foundation
import PDFKit
import WebKit

/// PDFKit owns navigation and SQLite owns persistence. The web observer is
/// transitional context delivery only; it cannot save a second copy.
@MainActor
final class ReaderNativePDFNavigationBridge: NSObject, WKScriptMessageHandlerWithReply {
    static let messageName = "bwNativePDFNavigation"
    private weak var webView: WKWebView?
    private weak var document: ReaderNativePDFDocument?
    private let trustedBaseURL: URL
    private var token: String?
    private var file = ""
    private var bookID = ""
    private var digest = ""
    private var isCurrent: (() -> Bool)?
    private var sequence = 0
    private var pending: ReaderNativePDFDocument.Position?
    private var delivery: Task<Void, Never>?
    private var jumping = false
    private var pageOffset = 0
    private var backPage: Int?
    var restorePosition: ((Int) throws -> [String:Any]?)?
    var savePosition: ((String, [String:Any]) throws -> Void)?
    var reportFailure: ((String) -> Void)?
    private var lastSaved: NSDictionary?
    private(set) var lastError: String?

    init(webView: WKWebView, trustedBaseURL: URL) {
        self.webView = webView
        self.trustedBaseURL = trustedBaseURL
        super.init()
    }

    func initialPosition() async throws -> [String: Any] {
        guard let webView, trusted(webView.url) else { throw unavailable() }
        let value = try await webView.callAsyncJavaScript(
            "const state = window.RC?.readerNavigation?.nativeState(); return state && {...state, pageOffset: window._pageOffset?.() || 0};",
            arguments: [:], in: nil, contentWorld: .page)
        guard let result = value as? [String: Any], let file = result["file"] as? String,
              !file.isEmpty, (result["total"] as? NSNumber)?.intValue ?? 0 > 0 else { throw unavailable() }
        pageOffset = min(10_000_000,max(-10_000_000,(result["pageOffset"] as? NSNumber)?.intValue ?? 0))
        return try restorePosition?((result["total"] as! NSNumber).intValue) ?? result
    }

    /// Called once the native viewport has a real layout. Open/restore and
    /// original overlay import happen before this ownership transfer.
    func attach(_ document: ReaderNativePDFDocument, bookID: String, contentSHA256: String,
                isCurrent: @escaping () -> Bool) async throws {
        guard token == nil, let webView, trusted(webView.url), isCurrent(),
              document.matches(bookID: bookID, contentSHA256: contentSHA256),
              document.view.bounds.width > 0, document.view.bounds.height > 0 else { throw unavailable() }
        let initial = try await initialPosition()
        guard token == nil, isCurrent(), document.matches(bookID: bookID, contentSHA256: contentSHA256),
              let file = initial["file"] as? String else { throw unavailable() }
        let lease = UUID().uuidString
        self.document = document; self.bookID = bookID; digest = contentSHA256.lowercased()
        self.file = file; self.isCurrent = isCurrent; token = lease; sequence = 0; lastError = nil; lastSaved = nil
        do {
            let result = try await webView.callAsyncJavaScript("""
                const owner = window.RC?.readerNavigation;
                if (!owner || owner.nativeState().file !== file || owner.nativeViewport) throw new Error('阅读视口已切换');
                owner.attachNativeViewport({ file, token, persistsNatively: true,
                  goToPage: page => window.webkit.messageHandlers.bwNativePDFNavigation.postMessage({
                    action: 'page', token, file, bookID, digest, value: page
                  }),
                  perform: (action, value) => window.webkit.messageHandlers.bwNativePDFNavigation.postMessage({
                    action, token, file, bookID, digest, value
                  })
                });
                return true;
                """, arguments: ["file": file, "token": lease, "bookID": bookID, "digest": digest],
                in: nil, contentWorld: .page)
            guard result as? Bool == true, valid(lease) else { throw unavailable() }
            document.onPosition = { [weak self] position in self?.enqueue(position) }
            try await publish(document.position, lease: lease)
        } catch {
            if token == lease { invalidate() }
            throw error
        }
    }

    /// Revoke synchronously before any asynchronous navigation/identity change.
    /// The token check prevents cleanup from detaching a newer book's viewport.
    func invalidate() {
        flushPendingPosition()
        let previous = token
        token = nil; delivery?.cancel(); delivery = nil; pending = nil
        document?.onPosition = nil; document = nil; isCurrent = nil
        file = ""; bookID = ""; digest = ""; jumping = false; backPage = nil; pageOffset = 0
        guard let previous, let webView else { return }
        Task { @MainActor [weak webView] in
            _ = try? await webView?.callAsyncJavaScript(
                "return window.RC?.readerNavigation?.detachNativeViewport(token);",
                arguments: ["token": previous], in: nil, contentWorld: .page)
        }
    }

    /// Scene suspension/book switches may arrive before the trailing timer.
    /// Commit synchronously before revoking identity; never depend on WebKit.
    func flushPendingPosition() {
        guard let lease = token, valid(lease), let value = pending else { return }
        do { try persist(payload(value)); pending = nil }
        catch { lastError = error.localizedDescription; reportFailure?(error.localizedDescription) }
    }

    private func persist(_ value: [String:Any]) throws {
        guard let savePosition else { throw unavailable() }
        let viewport = try ReaderNativeReadingPosition.validated(value)
        guard lastSaved?.isEqual(to:viewport) != true else { return }
        try savePosition(bookID,viewport)
        lastSaved = viewport as NSDictionary
    }

    private func valid(_ lease: String) -> Bool {
        token == lease && isCurrent?() == true && trusted(webView?.url)
            && document?.matches(bookID: bookID, contentSHA256: digest) == true
    }

    func displayPage(_ page: Int) -> Int {
        let printed = page - pageOffset
        return pageOffset != 0 && printed >= 1 ? printed : page
    }

    func state() throws -> [String:Any] {
        guard let lease = token, valid(lease), let document else { throw unavailable() }
        let page = document.position.page, total = document.view.document?.pageCount ?? 0
        return ["ready":total > 0,"unit":"页","position":page,"total":total,
                "display":displayPage(page),"firstDisplay":displayPage(1),"lastDisplay":displayPage(total),
                "totalLabel":String(displayPage(total)),"backLabel":backPage.map { "回到第 \(displayPage($0)) 页" } ?? "",
                "previous":page > 1,"next":page < total]
    }

    /// All native entry points share the same return anchor and persisted
    /// viewport. The webpage receives a projection, never executes the jump.
    func navigate(_ action: String, value: Any? = nil) async throws -> [String:Any] {
        guard let lease = token, valid(lease), let document else { throw unavailable() }
        let total = document.view.document?.pageCount ?? 0, current = document.position.page
        let target: Int
        switch action {
        case "previous": target = max(1,current - 1)
        case "next": target = min(total,current + 1)
        case "back": target = backPage ?? current
        case "page":
            let raw = (value as? String ?? (value as? NSNumber)?.stringValue ?? "").trimmingCharacters(in:.whitespacesAndNewlines)
            guard raw.range(of:"^-?[0-9]+$",options:.regularExpression) != nil, let number = Int(raw), abs(Double(number)) <= 9_007_199_254_740_991 else { throw unavailable() }
            target = min(total,max(1,number + pageOffset))
        case "position", "jump":
            guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
                  number.doubleValue >= 1, number.doubleValue <= Double(total) else { throw unavailable() }
            target = number.intValue
        default: throw unavailable()
        }
        let previousBack = backPage, old = document.position
        pending = nil; jumping = true
        do {
            if action == "back" { backPage = nil }
            else if action == "jump", target != current, backPage == nil { backPage = current }
            try document.go(to:target)
            try persist(payload(document.position))
        } catch {
            backPage = previousBack; try? document.go(to:old.page,fraction:old.fraction); jumping = false
            throw error
        }
        jumping = false
        try await publish(document.position,lease:lease)
        return try state()
    }

    /// Native settings use the same viewport/persistence path without making a
    /// round trip through the web command dispatcher.
    func performToolbar(_ action: String) async throws {
        guard let lease = token, valid(lease), let document else { throw unavailable() }
        pending = nil; jumping = true
        defer { jumping = false }
        if action == "spread" {
            let old = document.position
            let mode = old.mode != "spread" || old.spreadOffset == 0 ? "spread" : "continuous"
            document.setLayout(mode: mode, firstPageAlone: old.mode == "spread" && old.spreadOffset == 0)
        } else if action != "fit" { throw unavailable() }
        document.fitWidth()
        try await publish(document.position, lease: lease)
    }

    func applyCrop(_ crop: ReaderNativePDFCrop?, expectedBookID: String) async throws {
        guard let lease = token, valid(lease), bookID == expectedBookID, let document else { throw unavailable() }
        pending = nil; jumping = true
        let old = document.position.crop
        do {
            try document.setCrop(crop)
            try persist(payload(document.position))
        } catch {
            try? document.setCrop(old)
            jumping = false
            throw error
        }
        jumping = false
        try await publish(document.position, lease: lease)
    }

    private func enqueue(_ value: ReaderNativePDFDocument.Position) {
        guard !jumping, let lease = token, valid(lease) else { return }
        pending = value
        guard delivery == nil else { return }
        // One event-driven trailing delivery, with a single latest value while
        // crossing WebKit. No per-frame persistence and no background polling.
        delivery = Task { @MainActor [weak self] in
            guard let self else { return }
            defer { if self.token == lease { self.delivery = nil } }
            while self.valid(lease), !Task.isCancelled, self.pending != nil {
                do {
                    try await Task.sleep(for: .milliseconds(180))
                    guard self.valid(lease), !Task.isCancelled, let value = self.pending else { return }
                    self.pending = nil
                    try await self.publish(value, lease: lease)
                    self.lastError = nil
                } catch {
                    if !Task.isCancelled, self.valid(lease) {
                        if self.pending == nil { self.pending = self.document?.position }
                        self.lastError = error.localizedDescription; self.reportFailure?(error.localizedDescription)
                    }
                    return
                }
            }
        }
    }

    private func payload(_ position: ReaderNativePDFDocument.Position) -> [String: Any] {
        sequence += 1
        var value: [String: Any] = ["sequence": sequence, "page": position.page, "scale": Double(position.scale),
                "fraction": Double(position.fraction), "visiblePages": position.visiblePages,
                "mode": position.mode, "spreadOffset": position.spreadOffset, "cropEnabled": position.crop != nil,
                "backPage":backPage as Any? ?? NSNull()]
        if let crop = position.crop { value["crop"] = crop.percentages }
        return value
    }

    private func publish(_ position: ReaderNativePDFDocument.Position, lease: String) async throws {
        guard valid(lease), let webView else { throw unavailable() }
        let value = payload(position)
        try persist(value)
        let response = try await webView.callAsyncJavaScript("""
            const owner = window.RC?.readerNavigation;
            if (!owner || owner.nativeViewport?.token !== token) return false;
            if (position.sequence <= owner.nativeViewport.sequence) return true;
            owner.acceptNativePosition(token, position);
            return true;
            """, arguments: ["token": lease, "position": value], in: nil, contentWorld: .page)
        guard valid(lease), response as? Bool == true else { throw unavailable() }
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard message.name == Self.messageName, message.frameInfo.isMainFrame,
              let webView, message.webView === webView, trusted(message.frameInfo.request.url),
              let body = message.body as? [String: Any],
              Set(body.keys) == Set(["action", "token", "file", "bookID", "digest", "value"]),
              let action = body["action"] as? String, ["page", "jump", "back", "layout", "scale", "fit", "crop"].contains(action),
              let lease = body["token"] as? String,
              valid(lease), body["file"] as? String == file, body["bookID"] as? String == bookID,
              body["digest"] as? String == digest,
              let document else {
            replyHandler(nil, "翻页请求已过期或无效")
            return
        }
        if action == "jump" || action == "back" {
            Task { @MainActor [weak self] in
                do {
                    guard let self else { throw CancellationError() }
                    guard self.valid(lease) else { throw self.unavailable() }
                    _ = try await self.navigate(action,value:body["value"])
                    guard self.valid(lease) else { throw self.unavailable() }
                    replyHandler(["ok":true,"position":self.payload(document.position)],nil)
                } catch { replyHandler(nil,error.localizedDescription) }
            }
            return
        }
        do {
            // Discard an older queued scroll position before applying a command.
            // In-flight replies carry sequence numbers and cannot rewind it.
            pending = nil; jumping = true
            defer { jumping = false }
            switch action {
            case "page":
                guard let number = body["value"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
                      number.doubleValue >= 1, number.doubleValue <= Double(document.view.document?.pageCount ?? 0) else { throw unavailable() }
                try document.go(to: number.intValue)
            case "scale":
                guard let number = body["value"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      number.doubleValue.isFinite, number.doubleValue > 0, number.doubleValue <= 100 else { throw unavailable() }
                document.setScale(CGFloat(number.doubleValue))
            case "layout":
                guard let value = body["value"] as? [String: Any], Set(value.keys) == Set(["mode", "spreadOffset"]),
                      let mode = value["mode"] as? String, ["single", "continuous", "spread"].contains(mode),
                      let offset = value["spreadOffset"] as? NSNumber, CFGetTypeID(offset) != CFBooleanGetTypeID(),
                      [0.0, 1.0].contains(offset.doubleValue) else { throw unavailable() }
                document.setLayout(mode: mode, firstPageAlone: offset.intValue == 1)
                document.fitWidth()
            case "crop":
                guard let value = body["value"] as? [String: Any], Set(value.keys) == Set(["enabled", "crop"]),
                      let enabled = value["enabled"] as? NSNumber, CFGetTypeID(enabled) == CFBooleanGetTypeID(),
                      let data = value["crop"] as? [String: Any], let crop = ReaderNativePDFCrop(data) else { throw unavailable() }
                try document.setCrop(enabled.boolValue ? crop : nil)
            default:
                guard body["value"] is NSNull else { throw unavailable() }
                document.fitWidth()
            }
            guard valid(lease) else { throw unavailable() }
            let position = payload(document.position)
            try persist(position)
            replyHandler(["ok": true, "position": position], nil)
        } catch { replyHandler(nil, error.localizedDescription) }
    }

    private func trusted(_ url: URL?) -> Bool {
        guard let url else { return false }
        return url.scheme?.lowercased() == trustedBaseURL.scheme?.lowercased()
            && url.host?.lowercased() == trustedBaseURL.host?.lowercased()
            && url.port == trustedBaseURL.port && url.path.hasPrefix(trustedBaseURL.path)
            && url.path.hasSuffix("/shells/pdf.html")
    }

    private func unavailable() -> NSError {
        NSError(domain: "ReaderNativeNavigation", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "原生阅读视口尚未准备好或书籍已切换"])
    }
}
