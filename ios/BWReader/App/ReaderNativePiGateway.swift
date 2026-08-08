import Foundation
import WebKit

final class ReaderNativeNoRedirectDelegate:
    NSObject,
    URLSessionTaskDelegate,
    @unchecked Sendable
{
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

/// A narrow native gateway for network-only Reader actions. The local shell
/// never receives Pi cookies or account tokens; Swift attaches the existing
/// WKWebsiteDataStore cookies and returns only the bounded HTTP result.
@MainActor
final class ReaderNativePiGateway: NSObject, WKScriptMessageHandlerWithReply {
    static let messageName = "bwNativePiGateway"

    private static let requestContract = "reader-native-pi-request/1"
    private static let responseContract = "reader-native-pi-response/1"
    private static let maximumRequestBytes = 1 * 1_024 * 1_024
    private static let maximumResponseBytes = 8 * 1_024 * 1_024
    private static let piOrigin = URL(
        string: "https://bwicarus.taile44d0c.ts.net"
    )!

    private weak var webView: WKWebView?
    private let trustedBasePath: String
    private let redirectDelegate: ReaderNativeNoRedirectDelegate
    private let session: URLSession

    init(webView: WKWebView, trustedBaseURL: URL) {
        self.webView = webView
        trustedBasePath = trustedBaseURL.path
        let redirectDelegate = ReaderNativeNoRedirectDelegate()
        self.redirectDelegate = redirectDelegate
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.timeoutIntervalForRequest = 120
        configuration.timeoutIntervalForResource = 180
        session = URLSession(
            configuration: configuration,
            delegate: redirectDelegate,
            delegateQueue: nil
        )
        super.init()
    }

    deinit {
        session.invalidateAndCancel()
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
              isTrustedLocalURL(message.frameInfo.request.url) else {
            replyHandler(nil, "BW_PI_GATEWAY_SOURCE：Pi 网关来源无效")
            return
        }
        guard let request = Self.parse(message.body) else {
            replyHandler(nil, "BW_PI_GATEWAY_REQUEST：Pi 网关请求无效")
            return
        }

        Task { @MainActor [weak self] in
            guard let self else {
                replyHandler(nil, "BW_PI_GATEWAY_UNAVAILABLE：Pi 网关不可用")
                return
            }
            do {
                replyHandler(try await self.perform(request), nil)
            } catch {
                replyHandler(nil, error.localizedDescription)
            }
        }
    }

    private func isTrustedLocalURL(_ url: URL?) -> Bool {
        guard let url else { return false }
        return url.scheme?.lowercased() == "http"
            && url.host?.lowercased() == ReaderLocalRuntimeServer.host
            && url.port == Int(ReaderLocalRuntimeServer.port)
            && url.path.hasPrefix(trustedBasePath)
    }

    private func perform(_ input: NativeRequest) async throws -> [String: Any] {
        guard let target = URL(string: input.path, relativeTo: Self.piOrigin),
              target.scheme == Self.piOrigin.scheme,
              target.host == Self.piOrigin.host,
              target.port == Self.piOrigin.port,
              target.path.hasPrefix("/") else {
            throw GatewayError("BW_PI_GATEWAY_ROUTE：Pi API 地址无效")
        }
        var request = URLRequest(url: target)
        request.httpMethod = input.method
        request.httpBody = input.body.isEmpty ? nil : Data(input.body.utf8)
        request.httpShouldHandleCookies = false
        request.setValue(input.accept, forHTTPHeaderField: "Accept")
        if !input.contentType.isEmpty {
            request.setValue(input.contentType, forHTTPHeaderField: "Content-Type")
        }
        let matchingCookies = Self.cookies(
            for: target,
            from: await allCookies()
        )
        let cookieHeader = HTTPCookie.requestHeaderFields(with: matchingCookies)
        cookieHeader.forEach { request.setValue($1, forHTTPHeaderField: $0) }

        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw GatewayError("BW_PI_GATEWAY_RESPONSE：Pi 网关没有 HTTP 响应")
        }
        guard data.count <= Self.maximumResponseBytes else {
            throw GatewayError("BW_PI_GATEWAY_LIMIT：Pi 响应超过 8 MiB")
        }
        var headers: [String: String] = [:]
        for name in ["Content-Type", "Cache-Control"] {
            if let value = response.value(forHTTPHeaderField: name) {
                headers[name] = value
            }
        }
        return [
            "contract": Self.responseContract,
            "status": response.statusCode,
            "headers": headers,
            "bodyBase64": data.base64EncodedString(),
        ]
    }

    private func allCookies() async -> [HTTPCookie] {
        guard let webView else { return [] }
        return await withCheckedContinuation { continuation in
            webView.configuration.websiteDataStore.httpCookieStore
                .getAllCookies { continuation.resume(returning: $0) }
        }
    }

    private struct NativeRequest {
        let method: String
        let path: String
        let accept: String
        let contentType: String
        let body: String
    }

    private static func parse(_ raw: Any) -> NativeRequest? {
        guard let value = raw as? [String: Any],
              Set(value.keys) == Set([
                "contract", "action", "method", "path", "headers", "body",
              ]),
              value["contract"] as? String == requestContract,
              value["action"] as? String == "fetch",
              let method = value["method"] as? String,
              ["GET", "POST", "PUT", "PATCH", "DELETE"].contains(method),
              let path = value["path"] as? String,
              path.utf8.count <= 4_096,
              path.hasPrefix("/"),
              !path.hasPrefix("//"),
              !path.contains("\\"),
              !path.contains("#"),
              !path.unicodeScalars.contains(where: {
                $0.value < 0x20 || $0.value == 0x7f
              }),
              let canonicalPath = canonicalRequestPath(path),
              isAllowed(path: canonicalPath.path),
              let headers = value["headers"] as? [String: Any],
              Set(headers.keys).isSubset(of: Set(["Accept", "Content-Type"])),
              let body = value["body"] as? String,
              body.utf8.count <= maximumRequestBytes else {
            return nil
        }
        let accept = headers["Accept"] as? String ?? "*/*"
        let contentType = headers["Content-Type"] as? String ?? ""
        guard accept.utf8.count <= 256,
              contentType.utf8.count <= 256,
              !accept.contains("\r"), !accept.contains("\n"),
              !contentType.contains("\r"), !contentType.contains("\n") else {
            return nil
        }
        return NativeRequest(
            method: method,
            path: canonicalPath.requestTarget,
            accept: accept,
            contentType: contentType,
            body: body
        )
    }

    private struct CanonicalRequestPath {
        let path: String
        let requestTarget: String
    }

    /// Keeps the native gateway on an exact path allowlist. URL(relativeTo:)
    /// is intentionally never given dot segments or encoded path separators:
    /// either form could otherwise normalize into a different Pi endpoint
    /// after this process has already made its authorization decision.
    private static func canonicalRequestPath(
        _ raw: String
    ) -> CanonicalRequestPath? {
        guard let components = URLComponents(string: raw),
              components.scheme == nil,
              components.host == nil,
              components.fragment == nil else {
            return nil
        }
        let encodedPath = components.percentEncodedPath
        let loweredEncodedPath = encodedPath.lowercased()
        let forbiddenEncodedTokens = ["%2e", "%2f", "%5c"]
        guard !forbiddenEncodedTokens.contains(where: {
            loweredEncodedPath.contains($0)
        }) else {
            return nil
        }
        let segments = components.path.split(
            separator: "/",
            omittingEmptySubsequences: false
        )
        guard segments.first?.isEmpty == true,
              segments.count > 1,
              segments.dropFirst().allSatisfy({ segment in
                  !segment.isEmpty && segment != "." && segment != ".."
              }) else {
            return nil
        }
        let requestTarget = encodedPath
            + (components.percentEncodedQuery.map { "?\($0)" } ?? "")
        return CanonicalRequestPath(
            path: components.path,
            requestTarget: requestTarget
        )
    }

    private static func isAllowed(path: String) -> Bool {
        let exact = Set([
            "/pdf/api/dict", "/pdf/api/dict-jp", "/pdf/api/dict-jp-ai",
            "/pdf/api/dict-jp-zh", "/pdf/api/dict-quick",
            "/pdf/api/translate", "/pdf/api/translate-sentence",
            "/pdf/api/translate-config", "/pdf/api/explain",
            "/pdf/api/ai-stream-result", "/pdf/api/snippets-to-async",
            "/pdf/api/job-status", "/pdf/api/grammar-analyze",
            "/pdf/api/grammar-stream", "/pdf/api/grammar-tracked",
            "/pdf/api/grammar-books", "/pdf/api/grammar-history",
            "/pdf/api/grammar-history-save", "/pdf/api/grammar-forget",
            "/pdf/api/vocab-anki", "/pdf/api/vocab-mark",
            "/pdf/api/vocab-mastery-map", "/pdf/api/vocab-list",
            "/pdf/api/vocab-audio", "/pdf/api/jp-vocab-mark",
            "/pdf/api/phrase-mark", "/pdf/api/phrases",
            "/pdf/api/assistant", "/pdf/api/epub-assistant",
            "/pdf/api/epub-convo", "/pdf/api/anki-add-cards",
            "/pdf/api/review-queue", "/pdf/api/review-answer",
        ])
        if exact.contains(path) { return true }
        let segmentPrefixes = [
            "/pdf/api/entity/", "/pdf/api/epub-convo/",
            "/api/assistant/",
        ]
        return segmentPrefixes.contains { prefix in
            path.hasPrefix(prefix) && path.count > prefix.count
        }
    }

    private static func cookies(
        for requestURL: URL,
        from cookies: [HTTPCookie]
    ) -> [HTTPCookie] {
        guard let host = requestURL.host?.lowercased() else { return [] }
        let requestPath = requestURL.path.isEmpty ? "/" : requestURL.path
        let now = Date()
        let eligible = cookies.filter { cookie in
            let domain = cookie.domain.lowercased().trimmingCharacters(
                in: CharacterSet(charactersIn: ".")
            )
            let domainMatches = host == domain || host.hasSuffix(".\(domain)")
            let cookiePath = cookie.path.isEmpty ? "/" : cookie.path
            let pathMatches = requestPath == cookiePath
                || (requestPath.hasPrefix(cookiePath)
                    && (cookiePath.hasSuffix("/")
                        || requestPath.dropFirst(cookiePath.count).first == "/"))
            let expiryMatches = cookie.expiresDate.map { $0 > now } ?? true
            let secureMatches = !cookie.isSecure
                || requestURL.scheme?.lowercased() == "https"
            return !domain.isEmpty
                && domainMatches
                && pathMatches
                && expiryMatches
                && secureMatches
        }
        var preferredByName: [String: HTTPCookie] = [:]
        for cookie in eligible {
            if let current = preferredByName[cookie.name],
               current.path.count >= cookie.path.count {
                continue
            }
            preferredByName[cookie.name] = cookie
        }
        return Array(preferredByName.values)
    }

    private struct GatewayError: LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }
}
