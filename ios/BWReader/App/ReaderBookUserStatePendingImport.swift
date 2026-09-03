import Foundation

struct ReaderBookUserStateRemotePayload: Sendable {
    let packageData: Data
    let accountScopeDigest: String
}

struct ReaderBookUserStatePendingImport: Codable, Sendable {
    static let currentSchema = "reader-book-user-state-pending/1"

    let schema: String
    let localBookId: String
    let remoteBookId: String
    let contentSha256: String
    let accountScopeDigest: String
    let packageData: Data
    let stagedAt: String
}

struct ReaderBookUserStatePendingFetch: Codable, Sendable {
    static let currentSchema = "reader-book-user-state-pending-fetch/1"

    let schema: String
    let localBookId: String
    let remoteBookId: String
    let contentSha256: String
    let lastError: String?
    let updatedAt: String
}

enum ReaderBookUserStatePendingImportError: LocalizedError {
    case storageUnavailable(String)
    case invalidIdentity
    case contentVersionMismatch
    case invalidStoredPackage
    case authenticationUnavailable
    case accountScopeUnavailable
    case accountScopeChanged

    var errorDescription: String? {
        switch self {
        case .storageUnavailable(let message):
            return "无法保存待导入的书籍附属数据：\(message)"
        case .invalidIdentity:
            return "待导入的书籍附属数据身份无效"
        case .contentVersionMismatch:
            return "待导入的书籍附属数据不属于当前书籍版本"
        case .invalidStoredPackage:
            return "已保存的书籍附属数据无法验证"
        case .authenticationUnavailable:
            return "当前未登录原下载账户，书籍附属数据已保留且未导入"
        case .accountScopeUnavailable:
            return "无法确认当前服务器账户范围，书籍附属数据已保留且未导入"
        case .accountScopeChanged:
            return "当前服务器账户与下载书籍数据时不同，已保留待导入数据且未覆盖本机内容"
        }
    }
}

extension Notification.Name {
    static let readerBookUserStatePendingImportStaged = Notification.Name(
        "reader.book-user-state.pending-import-staged"
    )
    static let readerBookUserStatePendingImportFailed = Notification.Name(
        "reader.book-user-state.pending-import-failed"
    )
    static let readerBookUserStatePendingImportNotice = Notification.Name(
        "reader.book-user-state.pending-import-notice"
    )
}

/// Durable hand-off between the library downloader and the App-owned Reader
/// page. Raw package bytes remain native-only and are removed only after the
/// renderer confirms one atomic import transaction.
actor ReaderBookUserStatePendingImportStore {
    static let shared = ReaderBookUserStatePendingImportStore()

    static let notificationLocalBookIdKey = "localBookId"
    static let notificationMessageKey = "message"

    private let fileManager: FileManager
    private let rootURL: URL?
    private let initializationFailure: String?

    init(
        rootURL: URL? = nil,
        fileManager: FileManager = .default
    ) {
        self.fileManager = fileManager
        do {
            let resolvedRoot: URL
            if let rootURL {
                resolvedRoot = rootURL
            } else {
                let applicationSupport = try fileManager.url(
                    for: .applicationSupportDirectory,
                    in: .userDomainMask,
                    appropriateFor: nil,
                    create: true
                )
                resolvedRoot = applicationSupport
                    .appendingPathComponent("BWReader", isDirectory: true)
                    .appendingPathComponent(
                        "PendingUserStateImports",
                        isDirectory: true
                    )
            }
            try fileManager.createDirectory(
                at: resolvedRoot,
                withIntermediateDirectories: true
            )
            self.rootURL = resolvedRoot
            initializationFailure = nil
        } catch {
            self.rootURL = nil
            initializationFailure = error.localizedDescription
        }
    }

    func stage(
        payload: ReaderBookUserStateRemotePayload,
        localBookId: String,
        remoteBookId: String,
        contentSha256: String
    ) throws {
        guard Self.validLocalBookId(localBookId),
              Self.validRemoteBookId(remoteBookId),
              Self.isSHA256(contentSha256),
              Self.isSHA256(payload.accountScopeDigest) else {
            throw ReaderBookUserStatePendingImportError.invalidIdentity
        }
        let package = try ReaderBookUserStatePackageCodec.decode(
            payload.packageData
        )
        guard package.bookId == remoteBookId,
              package.contentSha256 == contentSha256 else {
            throw ReaderBookUserStatePendingImportError.contentVersionMismatch
        }
        let pending = ReaderBookUserStatePendingImport(
            schema: ReaderBookUserStatePendingImport.currentSchema,
            localBookId: localBookId,
            remoteBookId: remoteBookId,
            contentSha256: contentSha256,
            accountScopeDigest: payload.accountScopeDigest,
            packageData: payload.packageData,
            stagedAt: ISO8601DateFormatter().string(from: Date())
        )
        let encoder = PropertyListEncoder()
        encoder.outputFormat = .binary
        let data = try encoder.encode(pending)
        try data.write(to: try targetURL(localBookId), options: [.atomic])
        try? fileManager.removeItem(at: try fetchTargetURL(localBookId))
        NotificationCenter.default.post(
            name: .readerBookUserStatePendingImportStaged,
            object: nil,
            userInfo: [Self.notificationLocalBookIdKey: localBookId]
        )
    }

    func stageFetchIntent(
        localBookId: String,
        remoteBookId: String,
        contentSha256: String
    ) throws {
        try writeFetchIntent(
            ReaderBookUserStatePendingFetch(
                schema: ReaderBookUserStatePendingFetch.currentSchema,
                localBookId: localBookId,
                remoteBookId: remoteBookId,
                contentSha256: contentSha256,
                lastError: nil,
                updatedAt: ISO8601DateFormatter().string(from: Date())
            )
        )
    }

    func markFetchFailure(
        localBookId: String,
        message: String
    ) throws {
        guard let pending = try loadFetchIntent(localBookId: localBookId) else {
            return
        }
        try writeFetchIntent(ReaderBookUserStatePendingFetch(
            schema: pending.schema,
            localBookId: pending.localBookId,
            remoteBookId: pending.remoteBookId,
            contentSha256: pending.contentSha256,
            lastError: String(message.prefix(1_000)),
            updatedAt: ISO8601DateFormatter().string(from: Date())
        ))
    }

    func loadFetchIntent(
        localBookId: String
    ) throws -> ReaderBookUserStatePendingFetch? {
        let url = try fetchTargetURL(localBookId)
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        do {
            let value = try PropertyListDecoder().decode(
                ReaderBookUserStatePendingFetch.self,
                from: Data(contentsOf: url, options: [.mappedIfSafe])
            )
            guard value.schema == ReaderBookUserStatePendingFetch.currentSchema,
                  value.localBookId == localBookId,
                  Self.validRemoteBookId(value.remoteBookId),
                  Self.isSHA256(value.contentSha256) else {
                throw ReaderBookUserStatePendingImportError.invalidStoredPackage
            }
            return value
        } catch let error as ReaderBookUserStatePendingImportError {
            throw error
        } catch {
            throw ReaderBookUserStatePendingImportError.invalidStoredPackage
        }
    }

    func removeFetchIntent(
        _ pending: ReaderBookUserStatePendingFetch
    ) throws {
        guard let current = try loadFetchIntent(
            localBookId: pending.localBookId
        ), current.remoteBookId == pending.remoteBookId,
           current.contentSha256 == pending.contentSha256 else {
            return
        }
        try fileManager.removeItem(at: try fetchTargetURL(pending.localBookId))
    }

    func load(
        localBookId: String
    ) throws -> ReaderBookUserStatePendingImport? {
        let url = try targetURL(localBookId)
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        let pending: ReaderBookUserStatePendingImport
        do {
            pending = try PropertyListDecoder().decode(
                ReaderBookUserStatePendingImport.self,
                from: Data(contentsOf: url, options: [.mappedIfSafe])
            )
            let package = try ReaderBookUserStatePackageCodec.decode(
                pending.packageData
            )
            guard pending.schema == ReaderBookUserStatePendingImport.currentSchema,
                  pending.localBookId == localBookId,
                  Self.validRemoteBookId(pending.remoteBookId),
                  Self.isSHA256(pending.contentSha256),
                  Self.isSHA256(pending.accountScopeDigest),
                  package.bookId == pending.remoteBookId,
                  package.contentSha256 == pending.contentSha256 else {
                throw ReaderBookUserStatePendingImportError.invalidStoredPackage
            }
        } catch let error as ReaderBookUserStatePendingImportError {
            throw error
        } catch {
            throw ReaderBookUserStatePendingImportError.invalidStoredPackage
        }
        return pending
    }

    func remove(
        _ pending: ReaderBookUserStatePendingImport
    ) throws {
        guard let current = try load(localBookId: pending.localBookId) else {
            return
        }
        guard current.remoteBookId == pending.remoteBookId,
              current.contentSha256 == pending.contentSha256,
              current.accountScopeDigest == pending.accountScopeDigest,
              current.packageData == pending.packageData else {
            // A newer download replaced this hand-off while the old one was
            // importing. Never let the old completion erase the new package.
            return
        }
        if let fetch = try loadFetchIntent(localBookId: pending.localBookId),
           fetch.remoteBookId == pending.remoteBookId,
           fetch.contentSha256 == pending.contentSha256 {
            try fileManager.removeItem(
                at: try fetchTargetURL(pending.localBookId)
            )
        }
        try fileManager.removeItem(at: try targetURL(pending.localBookId))
    }

    nonisolated static func publishFailure(
        localBookId: String,
        message: String
    ) {
        NotificationCenter.default.post(
            name: .readerBookUserStatePendingImportFailed,
            object: nil,
            userInfo: [
                notificationLocalBookIdKey: localBookId,
                notificationMessageKey: String(message.prefix(1_000)),
            ]
        )
    }

    nonisolated static func publishNotice(
        localBookId: String,
        message: String
    ) {
        NotificationCenter.default.post(
            name: .readerBookUserStatePendingImportNotice,
            object: nil,
            userInfo: [
                notificationLocalBookIdKey: localBookId,
                notificationMessageKey: String(message.prefix(1_000)),
            ]
        )
    }

    private func targetURL(_ localBookId: String) throws -> URL {
        guard Self.validLocalBookId(localBookId) else {
            throw ReaderBookUserStatePendingImportError.invalidIdentity
        }
        guard let rootURL else {
            throw ReaderBookUserStatePendingImportError.storageUnavailable(
                initializationFailure ?? "Application Support 不可用"
            )
        }
        let leaf = ReaderBookUserStatePackageCodec.sha256(
            Data(localBookId.utf8)
        ) + ".plist"
        return rootURL.appendingPathComponent(leaf, isDirectory: false)
    }

    private func fetchTargetURL(_ localBookId: String) throws -> URL {
        let packageURL = try targetURL(localBookId)
        return packageURL
            .deletingPathExtension()
            .appendingPathExtension("fetch.plist")
    }

    private func writeFetchIntent(
        _ pending: ReaderBookUserStatePendingFetch
    ) throws {
        guard Self.validLocalBookId(pending.localBookId),
              Self.validRemoteBookId(pending.remoteBookId),
              Self.isSHA256(pending.contentSha256) else {
            throw ReaderBookUserStatePendingImportError.invalidIdentity
        }
        let encoder = PropertyListEncoder()
        encoder.outputFormat = .binary
        try encoder.encode(pending).write(
            to: try fetchTargetURL(pending.localBookId),
            options: [.atomic]
        )
    }

    private nonisolated static func validLocalBookId(_ value: String) -> Bool {
        value.range(
            of: #"^localbook-[A-Za-z0-9_-]{8,160}$"#,
            options: .regularExpression
        ) != nil
    }

    private nonisolated static func validRemoteBookId(_ value: String) -> Bool {
        value.range(
            of: #"^book_[a-f0-9]{32}$"#,
            options: .regularExpression
        ) != nil
    }

    private nonisolated static func isSHA256(_ value: String) -> Bool {
        value.range(
            of: #"^[a-f0-9]{64}$"#,
            options: .regularExpression
        ) != nil
    }
}
