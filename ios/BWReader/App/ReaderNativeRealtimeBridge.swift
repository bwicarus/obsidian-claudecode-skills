import Combine
import Foundation
import WebKit

@MainActor
final class ReaderRealtimeCredentialManager: ObservableObject {
    static let shared = ReaderRealtimeCredentialManager()

    @Published private(set) var status = ReaderRealtimeCredentialStatus.missing
    @Published private(set) var isRunning = false
    @Published private(set) var notice: String?
    @Published private(set) var errorMessage: String?

    private let store = ReaderRealtimeCredentialStore.shared

    private init() {
        refresh()
    }

    func refresh() {
        do {
            status = try store.status()
            errorMessage = nil
        } catch {
            status = .missing
            errorMessage = error.localizedDescription
        }
    }

    func saveExistingKey(_ apiKey: String) async {
        guard !isRunning else { return }
        isRunning = true
        notice = nil
        errorMessage = nil
        defer { isRunning = false }
        var savedToKeychain = false
        do {
            try store.save(
                apiKey: apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
            )
            savedToKeychain = true
            _ = try await ReaderRealtimeOpenAIClient.mintClientSecret()
            status = try store.status()
            notice = "Key 已存入 Apple Keychain，并已通过 OpenAI Realtime 联通验证"
        } catch {
            do {
                status = try store.status()
            } catch {
                status = .missing
            }
            errorMessage = savedToKeychain
                ? "Key 已保存，但 OpenAI 验证失败：\(error.localizedDescription)"
                : error.localizedDescription
        }
    }

    func clear() {
        notice = nil
        errorMessage = nil
        do {
            try store.clear()
            status = .missing
            notice = "已清除这台 iPad 中的 OpenAI Key"
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

@MainActor
final class ReaderNativeRealtimeBridge:
    NSObject,
    WKScriptMessageHandlerWithReply
{
    static let messageName = "bwNativeRealtime"

    private weak var webView: WKWebView?
    private let trustedBaseURL: URL

    init(webView: WKWebView, trustedBaseURL: URL) {
        self.webView = webView
        self.trustedBaseURL = trustedBaseURL
        super.init()
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        guard message.name == Self.messageName,
              message.frameInfo.isMainFrame,
              let webView,
              message.webView === webView,
              isTrustedLocalURL(webView.url),
              isTrustedLocalURL(message.frameInfo.request.url),
              let body = message.body as? [String: Any],
              let action = body["action"] as? String else {
            replyHandler(nil, "BW_NATIVE_REALTIME_SOURCE：Realtime 来源无效")
            return
        }
        Task { @MainActor in
            do {
                switch action {
                case "call":
                    guard Set(body.keys).isSubset(
                        of: ["action", "sdp", "file", "page"]
                    ),
                    let sdp = body["sdp"] as? String else {
                        throw ReaderRealtimeCredentialError.invalidRequest
                    }
                    let call = try await ReaderRealtimeOpenAIClient.openCall(
                        sdp: sdp
                    )
                    replyHandler([
                        "ok": true,
                        "sdp": call.answerSDP,
                        "call_id": call.callID,
                        "client_secret": call.clientSecret,
                        "model": call.model,
                        "rt_image": call.rtImage,
                        "compact_tokens": call.compactTokens,
                    ], nil)
                case "mint":
                    guard Set(body.keys).isSubset(of: ["action", "file", "page"])
                    else {
                        throw ReaderRealtimeCredentialError.invalidRequest
                    }
                    let minted = try await ReaderRealtimeOpenAIClient
                        .mintClientSecret()
                    replyHandler([
                        "ok": true,
                        "client_secret": minted.clientSecret,
                        "expires_at": minted.expiresAt,
                        "model": minted.model,
                        "rt_image": minted.rtImage,
                        "compact_tokens": minted.compactTokens,
                    ], nil)
                case "image":
                    guard Set(body.keys) == [
                        "action", "call_id", "client_secret", "tool",
                        "media_type", "b64",
                    ],
                    let callID = body["call_id"] as? String,
                    let clientSecret = body["client_secret"] as? String,
                    let tool = body["tool"] as? String,
                    ["see_ink", "see_page", "see_figure"].contains(tool),
                    let mediaType = body["media_type"] as? String,
                    let base64 = body["b64"] as? String else {
                        throw ReaderRealtimeCredentialError.invalidRequest
                    }
                    try await ReaderRealtimeOpenAIClient.injectImage(
                        callID: callID,
                        clientSecret: clientSecret,
                        mediaType: mediaType,
                        base64: base64
                    )
                    replyHandler(["ok": true], nil)
                case "hangup":
                    guard Set(body.keys) == [
                        "action", "call_id", "client_secret",
                    ],
                    let callID = body["call_id"] as? String,
                    let clientSecret = body["client_secret"] as? String else {
                        throw ReaderRealtimeCredentialError.invalidRequest
                    }
                    try await ReaderRealtimeOpenAIClient.hangup(
                        callID: callID,
                        clientSecret: clientSecret
                    )
                    replyHandler(["ok": true], nil)
                default:
                    throw ReaderRealtimeCredentialError.invalidRequest
                }
            } catch {
                replyHandler(nil, error.localizedDescription)
            }
        }
    }

    private func isTrustedLocalURL(_ url: URL?) -> Bool {
        guard let url,
              url.scheme?.lowercased() == trustedBaseURL.scheme?.lowercased(),
              url.host?.lowercased() == trustedBaseURL.host?.lowercased(),
              url.port == trustedBaseURL.port else {
            return false
        }
        let base = trustedBaseURL.path.hasSuffix("/")
            ? String(trustedBaseURL.path.dropLast())
            : trustedBaseURL.path
        return url.path == base || url.path.hasPrefix(base + "/")
    }
}
