import CoreFoundation
import Foundation
import WebKit

enum ReaderBookUserStateWebAdapterError: LocalizedError {
    case unavailable
    case untrustedDocument
    case contextChanged
    case invalidRequest
    case invalidResponse

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "本机书籍数据桥尚未准备好"
        case .untrustedDocument:
            return "本机书籍数据桥拒绝了非本机阅读页"
        case .contextChanged:
            return "读取期间已切换到另一本书，请重试"
        case .invalidRequest:
            return "本机书籍数据事务无效"
        case .invalidResponse:
            return "本机书籍数据桥返回了无法验证的结果"
        }
    }
}

/// Main-frame-only adapter for the App-owned IndexedDB transaction API. The
/// page implementation is supplied by native-local-runtime; this class never
/// falls back to a network/Pi write when that local API is unavailable.
@MainActor
final class ReaderBookUserStateWebAdapter: ReaderBookUserStateAtomicApplying {
    static let requestContract = "reader-book-user-state-web-request/1"
    static let responseContract = "reader-book-user-state-web-response/1"

    private weak var webView: WKWebView?
    private var trustedBaseURL: URL
    private var localBookId: String
    private var contextGeneration: UInt64 = 1

    init(
        webView: WKWebView,
        trustedBaseURL: URL,
        localBookId: String
    ) throws {
        guard Self.validTrustedBaseURL(trustedBaseURL),
              Self.validLocalBookId(localBookId) else {
            throw ReaderBookUserStateWebAdapterError.invalidRequest
        }
        self.webView = webView
        self.trustedBaseURL = trustedBaseURL
        self.localBookId = localBookId
    }

    func updateTrustedContext(
        baseURL: URL,
        localBookId: String
    ) throws {
        guard Self.validTrustedBaseURL(baseURL),
              Self.validLocalBookId(localBookId) else {
            throw ReaderBookUserStateWebAdapterError.invalidRequest
        }
        trustedBaseURL = baseURL
        self.localBookId = localBookId
        contextGeneration &+= 1
    }

    func snapshotHeaders(
        localBookId: String
    ) async throws -> [ReaderBookUserStateDomainName: ReaderBookUserStateDomainHeader] {
        guard localBookId == self.localBookId else {
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        let requestId = Self.requestId()
        let request: [String: Any] = [
            "contract": Self.requestContract,
            "action": "snapshot-headers",
            "requestId": requestId,
            "localBookId": localBookId,
        ]
        let raw = try await call(
            method: "snapshotHeaders",
            request: request
        )
        return try Self.parseSnapshotResponse(
            raw,
            requestId: requestId,
            localBookId: localBookId
        )
    }

    func applyAtomically(
        _ transaction: ReaderBookUserStateImportTransaction
    ) async throws -> ReaderBookUserStateImportReceipt {
        try Self.validate(transaction, expectedLocalBookId: localBookId)
        let requestId = Self.requestId()
        let transactionObject = try Self.jsonObject(transaction)
        let request: [String: Any] = [
            "contract": Self.requestContract,
            "action": "apply-atomically",
            "requestId": requestId,
            "localBookId": localBookId,
            "transaction": transactionObject,
        ]
        let raw = try await call(
            method: "applyAtomically",
            request: request
        )
        return try Self.parseApplyResponse(
            raw,
            requestId: requestId,
            transaction: transaction
        )
    }

    private func call(
        method: String,
        request: [String: Any]
    ) async throws -> Any {
        guard method == "snapshotHeaders" || method == "applyAtomically",
              let webView else {
            throw ReaderBookUserStateWebAdapterError.unavailable
        }
        guard let initialURL = webView.url,
              isTrusted(initialURL) else {
            throw ReaderBookUserStateWebAdapterError.untrustedDocument
        }
        let generation = contextGeneration
        let expectedScheme = trustedBaseURL.scheme!.lowercased()
        let expectedHost = trustedBaseURL.host!.lowercased()
        let expectedPort = Self.effectivePort(trustedBaseURL)!
        let expectedBasePath = trustedBaseURL.path

        let result = try await webView.callAsyncJavaScript(
            """
            if (window.top !== window) {
              throw new Error("BW_USER_STATE_NOT_MAIN_FRAME");
            }
            const trusted = () =>
              location.protocol.toLowerCase() === expectedScheme + ":" &&
              location.hostname.toLowerCase() === expectedHost &&
              Number(location.port || (expectedScheme === "https" ? 443 : 80))
                === expectedPort &&
              location.pathname.startsWith(expectedBasePath);
            if (!trusted()) throw new Error("BW_USER_STATE_ORIGIN_CHANGED");
            const api = window.BWReaderRuntime?.nativeLocalRuntime?.bookUserState;
            if (!api || typeof api[method] !== "function") {
              throw new Error("BW_USER_STATE_API_UNAVAILABLE");
            }
            const result = await api[method](request);
            if (!trusted() || window.top !== window) {
              throw new Error("BW_USER_STATE_ORIGIN_CHANGED");
            }
            return result;
            """,
            arguments: [
                "method": method,
                "request": request,
                "expectedScheme": expectedScheme,
                "expectedHost": expectedHost,
                "expectedPort": expectedPort,
                "expectedBasePath": expectedBasePath,
            ],
            in: nil,
            contentWorld: .page
        )

        guard generation == contextGeneration,
              self.localBookId == request["localBookId"] as? String,
              webView.url == initialURL,
              isTrusted(webView.url) else {
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        return result
    }

    private func isTrusted(_ url: URL?) -> Bool {
        guard let url,
              url.scheme?.lowercased() == trustedBaseURL.scheme?.lowercased(),
              url.host?.lowercased() == trustedBaseURL.host?.lowercased(),
              Self.effectivePort(url) == Self.effectivePort(trustedBaseURL) else {
            return false
        }
        return url.path.hasPrefix(trustedBaseURL.path)
    }

    private static func parseSnapshotResponse(
        _ value: Any,
        requestId: String,
        localBookId: String
    ) throws -> [ReaderBookUserStateDomainName: ReaderBookUserStateDomainHeader] {
        guard let response = value as? [String: Any],
              Set(response.keys) == Set([
                "contract", "action", "requestId", "ok", "localBookId",
                "headers",
              ]),
              response["contract"] as? String == responseContract,
              response["action"] as? String == "snapshot-headers",
              response["requestId"] as? String == requestId,
              response["localBookId"] as? String == localBookId,
              response["ok"] as? Bool == true,
              let values = response["headers"] as? [[String: Any]],
              values.count == ReaderBookUserStateDomainName.allCases.count else {
            throw ReaderBookUserStateWebAdapterError.invalidResponse
        }
        var result: [ReaderBookUserStateDomainName: ReaderBookUserStateDomainHeader] = [:]
        for value in values {
            guard Set(value.keys) == Set([
                "name", "digest", "revision", "empty",
            ]),
                  let nameRaw = value["name"] as? String,
                  let name = ReaderBookUserStateDomainName(rawValue: nameRaw),
                  result[name] == nil,
                  let digest = value["digest"] as? String,
                  Self.isSHA256(digest),
                  let revision = Self.strictInteger(value["revision"]),
                  (0...ReaderBookUserStatePackageCodec.maximumRevision)
                    .contains(revision),
                  let empty = value["empty"] as? Bool else {
                throw ReaderBookUserStateWebAdapterError.invalidResponse
            }
            result[name] = ReaderBookUserStateDomainHeader(
                digest: digest,
                revision: revision,
                empty: empty
            )
        }
        guard Set(result.keys) == Set(ReaderBookUserStateDomainName.allCases) else {
            throw ReaderBookUserStateWebAdapterError.invalidResponse
        }
        return result
    }

    private static func parseApplyResponse(
        _ value: Any,
        requestId: String,
        transaction: ReaderBookUserStateImportTransaction
    ) throws -> ReaderBookUserStateImportReceipt {
        guard let response = value as? [String: Any],
              Set(response.keys) == Set([
                "contract", "action", "requestId", "ok", "localBookId",
                "receipt",
              ]),
              response["contract"] as? String == responseContract,
              response["action"] as? String == "apply-atomically",
              response["requestId"] as? String == requestId,
              response["localBookId"] as? String == transaction.localBookId,
              response["ok"] as? Bool == true,
              let receipt = response["receipt"] as? [String: Any],
              Set(receipt.keys) == Set([
                "contract", "transactionId", "committed", "domainDigests",
              ]),
              receipt["contract"] as? String
                == ReaderBookUserStateImportReceipt.currentContract,
              receipt["transactionId"] as? String == transaction.transactionId,
              receipt["committed"] as? Bool == true,
              let digests = receipt["domainDigests"] as? [String: String] else {
            throw ReaderBookUserStateWebAdapterError.invalidResponse
        }
        let expected = Dictionary(uniqueKeysWithValues: transaction.domains.map {
            ($0.name.rawValue, $0.digest)
        })
        guard digests == expected else {
            throw ReaderBookUserStateWebAdapterError.invalidResponse
        }
        return ReaderBookUserStateImportReceipt(
            contract: ReaderBookUserStateImportReceipt.currentContract,
            transactionId: transaction.transactionId,
            committed: true,
            domainDigests: digests
        )
    }

    private static func validate(
        _ transaction: ReaderBookUserStateImportTransaction,
        expectedLocalBookId: String
    ) throws {
        guard transaction.contract
                == ReaderBookUserStateImportTransaction.currentContract,
              transaction.localBookId == expectedLocalBookId,
              transaction.transactionId.range(
                of: #"^us_[a-f0-9]{32}$"#,
                options: .regularExpression
              ) != nil,
              transaction.remoteBookId.range(
                of: #"^book_[a-f0-9]{32}$"#,
                options: .regularExpression
              ) != nil,
              isSHA256(transaction.contentSha256),
              (1...ReaderBookUserStatePackageCodec.maximumRevision)
                .contains(transaction.packageRevision),
              !transaction.domains.isEmpty,
              Set(transaction.domains.map(\.name)).count
                == transaction.domains.count,
              Set(transaction.expectedLocalHeaders.keys)
                == Set(transaction.domains.map { $0.name.rawValue }) else {
            throw ReaderBookUserStateWebAdapterError.invalidRequest
        }
        for header in transaction.expectedLocalHeaders.values {
            guard isSHA256(header.digest),
                  (0...ReaderBookUserStatePackageCodec.maximumRevision)
                    .contains(header.revision) else {
                throw ReaderBookUserStateWebAdapterError.invalidRequest
            }
        }
        var rawBytes = 0
        for domain in transaction.domains {
            do {
                rawBytes += try ReaderBookUserStatePackageCodec
                    .validateDomainPayload(domain)
            } catch {
                throw ReaderBookUserStateWebAdapterError.invalidRequest
            }
        }
        guard rawBytes <= ReaderBookUserStatePackageCodec.maximumRawDomainBytes else {
            throw ReaderBookUserStateWebAdapterError.invalidRequest
        }
    }

    private static func jsonObject<T: Encodable>(_ value: T) throws -> Any {
        let data = try JSONEncoder().encode(value)
        return try JSONSerialization.jsonObject(with: data)
    }

    private static func strictInteger(_ value: Any?) -> Int64? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else {
            return nil
        }
        let double = number.doubleValue
        guard double.isFinite,
              double.rounded(.towardZero) == double,
              double >= 0,
              double <= Double(ReaderBookUserStatePackageCodec.maximumRevision) else {
            return nil
        }
        return number.int64Value
    }

    private static func validTrustedBaseURL(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "http",
              url.host?.lowercased() == ReaderLocalRuntimeServer.host,
              effectivePort(url) == Int(ReaderLocalRuntimeServer.port),
              url.user == nil,
              url.password == nil,
              url.query == nil,
              url.fragment == nil,
              url.path.range(
                of: #"^/r/[a-f0-9]{64}/$"#,
                options: .regularExpression
              ) != nil else {
            return false
        }
        return true
    }

    private static func validLocalBookId(_ value: String) -> Bool {
        value.range(
            of: #"^localbook-[A-Za-z0-9_-]{8,160}$"#,
            options: .regularExpression
        ) != nil || value == "localbook-welcome"
    }

    private static func isSHA256(_ value: String) -> Bool {
        value.range(
            of: #"^[a-f0-9]{64}$"#,
            options: .regularExpression
        ) != nil
    }

    private static func effectivePort(_ url: URL) -> Int? {
        if let port = url.port { return port }
        switch url.scheme?.lowercased() {
        case "http": return 80
        case "https": return 443
        default: return nil
        }
    }

    private static func requestId() -> String {
        "usr_" + UUID().uuidString
            .replacingOccurrences(of: "-", with: "")
            .lowercased()
    }
}
