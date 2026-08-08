import Combine
import CryptoKit
import Foundation

enum ReaderLocalBookFormat: String, Codable, CaseIterable, Sendable {
    case pdf
    case epub

    var title: String {
        switch self {
        case .pdf:
            return "PDF"
        case .epub:
            return "EPUB"
        }
    }
}

enum ReaderLocalBookAvailability: String, Codable, Sendable {
    case available
}

struct ReaderLocalBookRecord: Codable, Hashable, Identifiable, Sendable {
    let id: String
    let libraryID: String
    let contentFingerprint: String
    let relativePath: String
    let title: String
    let format: ReaderLocalBookFormat
    let byteCount: Int64
    let modifiedAt: Date?
    let availability: ReaderLocalBookAvailability
    let contentSha256: String?
}

enum ReaderLocalLibraryError: LocalizedError {
    case folderNotConfigured
    case folderUnavailable
    case indexUnavailable
    case bookFromAnotherLibrary
    case invalidRelativePath
    case bookUnavailable
    case bookChanged

    var errorDescription: String? {
        switch self {
        case .folderNotConfigured:
            return "请先选择本机书籍文件夹"
        case .folderUnavailable:
            return "无法访问已选择的书籍文件夹，请重新选择"
        case .indexUnavailable:
            return "无法保存本机书籍索引"
        case .bookFromAnotherLibrary:
            return "这本书属于先前选择的文件夹，请刷新本机书库"
        case .invalidRelativePath:
            return "本机书籍相对路径无效"
        case .bookUnavailable:
            return "本机书籍已移动、删除或暂时无法读取"
        case .bookChanged:
            return "本机书籍在读取期间发生变化，请刷新书库后重试"
        }
    }
}

/// Keeps the selected folder's security scope alive while a future reader
/// consumes the local file. The library UI only creates this object through
/// its explicit open callback; it does not choose a renderer itself.
final class ReaderLocalBookAccess: Identifiable {
    let record: ReaderLocalBookRecord
    let url: URL

    var id: String { record.id }

    private let scopedRootURL: URL

    fileprivate init(
        record: ReaderLocalBookRecord,
        url: URL,
        scopedRootURL: URL
    ) {
        self.record = record
        self.url = url
        self.scopedRootURL = scopedRootURL
    }

    deinit {
        scopedRootURL.stopAccessingSecurityScopedResource()
    }
}

/// Native-only access to the selected library root. Download/upload clients
/// may retain this object while coordinating an atomic file operation. It is
/// never serialized or exposed to the WebView/Safari extension.
final class ReaderLocalFolderAccess {
    let url: URL

    private let scopedRootURL: URL

    fileprivate init(url: URL, scopedRootURL: URL) {
        self.url = url
        self.scopedRootURL = scopedRootURL
    }

    deinit {
        scopedRootURL.stopAccessingSecurityScopedResource()
    }
}

private struct ReaderLocalLibraryStoredIndex: Codable, Sendable {
    static let currentSchema = "reader-local-library-index/1"

    let schema: String
    let libraryID: String
    let folderName: String
    let scannedAt: Date
    let books: [ReaderLocalBookRecord]
}

private struct ReaderLocalLibraryScanResult: Sendable {
    let books: [ReaderLocalBookRecord]
    let refreshedBookmark: Data?
    let skippedErrors: Int
}

@MainActor
final class ReaderLocalLibraryManager: ObservableObject {
    static let shared = ReaderLocalLibraryManager()

    private enum DefaultsKey {
        static let bookmark = "reader.localLibrary.folderBookmark"
        static let folderName = "reader.localLibrary.folderName"
        static let libraryID = "reader.localLibrary.libraryID"
    }

    @Published private(set) var isConfigured: Bool
    @Published private(set) var isScanning = false
    @Published private(set) var folderName: String
    @Published private(set) var books: [ReaderLocalBookRecord]
    @Published private(set) var lastScannedAt: Date?
    @Published private(set) var skippedErrors = 0
    @Published private(set) var notice: String?
    @Published private(set) var errorMessage: String?

    private let defaults: UserDefaults
    private let indexURL: URL?
    private var libraryID: String

    var stableLibraryID: String { libraryID }

    private init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        indexURL = Self.makeIndexURL()

        let bookmark = defaults.data(forKey: DefaultsKey.bookmark)
        let storedLibraryID = defaults.string(forKey: DefaultsKey.libraryID) ?? ""
        let validLibraryID = UUID(uuidString: storedLibraryID) != nil
            ? storedLibraryID
            : (bookmark == nil ? "" : UUID().uuidString)
        if validLibraryID != storedLibraryID, !validLibraryID.isEmpty {
            defaults.set(validLibraryID, forKey: DefaultsKey.libraryID)
        }

        let loadedIndex = indexURL.flatMap(Self.loadIndex)
        let matchingIndex = loadedIndex?.schema == ReaderLocalLibraryStoredIndex.currentSchema
            && loadedIndex?.libraryID == validLibraryID
            ? loadedIndex
            : nil

        isConfigured = bookmark != nil
        folderName = defaults.string(forKey: DefaultsKey.folderName) ?? ""
        libraryID = validLibraryID
        books = matchingIndex?.books ?? []
        lastScannedAt = matchingIndex?.scannedAt
    }

    func configureFolder(_ url: URL) async {
        guard !isScanning else { return }
        notice = nil
        errorMessage = nil

        guard url.startAccessingSecurityScopedResource() else {
            errorMessage = ReaderLocalLibraryError.folderUnavailable.localizedDescription
            return
        }
        defer { url.stopAccessingSecurityScopedResource() }

        do {
            let values = try url.resourceValues(forKeys: [.isDirectoryKey])
            guard values.isDirectory == true else {
                throw ReaderLocalLibraryError.folderUnavailable
            }

            let bookmark = try url.bookmarkData(
                options: .minimalBookmark,
                includingResourceValuesForKeys: [.nameKey, .isDirectoryKey],
                relativeTo: nil
            )
            let preservesIdentity = Self.sameFolder(
                existingBookmark: defaults.data(forKey: DefaultsKey.bookmark),
                candidate: url
            )
            if !preservesIdentity || libraryID.isEmpty {
                libraryID = UUID().uuidString
                books = []
                lastScannedAt = nil
            }

            folderName = url.lastPathComponent
            isConfigured = true
            defaults.set(bookmark, forKey: DefaultsKey.bookmark)
            defaults.set(folderName, forKey: DefaultsKey.folderName)
            defaults.set(libraryID, forKey: DefaultsKey.libraryID)
            notice = "已选择 \(folderName)，正在建立本机索引"
        } catch {
            errorMessage = "保存书籍文件夹授权失败：\(error.localizedDescription)"
            return
        }

        await rescan()
    }

    func rescan() async {
        guard !isScanning else { return }
        guard let bookmark = defaults.data(forKey: DefaultsKey.bookmark),
              !libraryID.isEmpty else {
            errorMessage = ReaderLocalLibraryError.folderNotConfigured.localizedDescription
            return
        }

        isScanning = true
        notice = nil
        errorMessage = nil
        defer { isScanning = false }
        let scanLibraryID = libraryID
        let previousBooks = books

        do {
            let result = try await Task.detached(priority: .userInitiated) {
                try Self.scan(
                    bookmark: bookmark,
                    libraryID: scanLibraryID,
                    previousBooks: previousBooks
                )
            }.value
            let scannedAt = Date()
            let index = ReaderLocalLibraryStoredIndex(
                schema: ReaderLocalLibraryStoredIndex.currentSchema,
                libraryID: libraryID,
                folderName: folderName,
                scannedAt: scannedAt,
                books: result.books
            )
            guard let indexURL else {
                throw ReaderLocalLibraryError.indexUnavailable
            }
            try Self.writeIndex(index, to: indexURL)

            if let refreshedBookmark = result.refreshedBookmark {
                defaults.set(refreshedBookmark, forKey: DefaultsKey.bookmark)
            }
            books = result.books
            lastScannedAt = scannedAt
            skippedErrors = result.skippedErrors
            notice = result.skippedErrors == 0
                ? "本机书库已更新，共 \(books.count) 本"
                : "已索引 \(books.count) 本，另有 \(result.skippedErrors) 个项目暂时无法读取"
        } catch {
            errorMessage = "扫描本机书库失败：\(error.localizedDescription)"
        }
    }

    func reportError(_ error: Error) {
        notice = nil
        errorMessage = error.localizedDescription
    }

    func dismissMessages() {
        notice = nil
        errorMessage = nil
    }

    /// Provides a retained native-only scope for future atomic downloads.
    /// The caller must retain the returned object until its file operation is
    /// complete; no absolute path crosses into JS or the shared snapshot.
    func makeFolderAccess() throws -> ReaderLocalFolderAccess {
        guard let bookmark = defaults.data(forKey: DefaultsKey.bookmark) else {
            throw ReaderLocalLibraryError.folderNotConfigured
        }
        var isStale = false
        let rootURL = try URL(
            resolvingBookmarkData: bookmark,
            options: [],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        )
        guard rootURL.startAccessingSecurityScopedResource() else {
            throw ReaderLocalLibraryError.folderUnavailable
        }
        do {
            let values = try rootURL.resourceValues(forKeys: [.isDirectoryKey])
            guard values.isDirectory == true else {
                throw ReaderLocalLibraryError.folderUnavailable
            }
            if isStale {
                let refreshedBookmark = try rootURL.bookmarkData(
                    options: .minimalBookmark,
                    includingResourceValuesForKeys: [.nameKey, .isDirectoryKey],
                    relativeTo: nil
                )
                defaults.set(refreshedBookmark, forKey: DefaultsKey.bookmark)
            }
            return ReaderLocalFolderAccess(url: rootURL, scopedRootURL: rootURL)
        } catch {
            rootURL.stopAccessingSecurityScopedResource()
            throw error
        }
    }

    /// Resolves a stable index record into a scoped file access object. The
    /// caller must retain the returned object for the entire read session.
    func makeOpenAccess(for record: ReaderLocalBookRecord) throws -> ReaderLocalBookAccess {
        guard record.libraryID == libraryID else {
            throw ReaderLocalLibraryError.bookFromAnotherLibrary
        }
        guard books.contains(where: {
            $0.id == record.id && $0.relativePath == record.relativePath
        }) else {
            throw ReaderLocalLibraryError.bookUnavailable
        }
        guard let bookmark = defaults.data(forKey: DefaultsKey.bookmark) else {
            throw ReaderLocalLibraryError.folderNotConfigured
        }

        var isStale = false
        let rootURL = try URL(
            resolvingBookmarkData: bookmark,
            options: [],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        )
        guard rootURL.startAccessingSecurityScopedResource() else {
            throw ReaderLocalLibraryError.folderUnavailable
        }

        do {
            let fileURL = try Self.resolve(
                relativePath: record.relativePath,
                beneath: rootURL
            )
            let values = try fileURL.resourceValues(
                forKeys: [.isRegularFileKey, .isReadableKey, .isSymbolicLinkKey]
            )
            guard values.isRegularFile == true,
                  values.isReadable != false,
                  values.isSymbolicLink != true else {
                throw ReaderLocalLibraryError.bookUnavailable
            }
            guard ReaderLocalBookFormat(rawValue: fileURL.pathExtension.lowercased())
                    == record.format else {
                throw ReaderLocalLibraryError.bookUnavailable
            }

            if isStale {
                let refreshedBookmark = try rootURL.bookmarkData(
                    options: .minimalBookmark,
                    includingResourceValuesForKeys: [.nameKey, .isDirectoryKey],
                    relativeTo: nil
                )
                defaults.set(refreshedBookmark, forKey: DefaultsKey.bookmark)
            }
            return ReaderLocalBookAccess(
                record: record,
                url: fileURL,
                scopedRootURL: rootURL
            )
        } catch {
            rootURL.stopAccessingSecurityScopedResource()
            throw error
        }
    }

    /// Computes the authoritative digest only when a transfer or remote merge
    /// needs it. Normal folder scans retain their bounded head/tail fingerprint
    /// and therefore do not re-read every large book.
    func ensureContentSHA256(
        for record: ReaderLocalBookRecord
    ) async throws -> String {
        let access = try makeOpenAccess(for: record)
        defer { withExtendedLifetime(access) {} }
        let fileURL = access.url
        let before = try fileURL.resourceValues(
            forKeys: [.fileSizeKey, .contentModificationDateKey]
        )
        guard Int64(before.fileSize ?? -1) == record.byteCount,
              before.contentModificationDate == record.modifiedAt else {
            throw ReaderLocalLibraryError.bookChanged
        }
        if let digest = record.contentSha256, !digest.isEmpty {
            return digest
        }
        let digest = try await Task.detached(priority: .utility) {
            try Self.fullContentSHA256(of: fileURL)
        }.value
        let after = try fileURL.resourceValues(
            forKeys: [.fileSizeKey, .contentModificationDateKey]
        )
        guard after.fileSize == before.fileSize,
              after.contentModificationDate == before.contentModificationDate else {
            throw ReaderLocalLibraryError.bookChanged
        }

        guard let position = books.firstIndex(where: {
            $0.id == record.id && $0.relativePath == record.relativePath
        }) else {
            throw ReaderLocalLibraryError.bookUnavailable
        }
        let current = books[position]
        books[position] = ReaderLocalBookRecord(
            id: current.id,
            libraryID: current.libraryID,
            contentFingerprint: current.contentFingerprint,
            relativePath: current.relativePath,
            title: current.title,
            format: current.format,
            byteCount: current.byteCount,
            modifiedAt: current.modifiedAt,
            availability: current.availability,
            contentSha256: digest
        )
        if let indexURL {
            let index = ReaderLocalLibraryStoredIndex(
                schema: ReaderLocalLibraryStoredIndex.currentSchema,
                libraryID: libraryID,
                folderName: folderName,
                scannedAt: lastScannedAt ?? Date(),
                books: books
            )
            try Self.writeIndex(index, to: indexURL)
        }
        return digest
    }

    nonisolated private static func scan(
        bookmark: Data,
        libraryID: String,
        previousBooks: [ReaderLocalBookRecord]
    ) throws -> ReaderLocalLibraryScanResult {
        var isStale = false
        let rootURL = try URL(
            resolvingBookmarkData: bookmark,
            options: [],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        )
        guard rootURL.startAccessingSecurityScopedResource() else {
            throw ReaderLocalLibraryError.folderUnavailable
        }
        defer { rootURL.stopAccessingSecurityScopedResource() }

        let rootValues = try rootURL.resourceValues(forKeys: [.isDirectoryKey])
        guard rootValues.isDirectory == true else {
            throw ReaderLocalLibraryError.folderUnavailable
        }

        let refreshedBookmark = isStale
            ? try rootURL.bookmarkData(
                options: .minimalBookmark,
                includingResourceValuesForKeys: [.nameKey, .isDirectoryKey],
                relativeTo: nil
            )
            : nil
        let keys: Set<URLResourceKey> = [
            .isRegularFileKey,
            .isSymbolicLinkKey,
            .fileSizeKey,
            .contentModificationDateKey,
        ]
        var skippedErrors = 0
        let previousByPath = Dictionary(
            previousBooks.map { ($0.relativePath, $0) },
            uniquingKeysWith: { current, _ in current }
        )
        let previousByFingerprint = Dictionary(
            grouping: previousBooks,
            by: \ReaderLocalBookRecord.contentFingerprint
        )
        var claimedBookIDs = Set<String>()
        guard let enumerator = FileManager.default.enumerator(
            at: rootURL,
            includingPropertiesForKeys: Array(keys),
            options: [.skipsHiddenFiles, .skipsPackageDescendants],
            errorHandler: { _, _ in
                skippedErrors += 1
                return true
            }
        ) else {
            throw ReaderLocalLibraryError.folderUnavailable
        }

        var books: [ReaderLocalBookRecord] = []
        while let fileURL = enumerator.nextObject() as? URL {
            do {
                let values = try fileURL.resourceValues(forKeys: keys)
                guard values.isRegularFile == true,
                      values.isSymbolicLink != true,
                      let format = ReaderLocalBookFormat(
                        rawValue: fileURL.pathExtension.lowercased()
                      ) else {
                    continue
                }
                let relativePath = try relativePath(for: fileURL, beneath: rootURL)
                let normalizedPath = relativePath.precomposedStringWithCanonicalMapping
                let title = fileURL.deletingPathExtension().lastPathComponent
                let fingerprint = try sampledContentFingerprint(
                    of: fileURL,
                    format: format
                )
                let fingerprintMatches = previousByFingerprint[fingerprint.digest]
                    ?? []
                let uniqueRenameMatch: ReaderLocalBookRecord?
                if fingerprintMatches.count == 1,
                   let candidate = fingerprintMatches.first,
                   !claimedBookIDs.contains(candidate.id),
                   let previousURL = try? resolve(
                       relativePath: candidate.relativePath,
                       beneath: rootURL
                   ),
                   !FileManager.default.fileExists(atPath: previousURL.path) {
                    uniqueRenameMatch = candidate
                } else {
                    uniqueRenameMatch = nil
                }
                let cached = previousByPath[normalizedPath] ?? uniqueRenameMatch
                let cachedDigest = cached?.contentFingerprint == fingerprint.digest
                    && cached?.byteCount == fingerprint.byteCount
                    && cached?.modifiedAt == values.contentModificationDate
                    ? cached?.contentSha256
                    : nil
                let stableID: String
                if let cached, claimedBookIDs.insert(cached.id).inserted {
                    stableID = cached.id
                } else {
                    stableID = stableBookID(
                        libraryID: libraryID,
                        relativePath: normalizedPath,
                        contentFingerprint: fingerprint.digest
                    )
                    _ = claimedBookIDs.insert(stableID)
                }
                books.append(ReaderLocalBookRecord(
                    id: stableID,
                    libraryID: libraryID,
                    contentFingerprint: fingerprint.digest,
                    relativePath: normalizedPath,
                    title: title.isEmpty ? fileURL.lastPathComponent : title,
                    format: format,
                    byteCount: fingerprint.byteCount,
                    modifiedAt: values.contentModificationDate,
                    availability: .available,
                    contentSha256: cachedDigest
                ))
            } catch {
                skippedErrors += 1
            }
        }

        books.sort {
            $0.relativePath.localizedStandardCompare($1.relativePath) == .orderedAscending
        }
        return ReaderLocalLibraryScanResult(
            books: books,
            refreshedBookmark: refreshedBookmark,
            skippedErrors: skippedErrors
        )
    }

    nonisolated private static func sampledContentFingerprint(
        of fileURL: URL,
        format: ReaderLocalBookFormat
    ) throws -> (digest: String, byteCount: Int64) {
        let sampleLimit = 256 * 1_024
        let handle = try FileHandle(forReadingFrom: fileURL)
        defer { try? handle.close() }

        let fileSize = try handle.seekToEnd()
        try handle.seek(toOffset: 0)
        var hasher = SHA256()
        hasher.update(data: Data("reader-local-book-sample/1\u{0}".utf8))
        hasher.update(data: Data(format.rawValue.utf8))
        var bigEndianSize = fileSize.bigEndian
        withUnsafeBytes(of: &bigEndianSize) {
            hasher.update(bufferPointer: $0)
        }

        if fileSize <= UInt64(sampleLimit * 2) {
            let content = try handle.read(upToCount: Int(fileSize)) ?? Data()
            hasher.update(data: content)
        } else {
            let head = try handle.read(upToCount: sampleLimit) ?? Data()
            hasher.update(data: head)
            try handle.seek(toOffset: fileSize - UInt64(sampleLimit))
            let tail = try handle.read(upToCount: sampleLimit) ?? Data()
            hasher.update(data: tail)
        }

        let digest = hasher.finalize().map {
            String(format: "%02x", $0)
        }.joined()
        return (digest, Int64(clamping: fileSize))
    }

    nonisolated private static func fullContentSHA256(of fileURL: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: fileURL)
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let data = try handle.read(upToCount: 1_048_576) ?? Data()
            if data.isEmpty { break }
            hasher.update(data: data)
        }
        return hasher.finalize().map {
            String(format: "%02x", $0)
        }.joined()
    }

    /// Creates a stable identity for a newly discovered file instance. The
    /// sampled content remains part of the identity, while the normalized path
    /// keeps two byte-identical copies visible as two separate shelf entries.
    /// On later scans the persisted record wins, so an unambiguous rename can
    /// retain its prior identity.
    nonisolated private static func stableBookID(
        libraryID: String,
        relativePath: String,
        contentFingerprint: String
    ) -> String {
        var hasher = SHA256()
        hasher.update(data: Data("reader-local-book-instance/1\u{0}".utf8))
        hasher.update(data: Data(libraryID.utf8))
        hasher.update(data: Data([0]))
        hasher.update(data: Data(relativePath.utf8))
        hasher.update(data: Data([0]))
        hasher.update(data: Data(contentFingerprint.utf8))
        let digest = hasher.finalize().map {
            String(format: "%02x", $0)
        }.joined()
        return "localbook-\(digest)"
    }

    nonisolated private static func relativePath(
        for fileURL: URL,
        beneath rootURL: URL
    ) throws -> String {
        let rootPath = rootURL.standardizedFileURL.path
        let filePath = fileURL.standardizedFileURL.path
        let prefix = rootPath.hasSuffix("/") ? rootPath : rootPath + "/"
        guard filePath.hasPrefix(prefix) else {
            throw ReaderLocalLibraryError.invalidRelativePath
        }
        let relativePath = String(filePath.dropFirst(prefix.count))
        try validate(relativePath: relativePath)
        return relativePath
    }

    nonisolated private static func resolve(
        relativePath: String,
        beneath rootURL: URL
    ) throws -> URL {
        try validate(relativePath: relativePath)
        let components = relativePath.split(separator: "/").map(String.init)
        let candidate = components.reduce(rootURL) {
            $0.appendingPathComponent($1, isDirectory: false)
        }.standardizedFileURL
        _ = try self.relativePath(for: candidate, beneath: rootURL)
        return candidate
    }

    nonisolated private static func validate(relativePath: String) throws {
        let components = relativePath.split(
            separator: "/",
            omittingEmptySubsequences: false
        )
        guard !relativePath.isEmpty,
              !relativePath.hasPrefix("/"),
              !relativePath.contains("\\"),
              components.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else {
            throw ReaderLocalLibraryError.invalidRelativePath
        }
    }

    nonisolated private static func sameFolder(
        existingBookmark: Data?,
        candidate: URL
    ) -> Bool {
        guard let existingBookmark else { return false }
        var isStale = false
        guard let existingURL = try? URL(
            resolvingBookmarkData: existingBookmark,
            options: [],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        ) else {
            return false
        }
        return existingURL.standardizedFileURL == candidate.standardizedFileURL
    }

    nonisolated private static func makeIndexURL() -> URL? {
        let fileManager = FileManager.default
        guard let baseURL = try? fileManager.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ) else {
            return nil
        }
        let directory = baseURL.appendingPathComponent(
            "BWReader/LocalLibrary",
            isDirectory: true
        )
        do {
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: true
            )
            return directory.appendingPathComponent(
                "index.json",
                isDirectory: false
            )
        } catch {
            return nil
        }
    }

    nonisolated private static func loadIndex(
        from url: URL
    ) -> ReaderLocalLibraryStoredIndex? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .millisecondsSince1970
        return try? decoder.decode(ReaderLocalLibraryStoredIndex.self, from: data)
    }

    nonisolated private static func writeIndex(
        _ index: ReaderLocalLibraryStoredIndex,
        to url: URL
    ) throws {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .millisecondsSince1970
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(index)
        try data.write(to: url, options: .atomic)
    }
}
