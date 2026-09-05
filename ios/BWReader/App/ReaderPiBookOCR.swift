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
    // Older Pi releases omit this field; treat them as the original Pi
    // executor until the coordinated server update is deployed.
    let executor: String?
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

struct ReaderOCRExecutorStatus: Codable, Hashable, Identifiable, Sendable {
    let executor: String
    let online: Bool
    let acceptingJobs: Bool
    let engines: [String]
    let lastSeenAtEpochMs: Int64?
    let workerCount: Int?

    var id: String { executor }
}

private struct ReaderOCRExecutorsWireResponse: Decodable {
    let ok: Bool
    let contract: String
    let executors: [ReaderOCRExecutorStatus]
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

/// 服务器上的一次预处理结果。
///
/// 时间字段一律可选：Pi 若还没升级到带运行台账的版本，这些字段不会出现；
/// 用非可选类型会让整条响应解码失败，把"还没升级"变成"功能坏了"。
struct ReaderPiOCRRelease: Equatable, Identifiable, Sendable {
    let runId: String
    let revision: String
    let engine: String
    let executor: String
    let publishedAt: Date?
    let totalPages: Int?
    let isActive: Bool

    var id: String { runId }

    /// 「PC 高质量预处理 · 08-18 · 53 页」——日期取不到就不写，**不编造**。
    var displayTitle: String {
        var parts: [String] = [Self.engineTitle(engine: engine, executor: executor)]
        if let publishedAt {
            let formatter = DateFormatter()
            formatter.dateFormat = "MM-dd"
            parts.append(formatter.string(from: publishedAt))
        } else {
            parts.append("日期未知")
        }
        if let totalPages {
            parts.append("\(totalPages) 页")
        }
        return parts.joined(separator: " · ")
    }

    static func engineTitle(engine: String, executor: String) -> String {
        if engine == "legacy" { return "兼容旧结果" }
        return executor == "pc" ? "PC 高质量预处理" : "服务器预处理"
    }
}

struct ReaderPiOCRReleaseList: Equatable, Sendable {
    let activeRunId: String?
    let releases: [ReaderPiOCRRelease]
    let stagingArchiveBytes: Int64
}

private struct ReaderPiOCRReleaseWire: Decodable {
    let runId: String
    let revision: String
    let engine: String
    let executor: String
    let publishedAtEpochMs: Int64?
    let totalPages: Int?
    let isActive: Bool?
}

private struct ReaderPiOCRReleaseListWireResponse: Decodable {
    let ok: Bool
    let contract: String
    let activeRunId: String?
    let runs: [ReaderPiOCRReleaseWire]
    let stagingArchiveBytes: Int64?
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
    let engine: String?
    let executor: String?
    let processingProfile: String?
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
            return "服务器预处理返回了无法识别的数据"
        case .invalidURL:
            return "服务器预处理返回了不安全的地址"
        case .invalidManifest:
            return "服务器书籍附件清单无效"
        case .server(let status, let message):
            return "服务器预处理请求失败（HTTP \(status)）：\(message)"
        case .attachmentTooLarge:
            return "服务器书籍附件超过 App 的安全大小限制"
        case .attachmentSizeMismatch:
            return "服务器书籍附件大小校验失败"
        case .attachmentChecksumMismatch:
            return "服务器书籍附件摘要校验失败"
        case .localContentMismatch:
            return "本机书籍内容与服务器预处理版本不一致，请先上传或下载同一版本"
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
    private let baseURL = URL(string: "https://bwicarus-2.taile44d0c.ts.net/")!
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
        executor: String,
        force: Bool = false,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRJob {
        guard ["vision", "manga", "native"].contains(engine),
              ["pi", "pc"].contains(executor) else {
            throw ReaderPiOCRError.invalidResponse
        }
        var body = [
            "bookId": book.bookId,
            "contentSha256": book.contentSha256,
            "engine": engine,
        ]
        // The original Pi executor is the wire default.  Omitting this field
        // keeps a newly installed App able to start Pi work while the server
        // is rolling back or has not yet received the coordinated update.
        if executor == "pc" {
            body["executor"] = executor
        }
        return try await command(
            path: "pdf/api/library/ocr/start",
            body: body,
            // 只有用户明说重跑才带这个字段：旧服务器不认识 force，白名单会
            // 拒掉整个请求，所以默认路径必须保持跟以前逐字节一致。
            flags: force ? ["force": true] : [:],
            cookies: cookies
        )
    }

    /// 把 App 原生侧的预处理/导入错误送进服务器的客户端日志(/pdf/api/client-log,与网页侧 dlog 同一个文件)。
    /// 只发不等、失败静默:诊断通道绝不能反过来打断被诊断的功能(2026-09-04 用户「预处理附件导入失败」时日志里零痕迹)。
    func postClientLog(_ message: String, level: String = "error", cookies: [HTTPCookie]) {
        Task.detached(priority: .utility) { [session] in
            do {
                var request = URLRequest(url: try self.canonicalURL(path: "pdf/api/client-log"))
                request.httpMethod = "POST"
                request.timeoutInterval = 8
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                self.apply(cookies: cookies, to: &request)
                let payload: [String: Any] = [
                    "device": "ios-native",
                    "surface": "native",
                    "build": nativeAppBuildVersion,
                    "lines": [[
                        "t": ISO8601DateFormatter().string(from: Date()),
                        "level": level,
                        "msg": "[native] " + message.prefix(1800),
                    ]],
                ]
                request.httpBody = try JSONSerialization.data(withJSONObject: payload)
                _ = try await session.data(for: request)
            } catch {
                // 静默:见上
            }
        }
    }

    func executors(
        cookies: [HTTPCookie]
    ) async throws -> [ReaderOCRExecutorStatus] {
        var request = URLRequest(
            url: try canonicalURL(path: "pdf/api/library/ocr/executors")
        )
        request.httpMethod = "GET"
        // Availability is advisory and must never leave the row saying
        // “正在确认” for the long OCR transfer timeout.
        request.timeoutInterval = 5
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        apply(cookies: cookies, to: &request)
        let (data, response) = try await session.data(for: request)
        try validate(
            response,
            data: data,
            expectedPathPrefix: "/pdf/api/library/ocr/executors"
        )
        let payload: ReaderOCRExecutorsWireResponse
        do {
            payload = try JSONDecoder().decode(
                ReaderOCRExecutorsWireResponse.self,
                from: data
            )
        } catch {
            throw ReaderPiOCRError.invalidResponse
        }
        guard payload.ok, payload.contract == Self.contract else {
            throw ReaderPiOCRError.invalidResponse
        }
        var names = Set<String>()
        for status in payload.executors {
            guard ["pi", "pc"].contains(status.executor),
                  names.insert(status.executor).inserted,
                  Set(status.engines).isSubset(of: Set(["vision", "manga", "native"])),
                  (status.workerCount ?? 0) >= 0 else {
                throw ReaderPiOCRError.invalidResponse
            }
        }
        return payload.executors
    }

    static let releaseIndexContract = "reader-book-ocr-release-index/1"

    /// 列出服务器上这本书的**全部**预处理结果（带日期）。
    func releases(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRReleaseList {
        var components = URLComponents(
            url: try canonicalURL(path: "pdf/api/library/ocr/releases"),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [
            URLQueryItem(name: "bookId", value: book.bookId),
            URLQueryItem(name: "contentSha256", value: book.contentSha256),
        ]
        guard let url = components?.url else { throw ReaderPiOCRError.invalidURL }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        apply(cookies: cookies, to: &request)
        return try await releaseListResponse(
            for: request,
            expectedPath: "/pdf/api/library/ocr/releases"
        )
    }

    /// 把某一次结果设为当前生效。
    func activateRelease(
        book: ReaderRemoteBook,
        runId: String,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRReleaseList {
        try await releaseCommand(
            path: "pdf/api/library/ocr/releases/activate",
            book: book,
            body: ["runId": runId],
            cookies: cookies
        )
    }

    /// 删除某一次结果。删当前生效的那份需要 allowDeactivate 明确确认。
    func deleteRelease(
        book: ReaderRemoteBook,
        runId: String,
        allowDeactivate: Bool,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRReleaseList {
        try await releaseCommand(
            path: "pdf/api/library/ocr/releases/delete",
            book: book,
            body: ["runId": runId, "allowDeactivate": allowDeactivate],
            cookies: cookies
        )
    }

    private func releaseCommand(
        path: String,
        book: ReaderRemoteBook,
        body: [String: Any],
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRReleaseList {
        var request = URLRequest(url: try canonicalURL(path: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload: [String: Any] = [
            "bookId": book.bookId,
            "contentSha256": book.contentSha256,
        ]
        for (key, value) in body { payload[key] = value }
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)
        apply(cookies: cookies, to: &request)
        return try await releaseListResponse(for: request, expectedPath: "/" + path)
    }

    private func releaseListResponse(
        for request: URLRequest,
        expectedPath: String
    ) async throws -> ReaderPiOCRReleaseList {
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data, expectedPathPrefix: expectedPath)
        let payload: ReaderPiOCRReleaseListWireResponse
        do {
            payload = try JSONDecoder().decode(
                ReaderPiOCRReleaseListWireResponse.self,
                from: data
            )
        } catch {
            throw ReaderPiOCRError.invalidResponse
        }
        guard payload.ok, payload.contract == Self.releaseIndexContract else {
            throw ReaderPiOCRError.invalidResponse
        }
        let releases = payload.runs.map { run in
            ReaderPiOCRRelease(
                runId: run.runId,
                revision: run.revision,
                engine: run.engine,
                executor: run.executor,
                publishedAt: run.publishedAtEpochMs.map {
                    Date(timeIntervalSince1970: Double($0) / 1000)
                },
                totalPages: run.totalPages,
                isActive: run.isActive ?? (run.runId == payload.activeRunId)
            )
        }
        return ReaderPiOCRReleaseList(
            activeRunId: payload.activeRunId,
            releases: releases,
            stagingArchiveBytes: payload.stagingArchiveBytes ?? 0
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
        request.cachePolicy = .reloadIgnoringLocalCacheData
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
        // 服务端对 force 这类开关只认真 bool，不认 "true" 字符串 —— 分开传，
        // 免得为了塞一个布尔值把整个 body 放宽成 [String: Any]。
        flags: [String: Bool] = [:],
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRJob {
        var request = URLRequest(url: try canonicalURL(path: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload: [String: Any] = body
        for (key, value) in flags { payload[key] = value }
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)
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
                message = "服务器预处理接口未部署，或服务器返回了网页而不是协议数据"
            } else if responseContract == Self.contract,
                      let code = responseObject?["code"] as? String {
                switch code {
                case "legacy-adoption-busy":
                    message = "现有服务器结果检查正在进行，请稍后重试"
                case "legacy-result-incomplete":
                    message = "现有服务器预处理结果不完整，暂时不能采用"
                case "book-ocr-busy":
                    message = "这本书正在服务器预处理中，请完成或取消后再采用"
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
              manifest.engine.map({ ["vision", "manga", "native", "legacy"].contains($0) }) ?? true,
              manifest.executor.map({ ["pi", "pc"].contains($0) }) ?? true,
              manifest.processingProfile.map({ !$0.isEmpty && $0.count <= 80 }) ?? true,
              (manifest.executor != "pc"
                || (manifest.processingProfile.map({
                    ["quality-first-v1", "quality-first-v2", "quality-first-v3", "quality-first-v4", "quality-first-v5", "quality-first-v6"].contains($0)
                }) ?? false)),
              ((manifest.executor ?? "pi") != "pi"
                || (manifest.processingProfile.map({
                    ["pi-default-v1", "pi-default-v2", "pi-default-v3", "pi-default-v4", "pi-default-v5"].contains($0)
                }) ?? true)),
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
    static let shared = ReaderPiOCRCoordinator()

    @Published private(set) var jobs: [String: ReaderPiOCRJob] = [:]
    @Published private(set) var adoptions: [String: ReaderPiOCRAdoption] = [:]
    @Published private(set) var executorStatuses: [String: ReaderOCRExecutorStatus] = [:]
    @Published private(set) var refreshingExecutors = false
    @Published private(set) var activeBookID: String?
    @Published private(set) var previewingBookID: String?
    @Published private(set) var notice: String?
    @Published private var bookErrors: [String: BookError] = [:]
    @Published private(set) var errorMessage: String?
    @Published private(set) var errorBookID: String?

    private let client = ReaderPiOCRClient.shared
    private let defaults: UserDefaults
    private static let pendingImportsDefaultsKey =
        "reader-pi-ocr-pending-imports/2"
    private static let pendingImportTTLMilliseconds: Int64 =
        7 * 24 * 60 * 60 * 1_000
    private var pollingTasks: [String: Task<Void, Never>] = [:]
    private var pollingTaskTokens: [String: UUID] = [:]
    private var attachmentTasks: [String: Task<Void, Never>] = [:]
    private var attachmentTaskTokens: [String: UUID] = [:]
    /// Changes before every explicit server mutation. Passive status requests
    /// may only publish the response for the exact epoch they started in, so a
    /// late foreground refresh or poll cannot erase a newer request receipt.
    private var commandEpochs: [String: UUID] = [:]
    private struct LocalBinding {
        let bookID: String
        let contentSHA256: String
    }
    private struct BookError: Equatable {
        let contentSHA256: String
        let message: String
        let isExplicit: Bool
    }
    private struct PendingImport: Codable, Sendable {
        let remoteBook: ReaderRemoteBook
        let localBookID: String
        let contentSHA256: String
        let engine: String
        let executor: String
        let createdAtEpochMs: Int64
        let generation: String
        let jobID: String
        var revision: String?
    }
    private struct PendingImportClaim: Sendable {
        let remoteBookID: String
        let localBookID: String
        let contentSHA256: String
        let generation: String
        let jobID: String
        let revision: String
    }
    private enum PendingOwnershipTransition {
        case preserve
        case replaceConfirmed(engine: String?, executor: String?)
    }
    private var localBindings: [String: LocalBinding] = [:]
    /// 最近一次请求携带的 cookie:recordError 往服务器客户端日志出声时用(协调器拿不到 WebView)。
    private var lastKnownCookies: [HTTPCookie] = []
    private var pendingImports: [String: PendingImport]

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        pendingImports = Self.loadPendingImports(from: defaults)
        localBindings = Dictionary(
            uniqueKeysWithValues: pendingImports.map { remoteBookID, pending in
                (
                    remoteBookID,
                    LocalBinding(
                        bookID: pending.localBookID,
                        contentSHA256: pending.contentSHA256
                    )
                )
            }
        )
        Self.persist(pendingImports, to: defaults)
    }

    deinit {
        pollingTasks.values.forEach { $0.cancel() }
        attachmentTasks.values.forEach { $0.cancel() }
    }

    // 历次预处理结果：视图经协调器访问，不直接碰 client（client 是 private，
    // 而且这一族的既有写法就是"视图 → 协调器 → 客户端"）。
    func releases(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRReleaseList {
        try await client.releases(book: book, cookies: cookies)
    }

    func activateRelease(
        book: ReaderRemoteBook,
        runId: String,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRReleaseList {
        try await client.activateRelease(book: book, runId: runId, cookies: cookies)
    }

    func deleteRelease(
        book: ReaderRemoteBook,
        runId: String,
        allowDeactivate: Bool,
        cookies: [HTTPCookie]
    ) async throws -> ReaderPiOCRReleaseList {
        try await client.deleteRelease(
            book: book,
            runId: runId,
            allowDeactivate: allowDeactivate,
            cookies: cookies
        )
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

    func executorStatus(_ executor: String) -> ReaderOCRExecutorStatus? {
        executorStatuses[executor]
    }

    func refreshExecutors(cookies: [HTTPCookie]) async {
        guard !refreshingExecutors else { return }
        refreshingExecutors = true
        defer { refreshingExecutors = false }
        do {
            let values = try await client.executors(cookies: cookies)
            guard !Task.isCancelled else { return }
            executorStatuses = Dictionary(
                uniqueKeysWithValues: values.map { ($0.executor, $0) }
            )
        } catch {
            guard !isCancellation(error) else { return }
            // Availability is advisory. Keep the last verified status instead
            // of turning a transient status request into a book-level failure.
        }
    }

    func start(
        book: ReaderRemoteBook,
        engine: String,
        executor: String,
        force: Bool = false,
        cookies: [HTTPCookie],
        localBookID: String? = nil,
        localContentSHA256: String? = nil
    ) async {
        guard acquireExplicitCommand(for: book) else { return }
        defer { releaseExplicitCommand(for: book.bookId) }
        remember(
            localBookID: localBookID,
            localContentSHA256: localContentSHA256,
            for: book
        )
        await perform(
            book: book,
            cookies: cookies,
            ownershipTransition: .replaceConfirmed(
                engine: engine,
                executor: executor
            )
        ) {
            try await self.client.start(
                book: book,
                engine: engine,
                executor: executor,
                force: force,
                cookies: cookies
            )
        }
    }

    func refresh(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie],
        localBookID: String? = nil,
        localContentSHA256: String? = nil,
        previewsLegacyResults: Bool = false
    ) async {
        guard activeBookID != book.bookId else { return }
        reconcilePendingImport(
            localBookID: localBookID,
            localContentSHA256: localContentSHA256,
            for: book
        )
        let requestEpoch = commandEpoch(for: book.bookId)
        do {
            let job = try await client.status(book: book, cookies: cookies)
            guard !Task.isCancelled,
                  isPassiveResponseCurrent(
                    bookID: book.bookId,
                    epoch: requestEpoch
                  ) else { return }
            accept(job, book: book, cookies: cookies)
            if job.state == "idle", previewsLegacyResults {
                if let previewingBookID {
                    if previewingBookID == book.bookId { return }
                    recordError(
                        "正在检查另一册书的现有服务器结果，请稍后重试",
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
                guard !Task.isCancelled,
                      isPassiveResponseCurrent(
                        bookID: book.bookId,
                        epoch: requestEpoch
                      ) else { return }
                adoptions[book.bookId] = adoption
                clearPassiveError(for: book.bookId)
            } else if job.state != "idle" {
                adoptions.removeValue(forKey: book.bookId)
            }
        } catch {
            guard !isCancellation(error),
                  isPassiveResponseCurrent(
                    bookID: book.bookId,
                    epoch: requestEpoch
                  ) else { return }
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
        guard acquireExplicitCommand(for: book) else { return }
        defer { releaseExplicitCommand(for: book.bookId) }
        remember(
            localBookID: localBookID,
            localContentSHA256: localContentSHA256,
            for: book
        )
        clearError(for: book.bookId)
        notice = nil
        let requestedBinding = localBindings[book.bookId]
        await stopPolling(for: book.bookId)
        do {
            let result = try await client.adoptExisting(
                book: book,
                cookies: cookies
            )
            let ownershipClaim = try await replacePendingImport(
                book: book,
                job: result.job,
                expectedEngine: "legacy",
                expectedExecutor: "pi",
                requestedBinding: requestedBinding
            )
            adoptions[book.bookId] = result.adoption
            accept(
                result.job,
                book: book,
                cookies: cookies,
                importsAttachments: false
            )
            notice = result.already
                ? "现有服务器预处理结果已经采用"
                : result.job.message
            if let localBinding = localBindings[book.bookId] {
                let imported = await importAvailableAttachmentsImpl(
                    book: book,
                    localBookID: localBinding.bookID,
                    localContentSHA256: localBinding.contentSHA256,
                    cookies: cookies,
                    requiresManifest: true,
                    reportsExplicitFailure: true,
                    forceReimport: false,
                    ownershipClaim: ownershipClaim
                )
                if !imported { notice = nil }
            }
        } catch {
            schedulePoll(book: book, cookies: cookies)
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
        guard acquireExplicitCommand(for: book) else { return }
        defer { releaseExplicitCommand(for: book.bookId) }
        remember(
            localBookID: localBookID,
            localContentSHA256: localContentSHA256,
            for: book
        )
        let replacesJob = ["resume", "retry"].contains(action)
        let previous = jobs[book.bookId]
        let expectedEngine = previous?.engine
            ?? pendingImports[book.bookId]?.engine
        let expectedExecutor = previous.map {
            Self.normalizedExecutor($0.executor)
        } ?? pendingImports[book.bookId]?.executor
        await perform(
            book: book,
            cookies: cookies,
            ownershipTransition: replacesJob
                ? .replaceConfirmed(
                    engine: expectedEngine,
                    executor: expectedExecutor
                )
                : .preserve
        ) {
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
        reportsExplicitFailure: Bool = false,
        forceReimport: Bool = false
    ) async -> Bool {
        await importAvailableAttachmentsImpl(
            book: book,
            localBookID: localBookID,
            localContentSHA256: localContentSHA256,
            cookies: cookies,
            requiresManifest: requiresManifest,
            reportsExplicitFailure: reportsExplicitFailure,
            forceReimport: forceReimport,
            ownershipClaim: nil
        )
    }

    private func importAvailableAttachmentsImpl(
        book: ReaderRemoteBook,
        localBookID: String,
        localContentSHA256: String,
        cookies: [HTTPCookie],
        requiresManifest: Bool,
        reportsExplicitFailure: Bool,
        forceReimport: Bool,
        ownershipClaim: PendingImportClaim?
    ) async -> Bool {
        if reportsExplicitFailure {
            clearError(for: book.bookId)
        }
        lastKnownCookies = cookies
        do {
            if let ownershipClaim,
               !isCurrentPendingImport(ownershipClaim) {
                return false
            }
            guard localContentSHA256.caseInsensitiveCompare(
                book.contentSha256
            ) == .orderedSame else {
                throw ReaderPiOCRError.localContentMismatch
            }
            try await validateLocalBookStillMatches(
                localBookID: localBookID,
                expectedContentSHA256: localContentSHA256
            )
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
            if let ownershipClaim {
                guard attachmentManifest.revision == ownershipClaim.revision,
                      isCurrentPendingImport(ownershipClaim) else {
                    throw ReaderPiOCRError.invalidManifest
                }
            }
            let completionClaim = ownershipClaim
                ?? pendingImportClaim(
                    for: book,
                    localBookID: localBookID,
                    localContentSHA256: localContentSHA256,
                    revision: attachmentManifest.revision
                )
            if !forceReimport,
               try await NativeBookOCRManager.shared.hasImportedRevision(
                expectedContentSHA256: localContentSHA256,
                revision: attachmentManifest.revision
            ) {
                _ = try await NativeBookOCRManager.shared
                    .refreshLayerStateAndNotify(
                    bookID: localBookID,
                    expectedContentSHA256: localContentSHA256
                )
                clearPassiveError(for: book.bookId)
                if let completionClaim {
                    completePendingImport(completionClaim)
                }
                notice = attachmentManifest.executor == "pc"
                    ? "PC 高质量预处理结果已是最新"
                    : "服务器预处理结果已是最新"
                return true
            }
            let bundle = try await client.downloadAttachments(
                book: book,
                manifest: attachmentManifest,
                cookies: cookies
            )
            if let ownershipClaim,
               !isCurrentPendingImport(ownershipClaim) {
                return false
            }
            let manifest = NativeBookOCRDerivedAttachmentManifest(
                contract: bundle.manifest.contract,
                bookId: bundle.manifest.bookId,
                contentSha256: bundle.manifest.contentSha256,
                revision: bundle.manifest.revision,
                engine: bundle.manifest.engine,
                executor: bundle.manifest.executor,
                processingProfile: bundle.manifest.processingProfile,
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
            if let completionClaim {
                completePendingImport(completionClaim)
            }
            notice = bundle.manifest.executor == "pc"
                ? "已导入 PC 高质量预处理结果，可在文字层中选择"
                : "已导入服务器预处理结果，可在文字层中选择"
            return true
        } catch {
            guard !isCancellation(error) else { return false }
            if let ownershipClaim,
               let piError = error as? ReaderPiOCRError,
               case .localContentMismatch = piError {
                discardPendingImport(ownershipClaim)
            }
            recordError(
                "服务器预处理附件导入失败：\(error.localizedDescription)",
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

    /// Resume request-owned imports after the App returns to the foreground.
    /// Cookies are always reacquired by the App and are never persisted with
    /// the local receipt.
    func resumePendingImports(cookies: [HTTPCookie]) async {
        for pending in Array(pendingImports.values) {
            guard !Task.isCancelled else { return }
            let book = pending.remoteBook
            guard activeBookID != book.bookId else { continue }
            guard isPendingImportFresh(pending) else {
                discardPendingImport(
                    remoteBookID: book.bookId,
                    generation: pending.generation
                )
                continue
            }
            guard ReaderLocalLibraryManager.shared.books.contains(where: {
                $0.id == pending.localBookID
            }) else {
                discardPendingImport(
                    remoteBookID: book.bookId,
                    generation: pending.generation
                )
                continue
            }
            let requestEpoch = commandEpoch(for: book.bookId)
            do {
                let job = try await client.status(book: book, cookies: cookies)
                guard !Task.isCancelled,
                      pendingImports[book.bookId]?.generation
                        == pending.generation,
                      isPassiveResponseCurrent(
                        bookID: book.bookId,
                        epoch: requestEpoch
                      ) else { continue }
                accept(job, book: book, cookies: cookies)
            } catch {
                guard !isCancellation(error),
                      pendingImports[book.bookId]?.generation
                        == pending.generation,
                      isPassiveResponseCurrent(
                        bookID: book.bookId,
                        epoch: requestEpoch
                      ) else { continue }
                recordError(
                    error.localizedDescription,
                    for: book,
                    explicit: false
                )
            }
        }
    }

    private func perform(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie],
        ownershipTransition: PendingOwnershipTransition,
        operation: @escaping () async throws -> ReaderPiOCRJob
    ) async {
        clearError(for: book.bookId)
        notice = nil
        let requestedBinding: LocalBinding?
        switch ownershipTransition {
        case .replaceConfirmed(_, _):
            // Freeze the caller's exact local identity before the network
            // request suspends this actor. An older import may finish while the
            // command is in flight and remove its own receipt/binding.
            requestedBinding = localBindings[book.bookId]
        case .preserve:
            requestedBinding = nil
        }
        await stopPolling(for: book.bookId)
        do {
            let job = try await operation()
            try validateJob(
                job,
                for: book,
                expectedEngine: nil,
                expectedExecutor: nil
            )
            if case let .replaceConfirmed(engine, executor) =
                ownershipTransition {
                _ = try await replacePendingImport(
                    book: book,
                    job: job,
                    expectedEngine: engine,
                    expectedExecutor: executor,
                    requestedBinding: requestedBinding
                )
            }
            accept(job, book: book, cookies: cookies)
            notice = job.message
        } catch {
            schedulePoll(book: book, cookies: cookies)
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
        lastKnownCookies = cookies
        clearPassiveError(for: book.bookId)
        let ownershipClaim = validatedPendingImport(for: job, book: book)
        if importsAttachments,
           job.resultAvailable,
           let ownershipClaim {
            scheduleAttachmentImport(
                book: book,
                ownershipClaim: ownershipClaim,
                cookies: cookies
            )
        }
        if job.isActive {
            schedulePoll(book: book, cookies: cookies)
        } else {
            cancelPollingTask(for: book.bookId)
        }
    }

    private func acquireExplicitCommand(for book: ReaderRemoteBook) -> Bool {
        guard activeBookID == nil else {
            recordError(
                "另一项服务器预处理请求正在进行，请稍后再试",
                for: book,
                explicit: true
            )
            return false
        }
        activeBookID = book.bookId
        beginExplicitCommand(for: book.bookId)
        return true
    }

    private func releaseExplicitCommand(for bookID: String) {
        guard activeBookID == bookID else { return }
        activeBookID = nil
    }

    private func beginExplicitCommand(for bookID: String) {
        commandEpochs[bookID] = UUID()
    }

    private func commandEpoch(for bookID: String) -> UUID {
        if let epoch = commandEpochs[bookID] { return epoch }
        let epoch = UUID()
        commandEpochs[bookID] = epoch
        return epoch
    }

    private func isPassiveResponseCurrent(
        bookID: String,
        epoch: UUID
    ) -> Bool {
        commandEpochs[bookID] == epoch && activeBookID != bookID
    }

    private func isPollingTaskCurrent(bookID: String, token: UUID) -> Bool {
        pollingTaskTokens[bookID] == token
            && pollingTasks[bookID] != nil
    }

    private func finishPollingTask(bookID: String, token: UUID) {
        guard pollingTaskTokens[bookID] == token else { return }
        pollingTasks[bookID] = nil
        pollingTaskTokens[bookID] = nil
    }

    private func cancelPollingTask(for bookID: String) {
        pollingTasks[bookID]?.cancel()
        pollingTasks[bookID] = nil
        pollingTaskTokens[bookID] = nil
    }

    private func stopPolling(for bookID: String) async {
        while let task = pollingTasks[bookID] {
            let token = pollingTaskTokens[bookID]
            task.cancel()
            await task.value
            if token == pollingTaskTokens[bookID] {
                pollingTasks[bookID] = nil
                pollingTaskTokens[bookID] = nil
            }
        }
    }

    private func schedulePoll(book: ReaderRemoteBook, cookies: [HTTPCookie]) {
        guard pollingTasks[book.bookId] == nil else { return }
        let token = UUID()
        pollingTaskTokens[book.bookId] = token
        pollingTasks[book.bookId] = Task { @MainActor [weak self] in
            defer {
                self?.finishPollingTask(bookID: book.bookId, token: token)
            }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard !Task.isCancelled,
                      let self,
                      self.isPollingTaskCurrent(
                        bookID: book.bookId,
                        token: token
                      ),
                      self.activeBookID != book.bookId else { return }
                let requestEpoch = self.commandEpoch(for: book.bookId)
                do {
                    let job = try await self.client.status(book: book, cookies: cookies)
                    guard !Task.isCancelled,
                          self.isPollingTaskCurrent(
                            bookID: book.bookId,
                            token: token
                          ),
                          self.isPassiveResponseCurrent(
                            bookID: book.bookId,
                            epoch: requestEpoch
                          ) else { return }
                    self.accept(job, book: book, cookies: cookies)
                    if !job.isActive { return }
                } catch {
                    guard !self.isCancellation(error),
                          self.isPollingTaskCurrent(
                            bookID: book.bookId,
                            token: token
                          ),
                          self.isPassiveResponseCurrent(
                            bookID: book.bookId,
                            epoch: requestEpoch
                          ) else { return }
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
        // 出声到服务器客户端日志:用最近一次请求带过来的那组 cookie(协调器本身拿不到 WebView)
        client.postClientLog(
            "预处理[\(explicit ? "显式" : "被动")] \(book.name): \(message)",
            cookies: lastKnownCookies
        )
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

    private func reconcilePendingImport(
        localBookID: String?,
        localContentSHA256: String?,
        for book: ReaderRemoteBook
    ) {
        guard let pending = pendingImports[book.bookId] else { return }
        guard let localBookID,
              let localContentSHA256,
              localBookID == pending.localBookID,
              localContentSHA256.caseInsensitiveCompare(
                pending.contentSHA256
              ) == .orderedSame,
              book.contentSha256.caseInsensitiveCompare(
                pending.contentSHA256
              ) == .orderedSame else {
            return
        }
        localBindings[book.bookId] = LocalBinding(
            bookID: pending.localBookID,
            contentSHA256: pending.contentSHA256
        )
    }

    /// Only an explicit, successfully decoded server response may establish
    /// request ownership. If the App dies before receiving that response there
    /// is no safe way to distinguish this request from another device's job
    /// without a server nonce, so that narrow window deliberately fails closed.
    private func replacePendingImport(
        book: ReaderRemoteBook,
        job: ReaderPiOCRJob,
        expectedEngine: String?,
        expectedExecutor: String?,
        requestedBinding: LocalBinding?
    ) async throws -> PendingImportClaim? {
        try validateJob(
            job,
            for: book,
            expectedEngine: expectedEngine,
            expectedExecutor: expectedExecutor
        )
        // `requestedBinding` was frozen before the request itself suspended.
        // The older attachment task may still finish during this second await
        // and remove its own receipt plus localBindings entry; restore only the
        // exact identity captured for this newer request.
        await stopAttachmentImport(for: book.bookId)
        guard let binding = requestedBinding,
              binding.contentSHA256.caseInsensitiveCompare(book.contentSha256)
                == .orderedSame else {
            discardPendingImport(remoteBookID: book.bookId)
            return nil
        }
        localBindings[book.bookId] = binding
        guard let engine = job.engine,
              let jobID = job.jobId else {
            throw ReaderPiOCRError.invalidResponse
        }
        let revision = job.resultAvailable ? job.pageCharsRevision : nil
        let pending = PendingImport(
            remoteBook: book,
            localBookID: binding.bookID,
            contentSHA256: binding.contentSHA256,
            engine: engine,
            executor: Self.normalizedExecutor(job.executor),
            createdAtEpochMs: Int64(Date().timeIntervalSince1970 * 1_000),
            generation: UUID().uuidString,
            jobID: jobID,
            revision: revision
        )
        pendingImports[book.bookId] = pending
        persistPendingImports()
        guard let revision else { return nil }
        return Self.claim(for: pending, revision: revision)
    }

    private func validatedPendingImport(
        for job: ReaderPiOCRJob,
        book: ReaderRemoteBook
    ) -> PendingImportClaim? {
        guard var pending = pendingImports[book.bookId] else { return nil }
        guard pending.contentSHA256.caseInsensitiveCompare(
            book.contentSha256
        ) == .orderedSame else {
            discardPendingImport(
                remoteBookID: book.bookId,
                generation: pending.generation
            )
            return nil
        }
        guard isPendingImportFresh(pending) else {
            discardPendingImport(
                remoteBookID: book.bookId,
                generation: pending.generation
            )
            return nil
        }
        // The status endpoint's intentional idle value has no engine or job ID.
        // It means the owned job no longer exists, rather than that a different
        // device's result should be adopted.
        if job.state == "idle",
           job.bookId == book.bookId,
           job.contentSha256.caseInsensitiveCompare(book.contentSha256)
            == .orderedSame {
            discardPendingImport(
                remoteBookID: book.bookId,
                generation: pending.generation
            )
            return nil
        }
        do {
            try validateJob(
                job,
                for: book,
                expectedEngine: pending.engine,
                expectedExecutor: pending.executor
            )
            guard job.jobId == pending.jobID else {
                throw ReaderPiOCRError.invalidResponse
            }
        } catch {
            recordError(
                "服务器当前结果不属于这台设备发起的预处理请求，已停止自动导入",
                for: book,
                explicit: false
            )
            discardPendingImport(
                remoteBookID: book.bookId,
                generation: pending.generation
            )
            return nil
        }
        if job.resultAvailable, let revision = job.pageCharsRevision {
            guard pending.revision == nil || pending.revision == revision else {
                recordError(
                    "服务器在同一预处理任务下返回了不同结果版本，已停止自动导入",
                    for: book,
                    explicit: false
                )
                discardPendingImport(
                    remoteBookID: book.bookId,
                    generation: pending.generation
                )
                return nil
            }
            pending.revision = revision
            pendingImports[book.bookId] = pending
            persistPendingImports()
            localBindings[book.bookId] = LocalBinding(
                bookID: pending.localBookID,
                contentSHA256: pending.contentSHA256
            )
            return Self.claim(for: pending, revision: revision)
        }
        if ["idle", "failed", "cancelled"].contains(job.state) {
            discardPendingImport(
                remoteBookID: book.bookId,
                generation: pending.generation
            )
        }
        return nil
    }

    private func pendingImportClaim(
        for book: ReaderRemoteBook,
        localBookID: String,
        localContentSHA256: String,
        revision: String
    ) -> PendingImportClaim? {
        guard let pending = pendingImports[book.bookId] else { return nil }
        guard isPendingImportFresh(pending) else {
            discardPendingImport(
                remoteBookID: book.bookId,
                generation: pending.generation
            )
            return nil
        }
        guard
              pending.localBookID == localBookID,
              pending.contentSHA256.caseInsensitiveCompare(
                localContentSHA256
              ) == .orderedSame,
              pending.revision == revision else { return nil }
        return Self.claim(for: pending, revision: revision)
    }

    private func isCurrentPendingImport(_ claim: PendingImportClaim) -> Bool {
        guard let pending = pendingImports[claim.remoteBookID] else {
            return false
        }
        return pending.localBookID == claim.localBookID
            && pending.contentSHA256.caseInsensitiveCompare(
                claim.contentSHA256
            ) == .orderedSame
            && pending.generation == claim.generation
            && pending.jobID == claim.jobID
            && pending.revision == claim.revision
    }

    private func completePendingImport(_ claim: PendingImportClaim) {
        discardPendingImport(
            remoteBookID: claim.remoteBookID,
            generation: claim.generation,
            jobID: claim.jobID,
            revision: claim.revision
        )
    }

    private func discardPendingImport(_ claim: PendingImportClaim) {
        discardPendingImport(
            remoteBookID: claim.remoteBookID,
            generation: claim.generation,
            jobID: claim.jobID,
            revision: claim.revision
        )
    }

    private func discardPendingImport(
        remoteBookID: String,
        generation: String? = nil,
        jobID: String? = nil,
        revision: String? = nil
    ) {
        guard let pending = pendingImports[remoteBookID],
              generation.map({ $0 == pending.generation }) ?? true,
              jobID.map({ $0 == pending.jobID }) ?? true,
              revision.map({ $0 == pending.revision }) ?? true else { return }
        pendingImports.removeValue(forKey: remoteBookID)
        if localBindings[remoteBookID]?.bookID == pending.localBookID {
            localBindings.removeValue(forKey: remoteBookID)
        }
        persistPendingImports()
    }

    private func validateLocalBookStillMatches(
        localBookID: String,
        expectedContentSHA256: String
    ) async throws {
        let library = ReaderLocalLibraryManager.shared
        guard let record = library.books.first(where: { $0.id == localBookID })
        else { throw ReaderPiOCRError.localContentMismatch }
        let digest = try await library.ensureContentSHA256(for: record)
        guard digest.caseInsensitiveCompare(expectedContentSHA256)
            == .orderedSame else {
            throw ReaderPiOCRError.localContentMismatch
        }
    }

    private func persistPendingImports() {
        Self.persist(pendingImports, to: defaults)
    }

    private static func loadPendingImports(
        from defaults: UserDefaults
    ) -> [String: PendingImport] {
        guard let data = defaults.data(forKey: pendingImportsDefaultsKey),
              let decoded = try? JSONDecoder().decode(
                [String: PendingImport].self,
                from: data
              ) else { return [:] }
        let now = Int64(Date().timeIntervalSince1970 * 1_000)
        return decoded.filter { remoteBookID, pending in
            remoteBookID == pending.remoteBook.bookId
                && !pending.localBookID.isEmpty
                && isSHA256(pending.contentSHA256)
                && pending.remoteBook.contentSha256.caseInsensitiveCompare(
                    pending.contentSHA256
                ) == .orderedSame
                && ["vision", "manga", "native", "legacy"].contains(pending.engine)
                && ["pi", "pc"].contains(pending.executor)
                && isJobID(pending.jobID)
                && UUID(uuidString: pending.generation) != nil
                && (pending.revision.map { isRevision($0) } ?? true)
                && pending.createdAtEpochMs <= now + 5 * 60 * 1_000
                && pending.createdAtEpochMs
                    >= now - pendingImportTTLMilliseconds
        }
    }

    private static func persist(
        _ values: [String: PendingImport],
        to defaults: UserDefaults
    ) {
        guard !values.isEmpty else {
            defaults.removeObject(forKey: pendingImportsDefaultsKey)
            return
        }
        guard let data = try? JSONEncoder().encode(values) else { return }
        defaults.set(data, forKey: pendingImportsDefaultsKey)
    }

    private static func normalizedExecutor(_ executor: String?) -> String {
        executor ?? "pi"
    }

    private static func isSHA256(_ value: String) -> Bool {
        value.range(
            of: #"^[0-9a-fA-F]{64}$"#,
            options: .regularExpression
        ) != nil
    }

    private static func isJobID(_ value: String) -> Bool {
        value.range(
            of: #"^ocrjob_[0-9a-f]{32}$"#,
            options: .regularExpression
        ) != nil
    }

    private static func isRevision(_ value: String) -> Bool {
        value.range(
            of: #"^ocr_[0-9a-f]{20}$"#,
            options: .regularExpression
        ) != nil
    }

    private func isPendingImportFresh(_ pending: PendingImport) -> Bool {
        let now = Int64(Date().timeIntervalSince1970 * 1_000)
        return pending.createdAtEpochMs <= now + 5 * 60 * 1_000
            && pending.createdAtEpochMs
                >= now - Self.pendingImportTTLMilliseconds
    }

    private static func claim(
        for pending: PendingImport,
        revision: String
    ) -> PendingImportClaim {
        PendingImportClaim(
            remoteBookID: pending.remoteBook.bookId,
            localBookID: pending.localBookID,
            contentSHA256: pending.contentSHA256,
            generation: pending.generation,
            jobID: pending.jobID,
            revision: revision
        )
    }

    private func validateJob(
        _ job: ReaderPiOCRJob,
        for book: ReaderRemoteBook,
        expectedEngine: String?,
        expectedExecutor: String?
    ) throws {
        guard job.bookId == book.bookId,
              job.contentSha256.caseInsensitiveCompare(book.contentSha256)
                == .orderedSame,
              let jobID = job.jobId,
              Self.isJobID(jobID),
              let engine = job.engine,
              ["vision", "manga", "native", "legacy"].contains(engine),
              (expectedEngine.map({ $0 == engine }) ?? true) else {
            throw ReaderPiOCRError.invalidResponse
        }
        let executor = Self.normalizedExecutor(job.executor)
        guard ["pi", "pc"].contains(executor),
              expectedExecutor.map({ $0 == executor }) ?? true else {
            throw ReaderPiOCRError.invalidResponse
        }
        if job.resultAvailable {
            guard job.state == "succeeded",
                  let revision = job.pageCharsRevision,
                  Self.isRevision(revision) else {
                throw ReaderPiOCRError.invalidResponse
            }
        }
    }

    private func stopAttachmentImport(for remoteBookID: String) async {
        // Drain every slot observed before returning. MainActor is reentrant at
        // task.value; a stale response could otherwise install a second old
        // import in that gap and make the new revision skip scheduling.
        while let task = attachmentTasks[remoteBookID] {
            let token = attachmentTaskTokens[remoteBookID]
            task.cancel()
            await task.value
            if token == attachmentTaskTokens[remoteBookID] {
                attachmentTasks[remoteBookID] = nil
                attachmentTaskTokens[remoteBookID] = nil
            }
        }
    }

    private func isAttachmentTaskCurrent(
        bookID: String,
        token: UUID
    ) -> Bool {
        attachmentTaskTokens[bookID] == token
            && attachmentTasks[bookID] != nil
    }

    private func finishAttachmentTask(bookID: String, token: UUID) {
        guard attachmentTaskTokens[bookID] == token else { return }
        attachmentTasks[bookID] = nil
        attachmentTaskTokens[bookID] = nil
    }

    private func scheduleAttachmentImport(
        book: ReaderRemoteBook,
        ownershipClaim: PendingImportClaim,
        cookies: [HTTPCookie]
    ) {
        guard isCurrentPendingImport(ownershipClaim),
              attachmentTasks[book.bookId] == nil else { return }
        let token = UUID()
        attachmentTaskTokens[book.bookId] = token
        attachmentTasks[book.bookId] = Task { @MainActor [weak self] in
            guard let self,
                  self.isAttachmentTaskCurrent(
                    bookID: book.bookId,
                    token: token
                  ) else { return }
            defer {
                self.finishAttachmentTask(bookID: book.bookId, token: token)
            }
            _ = await self.importAvailableAttachmentsImpl(
                book: book,
                localBookID: ownershipClaim.localBookID,
                localContentSHA256: ownershipClaim.contentSHA256,
                cookies: cookies,
                requiresManifest: true,
                reportsExplicitFailure: false,
                forceReimport: false,
                ownershipClaim: ownershipClaim
            )
        }
    }
}
