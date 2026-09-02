import CryptoKit
import Combine
import Foundation

struct ReaderRemoteBook: Codable, Hashable, Identifiable, Sendable {
    let bookId: String
    let name: String
    let kind: String
    let rel: String
    let size: Int64
    let mtime: Int64
    let version: String
    let contentSha256: String
    let downloadUrl: String
    let attachmentsUrl: String?

    var id: String { bookId }
}

struct ReaderRemoteLibraryCatalog: Decodable, Sendable {
    let ok: Bool
    let books: [ReaderRemoteBook]
}

struct ReaderRemoteLibraryUploadResult: Decodable, Sendable {
    let ok: Bool
    let deduplicated: Bool
    let book: ReaderRemoteBook
}

enum ReaderRemoteLibraryError: LocalizedError {
    case invalidResponse
    case server(status: Int, message: String)
    case rejected(String)
    case invalidDownloadURL
    case invalidBookName
    case localFileChanged
    case sizeMismatch(expected: Int64, actual: Int64)
    case checksumMismatch

    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "Pi 书库返回了无法识别的数据"
        case .server(let status, let message):
            return "Pi 书库请求失败（HTTP \(status)）：\(message)"
        case .rejected(let message):
            return message
        case .invalidDownloadURL:
            return "Pi 书库返回了不安全的下载地址"
        case .invalidBookName:
            return "书籍文件名无效"
        case .localFileChanged:
            return "本机书籍在上传准备期间发生变化，请刷新书库后重试"
        case .sizeMismatch(let expected, let actual):
            return "下载大小不一致（应为 \(expected) 字节，实际 \(actual) 字节）"
        case .checksumMismatch:
            return "下载校验失败，未保存不完整文件"
        }
    }
}

private final class ReaderRemoteLibraryRedirectDelegate: NSObject,
    URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        // Library API routes are canonical and never need redirects. Refusing
        // them also guarantees a manually supplied Reader cookie cannot be
        // forwarded to a different host by URLSession redirect behavior.
        completionHandler(nil)
    }
}

/// Authenticated client for the Pi-backed durable library. It deliberately
/// receives cookies from the Reader WKWebView instead of storing a second set
/// of credentials in native defaults or the shared App Group.
final class ReaderRemoteLibraryClient {
    static let shared = ReaderRemoteLibraryClient()

    private let baseURL = URL(
        string: "https://bwicarus-2.taile44d0c.ts.net/"
    )!
    private let redirectDelegate: ReaderRemoteLibraryRedirectDelegate
    private let session: URLSession

    private init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpShouldSetCookies = false
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForRequest = 120
        configuration.timeoutIntervalForResource = 3_600
        let redirectDelegate = ReaderRemoteLibraryRedirectDelegate()
        self.redirectDelegate = redirectDelegate
        session = URLSession(
            configuration: configuration,
            delegate: redirectDelegate,
            delegateQueue: nil
        )
    }

    func catalog(cookies: [HTTPCookie]) async throws -> [ReaderRemoteBook] {
        var request = try request(
            path: "pdf/api/library/catalog",
            method: "GET",
            cookies: cookies
        )
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, response) = try await session.data(for: request)
        try validate(
            response: response,
            data: data,
            expectedPathPrefix: "/pdf/api/library/catalog"
        )
        let payload = try JSONDecoder().decode(
            ReaderRemoteLibraryCatalog.self,
            from: data
        )
        guard payload.ok else {
            throw ReaderRemoteLibraryError.rejected("Pi 书库拒绝读取目录")
        }
        return payload.books
    }

    func upload(
        fileURL: URL,
        targetDirectory: String = "资源/uploads",
        cookies: [HTTPCookie]
    ) async throws -> ReaderRemoteLibraryUploadResult {
        let multipartURL = try await Task.detached(priority: .userInitiated) {
            try Self.makeMultipartBody(
                fileURL: fileURL,
                targetDirectory: targetDirectory
            )
        }.value
        defer { try? FileManager.default.removeItem(at: multipartURL.url) }

        var request = try request(
            path: "pdf/api/library/upload",
            method: "POST",
            cookies: cookies
        )
        request.setValue(
            "multipart/form-data; boundary=\(multipartURL.boundary)",
            forHTTPHeaderField: "Content-Type"
        )
        let (data, response) = try await session.upload(
            for: request,
            fromFile: multipartURL.url
        )
        try validate(
            response: response,
            data: data,
            expectedPathPrefix: "/pdf/api/library/upload"
        )
        let payload = try JSONDecoder().decode(
            ReaderRemoteLibraryUploadResult.self,
            from: data
        )
        guard payload.ok else {
            throw ReaderRemoteLibraryError.rejected("Pi 书库拒绝上传")
        }
        return payload
    }

    /// Downloads into the selected user folder. The final name is chosen
    /// without overwriting an existing file; bytes are verified in a hidden
    /// staging file in that same directory and only then renamed into place.
    func download(
        book: ReaderRemoteBook,
        destinationDirectory: URL,
        cookies: [HTTPCookie]
    ) async throws -> URL {
        guard !book.name.isEmpty,
              book.name != ".",
              book.name != "..",
              !book.name.contains("/"),
              !book.name.contains("\\") else {
            throw ReaderRemoteLibraryError.invalidBookName
        }
        guard let url = URL(string: book.downloadUrl, relativeTo: baseURL),
              let scheme = url.scheme?.lowercased(),
              scheme == baseURL.scheme?.lowercased(),
              url.host?.lowercased() == baseURL.host?.lowercased(),
              url.port == baseURL.port,
              url.path.hasPrefix("/pdf/api/library/download/") else {
            throw ReaderRemoteLibraryError.invalidDownloadURL
        }

        var request = URLRequest(url: url.absoluteURL)
        request.httpMethod = "GET"
        apply(cookies: cookies, to: &request)
        let (temporaryURL, response) = try await session.download(for: request)
        try validate(
            response: response,
            data: Data(),
            expectedPathPrefix: "/pdf/api/library/download/"
        )

        let fileManager = FileManager.default
        let stagingURL = destinationDirectory.appendingPathComponent(
            ".bwreader-\(UUID().uuidString).download",
            isDirectory: false
        )
        defer { try? fileManager.removeItem(at: stagingURL) }
        try fileManager.copyItem(at: temporaryURL, to: stagingURL)

        let values = try stagingURL.resourceValues(forKeys: [.fileSizeKey])
        let actualSize = Int64(values.fileSize ?? 0)
        if book.size > 0, actualSize != book.size {
            throw ReaderRemoteLibraryError.sizeMismatch(
                expected: book.size,
                actual: actualSize
            )
        }
        if !book.contentSha256.isEmpty {
            let actualHash = try sha256(of: stagingURL)
            guard actualHash.caseInsensitiveCompare(book.contentSha256)
                == .orderedSame else {
                throw ReaderRemoteLibraryError.checksumMismatch
            }
        }

        let destinationURL = availableDestination(
            directory: destinationDirectory,
            preferredName: book.name
        )
        try fileManager.moveItem(at: stagingURL, to: destinationURL)
        return destinationURL
    }

    /// Fetches the account-scoped mutable state separately from immutable OCR
    /// attachments. A missing package is normal; every other response is
    /// verified before raw JSON bytes may enter native staging.
    func userStatePackage(
        book: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async throws -> ReaderBookUserStateRemotePayload? {
        try await userStatePackage(
            bookId: book.bookId,
            contentSha256: book.contentSha256,
            cookies: cookies
        )
    }

    func userStatePackage(
        bookId: String,
        contentSha256: String,
        cookies: [HTTPCookie]
    ) async throws -> ReaderBookUserStateRemotePayload? {
        guard bookId.range(
            of: #"^book_[a-f0-9]{32}$"#,
            options: .regularExpression
        ) != nil,
              contentSha256.range(
                of: #"^[a-f0-9]{64}$"#,
                options: .regularExpression
              ) != nil else {
            throw ReaderRemoteLibraryError.invalidResponse
        }
        let expectedPath = "/pdf/api/library/user-state/\(bookId)"
        var components = URLComponents(
            url: baseURL.appendingPathComponent(
                "pdf/api/library/user-state/\(bookId)"
            ),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [
            URLQueryItem(
                name: "contentSha256",
                value: contentSha256
            ),
        ]
        guard let url = components?.url,
              url.scheme?.lowercased() == baseURL.scheme?.lowercased(),
              url.host?.lowercased() == baseURL.host?.lowercased(),
              url.port == baseURL.port,
              url.path == expectedPath else {
            throw ReaderRemoteLibraryError.invalidDownloadURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        apply(cookies: cookies, to: &request)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse,
              http.url?.scheme?.lowercased() == baseURL.scheme?.lowercased(),
              http.url?.host?.lowercased() == baseURL.host?.lowercased(),
              http.url?.port == baseURL.port,
              http.url?.path == expectedPath else {
            throw ReaderRemoteLibraryError.rejected(
                "Reader 登录状态已失效，请先在 App 中重新登录"
            )
        }
        if http.statusCode == 404 { return nil }
        try validate(
            response: response,
            data: data,
            expectedPathPrefix: expectedPath
        )
        guard data.count <= ReaderBookUserStatePackageCodec.maximumPackageBytes,
              http.value(
                forHTTPHeaderField: "X-Reader-User-State-Contract"
              ) == ReaderBookUserStatePackage.currentContract,
              let accountScopeDigest = http.value(
                forHTTPHeaderField: "X-Reader-Account-Scope-Digest"
              )?.lowercased(),
              accountScopeDigest.range(
                of: #"^[a-f0-9]{64}$"#,
                options: .regularExpression
              ) != nil else {
            throw ReaderRemoteLibraryError.rejected(
                "Pi 返回的书籍附属数据账户证明无效"
            )
        }
        let package = try ReaderBookUserStatePackageCodec.decode(data)
        guard package.bookId == bookId,
              package.contentSha256 == contentSha256 else {
            throw ReaderBookUserStatePackageError.contentVersionMismatch
        }
        return ReaderBookUserStateRemotePayload(
            packageData: data,
            accountScopeDigest: accountScopeDigest
        )
    }

    private func request(
        path: String,
        method: String,
        cookies: [HTTPCookie]
    ) throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL,
              url.scheme == baseURL.scheme,
              url.host == baseURL.host,
              url.port == baseURL.port else {
            throw ReaderRemoteLibraryError.invalidDownloadURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        apply(cookies: cookies, to: &request)
        return request
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
        let fields = HTTPCookie.requestHeaderFields(
            with: Array(preferredByName.values)
        )
        fields.forEach { request.setValue($1, forHTTPHeaderField: $0) }
    }

    private func validate(
        response: URLResponse,
        data: Data,
        expectedPathPrefix: String
    ) throws {
        guard let http = response as? HTTPURLResponse else {
            throw ReaderRemoteLibraryError.invalidResponse
        }
        guard http.url?.scheme?.lowercased() == baseURL.scheme?.lowercased(),
              http.url?.host?.lowercased() == baseURL.host?.lowercased(),
              http.url?.port == baseURL.port,
              http.url?.path.hasPrefix(expectedPathPrefix) == true else {
            throw ReaderRemoteLibraryError.rejected(
                "Reader 登录状态已失效，请先在 App 中重新登录"
            )
        }
        guard (200..<300).contains(http.statusCode) else {
            let message = String(data: data.prefix(1_024), encoding: .utf8)
                ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
            throw ReaderRemoteLibraryError.server(
                status: http.statusCode,
                message: message
            )
        }
    }

    private static func makeMultipartBody(
        fileURL: URL,
        targetDirectory: String
    ) throws -> (url: URL, boundary: String) {
        let boundary = "BWReaderBoundary\(UUID().uuidString.replacingOccurrences(of: "-", with: ""))"
        let outputURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("bwreader-upload-\(UUID().uuidString).multipart")
        var completed = false
        defer {
            if !completed {
                try? FileManager.default.removeItem(at: outputURL)
            }
        }
        let before = try fileURL.resourceValues(
            forKeys: [.fileSizeKey, .contentModificationDateKey]
        )
        FileManager.default.createFile(atPath: outputURL.path, contents: nil)
        let output = try FileHandle(forWritingTo: outputURL)
        defer { try? output.close() }

        func write(_ string: String) throws {
            guard let data = string.data(using: .utf8) else {
                throw ReaderRemoteLibraryError.invalidResponse
            }
            try output.write(contentsOf: data)
        }

        let safeName = fileURL.lastPathComponent
            .replacingOccurrences(of: "\"", with: "_")
            .replacingOccurrences(of: "\r", with: "_")
            .replacingOccurrences(of: "\n", with: "_")
        try write("--\(boundary)\r\n")
        try write("Content-Disposition: form-data; name=\"target_dir\"\r\n\r\n")
        try write(targetDirectory)
        try write("\r\n--\(boundary)\r\n")
        try write(
            "Content-Disposition: form-data; name=\"file\"; filename=\"\(safeName)\"\r\n"
        )
        let mime = fileURL.pathExtension.lowercased() == "epub"
            ? "application/epub+zip"
            : "application/pdf"
        try write("Content-Type: \(mime)\r\n\r\n")

        let input = try FileHandle(forReadingFrom: fileURL)
        defer { try? input.close() }
        while true {
            let chunk = try input.read(upToCount: 1_048_576) ?? Data()
            if chunk.isEmpty { break }
            try output.write(contentsOf: chunk)
        }
        try write("\r\n--\(boundary)--\r\n")
        try output.synchronize()
        let after = try fileURL.resourceValues(
            forKeys: [.fileSizeKey, .contentModificationDateKey]
        )
        guard before.fileSize == after.fileSize,
              before.contentModificationDate == after.contentModificationDate else {
            throw ReaderRemoteLibraryError.localFileChanged
        }
        completed = true
        return (outputURL, boundary)
    }

    private func availableDestination(
        directory: URL,
        preferredName: String
    ) -> URL {
        let fileManager = FileManager.default
        let original = directory.appendingPathComponent(preferredName)
        guard fileManager.fileExists(atPath: original.path) else { return original }

        let extensionName = original.pathExtension
        let stem = original.deletingPathExtension().lastPathComponent
        for index in 1...999 {
            let suffix = extensionName.isEmpty
                ? "\(stem)-\(index)"
                : "\(stem)-\(index).\(extensionName)"
            let candidate = directory.appendingPathComponent(suffix)
            if !fileManager.fileExists(atPath: candidate.path) {
                return candidate
            }
        }
        return directory.appendingPathComponent(
            "\(UUID().uuidString)-\(preferredName)"
        )
    }

    private func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let data = try handle.read(upToCount: 1_048_576) ?? Data()
            if data.isEmpty { break }
            hasher.update(data: data)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}

enum ReaderLibrarySyncState: String, Sendable {
    case localOnly
    case piOnly
    case synced
    case localNewer
    case piNewer
    case conflict

    var title: String {
        switch self {
        case .localOnly: return "仅本机"
        case .piOnly: return "仅 Pi"
        case .synced: return "本机 + Pi"
        case .localNewer: return "本机有更新"
        case .piNewer: return "Pi 有更新"
        case .conflict: return "两端均有更新"
        }
    }
}

private struct ReaderLibrarySyncLink: Codable, Sendable {
    let localLibraryID: String
    let localBookID: String
    let remoteBookID: String
    let lastSyncedSha256: String
}

private struct ReaderLibrarySyncLinkStore: Codable, Sendable {
    static let currentSchema = "reader-library-sync-links/1"

    let schema: String
    let links: [ReaderLibrarySyncLink]
}

@MainActor
final class ReaderRemoteLibraryCoordinator: ObservableObject {
    @Published private(set) var books: [ReaderRemoteBook] = []
    @Published private(set) var isRefreshing = false
    @Published private(set) var activeBookID: String?
    @Published private(set) var remoteToLocalID: [String: String] = [:]
    @Published private(set) var localDigests: [String: String] = [:]
    @Published private(set) var notice: String?
    @Published private(set) var errorMessage: String?

    private let client = ReaderRemoteLibraryClient.shared
    private let pendingUserStateStore =
        ReaderBookUserStatePendingImportStore.shared
    private let defaults: UserDefaults
    private var links: [ReaderLibrarySyncLink]
    private var activeLibraryID = ""

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if let data = defaults.data(forKey: "reader.library.syncLinks"),
           let stored = try? JSONDecoder().decode(
                ReaderLibrarySyncLinkStore.self,
                from: data
           ),
           stored.schema == ReaderLibrarySyncLinkStore.currentSchema {
            links = stored.links
        } else {
            links = []
        }
    }

    func refresh(
        cookies: [HTTPCookie],
        localLibrary: ReaderLocalLibraryManager
    ) async {
        guard !isRefreshing else { return }
        isRefreshing = true
        errorMessage = nil
        defer { isRefreshing = false }
        do {
            let catalog = try await client.catalog(cookies: cookies)
            books = catalog.sorted {
                $0.name.localizedStandardCompare($1.name) == .orderedAscending
            }
            await reconcile(localLibrary: localLibrary)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func upload(
        _ localBook: ReaderLocalBookRecord,
        localLibrary: ReaderLocalLibraryManager,
        cookies: [HTTPCookie]
    ) async -> ReaderRemoteBook? {
        guard activeBookID == nil else { return nil }
        activeBookID = localBook.id
        notice = nil
        errorMessage = nil
        defer { activeBookID = nil }
        do {
            let access = try localLibrary.makeOpenAccess(for: localBook)
            defer { withExtendedLifetime(access) {} }
            let result = try await client.upload(
                fileURL: access.url,
                cookies: cookies
            )
            books.removeAll { $0.bookId == result.book.bookId }
            books.append(result.book)
            books.sort {
                $0.name.localizedStandardCompare($1.name) == .orderedAscending
            }
            localDigests[localBook.id] = result.book.contentSha256
            upsertLink(
                localBook: localBook,
                remoteBook: result.book,
                syncedDigest: result.book.contentSha256
            )
            remoteToLocalID = remoteToLocalID.filter { $0.value != localBook.id }
            remoteToLocalID[result.book.bookId] = localBook.id
            notice = result.deduplicated
                ? "Pi 已有相同内容，已关联并准备打开"
                : "已上传到 Pi 书库"
            return result.book
        } catch {
            errorMessage = "上传失败：\(error.localizedDescription)"
            return nil
        }
    }

    func download(
        _ remoteBook: ReaderRemoteBook,
        localLibrary: ReaderLocalLibraryManager,
        cookies: [HTTPCookie]
    ) async -> ReaderLocalBookRecord? {
        guard activeBookID == nil else { return nil }
        activeBookID = remoteBook.bookId
        notice = nil
        errorMessage = nil
        defer { activeBookID = nil }
        do {
            let folderAccess = try localLibrary.makeFolderAccess()
            defer { withExtendedLifetime(folderAccess) {} }
            let savedURL = try await client.download(
                book: remoteBook,
                destinationDirectory: folderAccess.url,
                cookies: cookies
            )
            await localLibrary.rescan()
            let downloaded = localLibrary.books.first(where: {
                $0.relativePath == savedURL.lastPathComponent
            })
            if let downloaded {
                localDigests[downloaded.id] = remoteBook.contentSha256
                upsertLink(
                    localBook: downloaded,
                    remoteBook: remoteBook,
                    syncedDigest: remoteBook.contentSha256
                )
            }
            await reconcile(localLibrary: localLibrary)
            notice = "已下载到本机：\(savedURL.lastPathComponent)"
            return downloaded
        } catch {
            errorMessage = "下载失败：\(error.localizedDescription)"
            return nil
        }
    }

    /// Runs beside immutable OCR attachment download. A mutable user-state
    /// failure never rolls back the verified original book and the durable
    /// fetch intent lets the Reader retry after the local page is ready.
    func fetchAndStageUserState(
        for remoteBook: ReaderRemoteBook,
        localBook: ReaderLocalBookRecord,
        cookies: [HTTPCookie]
    ) async {
        do {
            try await pendingUserStateStore.stageFetchIntent(
                localBookId: localBook.id,
                remoteBookId: remoteBook.bookId,
                contentSha256: remoteBook.contentSha256
            )
        } catch {
            let message = error.localizedDescription
            errorMessage = "书籍已下载，但无法保留附属数据重试任务：\(message)"
            ReaderBookUserStatePendingImportStore.publishFailure(
                localBookId: localBook.id,
                message: errorMessage ?? message
            )
            return
        }

        do {
            guard let payload = try await client.userStatePackage(
                book: remoteBook,
                cookies: cookies
            ) else {
                let message = "Pi 上这本书暂无用户附属数据；原书和识别附件不受影响"
                try? await pendingUserStateStore.markFetchFailure(
                    localBookId: localBook.id,
                    message: message
                )
                notice = message
                ReaderBookUserStatePendingImportStore.publishNotice(
                    localBookId: localBook.id,
                    message: message
                )
                return
            }
            try await pendingUserStateStore.stage(
                payload: payload,
                localBookId: localBook.id,
                remoteBookId: remoteBook.bookId,
                contentSha256: remoteBook.contentSha256
            )
            notice = "书籍已下载，Pi 用户数据将在本机阅读页就绪后安全合并"
        } catch {
            let message = error.localizedDescription
            try? await pendingUserStateStore.markFetchFailure(
                localBookId: localBook.id,
                message: message
            )
            errorMessage = "书籍已下载，但 Pi 用户数据获取失败：\(message)；打开本书可重试"
            ReaderBookUserStatePendingImportStore.publishFailure(
                localBookId: localBook.id,
                message: errorMessage ?? message
            )
        }
    }

    func dismissMessages() {
        notice = nil
        errorMessage = nil
    }

    func localBookID(for remoteBook: ReaderRemoteBook) -> String? {
        remoteToLocalID[remoteBook.bookId]
    }

    func remoteBook(for localBook: ReaderLocalBookRecord) -> ReaderRemoteBook? {
        guard let remoteID = remoteToLocalID.first(where: {
            $0.value == localBook.id
        })?.key else {
            return nil
        }
        return books.first { $0.bookId == remoteID }
    }

    /// Produces the only book binding accepted by the native Pi gateway. The
    /// remote entry must come from the current catalog, the mapping must belong
    /// to the active local folder and every available local digest must agree
    /// with that catalog entry. A persisted v1 link alone is intentionally not
    /// sufficient because the Pi file may have changed since the last launch.
    func verifiedNativeRemoteBookBinding(
        for localBook: ReaderLocalBookRecord,
        localContentSHA256: String?
    ) -> ReaderNativeRemoteBookBinding? {
        guard activeLibraryID == localBook.libraryID,
              let remoteBook = remoteBook(for: localBook),
              remoteBook.kind.lowercased() == localBook.format.rawValue else {
            return nil
        }
        let indexedDigest = localContentSHA256.flatMap(Self.normalizedSHA256)
        let reconciledDigest = localDigests[localBook.id]
            .flatMap(Self.normalizedSHA256)
        if let indexedDigest, let reconciledDigest,
           indexedDigest != reconciledDigest {
            return nil
        }
        guard let localDigest = reconciledDigest ?? indexedDigest,
              let remoteDigest = Self.normalizedSHA256(
                remoteBook.contentSha256
              ),
              localDigest == remoteDigest else {
            return nil
        }
        return ReaderNativeRemoteBookBinding(
            localLibraryID: localBook.libraryID,
            localBookID: localBook.id,
            remoteBookID: remoteBook.bookId,
            localContentSHA256: localDigest,
            remoteContentSHA256: remoteDigest,
            remoteRelativePath: remoteBook.rel
        )
    }

    func syncState(for localBook: ReaderLocalBookRecord) -> ReaderLibrarySyncState {
        guard let remoteBook = remoteBook(for: localBook) else { return .localOnly }
        return linkedState(localBook: localBook, remoteBook: remoteBook)
    }

    func syncState(for remoteBook: ReaderRemoteBook) -> ReaderLibrarySyncState {
        guard let localID = remoteToLocalID[remoteBook.bookId],
              let digest = localDigests[localID],
              let link = links.first(where: {
                  $0.localLibraryID == activeLibraryID
                    && $0.remoteBookID == remoteBook.bookId
                    && $0.localBookID == localID
              }) else {
            return .piOnly
        }
        return compare(
            localDigest: digest,
            remoteDigest: remoteBook.contentSha256,
            lastSyncedDigest: link.lastSyncedSha256
        )
    }

    func readerURL(for book: ReaderRemoteBook) -> URL? {
        var components = URLComponents(
            url: URL(
                string: book.kind.lowercased() == "epub"
                    ? "https://bwicarus-2.taile44d0c.ts.net/pdf/epub/view"
                    : "https://bwicarus-2.taile44d0c.ts.net/pdf/view"
            )!,
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [URLQueryItem(name: "file", value: book.rel)]
        return components?.url
    }

    private func reconcile(localLibrary: ReaderLocalLibraryManager) async {
        var matches: [String: String] = [:]
        var digests: [String: String] = [:]
        let libraryID = localLibrary.stableLibraryID
        activeLibraryID = libraryID
        let localIDs = Set(localLibrary.books.map(\.id))
        let remoteIDs = Set(books.map(\.bookId))
        for link in links where
            link.localLibraryID == libraryID
                && localIDs.contains(link.localBookID)
                && remoteIDs.contains(link.remoteBookID) {
            matches[link.remoteBookID] = link.localBookID
        }
        let candidatesByShape = Dictionary(
            grouping: books,
            by: { "\($0.kind.lowercased()):\($0.size)" }
        )
        for localBook in localLibrary.books {
            let shape = "\(localBook.format.rawValue):\(localBook.byteCount)"
            let candidates = candidatesByShape[shape] ?? []
            let hasPersistedLink = matches.values.contains(localBook.id)
            guard hasPersistedLink || !candidates.isEmpty else {
                continue
            }
            do {
                let digest = try await localLibrary.ensureContentSHA256(for: localBook)
                digests[localBook.id] = digest
                if !hasPersistedLink,
                   let remoteBook = candidates.first(where: {
                       matches[$0.bookId] == nil
                           && $0.contentSha256.caseInsensitiveCompare(digest)
                               == .orderedSame
                   }) {
                    matches[remoteBook.bookId] = localBook.id
                    upsertLink(
                        localBook: localBook,
                        remoteBook: remoteBook,
                        syncedDigest: digest,
                        persist: false
                    )
                }
            } catch {
                // A moved or temporarily unavailable local file must not make
                // the remote catalog unusable. The local scan reports it.
            }
        }
        remoteToLocalID = matches
        localDigests = digests
        persistLinks()
    }

    private func linkedState(
        localBook: ReaderLocalBookRecord,
        remoteBook: ReaderRemoteBook
    ) -> ReaderLibrarySyncState {
        guard let digest = localDigests[localBook.id],
              let link = links.first(where: {
                  $0.localLibraryID == localBook.libraryID
                    && $0.localBookID == localBook.id
                    && $0.remoteBookID == remoteBook.bookId
              }) else {
            return .conflict
        }
        return compare(
            localDigest: digest,
            remoteDigest: remoteBook.contentSha256,
            lastSyncedDigest: link.lastSyncedSha256
        )
    }

    private func compare(
        localDigest: String,
        remoteDigest: String,
        lastSyncedDigest: String
    ) -> ReaderLibrarySyncState {
        if localDigest.caseInsensitiveCompare(remoteDigest) == .orderedSame {
            return .synced
        }
        let localUnchanged = localDigest.caseInsensitiveCompare(lastSyncedDigest)
            == .orderedSame
        let remoteUnchanged = remoteDigest.caseInsensitiveCompare(lastSyncedDigest)
            == .orderedSame
        if localUnchanged, !remoteUnchanged { return .piNewer }
        if remoteUnchanged, !localUnchanged { return .localNewer }
        return .conflict
    }

    private static func normalizedSHA256(_ value: String) -> String? {
        guard value.range(
            of: #"^[a-fA-F0-9]{64}$"#,
            options: .regularExpression
        ) != nil else {
            return nil
        }
        return value.lowercased()
    }

    private func upsertLink(
        localBook: ReaderLocalBookRecord,
        remoteBook: ReaderRemoteBook,
        syncedDigest: String,
        persist: Bool = true
    ) {
        links.removeAll {
            $0.localLibraryID == localBook.libraryID
                && ($0.localBookID == localBook.id
                    || $0.remoteBookID == remoteBook.bookId)
        }
        links.append(ReaderLibrarySyncLink(
            localLibraryID: localBook.libraryID,
            localBookID: localBook.id,
            remoteBookID: remoteBook.bookId,
            lastSyncedSha256: syncedDigest
        ))
        if persist { persistLinks() }
    }

    private func persistLinks() {
        let stored = ReaderLibrarySyncLinkStore(
            schema: ReaderLibrarySyncLinkStore.currentSchema,
            links: links
        )
        if let data = try? JSONEncoder().encode(stored) {
            defaults.set(data, forKey: "reader.library.syncLinks")
        }
    }
}
