import Foundation
import WebKit

/// A book identity may cross the native-to-Pi boundary only after the App has
/// compared the exact local bytes with the current Pi catalog entry. Both
/// digests are retained so the gateway can fail closed even if a caller
/// accidentally constructs an unchecked binding in the future.
struct ReaderNativeRemoteBookBinding: Equatable, Sendable {
    let localLibraryID: String
    let localBookID: String
    let remoteBookID: String
    let localContentSHA256: String
    let remoteContentSHA256: String
    let remoteRelativePath: String

    var localFileIdentity: String { "localbook:\(localBookID)" }
}

/// A narrow native gateway for network-only Reader actions. The local shell
/// never receives Pi cookies or account tokens. Swift authorizes and rewrites
/// the request, then returns a one-use loopback URL whose body is streamed by
/// ReaderLocalRuntimeServer as a normal Fetch Response/ReadableStream.
@MainActor
final class ReaderNativePiGateway: NSObject, WKScriptMessageHandlerWithReply {
    static let messageName = "bwNativePiGateway"
    static let piHost = "bwicarus-2.taile44d0c.ts.net"

    private static let requestContract = "reader-native-pi-request/2"
    private static let responseContract = "reader-native-pi-response/2"
    private static let maximumRequestBytes = 8 * 1_024 * 1_024
    private static let maximumBase64RequestCharacters =
        ((maximumRequestBytes + 2) / 3) * 4
    private static let piOrigin = URL(
        string: "https://\(piHost)"
    )!

    private weak var webView: WKWebView?
    private let trustedBasePath: String
    private let trustedBaseURL: URL
    private let piProxyBroker: ReaderNativePiProxyBroker
    private let interfaceManifest: ReaderNativeInterfaceManifest?
    private let interfaceManifestError: String?
    private var currentRemoteBookBinding: ReaderNativeRemoteBookBinding?
    private var catalogRemoteBookBindings: [ReaderNativeRemoteBookBinding] = []
    private var scopeEpoch: UInt64 = 0
    private var continuations: [String: RemoteContinuation] = [:]

    init(
        webView: WKWebView,
        trustedBaseURL: URL,
        piProxyBroker: ReaderNativePiProxyBroker
    ) {
        self.webView = webView
        self.trustedBaseURL = trustedBaseURL
        self.piProxyBroker = piProxyBroker
        trustedBasePath = trustedBaseURL.path
        do {
            interfaceManifest = try ReaderNativeInterfaceManifest()
            interfaceManifestError = nil
        } catch {
            interfaceManifest = nil
            interfaceManifestError = "原生 Reader 接口清单不可用"
        }
        super.init()
        piProxyBroker.installResourceRequestPreparer { [weak self] request in
            guard let self else {
                throw GatewayError("BW_PI_GATEWAY_UNAVAILABLE：Pi 网关不可用")
            }
            return try await self.prepareAuthorizedResource(request)
        }
    }

    func updateTrustedRemoteBookBinding(
        _ binding: ReaderNativeRemoteBookBinding?
    ) {
        updateTrustedRemoteBookBindings(
            current: binding,
            catalog: binding.map { [$0] } ?? []
        )
    }

    /// The catalog set is intentionally explicit and digest-verified by the
    /// native library coordinator. It enables cross-book favorite resources
    /// without ever accepting an arbitrary Pi path supplied by JavaScript.
    func updateTrustedRemoteBookBindings(
        current: ReaderNativeRemoteBookBinding?,
        catalog: [ReaderNativeRemoteBookBinding]
    ) {
        let sanitizedCurrent = current.flatMap {
            Self.isValidRemoteBookBinding($0) ? $0 : nil
        }
        var sanitizedCatalog = Self.sanitizedBindings(catalog)
        if let sanitizedCurrent,
           !sanitizedCatalog.contains(sanitizedCurrent) {
            sanitizedCatalog.append(sanitizedCurrent)
            sanitizedCatalog.sort { $0.remoteBookID < $1.remoteBookID }
        }
        guard sanitizedCurrent != currentRemoteBookBinding
                || sanitizedCatalog != catalogRemoteBookBindings else {
            return
        }
        currentRemoteBookBinding = sanitizedCurrent
        catalogRemoteBookBindings = sanitizedCatalog
        scopeEpoch &+= 1
        continuations.removeAll(keepingCapacity: false)
        piProxyBroker.rotateScope(to: scopeEpoch)
    }

    deinit {
        piProxyBroker.cancelAll()
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
        guard let surface = nativeSurface(
            for: message.frameInfo.request.url
        ) else {
            replyHandler(nil, "BW_PI_GATEWAY_SOURCE：原生 Reader 类型无效")
            return
        }
        guard interfaceManifest != nil else {
            replyHandler(
                nil,
                "BW_PI_GATEWAY_MANIFEST：\(interfaceManifestError ?? "接口清单不可用")"
            )
            return
        }
        guard let routePolicy = interfaceManifest?.piRoutePolicy(
            path: request.routePath,
            method: request.method,
            surface: surface
        ) else {
            replyHandler(nil, "BW_PI_GATEWAY_ROUTE：接口未由 Pi 网关认领")
            return
        }

        let authorization: AuthorizedNativeRequest
        do {
            authorization = try authorize(
                request,
                remoteBookPolicy: routePolicy.remoteBook
            )
        } catch {
            replyHandler(nil, error.localizedDescription)
            return
        }
        let authorizedEpoch = scopeEpoch

        Task { @MainActor [weak self] in
            guard let self else {
                replyHandler(nil, "BW_PI_GATEWAY_UNAVAILABLE：Pi 网关不可用")
                return
            }
            do {
                guard authorizedEpoch == self.scopeEpoch else {
                    throw GatewayError(
                        "BW_PI_GATEWAY_REMOTE_BOOK：阅读书籍已经切换，请重试"
                    )
                }
                let prepared = try await self.prepareProxyRequest(
                    authorization.request,
                    authorizedEpoch: authorizedEpoch
                )
                let ticket = try self.piProxyBroker.issueAuthorizedRequest(
                    prepared
                )
                guard authorizedEpoch == self.scopeEpoch else {
                    self.piProxyBroker.rotateScope(to: self.scopeEpoch)
                    throw GatewayError(
                        "BW_PI_GATEWAY_REMOTE_BOOK：阅读书籍已经切换，请重试"
                    )
                }
                if let rid = authorization.registersContinuationRID {
                    self.registerContinuation(
                        rid: rid,
                        routePath: authorization.request.routePath,
                        epoch: authorizedEpoch
                    )
                }
                let streamURL = self.trustedBaseURL
                    .appendingPathComponent(
                        ReaderNativePiProxyBroker.routeComponent,
                        isDirectory: true
                    )
                    .appendingPathComponent(ticket, isDirectory: false)
                replyHandler([
                    "contract": Self.responseContract,
                    "streamURL": streamURL.absoluteString,
                ], nil)
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

    private func nativeSurface(
        for url: URL?
    ) -> ReaderNativeInterfaceSurface? {
        guard isTrustedLocalURL(url), let path = url?.path else { return nil }
        if path.hasSuffix("/shells/pdf.html") { return .pdf }
        if path.hasSuffix("/shells/epub.html") { return .epub }
        return nil
    }

    private func prepareProxyRequest(
        _ input: NativeRequest,
        authorizedEpoch: UInt64,
        range: String? = nil
    ) async throws -> ReaderNativePiPreparedProxyRequest {
        guard authorizedEpoch == scopeEpoch,
              let target = URL(string: input.path, relativeTo: Self.piOrigin),
              target.scheme == Self.piOrigin.scheme,
              target.host == Self.piOrigin.host,
              target.port == Self.piOrigin.port,
              target.path.hasPrefix("/") else {
            throw GatewayError("BW_PI_GATEWAY_ROUTE：Pi API 地址无效")
        }
        var request = URLRequest(url: target)
        request.httpMethod = input.method
        request.httpBody = input.body.isEmpty ? nil : input.body
        request.httpShouldHandleCookies = false
        request.setValue(input.accept, forHTTPHeaderField: "Accept")
        request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        if !input.contentType.isEmpty {
            request.setValue(input.contentType, forHTTPHeaderField: "Content-Type")
        }
        if let range {
            guard Self.isSafeHeaderValue(range, maximumBytes: 256),
                  range.lowercased().hasPrefix("bytes=") else {
                throw GatewayError("BW_PI_GATEWAY_REQUEST：Range 请求头无效")
            }
            request.setValue(range, forHTTPHeaderField: "Range")
        }
        let matchingCookies = Self.cookies(
            for: target,
            from: await allCookies()
        )
        guard authorizedEpoch == scopeEpoch else {
            throw GatewayError(
                "BW_PI_GATEWAY_REMOTE_BOOK：阅读书籍已经切换，请重试"
            )
        }
        let cookieHeader = HTTPCookie.requestHeaderFields(with: matchingCookies)
        cookieHeader.forEach { request.setValue($1, forHTTPHeaderField: $0) }
        return ReaderNativePiPreparedProxyRequest(
            request: request,
            scopeEpoch: authorizedEpoch
        )
    }

    private func prepareAuthorizedResource(
        _ resource: ReaderNativePiResourceProxyRequest
    ) async throws -> ReaderNativePiPreparedProxyRequest {
        guard Self.isSafeHeaderValue(resource.accept, maximumBytes: 256),
              let canonical = Self.canonicalRequestPath(resource.requestTarget),
              let routePolicy = interfaceManifest?.piRoutePolicy(
                path: canonical.path,
                method: "GET",
                surface: resource.surface
              ) else {
            throw GatewayError(
                "BW_PI_GATEWAY_ROUTE：资源接口未由 Pi 网关认领"
            )
        }
        let authorizedTarget = try Self.forceAssetProxyMode(canonical)
        let request = NativeRequest(
            method: "GET",
            routePath: canonical.path,
            path: authorizedTarget,
            accept: resource.accept,
            contentType: "",
            body: Data()
        )
        let authorization = try authorize(
            request,
            remoteBookPolicy: routePolicy.remoteBook
        )
        let authorizedEpoch = scopeEpoch
        return try await prepareProxyRequest(
            authorization.request,
            authorizedEpoch: authorizedEpoch,
            range: resource.range
        )
    }

    /// Pi's asset endpoint may otherwise redirect to an external origin. The
    /// native direct-resource path always asks Pi to relay the bytes itself so
    /// the already-authorized same-origin stream and no-redirect policy remain
    /// authoritative. Caller-supplied query items are intentionally discarded.
    private static func forceAssetProxyMode(
        _ canonical: CanonicalRequestPath
    ) throws -> String {
        guard canonical.path.hasPrefix("/pdf/api/asset/") else {
            return canonical.requestTarget
        }
        guard var components = URLComponents(string: canonical.requestTarget)
        else {
            throw GatewayError("BW_PI_GATEWAY_ROUTE：资源 API 地址无效")
        }
        components.queryItems = [URLQueryItem(name: "proxy", value: "1")]
        let candidate = components.percentEncodedPath
            + (components.percentEncodedQuery.map { "?\($0)" } ?? "")
        guard let forced = canonicalRequestPath(candidate),
              forced.path == canonical.path else {
            throw GatewayError("BW_PI_GATEWAY_ROUTE：资源 API 地址无效")
        }
        return forced.requestTarget
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
        let routePath: String
        let path: String
        let accept: String
        let contentType: String
        let body: Data
    }

    private struct AuthorizedNativeRequest {
        let request: NativeRequest
        let registersContinuationRID: String?
    }

    private struct RemoteContinuation {
        let routePath: String
        let epoch: UInt64
        let createdAt: Date
    }

    private func authorize(
        _ input: NativeRequest,
        remoteBookPolicy policy: ReaderNativeRemoteBookPolicy?
    ) throws -> AuthorizedNativeRequest {
        guard let policy else {
            return AuthorizedNativeRequest(
                request: input,
                registersContinuationRID: nil
            )
        }
        let bindings: [ReaderNativeRemoteBookBinding]
        switch policy.scope {
        case .current:
            bindings = currentRemoteBookBinding.map { [$0] } ?? []
        case .catalog:
            bindings = catalogRemoteBookBindings
        }

        var components = URLComponents(string: input.path)
        guard components != nil else {
            throw GatewayError("BW_PI_GATEWAY_ROUTE：Pi API 地址无效")
        }
        var jsonRoot: Any?
        var parsedJSON = false
        var jsonChanged = false
        var matchedBinding: ReaderNativeRemoteBookBinding?

        func record(_ binding: ReaderNativeRemoteBookBinding) throws {
            if let matchedBinding,
               matchedBinding.remoteBookID != binding.remoteBookID {
                throw GatewayError(
                    "BW_PI_GATEWAY_REMOTE_BOOK：一次请求不能混用多本书籍身份"
                )
            }
            matchedBinding = binding
        }

        func ensureJSON() throws -> Any {
            if parsedJSON {
                guard let jsonRoot else {
                    throw GatewayError(
                        "BW_PI_GATEWAY_REMOTE_BOOK：JSON 请求无效"
                    )
                }
                return jsonRoot
            }
            parsedJSON = true
            guard Self.isJSONContentType(input.contentType),
                  !input.body.isEmpty,
                  let decoded = try? JSONSerialization.jsonObject(
                    with: input.body
                  ) else {
                throw GatewayError(
                    "BW_PI_GATEWAY_REMOTE_BOOK：含书籍身份的请求体必须是 JSON"
                )
            }
            jsonRoot = decoded
            return decoded
        }

        let identityRequired = policy.mode == .required
            || policy.requiredMethods.contains(input.method)
        let mayOmitUnboundLocalIdentity = !identityRequired

        for identity in policy.identities where identity.methods.contains(input.method) {
            switch identity.location {
            case .query:
                guard var queryItems = components?.queryItems else { continue }
                let name = String(identity.pointer.dropFirst())
                var found = false
                var rewrittenItems: [URLQueryItem] = []
                rewrittenItems.reserveCapacity(queryItems.count)
                for item in queryItems {
                    guard item.name == name else {
                        rewrittenItems.append(item)
                        continue
                    }
                    found = true
                    guard let value = item.value else {
                        throw GatewayError(
                            "BW_PI_GATEWAY_REMOTE_BOOK：书籍身份字段无效"
                        )
                    }
                    let rewritten: RewrittenIdentity
                    do {
                        rewritten = try Self.rewriteIdentity(
                            value,
                            transform: identity.transform,
                            bindings: bindings
                        )
                    } catch {
                        guard mayOmitUnboundLocalIdentity,
                              Self.isStrictLocalBookIdentity(
                                value,
                                transform: identity.transform
                              ) else {
                            throw error
                        }
                        // Conditional routes may work entirely from supplied
                        // text. Never forward an App-only identity that Pi
                        // cannot resolve; omission is safer than inventing a
                        // remote path.
                        continue
                    }
                    try record(rewritten.binding)
                    rewrittenItems.append(URLQueryItem(
                        name: name,
                        value: rewritten.value
                    ))
                }
                if found {
                    components?.queryItems = rewrittenItems.isEmpty
                        ? nil : rewrittenItems
                }
            case .json:
                var root = try ensureJSON()
                guard let rawIdentity = Self.value(
                    at: identity.pointer,
                    in: root
                ) else { continue }
                guard let stringIdentity = rawIdentity as? String else {
                    throw GatewayError(
                        "BW_PI_GATEWAY_REMOTE_BOOK：书籍身份字段必须是字符串"
                    )
                }
                let rewritten: RewrittenIdentity?
                do {
                    rewritten = try Self.rewriteJSONPointer(
                        identity.pointer,
                        in: &root,
                        transform: identity.transform,
                        bindings: bindings
                    )
                } catch {
                    guard mayOmitUnboundLocalIdentity,
                          Self.isStrictLocalBookIdentity(
                            stringIdentity,
                            transform: identity.transform
                          ), Self.removeJSONPointer(
                            identity.pointer,
                            from: &root
                          ) else {
                        throw error
                    }
                    jsonChanged = true
                    jsonRoot = root
                    continue
                }
                if let rewritten {
                    try record(rewritten.binding)
                    jsonChanged = jsonChanged || rewritten.changed
                    jsonRoot = root
                }
            }
        }

        var continuationRID: String?
        var continuationWasAuthorized = false
        if let continuation = policy.continuation {
            let root = try ensureJSON()
            if let rid = Self.stringValue(
                at: continuation.pointer,
                in: root
            ), Self.isSafeRID(rid) {
                if matchedBinding != nil {
                    continuationRID = rid
                } else if Self.isValidContinuationOffset(
                    Self.value(at: continuation.fromPointer, in: root)
                ), let registered = continuations[rid],
                   registered.routePath == input.routePath,
                   registered.epoch == scopeEpoch {
                    continuationWasAuthorized = true
                }
            }
        }

        guard !identityRequired
                || matchedBinding != nil
                || continuationWasAuthorized else {
            let detail = bindings.isEmpty
                ? "这本本机书尚未关联 Pi 的同摘要书体；请先在书库同步书体到 Pi，再选择 Pi 预处理"
                : "请求未提供可验证的书籍身份或续传凭据"
            throw GatewayError("BW_PI_GATEWAY_REMOTE_BOOK：\(detail)")
        }

        var rewrittenPath = input.path
        if let components {
            let candidate = components.percentEncodedPath
                + (components.percentEncodedQuery.map { "?\($0)" } ?? "")
            guard let canonical = Self.canonicalRequestPath(candidate),
                  canonical.path == input.routePath else {
                throw GatewayError(
                    "BW_PI_GATEWAY_ROUTE：Pi API 地址改写后无效"
                )
            }
            rewrittenPath = canonical.requestTarget
        }

        var rewrittenBody = input.body
        if jsonChanged, let jsonRoot {
            let encoded = try JSONSerialization.data(withJSONObject: jsonRoot)
            guard encoded.count <= Self.maximumRequestBytes else {
                throw GatewayError(
                    "BW_PI_GATEWAY_LIMIT：Pi 请求改写后超过限制"
                )
            }
            rewrittenBody = encoded
        }
        return AuthorizedNativeRequest(
            request: NativeRequest(
                method: input.method,
                routePath: input.routePath,
                path: rewrittenPath,
                accept: input.accept,
                contentType: input.contentType,
                body: rewrittenBody
            ),
            registersContinuationRID: continuationRID
        )
    }

    private func registerContinuation(
        rid: String,
        routePath: String,
        epoch: UInt64
    ) {
        let cutoff = Date().addingTimeInterval(-10 * 60)
        continuations = continuations.filter {
            $0.value.epoch == scopeEpoch && $0.value.createdAt > cutoff
        }
        if continuations.count >= 256,
           let oldest = continuations.min(by: {
               $0.value.createdAt < $1.value.createdAt
           })?.key {
            continuations.removeValue(forKey: oldest)
        }
        continuations[rid] = RemoteContinuation(
            routePath: routePath,
            epoch: epoch,
            createdAt: Date()
        )
    }

    private struct RewrittenIdentity {
        let value: String
        let binding: ReaderNativeRemoteBookBinding
        let changed: Bool
    }

    private static func rewriteIdentity(
        _ value: String,
        transform: ReaderNativeRemoteBookIdentityTransform,
        bindings: [ReaderNativeRemoteBookBinding]
    ) throws -> RewrittenIdentity {
        let prefix: String
        let suffix: String
        switch transform {
        case .exact:
            prefix = value
            suffix = ""
        case .prefixBeforeDelimiter:
            guard let delimiter = value.range(of: "::"),
                  delimiter.lowerBound > value.startIndex,
                  delimiter.upperBound < value.endIndex else {
                throw GatewayError(
                    "BW_PI_GATEWAY_REMOTE_BOOK：复合书籍身份格式无效"
                )
            }
            prefix = String(value[..<delimiter.lowerBound])
            suffix = String(value[delimiter.lowerBound...])
        }
        for binding in bindings {
            if prefix == binding.localFileIdentity
                || prefix == binding.localBookID {
                return RewrittenIdentity(
                    value: binding.remoteRelativePath + suffix,
                    binding: binding,
                    changed: true
                )
            }
            if prefix == binding.remoteRelativePath {
                return RewrittenIdentity(
                    value: value,
                    binding: binding,
                    changed: false
                )
            }
        }
        throw GatewayError(
            "BW_PI_GATEWAY_REMOTE_BOOK：请求指向的不是可信本机书"
        )
    }

    private static func rewriteJSONPointer(
        _ pointer: String,
        in root: inout Any,
        transform: ReaderNativeRemoteBookIdentityTransform,
        bindings: [ReaderNativeRemoteBookBinding]
    ) throws -> RewrittenIdentity? {
        let segments = pointer.dropFirst().split(separator: "/").map(String.init)
        return try rewriteJSONSegments(
            ArraySlice(segments),
            in: &root,
            transform: transform,
            bindings: bindings
        )
    }

    private static func rewriteJSONSegments(
        _ segments: ArraySlice<String>,
        in value: inout Any,
        transform: ReaderNativeRemoteBookIdentityTransform,
        bindings: [ReaderNativeRemoteBookBinding]
    ) throws -> RewrittenIdentity? {
        guard let first = segments.first else {
            guard let string = value as? String else {
                throw GatewayError(
                    "BW_PI_GATEWAY_REMOTE_BOOK：书籍身份字段必须是字符串"
                )
            }
            let rewritten = try rewriteIdentity(
                string,
                transform: transform,
                bindings: bindings
            )
            value = rewritten.value
            return rewritten
        }
        let remaining = segments.dropFirst()
        if var object = value as? [String: Any] {
            guard var child = object[first] else { return nil }
            let result = try rewriteJSONSegments(
                remaining,
                in: &child,
                transform: transform,
                bindings: bindings
            )
            if result != nil {
                object[first] = child
                value = object
            }
            return result
        }
        if var array = value as? [Any],
           let index = Int(first), array.indices.contains(index) {
            var child = array[index]
            let result = try rewriteJSONSegments(
                remaining,
                in: &child,
                transform: transform,
                bindings: bindings
            )
            if result != nil {
                array[index] = child
                value = array
            }
            return result
        }
        return nil
    }

    private static func removeJSONPointer(
        _ pointer: String,
        from root: inout Any
    ) -> Bool {
        let segments = pointer.dropFirst().split(separator: "/").map(String.init)
        guard !segments.isEmpty else { return false }
        return removeJSONSegments(ArraySlice(segments), from: &root)
    }

    private static func removeJSONSegments(
        _ segments: ArraySlice<String>,
        from value: inout Any
    ) -> Bool {
        guard let first = segments.first else { return false }
        let remaining = segments.dropFirst()
        if var object = value as? [String: Any] {
            if remaining.isEmpty {
                guard object.removeValue(forKey: first) != nil else {
                    return false
                }
                value = object
                return true
            }
            guard var child = object[first],
                  removeJSONSegments(remaining, from: &child) else {
                return false
            }
            object[first] = child
            value = object
            return true
        }
        if var array = value as? [Any],
           let index = Int(first), array.indices.contains(index) {
            if remaining.isEmpty {
                array.remove(at: index)
                value = array
                return true
            }
            var child = array[index]
            guard removeJSONSegments(remaining, from: &child) else {
                return false
            }
            array[index] = child
            value = array
            return true
        }
        return false
    }

    private static func isStrictLocalBookIdentity(
        _ value: String,
        transform: ReaderNativeRemoteBookIdentityTransform
    ) -> Bool {
        guard transform == .exact else { return false }
        if value.range(
            of: #"^localbook:[a-f0-9]{64}$"#,
            options: .regularExpression
        ) != nil {
            return true
        }
        return value.range(
            of: #"^(?:localbook:)?localbook-[a-f0-9]{64}$"#,
            options: .regularExpression
        ) != nil
    }

    private static func value(at pointer: String, in root: Any) -> Any? {
        let segments = pointer.dropFirst().split(separator: "/").map(String.init)
        var current: Any = root
        for segment in segments {
            if let object = current as? [String: Any],
               let next = object[segment] {
                current = next
            } else if let array = current as? [Any],
                      let index = Int(segment), array.indices.contains(index) {
                current = array[index]
            } else {
                return nil
            }
        }
        return current
    }

    private static func stringValue(at pointer: String, in root: Any) -> String? {
        value(at: pointer, in: root) as? String
    }

    private static func isValidContinuationOffset(_ value: Any?) -> Bool {
        guard let number = value as? NSNumber,
              !(value is Bool),
              number.doubleValue >= 0,
              number.doubleValue.rounded(.towardZero) == number.doubleValue else {
            return false
        }
        return number.doubleValue <= Double(Int.max)
    }

    private static func isSafeRID(_ value: String) -> Bool {
        value.range(
            of: #"^[A-Za-z0-9._:-]{1,160}$"#,
            options: .regularExpression
        ) != nil
    }

    private static func sanitizedBindings(
        _ bindings: [ReaderNativeRemoteBookBinding]
    ) -> [ReaderNativeRemoteBookBinding] {
        var byRemoteID: [String: ReaderNativeRemoteBookBinding] = [:]
        var conflictingRemoteIDs = Set<String>()
        for binding in bindings where isValidRemoteBookBinding(binding) {
            guard !conflictingRemoteIDs.contains(binding.remoteBookID) else {
                continue
            }
            if let existing = byRemoteID[binding.remoteBookID],
               existing != binding {
                // Conflicting catalog claims for one remote identity are all
                // discarded instead of selecting one by array order.
                byRemoteID[binding.remoteBookID] = nil
                conflictingRemoteIDs.insert(binding.remoteBookID)
                continue
            }
            byRemoteID[binding.remoteBookID] = binding
        }
        return byRemoteID.values.sorted { $0.remoteBookID < $1.remoteBookID }
    }

    private static func isSafeHeaderValue(
        _ value: String,
        maximumBytes: Int
    ) -> Bool {
        !value.isEmpty
            && value.utf8.count <= maximumBytes
            && !value.contains("\r")
            && !value.contains("\n")
            && !value.unicodeScalars.contains(where: {
                $0.value < 0x20 && $0.value != 0x09
            })
    }

    private static func isValidRemoteBookBinding(
        _ binding: ReaderNativeRemoteBookBinding
    ) -> Bool {
        guard UUID(uuidString: binding.localLibraryID) != nil,
              binding.localBookID.range(
                of: #"^localbook-[a-f0-9]{64}$"#,
                options: .regularExpression
              ) != nil,
              binding.remoteBookID.range(
                of: #"^book_[a-f0-9]{32}$"#,
                options: .regularExpression
              ) != nil,
              isSHA256(binding.localContentSHA256),
              isSHA256(binding.remoteContentSHA256),
              binding.localContentSHA256.caseInsensitiveCompare(
                binding.remoteContentSHA256
              ) == .orderedSame else {
            return false
        }
        return isSafeRemoteRelativePath(binding.remoteRelativePath)
    }

    private static func isSHA256(_ value: String) -> Bool {
        value.range(
            of: #"^[a-fA-F0-9]{64}$"#,
            options: .regularExpression
        ) != nil
    }

    private static func isJSONContentType(_ value: String) -> Bool {
        let mediaType = value.split(separator: ";", maxSplits: 1)
            .first
            .map(String.init)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        return mediaType == "application/json"
    }

    private static func isSafeRemoteRelativePath(_ value: String) -> Bool {
        guard !value.isEmpty, value.utf8.count <= 2_048,
              !value.hasPrefix("/"), !value.contains("\\"),
              !value.contains("?"), !value.contains("#"),
              !value.unicodeScalars.contains(where: {
                  $0.value < 0x20 || $0.value == 0x7f
              }) else {
            return false
        }
        let segments = value.split(
            separator: "/",
            omittingEmptySubsequences: false
        )
        return !segments.isEmpty && segments.allSatisfy {
            !$0.isEmpty && $0 != "." && $0 != ".."
        }
    }

    private static func parse(_ raw: Any) -> NativeRequest? {
        guard let value = raw as? [String: Any],
              Set(value.keys) == Set([
                "contract", "action", "method", "path", "headers", "body",
                "bodyEncoding",
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
              let headers = value["headers"] as? [String: Any],
              Set(headers.keys).isSubset(of: Set(["Accept", "Content-Type"])),
              let bodyText = value["body"] as? String,
              let bodyEncoding = value["bodyEncoding"] as? String else {
            return nil
        }
        let body: Data
        switch bodyEncoding {
        case "utf8":
            guard let data = bodyText.data(using: .utf8),
                  data.count <= maximumRequestBytes else { return nil }
            body = data
        case "base64":
            guard bodyText.utf8.count <= maximumBase64RequestCharacters,
                  let data = Data(base64Encoded: bodyText, options: []),
                  data.base64EncodedString() == bodyText,
                  data.count <= maximumRequestBytes else { return nil }
            body = data
        default:
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
            routePath: canonicalPath.path,
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
