import CoreFoundation
import CryptoKit
import Foundation
import WebKit

private final class ReaderNativeNoRedirectDelegate: NSObject,
    URLSessionTaskDelegate {
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

/// App-owned sync gateway. Account identity, namespace and owner credentials
/// stay in Swift memory; the local Reader page can only submit sync-v3 payloads
/// and receive the relay's business response.
@MainActor
final class ReaderNativePiSyncBridge: NSObject, WKScriptMessageHandlerWithReply {
    static let messageName = "bwNativePiSync"
    static let loginURL = URL(
        string: "https://bwicarus-2.taile44d0c.ts.net/login"
    )!

    private static let requestContract = "reader-native-pi-sync-request/1"
    private static let responseContract = "reader-native-pi-sync-response/1"
    private static let bootstrapContract = "native-sync-bootstrap/1"
    private static let leaseContract = "owner-lease/1"
    private static let syncContract = "sync-v3"
    private static let changeContract = "record-parent-state/1"
    private static let registryDigest = "sync-v3:record-parent-state/1|card-entities:explicit:0:1|card-states:explicit:0:1|user-settings:explicit:0:1|vocabulary-state:explicit:0:1"
    private static let maximumRequestBytes = 2 * 1_024 * 1_024
    private static let maximumResponseBytes = 4 * 1_024 * 1_024
    private static let piOrigin = URL(
        string: "https://bwicarus-2.taile44d0c.ts.net"
    )!
    private static let familyDefaultsKey = "BWReaderNativeSyncDeviceFamilyV1"

    private weak var webView: WKWebView?
    private let trustedBasePath: String
    private let ownerInstanceID: String
    private let deviceFamilyID: String
    private let redirectDelegate: ReaderNativeNoRedirectDelegate
    private let session: URLSession
    private var lease: Lease?
    private var operationInProgress = false

    init(webView: WKWebView, trustedBaseURL: URL) {
        self.webView = webView
        trustedBasePath = trustedBaseURL.path
        ownerInstanceID = "owner-instance-v1:native:\(Self.randomHex())"
        deviceFamilyID = Self.loadOrCreateDeviceFamilyID()
        let redirectDelegate = ReaderNativeNoRedirectDelegate()
        self.redirectDelegate = redirectDelegate
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.timeoutIntervalForRequest = 45
        configuration.timeoutIntervalForResource = 90
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
            replyHandler(Self.failure(
                requestID: "invalid",
                action: "invalid",
                code: "BW_NATIVE_SYNC_SOURCE",
                message: "同步桥来源无效",
                retryable: false
            ), nil)
            return
        }
        guard let input = Self.parse(message.body) else {
            replyHandler(Self.failure(
                requestID: "invalid",
                action: "invalid",
                code: "BW_NATIVE_SYNC_REQUEST",
                message: "同步桥请求无效",
                retryable: false
            ), nil)
            return
        }

        Task { @MainActor [weak self] in
            guard let self else {
                replyHandler(Self.failure(
                    requestID: input.requestID,
                    action: input.action,
                    code: "BW_NATIVE_SYNC_UNAVAILABLE",
                    message: "同步桥不可用",
                    retryable: true
                ), nil)
                return
            }
            do {
                guard !self.operationInProgress else {
                    throw BridgeError(
                        "BW_NATIVE_SYNC_BUSY",
                        "另一项同步请求仍在处理",
                        true
                    )
                }
                self.operationInProgress = true
                defer { self.operationInProgress = false }
                replyHandler(try await self.perform(input), nil)
            } catch let error as BridgeError {
                replyHandler(Self.failure(
                    requestID: input.requestID,
                    action: input.action,
                    code: error.code,
                    message: error.message,
                    retryable: error.retryable
                ), nil)
            } catch {
                replyHandler(Self.failure(
                    requestID: input.requestID,
                    action: input.action,
                    code: "BW_NATIVE_SYNC_NETWORK",
                    message: "无法连接服务器同步服务",
                    retryable: true
                ), nil)
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

    private func perform(_ input: Input) async throws -> [String: Any] {
        switch input.action {
        case "start":
            return try await start(input)
        case "exchange", "snapshot":
            return try await forward(input)
        case "release":
            return try await release(input)
        default:
            throw BridgeError(
                "BW_NATIVE_SYNC_REQUEST", "同步动作无效", false
            )
        }
    }

    private func start(_ input: Input) async throws -> [String: Any] {
        guard let deviceID = input.deviceID,
              let registryDigest = input.registryDigest else {
            throw BridgeError(
                "BW_NATIVE_SYNC_REQUEST", "同步启动字段不完整", false
            )
        }
        if let current = lease {
            if current.deviceID == deviceID,
               current.registryDigest == registryDigest {
                if current.expiresAt.timeIntervalSinceNow >= 10 {
                    return Self.success(
                        requestID: input.requestID,
                        action: input.action,
                        result: [
                            "state": "ready",
                            "accountBinding": Self.accountBinding(
                                namespace: current.namespace
                            ),
                        ]
                    )
                }
                do {
                    lease = try await renew(current)
                    return Self.success(
                        requestID: input.requestID,
                        action: input.action,
                        result: [
                            "state": "ready",
                            "accountBinding": Self.accountBinding(
                                namespace: current.namespace
                            ),
                        ]
                    )
                } catch {
                    // A stale or uncertain capability must never be reused.
                    // Re-authenticate and claim from a fresh server fence.
                    lease = nil
                }
            } else if current.expiresAt.timeIntervalSinceNow > 0 {
                throw BridgeError(
                    "BW_NATIVE_SYNC_BUSY",
                    "另一项本机同步仍在进行",
                    true
                )
            } else {
                lease = nil
            }
        }

        let bootstrap = try await postJSON(
            path: "/api/reader/sync/native/bootstrap",
            body: [
                "contract": Self.bootstrapContract,
                "requestId": input.requestID,
                "syncContract": Self.syncContract,
                "syncChangeContract": Self.changeContract,
                "registryDigest": registryDigest,
                "deviceFamilyId": deviceFamilyID,
                "ownerInstanceId": ownerInstanceID,
                "deviceId": deviceID,
            ]
        )
        guard bootstrap["contract"] as? String == Self.bootstrapContract,
              bootstrap["requestId"] as? String == input.requestID,
              let namespace = bootstrap["ownerNamespace"] as? String,
              namespace.range(
                of: "^acct-v1-[a-f0-9]{64}$",
                options: .regularExpression
              ) != nil,
              let bootstrapToken = bootstrap["nativeBootstrapToken"] as? String,
              bootstrapToken.range(
                of: "^native-bootstrap-v1-[0-9]{1,12}-[a-f0-9]{64}$",
                options: .regularExpression
              ) != nil else {
            throw BridgeError(
                "BW_NATIVE_SYNC_BOOTSTRAP_RESPONSE",
                "服务器返回了无效的同步身份",
                false
            )
        }

        let claim = try await postJSON(
            path: "/api/reader/sync/owner/claim",
            body: [
                "contract": Self.leaseContract,
                "ownerNamespace": namespace,
                "deviceId": deviceID,
                "deviceFamilyId": deviceFamilyID,
                "ownerRole": "native",
                "ownerInstanceId": ownerInstanceID,
                "syncContract": Self.syncContract,
                "syncChangeContract": Self.changeContract,
                "registryDigest": registryDigest,
                "nativeBootstrapToken": bootstrapToken,
            ]
        )
        lease = try checkedLease(
            claim,
            namespace: namespace,
            deviceID: deviceID,
            registryDigest: registryDigest
        )
        return Self.success(
            requestID: input.requestID,
            action: input.action,
            result: [
                "state": "ready",
                "accountBinding": Self.accountBinding(namespace: namespace),
            ]
        )
    }

    /// Stable checkpoint fence for one authenticated Pi account. This digest
    /// is compare-only: it does not reveal the namespace and grants no owner
    /// capability to the page world.
    private static func accountBinding(namespace: String) -> String {
        let digest = SHA256.hash(
            data: Data(("reader-sync-account-binding/1\0" + namespace).utf8)
        ).map { String(format: "%02x", $0) }.joined()
        return "sha256:" + digest
    }

    private func forward(_ input: Input) async throws -> [String: Any] {
        guard var current = lease,
              current.deviceID == input.deviceID,
              current.registryDigest == input.registryDigest,
              var payload = input.payload else {
            throw BridgeError(
                "BW_NATIVE_SYNC_OWNER_INACTIVE",
                "同步 owner 尚未建立或已经失效",
                false
            )
        }
        if current.expiresAt.timeIntervalSinceNow < 10 {
            current = try await renew(current)
            lease = current
        }
        guard payload["contract"] as? String == "sync-gateway/2",
              payload["deviceId"] as? String == current.deviceID,
              Self.isAllowedPayload(payload, action: input.action) else {
            // 说清是哪一条不合(2026-09-03):切服务器后整批同步被这里拒了,只有一个码,
            // 查了两小时才知道该看哪。原因只进错误消息,不改变拒绝语义。
            let reason: String
            if payload["contract"] as? String != "sync-gateway/2" {
                reason = "contract 不是 sync-gateway/2"
            } else if payload["deviceId"] as? String != current.deviceID {
                reason = "deviceId 与 owner 租约不一致"
            } else {
                reason = Self.payloadRejectReason(payload, action: input.action)
            }
            throw BridgeError(
                "BW_NATIVE_SYNC_PAYLOAD",
                "同步载荷不符合 sync-gateway/2：" + reason,
                false
            )
        }
        payload["ownerNamespace"] = current.namespace
        payload["syncContract"] = Self.syncContract
        payload["syncChangeContract"] = Self.changeContract
        payload["registryDigest"] = current.registryDigest
        payload.merge(current.credentials) { _, new in new }
        let result = try await postJSON(
            path: input.action == "exchange"
                ? "/api/reader/sync/exchange"
                : "/api/reader/sync/snapshot",
            body: payload
        )
        guard result["contract"] as? String == "sync-gateway/2",
              result["ownerNamespace"] == nil,
              result["ownerToken"] == nil,
              result["nativeBootstrapToken"] == nil else {
            throw BridgeError(
                "BW_NATIVE_SYNC_RESPONSE",
                "服务器返回了无效的同步结果",
                false
            )
        }
        return Self.success(
            requestID: input.requestID,
            action: input.action,
            result: result
        )
    }

    private func release(_ input: Input) async throws -> [String: Any] {
        guard let current = lease else {
            return Self.success(
                requestID: input.requestID,
                action: input.action,
                result: ["state": "released", "replayed": true]
            )
        }
        do {
            let result = try await postJSON(
                path: "/api/reader/sync/owner/release",
                body: current.leaseBody
            )
            guard result["released"] as? Bool == true else {
                throw BridgeError(
                    "BW_NATIVE_SYNC_RELEASE_UNKNOWN",
                    "无法确认同步 owner 是否已释放",
                    true
                )
            }
            lease = nil
            return Self.success(
                requestID: input.requestID,
                action: input.action,
                result: [
                    "state": "released",
                    "replayed": result["replayed"] as? Bool ?? false,
                ]
            )
        } catch {
            // Do not retain a possibly stale capability after an unknown
            // release. The server lease expires by itself within 30 seconds.
            lease = nil
            throw error
        }
    }

    private func renew(_ current: Lease) async throws -> Lease {
        let result = try await postJSON(
            path: "/api/reader/sync/owner/renew",
            body: current.leaseBody
        )
        return try checkedLease(
            result,
            namespace: current.namespace,
            deviceID: current.deviceID,
            registryDigest: current.registryDigest
        )
    }

    private func checkedLease(
        _ value: [String: Any],
        namespace: String,
        deviceID: String,
        registryDigest: String
    ) throws -> Lease {
        guard value["contract"] as? String == Self.leaseContract,
              value["deviceFamilyId"] as? String == deviceFamilyID,
              value["ownerRole"] as? String == "native",
              value["ownerInstanceId"] as? String == ownerInstanceID,
              value["deviceId"] as? String == deviceID,
              let generation = value["ownerGeneration"] as? NSNumber,
              generation.int64Value >= 1,
              let token = value["ownerToken"] as? String,
              token.range(
                of: "^owner-token-v1-[A-Za-z0-9_-]{24,256}$",
                options: .regularExpression
              ) != nil,
              let expiresAt = value["expiresAt"] as? NSNumber,
              expiresAt.doubleValue > Date().timeIntervalSince1970 else {
            throw BridgeError(
                "BW_NATIVE_SYNC_OWNER_RESPONSE",
                "服务器返回了无效的 owner lease",
                false
            )
        }
        return Lease(
            namespace: namespace,
            deviceID: deviceID,
            deviceFamilyID: deviceFamilyID,
            ownerInstanceID: ownerInstanceID,
            registryDigest: registryDigest,
            generation: generation.int64Value,
            token: token,
            expiresAt: Date(timeIntervalSince1970: expiresAt.doubleValue)
        )
    }

    private func postJSON(
        path: String,
        body: [String: Any]
    ) async throws -> [String: Any] {
        guard let data = try? JSONSerialization.data(withJSONObject: body),
              data.count <= Self.maximumRequestBytes,
              let target = URL(string: path, relativeTo: Self.piOrigin),
              target.scheme == Self.piOrigin.scheme,
              target.host == Self.piOrigin.host,
              target.port == Self.piOrigin.port else {
            throw BridgeError(
                "BW_NATIVE_SYNC_LIMIT", "同步请求无效或过大", false
            )
        }
        var request = URLRequest(url: target)
        request.httpMethod = "POST"
        request.httpBody = data
        request.httpShouldHandleCookies = false
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let matching = Self.cookies(for: target, from: await allCookies())
        HTTPCookie.requestHeaderFields(with: matching).forEach {
            request.setValue($1, forHTTPHeaderField: $0)
        }

        let (responseData, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse,
              responseData.count <= Self.maximumResponseBytes else {
            throw BridgeError(
                "BW_NATIVE_SYNC_RESPONSE", "服务器同步响应无效或过大", false
            )
        }
        let decoded = try? JSONSerialization.jsonObject(with: responseData)
        let value = decoded as? [String: Any]
        guard (200..<300).contains(http.statusCode),
              value?["ok"] as? Bool == true else {
            let code: String
            if http.statusCode == 401 {
                code = "BW_PI_AUTH_REQUIRED"
            } else {
                code = (value?["code"] as? String).flatMap(Self.safeCode)
                    ?? "BW_NATIVE_SYNC_HTTP"
            }
            throw BridgeError(
                code,
                code == "BW_PI_AUTH_REQUIRED"
                    ? "服务器登录状态无效，请先在 App 中登录"
                    : "服务器同步服务拒绝请求",
                http.statusCode == 408 || http.statusCode == 429
                    || http.statusCode >= 500
            )
        }
        guard let value else {
            throw BridgeError(
                "BW_NATIVE_SYNC_RESPONSE", "服务器同步响应不是 JSON", false
            )
        }
        return value
    }

    private func allCookies() async -> [HTTPCookie] {
        guard let webView else { return [] }
        return await withCheckedContinuation { continuation in
            webView.configuration.websiteDataStore.httpCookieStore
                .getAllCookies { continuation.resume(returning: $0) }
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

    private struct Input {
        let action: String
        let requestID: String
        let deviceID: String?
        let registryDigest: String?
        let payload: [String: Any]?
    }

    private struct Lease {
        let namespace: String
        let deviceID: String
        let deviceFamilyID: String
        let ownerInstanceID: String
        let registryDigest: String
        let generation: Int64
        let token: String
        let expiresAt: Date

        var credentials: [String: Any] {
            [
                "deviceFamilyId": deviceFamilyID,
                "ownerRole": "native",
                "ownerInstanceId": ownerInstanceID,
                "ownerGeneration": generation,
                "ownerToken": token,
            ]
        }

        var leaseBody: [String: Any] {
            let value: [String: Any] = [
                "contract": ReaderNativePiSyncBridge.leaseContract,
                "ownerNamespace": namespace,
                "deviceId": deviceID,
                "deviceFamilyId": deviceFamilyID,
                "ownerRole": "native",
                "ownerInstanceId": ownerInstanceID,
                "ownerGeneration": generation,
                "ownerToken": token,
                "syncContract": ReaderNativePiSyncBridge.syncContract,
                "syncChangeContract": ReaderNativePiSyncBridge.changeContract,
                "registryDigest": registryDigest,
            ]
            return value
        }
    }

    private static func parse(_ raw: Any) -> Input? {
        guard let value = raw as? [String: Any],
              value["contract"] as? String == requestContract,
              let action = value["action"] as? String,
              ["start", "exchange", "snapshot", "release"].contains(action),
              let requestID = value["requestId"] as? String,
              safeName(requestID) else { return nil }
        if action == "release" {
            guard Set(value.keys) == Set(["contract", "action", "requestId"])
            else { return nil }
            return Input(
                action: action,
                requestID: requestID,
                deviceID: nil,
                registryDigest: nil,
                payload: nil
            )
        }
        let baseKeys = Set([
            "contract", "action", "requestId", "deviceId",
            "syncContract", "syncChangeContract", "registryDigest",
        ])
        let expected = action == "start" ? baseKeys : baseKeys.union(["payload"])
        guard Set(value.keys) == expected,
              value["syncContract"] as? String == syncContract,
              value["syncChangeContract"] as? String == changeContract,
              let deviceID = value["deviceId"] as? String,
              safeName(deviceID),
              let digest = value["registryDigest"] as? String,
              digest == registryDigest else { return nil }
        let payload = value["payload"] as? [String: Any]
        if action != "start" && payload == nil { return nil }
        return Input(
            action: action,
            requestID: requestID,
            deviceID: deviceID,
            registryDigest: digest,
            payload: payload
        )
    }

    private static func isAllowedPayload(
        _ payload: [String: Any],
        action: String
    ) -> Bool {
        if action == "exchange" {
            guard Set(payload.keys) == Set([
                "contract", "direction", "deviceId", "cursor", "limit",
                "changes",
            ]),
            let direction = payload["direction"] as? String,
            ["push", "pull"].contains(direction),
            isSafeInteger(payload["cursor"], minimum: 0),
            isSafeInteger(payload["limit"], minimum: 1, maximum: 100),
            let changes = payload["changes"] as? [[String: Any]],
            changes.count <= 100,
            direction != "pull" || changes.isEmpty,
            direction != "push" || changes.allSatisfy(isAllowedChange)
            else { return false }
            return true
        }
        guard action == "snapshot",
              Set(payload.keys) == Set([
                "contract", "deviceId", "snapshotId", "offset", "limit",
              ]),
              let snapshotID = payload["snapshotId"] as? String,
              // 空 snapshotId = 请服务器新建快照(relay 2279 行:空则生成 snap-…)。首次完整对账
              // 必然是空的;此前这里要求非空 → 完整对账在桥这一步就被拒,从未到过服务器(2026-09-03)。
              snapshotID.isEmpty || safeName(snapshotID),
              isSafeInteger(payload["offset"], minimum: 0),
              isSafeInteger(payload["limit"], minimum: 1, maximum: 100)
        else { return false }
        return true
    }

    /// 只在 isAllowedPayload 已判假时调用：逐条复查，返回第一条不合规的描述（含 change 序号/字段名）。
    private static func payloadRejectReason(
        _ payload: [String: Any],
        action: String
    ) -> String {
        if action == "exchange" {
            let expected = Set(["contract", "direction", "deviceId", "cursor", "limit", "changes"])
            if Set(payload.keys) != expected {
                return "exchange 顶层键=" + payload.keys.sorted().joined(separator: ",")
            }
            guard let direction = payload["direction"] as? String, ["push", "pull"].contains(direction) else {
                return "direction 非 push/pull"
            }
            if !isSafeInteger(payload["cursor"], minimum: 0) { return "cursor 不是 ≥0 的安全整数" }
            if !isSafeInteger(payload["limit"], minimum: 1, maximum: 100) { return "limit 不在 1…100" }
            guard let changes = payload["changes"] as? [[String: Any]] else { return "changes 不是对象数组" }
            if changes.count > 100 { return "changes 多于 100 条(\(changes.count))" }
            if direction == "pull", !changes.isEmpty { return "pull 携带了 changes" }
            for (index, change) in changes.enumerated() where !isAllowedChange(change) {
                let collection = (change["collection"] as? String) ?? "?"
                let keys = change.keys.sorted().joined(separator: ",")
                var detail = "change#\(index) collection=\(collection) keys=\(keys)"
                if !requiredChangeKeys.isSubset(of: change.keys) || !Set(change.keys).isSubset(of: allowedChangeKeys) {
                    detail += " 键集合不合规"
                }
                if change["cursor"] != nil, !isSafeInteger(change["cursor"], minimum: 1) { detail += " cursor<1" }
                if let mutationID = change["mutationId"] as? String, !safeName(mutationID) { detail += " mutationId 非法" }
                if let operation = change["operation"] as? String, !["put", "remove"].contains(operation) { detail += " operation=\(operation)" }
                if !allowedCollections.contains(collection) { detail += " collection 不在白名单" }
                if let record = change["record"] as? [String: Any] {
                    let recordKeys = record.keys.sorted().joined(separator: ",")
                    detail += " recordKeys=\(recordKeys)"
                    if let recordID = record["id"] as? String, !safeName(recordID) { detail += " record.id 非法" }
                    if let updatedBy = record["updatedBy"] as? String, !isSafeUpdatedBy(updatedBy) { detail += " updatedBy 非法" }
                    if let value = record["value"] as? [String: Any] {
                        if value["id"] as? String != record["id"] as? String { detail += " value.id≠record.id" }
                        if !isBoundedJSONObject(value, maximumBytes: 512 * 1_024) { detail += " value>512KB" }
                    } else { detail += " value 缺失" }
                    if let causal = record["causal"] as? [String: Any] {
                        if !isAllowedCausalProof(causal) { detail += " causal 不合规" }
                    } else { detail += " causal 缺失" }
                } else { detail += " record 缺失" }
                return detail
            }
            return "未定位到具体字段"
        }
        var detail = "snapshot 载荷不合规：keys=" + payload.keys.sorted().joined(separator: ",")
        if let snapshotID = payload["snapshotId"] as? String, !snapshotID.isEmpty, !safeName(snapshotID) { detail += " snapshotId 非法" }
        if !isSafeInteger(payload["offset"], minimum: 0) { detail += " offset<0" }
        if !isSafeInteger(payload["limit"], minimum: 1, maximum: 100) { detail += " limit 不在 1…100" }
        return detail
    }

    private static let allowedCollections: Set<String> = [
        "card-entities", "card-states",
        "user-settings", "vocabulary-state",
    ]

    /// 变更键集合。`cursor` 可选：完整对账（服务器 resetRequired 后）推送的 snapshot-overlay 变更
    /// 由 sync-coordinator.snapshotChange 合成，天生不带 cursor（relay 的 _normalize_change 也不读它）。
    /// 2026-09-03 换服务器主机后首次走到这条路径，这里曾把 cursor 当必填而整批拒收（BW_NATIVE_SYNC_PAYLOAD）。
    private static let requiredChangeKeys: Set<String> = ["mutationId", "operation", "collection", "record"]
    private static let allowedChangeKeys: Set<String> = requiredChangeKeys.union(["cursor"])

    private static func isAllowedChange(_ change: [String: Any]) -> Bool {
        let keys = Set(change.keys)
        guard requiredChangeKeys.isSubset(of: keys), keys.isSubset(of: allowedChangeKeys),
        change["cursor"] == nil || isSafeInteger(change["cursor"], minimum: 1),
        let mutationID = change["mutationId"] as? String,
        safeName(mutationID),
        let operation = change["operation"] as? String,
        ["put", "remove"].contains(operation),
        let collection = change["collection"] as? String,
        allowedCollections.contains(collection),
        let record = change["record"] as? [String: Any],
        isAllowedRecord(record, collection: collection, operation: operation)
        else { return false }
        return true
    }

    private static func isAllowedRecord(
        _ record: [String: Any],
        collection: String,
        operation: String
    ) -> Bool {
        guard Set(record.keys) == Set([
            "schema", "collection", "id", "rev", "updatedAt", "updatedBy",
            "deleted", "value", "causal",
        ]),
        isSafeInteger(record["schema"], minimum: 1, maximum: 1),
        record["collection"] as? String == collection,
        let recordID = record["id"] as? String,
        safeName(recordID),
        isSafeInteger(record["rev"], minimum: 1),
        isFiniteNonnegativeNumber(record["updatedAt"]),
        let updatedBy = record["updatedBy"] as? String,
        isSafeUpdatedBy(updatedBy),
        let deleted = record["deleted"] as? Bool,
        deleted == (operation == "remove"),
        let value = record["value"] as? [String: Any],
        value["id"] as? String == recordID,
        isBoundedJSONObject(value, maximumBytes: 512 * 1_024),
        let causal = record["causal"] as? [String: Any],
        isAllowedCausalProof(causal)
        else { return false }
        return true
    }

    private static func isAllowedCausalProof(_ proof: [String: Any]) -> Bool {
        guard Set(proof.keys) == Set(["contract", "parent"]),
              proof["contract"] as? String == changeContract else {
            return false
        }
        if proof["parent"] is NSNull { return true }
        guard let parent = proof["parent"] as? [String: Any],
              let deleted = parent["deleted"] as? Bool else { return false }
        let expected = deleted ? Set(["deleted"]) : Set(["deleted", "value"])
        return Set(parent.keys) == expected
            // 与 data-store.js / reader_sync_relay.py 的 MAX_CAUSAL_PARENT_BYTES(512KB)对齐；此前 256KB 偏严
            && isBoundedJSONObject(parent, maximumBytes: 512 * 1_024)
    }

    private static func isSafeInteger(
        _ raw: Any?,
        minimum: Int64,
        maximum: Int64 = 9_007_199_254_740_991
    ) -> Bool {
        guard let number = raw as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else { return false }
        let value = number.doubleValue
        return value.isFinite && value.rounded(.towardZero) == value
            && value >= Double(minimum) && value <= Double(maximum)
    }

    private static func isFiniteNonnegativeNumber(_ raw: Any?) -> Bool {
        guard let number = raw as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else { return false }
        return number.doubleValue.isFinite && number.doubleValue >= 0
    }

    private static func isSafeUpdatedBy(_ value: String) -> Bool {
        value == value.trimmingCharacters(in: .whitespacesAndNewlines)
            && !value.isEmpty && value.utf8.count <= 300
            && value.unicodeScalars.allSatisfy {
                $0.value >= 32 && $0.value != 127
            }
    }

    private static func isBoundedJSONObject(
        _ value: Any,
        maximumBytes: Int
    ) -> Bool {
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(
                withJSONObject: value,
                options: [.sortedKeys]
              ) else { return false }
        return data.count <= maximumBytes
    }

    private static func success(
        requestID: String,
        action: String,
        result: [String: Any]
    ) -> [String: Any] {
        [
            "contract": responseContract,
            "ok": true,
            "requestId": requestID,
            "action": action,
            "result": result,
        ]
    }

    private static func failure(
        requestID: String,
        action: String,
        code: String,
        message: String,
        retryable: Bool
    ) -> [String: Any] {
        [
            "contract": responseContract,
            "ok": false,
            "requestId": requestID,
            "action": action,
            "errorCode": safeCode(code) ?? "BW_NATIVE_SYNC",
            "message": String(message.prefix(240)),
            "retryable": retryable,
        ]
    }

    private static func safeCode(_ value: String) -> String? {
        value.range(
            of: "^[A-Z][A-Z0-9_]{0,79}$",
            options: .regularExpression
        ) == nil ? nil : value
    }

    private static func safeName(_ value: String) -> Bool {
        value.range(
            of: "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            options: .regularExpression
        ) != nil
    }

    private static func randomHex() -> String {
        UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    }

    private static func loadOrCreateDeviceFamilyID() -> String {
        let defaults = UserDefaults.standard
        if let value = defaults.string(forKey: familyDefaultsKey),
           value.range(
             of: "^native-app-v1-[a-f0-9]{32}$",
             options: .regularExpression
           ) != nil {
            return value
        }
        let value = "native-app-v1-\(randomHex())"
        defaults.set(value, forKey: familyDefaultsKey)
        return value
    }

    private struct BridgeError: Error {
        let code: String
        let message: String
        let retryable: Bool
        init(_ code: String, _ message: String, _ retryable: Bool) {
            self.code = code
            self.message = message
            self.retryable = retryable
        }
    }
}
