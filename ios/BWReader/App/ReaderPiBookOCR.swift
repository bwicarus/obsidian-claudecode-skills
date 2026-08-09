import Combine
import CryptoKit
import Foundation

struct ReaderPiOCRStageProgress: Codable, Hashable, Sendable {
    let total: Int
    let completed: Int
    let pending: Int
    let failed: Int
    let unavailable: Int

    var fractionCompleted: Double {
        guard total > 0 else { return 0 }
        return min(1, max(0, Double(completed) / Double(total)))
    }
}

struct ReaderPiOCRJob: Codable, Hashable, Identifiable, Sendable {
    let jobId: String?
    let bookId: String
    let contentSha256: String
    // The idle status deliberately has no engine yet.
    let engine: String?
    let state: String
    let phase: String
    let processedPages: Int
    let totalPages: Int
    let successfulPages: Int
    let failedPages: Int
    let percent: Double
    let etaSeconds: Int?
    let message: String
    let canPause: Bool
    let canResume: Bool
    let canCancel: Bool
    let canRetry: Bool
    let resultAvailable: Bool
    let pageCharsRevision: String?
    let currentPage: Int?
    let formulaPendingRegions: Int
    let formulaFailedRegions: Int
    let textProgress: ReaderPiOCRStageProgress
    let wordProgress: ReaderPiOCRStageProgress
    let formulaProgress: ReaderPiOCRStageProgress
    let errorCode: String?
    let error: String?

    var id: String { jobId ?? "\(bookId):\(contentSha256):\(engine ?? "idle")" }

    var isActive: Bool {
        ["queued", "running", "pause-requested", "cancel-requested"].contains(state)
    }
}

private struct ReaderPiOCRWireResponse: Decodable {
    let ok: Bool
    let contract: String
    let job: ReaderPiOCRJob
}

struct ReaderPiOCRAdoption: Codable, Hashable, Sendable {
    struct PageSources: Codable, Hashable, Sendable {
        let overrideCount: Int
        let charCache: Int
        let embedded: Int
        let missing: Int

        private enum CodingKeys: String, CodingKey {
            case overrideCount = "override"
            case charCache = "char-cache"
            case embedded
            case missing
        }
    }

    struct Formula: Codable, Hashable, Sendable {
        let state: String
        let count: Int
        let reason: String?
    }

    let contract: String
    let bookId: String
    let contentSha256: String
    let available: Bool
    let alreadyAdopted: Bool
    let sourceEngine: String
    let totalPages: Int
    let pageSources: PageSources
    let missingPages: [Int]
    let formula: Formula
    let totalBytes: Int64?
    let revision: String?
}

private struct ReaderPiOCRAdoptionPreviewWireResponse: Decodable {
    let ok: Bool
    let contract: String
    let adoption: ReaderPiOCRAdoption
}

private struct ReaderPiOCRAdoptWireResponse: Decodable {
    let ok: Bool
    let contract: String
    let already: Bool
    let job: ReaderPiOCRJob
    let adoption: ReaderPiOCRAdoption
}

struct ReaderPiOCRAttachmentFile: Codable, Hashable, Sendable {
    let attachmentId: String
    let kind: String
    let category: String
    let mergePolicy: String
    let mediaType: String
    let size: Int64
    let sha256: String
    let downloadUrl: String
    let page: Int?
}

struct ReaderPiOCRAttachmentManifest: Codable, Hashable, Sendable {
    let contract: String
    let schema: Int
    let bookId: String
    let contentSha256: String
    let revision: String
    let category: String
    let mergePolicy: String
    let files: [ReaderPiOCRAttachmentFile]
}

struct ReaderPiOCRAttachmentBundle: Sendable {
    let manifest: ReaderPiOCRAttachmentManifest
    let files: [String: Data]
}

enum ReaderPiOCRError: LocalizedError {
    case invalidResponse
    case invalidURL
    case invalidManifest
    case server(status: Int, message: String)
    case attachmentTooLarge
    case attachmentSizeMismatch
    case attachmentChecksumMismatch
    case localContentMismatch

    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "Pi 预处理返回了无法识别的数据"
        case .invalidURL:
            return "Pi 预处理返回了不安全的地址"
        case .invalidManifest:
            return "Pi 书籍附件清单无效"
        case .server(let status, let message):
            return "Pi 预处理请求失败（HTTP \(status)）：\(message)"
        case .attachmentTooLarge:
            return "Pi 书籍附件超过 App 的安全大小限制"
        case .attachmentSizeMismatch:
            return "Pi 书籍附件大小校验失败"
        case .attachmentChecksumMismatch:
            return "Pi 书籍附件摘要校验失败"
        case .localContentMismatch:
            return "本机书籍内容与 Pi 预处理版本不一致，请先上传或下载同一版本"
        }
    }
}

private final class ReaderPiOCRRedirectDelegate: NSObject, URLSessionTaskDelegate {
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

final class ReaderPiOCRClient {
    static let shared = ReaderPiOCRClient()

    private static let contract = "reader-library-ocr/1"
    private static let attachmentContract = "reader-book-attachments/1"
    private static let maxAttachmentBytes: Int64 = 32 * 1_024 * 1_024
    private static let maxBundleBytes: Int64 = 512 * 1_024 * 1_024
    private let baseURL = URL(string: "https://bwicarus.taile44d0c.ts.net/")!
    private let redirectDelegate: ReaderPiOCRRedirectDelegate
    private let session: URLSession

    private init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpShouldSetCookies = false
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForRequest = 120
        configuration.timeoutIntervalForResource = 3_600
        let delegate = ReaderPiOCRRedirectDelegate()
        redirectDelegate = delegate
        session = URLSession(
            configuration: configuration,
            delegate: delegate,
            delegateQueue: nil
        )
    }

    func start(
        book: ReaderRemoteBook,
        engine: String,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRJob {
        guard ["vision", "manga"].contains(engine) else {
            throw ReaderPiOCRError.invalidResponse
        }
        return try await command(
            path: "pdf/api/library/ocr/start",
            body: [
                "bookId": book.bookId,
                "contentSha256": book.contentSha256,
                "engine": engine,
            ],
            cookies: cookies
        )
    }

    func status(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRJob {
        var components = URLComponents(
            url: try canonicalURL(path: "pdf/api/library/ocr/status"),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [
            URLQueryItem(name: "bookId", value: book.bookId),
            URLQueryItem(name: "contentSha256", value: book.contentSha256),
        ]
        guard let url = components?.url else { throw ReaderPiOCRError.invalidURL }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        apply(cookies: cookies, to: &request)
        return try await jobResponse(for: request, expectedPath: "/pdf/api/library/ocr/status")
    }

    func adoptionPreview(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRAdoption {
        var components = URLComponents(
            url: try canonicalURL(path: "pdf/api/library/ocr/adoption-preview"),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [
            URLQueryItem(name: "bookId", value: book.bookId),
            URLQueryItem(name: "contentSha256", value: book.contentSha256),
        ]
        guard let url = components?.url else { throw ReaderPiOCRError.invalidURL }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        apply(cookies: cookies, to: &request)
        let (data, response) = try await session.data(for: request)
        try validate(
            response,
            data: data,
            expectedPathPrefix: "/pdf/api/library/ocr/adoption-preview"
        )
        let payload: ReaderPiOCRAdoptionPreviewWireResponse
        do {
            payload = try JSONDecoder().decode(
                ReaderPiOCRAdoptionPreviewWireResponse.self,
                from: data
            )
        } catch {
            throw ReaderPiOCRError.invalidResponse
        }
        guard payload.ok, payload.contract == Self.contract else {
            throw ReaderPiOCRError.invalidResponse
        }
        try validate(payload.adoption, for: book)
        return payload.adoption
    }

    func adoptExisting(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async throws -> (
        job: ReaderPiOCRJob,
        adoption: ReaderPiOCRAdoption,
        already: Bool
    ) {
        var request = URLRequest(
            url: try canonicalURL(path: "pdf/api/library/ocr/adopt")
        )
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "bookId": book.bookId,
            "contentSha256": book.contentSha256,
        ])
        apply(cookies: cookies, to: &request)
        let (data, response) = try await session.data(for: request)
        try validate(
            response,
            data: data,
            expectedPathPrefix: "/pdf/api/library/ocr/adopt"
        )
        let payload: ReaderPiOCRAdoptWireResponse
        do {
            payload = try JSONDecoder().decode(
                ReaderPiOCRAdoptWireResponse.self,
                from: data
            )
        } catch {
            throw ReaderPiOCRError.invalidResponse
        }
        guard payload.ok,
              payload.contract == Self.contract,
              payload.job.bookId == book.bookId,
              payload.job.contentSha256.caseInsensitiveCompare(
                book.contentSha256
              ) == .orderedSame else {
            throw ReaderPiOCRError.invalidResponse
        }
        try validate(payload.adoption, for: book)
        guard payload.adoption.available,
              payload.adoption.alreadyAdopted,
              payload.job.engine == "legacy",
              payload.job.state == "succeeded",
              payload.job.resultAvailable,
              payload.job.pageCharsRevision == payload.adoption.revision else {
            throw ReaderPiOCRError.invalidResponse
        }
        return (payload.job, payload.adoption, payload.already)
    }

    func control(
        _ action: String,
        book: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRJob {
        guard ["pause", "resume", "cancel", "retry"].contains(action) else {
            throw ReaderPiOCRError.invalidResponse
        }
        return try await command(
            path: "pdf/api/library/ocr/\(action)",
            body: [
                "bookId": book.bookId,
                "contentSha256": book.contentSha256,
            ],
            cookies: cookies
        )
    }

    /// Reads and validates only the small manifest. Callers can consult the
    /// native store's durable import receipt before downloading page payloads.
    func attachmentManifest(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRAttachmentManifest {
        let manifestPath = book.attachmentsUrl
            ?? "/pdf/api/library/attachments/\(book.bookId)"
        guard let manifestURL = URL(string: manifestPath, relativeTo: baseURL)?.absoluteURL,
              isCanonical(manifestURL),
              manifestURL.path == "/pdf/api/library/attachments/\(book.bookId)" else {
            throw ReaderPiOCRError.invalidURL
        }
        var components = URLComponents(url: manifestURL, resolvingAgainstBaseURL: false)
        components?.queryItems = [
            URLQueryItem(name: "contentSha256", value: book.contentSha256),
        ]
        guard let url = components?.url else { throw ReaderPiOCRError.invalidURL }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        apply(cookies: cookies, to: &request)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data, expectedPathPrefix: manifestURL.path)
        let manifest = try JSONDecoder().decode(
            ReaderPiOCRAttachmentManifest.self,
            from: data
        )
        try validate(manifest, for: book)
        return manifest
    }

    /// A book can be downloaded before Pi preprocessing has ever run. That
    /// ordinary 404 means "no derived attachments", not a failed book download.
    func attachmentManifestIfAvailable(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRAttachmentManifest? {
        do {
            return try await attachmentManifest(book: book, cookies: cookies)
        } catch let error as ReaderPiOCRError {
            if case .server(let status, _) = error, status == 404 {
                return nil
            }
            throw error
        }
    }

    func downloadAttachments(
        book: ReaderRemoteBook,
        manifest: ReaderPiOCRAttachmentManifest,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRAttachmentBundle {
        try validate(manifest, for: book)
        var files: [String: Data] = [:]
        var aggregate: Int64 = 0
        for entry in manifest.files {
            guard entry.size >= 0,
                  entry.size <= Self.maxAttachmentBytes else {
                throw ReaderPiOCRError.attachmentTooLarge
            }
            let (nextAggregate, overflow) = aggregate.addingReportingOverflow(
                entry.size
            )
            guard !overflow, nextAggregate <= Self.maxBundleBytes else {
                throw ReaderPiOCRError.attachmentTooLarge
            }
            aggregate = nextAggregate
            guard let attachmentURL = URL(
                string: entry.downloadUrl,
                relativeTo: baseURL
            )?.absoluteURL,
                  isCanonical(attachmentURL),
                  attachmentURL.path
                    == "/pdf/api/library/attachments/\(book.bookId)/\(entry.attachmentId)" else {
                throw ReaderPiOCRError.invalidURL
            }
            var attachmentRequest = URLRequest(url: attachmentURL)
            attachmentRequest.httpMethod = "GET"
            attachmentRequest.setValue("application/json", forHTTPHeaderField: "Accept")
            apply(cookies: cookies, to: &attachmentRequest)
            let (payload, attachmentResponse) = try await session.data(for: attachmentRequest)
            try validate(
                attachmentResponse,
                data: payload,
                expectedPathPrefix: attachmentURL.path
            )
            guard Int64(payload.count) == entry.size else {
                throw ReaderPiOCRError.attachmentSizeMismatch
            }
            let digest = SHA256.hash(data: payload)
                .map { String(format: "%02x", $0) }
                .joined()
            guard digest.caseInsensitiveCompare(entry.sha256) == .orderedSame else {
                throw ReaderPiOCRError.attachmentChecksumMismatch
            }
            files[entry.attachmentId] = payload
        }
        return ReaderPiOCRAttachmentBundle(manifest: manifest, files: files)
    }

    /// Compatibility convenience. The coordinator intentionally does not use
    /// this method because it must check the durable receipt between these two
    /// network phases.
    func downloadAttachmentsIfAvailable(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRAttachmentBundle? {
        guard let manifest = try await attachmentManifestIfAvailable(
            book: book,
            cookies: cookies
        ) else { return nil }
        return try await downloadAttachments(
            book: book,
            manifest: manifest,
            cookies: cookies
        )
    }

    private func command(
        path: String,
        body: [String: String],
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRJob {
        var request = URLRequest(url: try canonicalURL(path: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        apply(cookies: cookies, to: &request)
        return try await jobResponse(for: request, expectedPath: "/\(path)")
    }

    private func jobResponse(
        for request: URLRequest,
        expectedPath: String
    ) async throws -> ReaderPiOCRJob {
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data, expectedPathPrefix: expectedPath)
        let payload: ReaderPiOCRWireResponse
        do {
            payload = try JSONDecoder().decode(
                ReaderPiOCRWireResponse.self,
                from: data
            )
        } catch {
            throw ReaderPiOCRError.invalidResponse
        }
        guard payload.ok, payload.contract == Self.contract else {
            throw ReaderPiOCRError.invalidResponse
        }
        return payload.job
    }

    private func canonicalURL(path: String) throws -> URL {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL,
              isCanonical(url) else {
            throw ReaderPiOCRError.invalidURL
        }
        return url
    }

    private func isCanonical(_ url: URL) -> Bool {
        url.scheme?.lowercased() == baseURL.scheme?.lowercased()
            && url.host?.lowercased() == baseURL.host?.lowercased()
            && url.port == baseURL.port
    }

    private func validate(
        _ response: URLResponse,
        data: Data,
        expectedPathPrefix: String
    ) throws {
        guard let http = response as? HTTPURLResponse,
              let responseURL = http.url,
              isCanonical(responseURL),
              responseURL.path.hasPrefix(expectedPathPrefix) else {
            throw ReaderPiOCRError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            var message = String(data: data.prefix(1_024), encoding: .utf8)
                ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
            let contentType = http.value(forHTTPHeaderField: "Content-Type")?
                .lowercased() ?? ""
            let responseObject = (
                (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            )
            let responseContract = responseObject?["contract"] as? String
            if (http.statusCode == 404 && responseContract != Self.contract)
                || contentType.contains("text/html")
                || message.trimmingCharacters(in: .whitespacesAndNewlines)
                    .hasPrefix("<") {
                message = "Pi 预处理接口未部署，或服务器返回了网页而不是协议数据"
            } else if responseContract == Self.contract,
                      let code = responseObject?["code"] as? String {
                switch code {
                case "legacy-adoption-busy":
                    message = "现有 Pi 结果检查正在进行，请稍后重试"
                case "legacy-result-incomplete":
                    message = "现有 Pi 预处理结果不完整，暂时不能采用"
                case "book-ocr-busy":
                    message = "这本书正在 Pi 预处理中，请完成或取消后再采用"
                default:
                    message = responseObject?["error"] as? String ?? message
                }
            }
            throw ReaderPiOCRError.server(status: http.statusCode, message: message)
        }
    }

    private func validate(
        _ manifest: ReaderPiOCRAttachmentManifest,
        for book: ReaderRemoteBook
    ) throws {
        guard manifest.contract == Self.attachmentContract,
              manifest.schema == 1,
              manifest.bookId == book.bookId,
              manifest.contentSha256.caseInsensitiveCompare(book.contentSha256)
                == .orderedSame,
              manifest.category == "derived",
              manifest.mergePolicy == "immutable",
              manifest.files.count <= 5_001 else {
            throw ReaderPiOCRError.invalidManifest
        }
        var identifiers = Set<String>()
        for entry in manifest.files {
            guard !entry.attachmentId.isEmpty,
                  identifiers.insert(entry.attachmentId).inserted,
                  entry.category == "derived",
                  entry.mergePolicy == "immutable",
                  entry.mediaType == "application/json",
                  entry.sha256.count == 64,
                  entry.sha256.allSatisfy({ $0.isHexDigit }) else {
                throw ReaderPiOCRError.invalidManifest
            }
        }
    }

    private func validate(
        _ adoption: ReaderPiOCRAdoption,
        for book: ReaderRemoteBook
    ) throws {
        let sources = adoption.pageSources
        let sourceCounts = [
            sources.overrideCount,
            sources.charCache,
            sources.embedded,
            sources.missing,
        ]
        guard adoption.contract == "reader-library-ocr-adoption/1",
              adoption.bookId == book.bookId,
              adoption.contentSha256.caseInsensitiveCompare(book.contentSha256)
                == .orderedSame,
              adoption.sourceEngine == "legacy",
              adoption.totalPages >= 0,
              sourceCounts.allSatisfy({
                $0 >= 0 && $0 <= adoption.totalPages
              }),
              ["succeeded", "pending", "failed"].contains(
                adoption.formula.state
              ),
              adoption.formula.count >= 0,
              adoption.totalBytes.map({ $0 >= 0 }) ?? true,
              adoption.missingPages.allSatisfy({
                $0 > 0 && $0 <= adoption.totalPages
              }),
              Set(adoption.missingPages).count == adoption.missingPages.count else {
            throw ReaderPiOCRError.invalidResponse
        }
        var sourceTotal = 0
        for count in sourceCounts {
            let (next, overflow) = sourceTotal.addingReportingOverflow(count)
            guard !overflow else { throw ReaderPiOCRError.invalidResponse }
            sourceTotal = next
        }
        let revisionIsValid = adoption.revision.map {
            $0.range(
                of: #"^ocr_[0-9a-f]{20}$"#,
                options: .regularExpression
            ) != nil
        } ?? false
        guard sourceTotal == adoption.totalPages,
              sources.missing == adoption.missingPages.count,
              adoption.available == (sources.missing == 0),
              !adoption.alreadyAdopted
                || (adoption.available && revisionIsValid),
              adoption.revision == nil || revisionIsValid else {
            throw ReaderPiOCRError.invalidResponse
        }
    }

    private func apply(cookies: [HTTPCookie], to request: inout URLRequest) {
        guard let requestURL = request.url,
              let host = requestURL.host?.lowercased() else { return }
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
            return !domain.isEmpty
                && domainMatches
                && pathMatches
                && (cookie.expiresDate.map { $0 > now } ?? true)
                && (!cookie.isSecure || requestURL.scheme?.lowercased() == "https")
        }
        var preferred: [String: HTTPCookie] = [:]
        for cookie in eligible {
            if let existing = preferred[cookie.name],
               existing.path.count >= cookie.path.count {
                continue
            }
            preferred[cookie.name] = cookie
        }
        HTTPCookie.requestHeaderFields(with: Array(preferred.values))
            .forEach { request.setValue($1, forHTTPHeaderField: $0) }
    }
}

@MainActor
final class ReaderPiOCRCoordinator: ObservableObject {
    @Published private(set) var jobs: [String: ReaderPiOCRJob] = [:]
    @Published private(set) var adoptions: [String: ReaderPiOCRAdoption] = [:]
    @Published private(set) var activeBookID: String?
    @Published private(set) var previewingBookID: String?
    @Published private(set) var notice: String?
    @Published private var bookErrors: [String: BookError] = [:]
    @Published private(set) var errorMessage: String?
    @Published private(set) var errorBookID: String?

    private let client = ReaderPiOCRClient.shared
    private var pollingTasks: [String: Task<Void, Never>] = [:]
    private var attachmentTasks: [String: Task<Void, Never>] = [:]
    private struct LocalBinding {
        let bookID: String
        let contentSHA256: String
    }
    private struct BookError: Equatable {
        let contentSHA256: String
        let message: String
        let isExplicit: Bool
    }
    private var localBindings: [String: LocalBinding] = [:]

    deinit {
        pollingTasks.values.forEach { $0.cancel() }
        attachmentTasks.values.forEach { $0.cancel() }
    }

    func job(for book: ReaderRemoteBook) -> ReaderPiOCRJob? {
        guard let job = jobs[book.bookId],
              job.contentSha256.caseInsensitiveCompare(book.contentSha256)
                == .orderedSame else { return nil }
        return job
    }

    func adoption(for book: ReaderRemoteBook) -> ReaderPiOCRAdoption? {
        guard let adoption = adoptions[book.bookId],
              adoption.contentSha256.caseInsensitiveCompare(book.contentSha256)
                == .orderedSame else { return nil }
        return adoption
    }

    func error(for book: ReaderRemoteBook) -> String? {
        guard let error = bookErrors[book.bookId],
              error.contentSHA256.caseInsensitiveCompare(book.contentSha256)
                == .orderedSame else { return nil }
        return error.message
    }

    func start(
        book: ReaderRemoteBook,
        engine: String,
        cookies: [HTTPCookie],
        localBookID: String? = nil,
        localContentSHA256: String? = nil
    ) async {
        remember(
            localBookID: localBookID,
            localContentSHA256: localContentSHA256,
            for: book
        )
        await perform(book: book, cookies: cookies) {
            try await self.client.start(book: book, engine: engine, cookies: cookies)
        }
    }

    func refresh(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie],
        localBookID: String? = nil,
        localContentSHA256: String? = nil,
        previewsLegacyResults: Bool = false
    ) async {
        remember(
            localBookID: localBookID,
            localContentSHA256: localContentSHA256,
            for: book
        )
        do {
            let job = try await client.status(book: book, cookies: cookies)
            guard !Task.isCancelled else { return }
            accept(job, book: book, cookies: cookies)
            if job.state == "idle", previewsLegacyResults {
                if let previewingBookID {
                    if previewingBookID == book.bookId { return }
                    recordError(
                        "正在检查另一册书的现有 Pi 结果，请稍后重试",
                        for: book,
                        explicit: false
                    )
                    return
                }
                previewingBookID = book.bookId
                defer {
                    if previewingBookID == book.bookId {
                        previewingBookID = nil
                    }
                }
                let adoption = try await client.adoptionPreview(
                    book: book,
                    cookies: cookies
                )
                guard !Task.isCancelled else { return }
                adoptions[book.bookId] = adoption
                clearPassiveError(for: book.bookId)
            } else if job.state != "idle" {
                adoptions.removeValue(forKey: book.bookId)
            }
        } catch {
            guard !isCancellation(error) else { return }
            recordError(
                error.localizedDescription,
                for: book,
                explicit: false
            )
        }
    }

    func adoptExisting(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie],
        localBookID: String? = nil,
        localContentSHA256: String? = nil
    ) async {
        remember(
            localBookID: localBookID,
            localContentSHA256: localContentSHA256,
            for: book
        )
        guard activeBookID == nil || activeBookID == book.bookId else {
            recordError(
                "另一册书正在进行 Pi 预处理请求，请稍后再试",
                for: book,
                explicit: true
            )
            return
        }
        activeBookID = book.bookId
        clearError(for: book.bookId)
        notice = nil
        defer { activeBookID = nil }
        do {
            let result = try await client.adoptExisting(
                book: book,
                cookies: cookies
            )
            adoptions[book.bookId] = result.adoption
            accept(
                result.job,
                book: book,
                cookies: cookies,
                importsAttachments: false
            )
            notice = result.already
                ? "现有 Pi 预处理结果已经采用"
                : result.job.message
            if let localBinding = localBindings[book.bookId] {
                let imported = await importAvailableAttachments(
                    book: book,
                    localBookID: localBinding.bookID,
                    localContentSHA256: localBinding.contentSHA256,
                    cookies: cookies,
                    requiresManifest: true,
                    reportsExplicitFailure: true
                )
                if !imported { notice = nil }
            }
        } catch {
            guard !isCancellation(error) else { return }
            recordError(
                error.localizedDescription,
                for: book,
                explicit: true
            )
        }
    }

    func control(
        _ action: String,
        book: ReaderRemoteBook,
        cookies: [HTTPCookie],
        localBookID: String? = nil,
        localContentSHA256: String? = nil
    ) async {
        remember(
            localBookID: localBookID,
            localContentSHA256: localContentSHA256,
            for: book
        )
        await perform(book: book, cookies: cookies) {
            try await self.client.control(action, book: book, cookies: cookies)
        }
    }

    /// Explicitly imports an immutable Pi-derived revision into the native
    /// sidecar store. It is also called after a Pi book download. A download
    /// before preprocessing may have no attachments; a published/adopted job
    /// requires its manifest and reports a missing one as an import failure.
    func importAvailableAttachments(
        book: ReaderRemoteBook,
        localBookID: String,
        localContentSHA256: String,
        cookies: [HTTPCookie],
        requiresManifest: Bool = false,
        reportsExplicitFailure: Bool = false
    ) async -> Bool {
        if reportsExplicitFailure {
            clearError(for: book.bookId)
        }
        do {
            guard localContentSHA256.caseInsensitiveCompare(
                book.contentSha256
            ) == .orderedSame else {
                throw ReaderPiOCRError.localContentMismatch
            }
            remember(
                localBookID: localBookID,
                localContentSHA256: localContentSHA256,
                for: book
            )
            let attachmentManifest: ReaderPiOCRAttachmentManifest
            if requiresManifest {
                attachmentManifest = try await client.attachmentManifest(
                    book: book,
                    cookies: cookies
                )
            } else {
                guard let available = try await client
                    .attachmentManifestIfAvailable(
                    book: book,
                    cookies: cookies
                ) else {
                    clearPassiveError(for: book.bookId)
                    return true
                }
                attachmentManifest = available
            }
            guard attachmentManifest.contentSha256.caseInsensitiveCompare(
                localContentSHA256
            ) == .orderedSame else {
                throw ReaderPiOCRError.localContentMismatch
            }
            if try await NativeBookOCRManager.shared.hasImportedRevision(
                expectedContentSHA256: localContentSHA256,
                revision: attachmentManifest.revision
            ) {
                clearPassiveError(for: book.bookId)
                notice = "Pi 预处理结果已是最新"
                return true
            }
            let bundle = try await client.downloadAttachments(
                book: book,
                manifest: attachmentManifest,
                cookies: cookies
            )
            let manifest = NativeBookOCRDerivedAttachmentManifest(
                contract: bundle.manifest.contract,
                bookId: bundle.manifest.bookId,
                contentSha256: bundle.manifest.contentSha256,
                revision: bundle.manifest.revision,
                files: bundle.manifest.files.map { entry in
                    NativeBookOCRDerivedAttachmentManifest.File(
                        attachmentId: entry.attachmentId,
                        kind: entry.kind,
                        category: entry.category,
                        mergePolicy: entry.mergePolicy,
                        mediaType: entry.mediaType,
                        size: entry.size,
                        sha256: entry.sha256,
                        downloadUrl: entry.downloadUrl,
                        page: entry.page
                    )
                }
            )
            _ = try await NativeBookOCRManager.shared.importDerivedAttachments(
                bookID: localBookID,
                expectedContentSHA256: localContentSHA256,
                manifest: manifest,
                files: bundle.files
            )
            clearPassiveError(for: book.bookId)
            notice = "已导入 Pi 预处理结果"
            return true
        } catch {
            guard !isCancellation(error) else { return false }
            recordError(
                "Pi 预处理附件导入失败：\(error.localizedDescription)",
                for: book,
                explicit: reportsExplicitFailure
            )
            return false
        }
    }

    func dismissMessages() {
        notice = nil
        errorMessage = nil
        errorBookID = nil
    }

    private func perform(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie],
        operation: @escaping () async throws -> ReaderPiOCRJob
    ) async {
        guard activeBookID == nil || activeBookID == book.bookId else {
            recordError(
                "另一册书正在进行 Pi 预处理请求，请稍后再试",
                for: book,
                explicit: true
            )
            return
        }
        activeBookID = book.bookId
        clearError(for: book.bookId)
        notice = nil
        defer { activeBookID = nil }
        do {
            let job = try await operation()
            accept(job, book: book, cookies: cookies)
            notice = job.message
        } catch {
            guard !isCancellation(error) else { return }
            recordError(
                error.localizedDescription,
                for: book,
                explicit: true
            )
        }
    }

    private func accept(
        _ job: ReaderPiOCRJob,
        book: ReaderRemoteBook,
        cookies: [HTTPCookie],
        importsAttachments: Bool = true
    ) {
        jobs[book.bookId] = job
        clearPassiveError(for: book.bookId)
        if importsAttachments,
           job.resultAvailable,
           let localBinding = localBindings[book.bookId] {
            scheduleAttachmentImport(
                book: book,
                localBinding: localBinding,
                cookies: cookies
            )
        }
        if job.isActive {
            schedulePoll(book: book, cookies: cookies)
        } else {
            pollingTasks[book.bookId]?.cancel()
            pollingTasks[book.bookId] = nil
        }
    }

    private func schedulePoll(book: ReaderRemoteBook, cookies: [HTTPCookie]) {
        guard pollingTasks[book.bookId] == nil else { return }
        pollingTasks[book.bookId] = Task { @MainActor [weak self] in
            defer { self?.pollingTasks[book.bookId] = nil }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard !Task.isCancelled, let self else { return }
                do {
                    let job = try await self.client.status(book: book, cookies: cookies)
                    guard !Task.isCancelled else { return }
                    self.accept(job, book: book, cookies: cookies)
                    if !job.isActive { return }
                } catch {
                    guard !self.isCancellation(error) else { return }
                    self.recordError(
                        error.localizedDescription,
                        for: book,
                        explicit: false
                    )
                    return
                }
            }
        }
    }

    private func recordError(
        _ message: String,
        for book: ReaderRemoteBook,
        explicit: Bool
    ) {
        let bookID = book.bookId
        if let existing = bookErrors[bookID],
           existing.isExplicit,
           !explicit,
           existing.contentSHA256.caseInsensitiveCompare(book.contentSha256)
            == .orderedSame {
            return
        }
        bookErrors[bookID] = BookError(
            contentSHA256: book.contentSha256.lowercased(),
            message: message,
            isExplicit: explicit
        )
        if explicit {
            errorBookID = bookID
            errorMessage = message
        }
    }

    private func clearPassiveError(for bookID: String) {
        guard bookErrors[bookID]?.isExplicit == false else { return }
        bookErrors.removeValue(forKey: bookID)
    }

    private func clearError(for bookID: String) {
        bookErrors.removeValue(forKey: bookID)
        guard errorBookID == bookID else { return }
        errorBookID = nil
        errorMessage = nil
    }

    private func isCancellation(_ error: Error) -> Bool {
        error is CancellationError
            || (error as? URLError)?.code == .cancelled
            || Task.isCancelled
    }

    private func remember(
        localBookID: String?,
        localContentSHA256: String?,
        for book: ReaderRemoteBook
    ) {
        guard let localBookID,
              !localBookID.isEmpty,
              let localContentSHA256,
              localContentSHA256.range(
                of: #"^[0-9a-fA-F]{64}$"#,
                options: .regularExpression
              ) != nil,
              localContentSHA256.caseInsensitiveCompare(
                book.contentSha256
              ) == .orderedSame else {
            localBindings.removeValue(forKey: book.bookId)
            return
        }
        localBindings[book.bookId] = LocalBinding(
            bookID: localBookID,
            contentSHA256: localContentSHA256.lowercased()
        )
    }

    private func scheduleAttachmentImport(
        book: ReaderRemoteBook,
        localBinding: LocalBinding,
        cookies: [HTTPCookie]
    ) {
        guard attachmentTasks[book.bookId] == nil else { return }
        attachmentTasks[book.bookId] = Task { @MainActor [weak self] in
            guard let self else { return }
            defer { self.attachmentTasks[book.bookId] = nil }
            _ = await self.importAvailableAttachments(
                book: book,
                localBookID: localBinding.bookID,
                localContentSHA256: localBinding.contentSHA256,
                cookies: cookies,
                requiresManifest: true
            )
        }
    }
}
