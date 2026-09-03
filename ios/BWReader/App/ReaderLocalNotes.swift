import Foundation
import SwiftUI

enum ReaderLocalNotesError: LocalizedError {
    case folderNotConfigured
    case folderUnavailable
    case invalidNote
    case noAvailableFileName
    case sharedIndexUnavailable
    case pendingNotesPreventFolderChange
    case vaultChanged

    var errorDescription: String? {
        switch self {
        case .folderNotConfigured:
            return "请先选择 Obsidian Vault 文件夹"
        case .folderUnavailable:
            return "无法访问已选择的 Obsidian 文件夹，请重新选择"
        case .invalidNote:
            return "笔记名称或内容无效"
        case .noAvailableFileName:
            return "同名笔记过多，请换一个名称"
        case .sharedIndexUnavailable:
            return "笔记已写入，但共享索引更新失败；请保持 App 打开后重试"
        case .pendingNotesPreventFolderChange:
            return "仍有扩展笔记等待写入，请等待同步完成后再更换文件夹"
        case .vaultChanged:
            return "这条待写笔记属于先前选择的 Vault，已暂停以避免写入错误文件夹"
        }
    }
}

struct ReaderLocalNoteWriteReceipt: Sendable {
    let notePath: String
    let obsidianURL: String
}

private struct ReaderLocalNoteFileWriteResult: Sendable {
    let fileURL: URL
    let refreshedBookmark: Data?
}

@MainActor
final class ReaderLocalNotesManager: ObservableObject {
    static let shared = ReaderLocalNotesManager()

    private enum DefaultsKey {
        static let enabled = "reader.localNotes.enabled"
        static let bookmark = "reader.localNotes.folderBookmark"
        static let folderName = "reader.localNotes.folderName"
        static let vaultGeneration = "reader.localNotes.vaultGeneration"
    }

    @Published private(set) var isEnabled: Bool
    @Published private(set) var isConfigured: Bool
    @Published private(set) var folderName: String
    @Published private(set) var notice: String?
    @Published private(set) var errorMessage: String?
    @Published private(set) var notes: [ReaderLocalNoteProjection]

    private let defaults: UserDefaults
    private let featureStore = ReaderNativeFeatureStore()
    private let outbox = ReaderLocalNoteOutboxStore()
    private let writerQueue = DispatchQueue(
        label: "space.bwicarus.bwreader.local-notes",
        qos: .userInitiated
    )
    private var drainingPendingCreates = false
    private var vaultGeneration: String

    private init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        let bookmark = defaults.data(forKey: DefaultsKey.bookmark)
        let existingGeneration = defaults.string(
            forKey: DefaultsKey.vaultGeneration
        ) ?? ""
        let initialGeneration: String
        if bookmark != nil {
            initialGeneration = ReaderNativeBridgeContract.isSafeRequestID(
                existingGeneration
            ) ? existingGeneration : UUID().uuidString
            if initialGeneration != existingGeneration {
                defaults.set(
                    initialGeneration,
                    forKey: DefaultsKey.vaultGeneration
                )
            }
        } else {
            initialGeneration = ""
        }
        let loadedNotes: [ReaderLocalNoteProjection]
        let initialError: String?
        do {
            loadedNotes = try ReaderNativeFeatureStore()
                .loadLocalNotesState()?.notes ?? []
            initialError = nil
        } catch {
            loadedNotes = []
            initialError = "本机笔记索引读取失败：\(error.localizedDescription)"
        }
        isConfigured = bookmark != nil
        folderName = defaults.string(forKey: DefaultsKey.folderName) ?? ""
        isEnabled = bookmark != nil && defaults.bool(forKey: DefaultsKey.enabled)
        notice = nil
        errorMessage = initialError
        notes = loadedNotes
        vaultGeneration = initialGeneration
        if initialError == nil {
            publishSharedState()
        }
    }

    func setEnabled(_ enabled: Bool) {
        errorMessage = nil
        notice = nil
        guard !enabled || isConfigured else {
            isEnabled = false
            defaults.set(false, forKey: DefaultsKey.enabled)
            errorMessage = ReaderLocalNotesError.folderNotConfigured.localizedDescription
            publishSharedState()
            return
        }
        isEnabled = enabled
        defaults.set(enabled, forKey: DefaultsKey.enabled)
        notice = enabled
            ? "本机 Obsidian 笔记已开启"
            : "已切回原有服务器笔记线路"
        publishSharedState()
    }

    func configureFolder(_ url: URL) {
        errorMessage = nil
        notice = nil
        guard url.startAccessingSecurityScopedResource() else {
            errorMessage = ReaderLocalNotesError.folderUnavailable.localizedDescription
            return
        }
        defer { url.stopAccessingSecurityScopedResource() }
        do {
            guard try outbox.pending().isEmpty else {
                throw ReaderLocalNotesError.pendingNotesPreventFolderChange
            }
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(
                atPath: url.path,
                isDirectory: &isDirectory
            ), isDirectory.boolValue else {
                throw ReaderLocalNotesError.folderUnavailable
            }
            let bookmark = try url.bookmarkData(
                options: .minimalBookmark,
                includingResourceValuesForKeys: [.nameKey, .isDirectoryKey],
                relativeTo: nil
            )
            defaults.set(bookmark, forKey: DefaultsKey.bookmark)
            defaults.set(url.lastPathComponent, forKey: DefaultsKey.folderName)
            let generation = UUID().uuidString
            defaults.set(generation, forKey: DefaultsKey.vaultGeneration)
            defaults.set(true, forKey: DefaultsKey.enabled)
            folderName = url.lastPathComponent
            isConfigured = true
            isEnabled = true
            vaultGeneration = generation
            // A new security-scoped folder is a new local authority. Do not
            // present or deduplicate against the previous Vault's projection.
            notes = []
            notice = "已选择 \(folderName)，本机笔记已开启"
            publishSharedState()
        } catch {
            errorMessage = "保存文件夹授权失败：\(error.localizedDescription)"
        }
    }

    func clearFolder() {
        do {
            guard try outbox.pending().isEmpty else {
                errorMessage = ReaderLocalNotesError
                    .pendingNotesPreventFolderChange.localizedDescription
                notice = nil
                return
            }
        } catch {
            errorMessage = "本机笔记队列读取失败：\(error.localizedDescription)"
            notice = nil
            return
        }
        defaults.removeObject(forKey: DefaultsKey.bookmark)
        defaults.removeObject(forKey: DefaultsKey.folderName)
        defaults.removeObject(forKey: DefaultsKey.vaultGeneration)
        defaults.set(false, forKey: DefaultsKey.enabled)
        isEnabled = false
        isConfigured = false
        folderName = ""
        vaultGeneration = ""
        notice = "已移除本机文件夹授权，继续使用服务器笔记线路"
        errorMessage = nil
        publishSharedState()
    }

    func dismissMessages() {
        notice = nil
        errorMessage = nil
    }

    func reportError(_ error: Error) {
        notice = nil
        errorMessage = error.localizedDescription
    }

    func createNote(
        id: String = UUID().uuidString,
        name: String,
        text: String,
        sourceFile: String,
        sourcePage: Int
    ) async throws -> ReaderLocalNoteWriteReceipt {
        guard isEnabled else {
            throw ReaderLocalNotesError.folderNotConfigured
        }
        guard let bookmark = defaults.data(forKey: DefaultsKey.bookmark) else {
            throw ReaderLocalNotesError.folderNotConfigured
        }
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedName.isEmpty, !trimmedText.isEmpty else {
            throw ReaderLocalNotesError.invalidNote
        }
        let safeName = ReaderLocalNoteFormat.sanitizedFileBaseName(trimmedName)
        let markdown = ReaderLocalNoteFormat.markdown(
            name: trimmedName,
            text: trimmedText,
            sourceFile: sourceFile,
            sourcePage: sourcePage
        )
        let writeResult = try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<ReaderLocalNoteFileWriteResult, Error>) in
            writerQueue.async {
                do {
                    continuation.resume(returning: try Self.writeNoteFile(
                        bookmark: bookmark,
                        markdown: markdown,
                        baseName: safeName
                    ))
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
        if let refreshedBookmark = writeResult.refreshedBookmark {
            defaults.set(refreshedBookmark, forKey: DefaultsKey.bookmark)
        }
        let writtenURL = writeResult.fileURL
        let markdownHash = ReaderLocalNoteFormat.contentHash(markdown)
        let record = notes.first(where: {
            $0.fileName == writtenURL.lastPathComponent &&
                $0.contentHash == markdownHash
        }) ?? ReaderLocalNoteProjection(
            id: id,
            title: trimmedName,
            fileName: writtenURL.lastPathComponent,
            content: markdown,
            sourceFile: sourceFile,
            sourcePage: sourcePage,
            pendingExport: false
        )
        notes.removeAll { $0.fileName == record.fileName }
        notes.insert(record, at: 0)
        if notes.count > 50 {
            notes.removeLast(notes.count - 50)
        }
        guard publishSharedState(
            projectionFailurePrefix: "笔记已写入，但扩展索引更新失败"
        ) else {
            throw ReaderLocalNotesError.sharedIndexUnavailable
        }
        notice = "已写入 \(writtenURL.lastPathComponent)"

        var components = URLComponents()
        components.scheme = "obsidian"
        components.host = "open"
        components.queryItems = [
            URLQueryItem(name: "vault", value: folderName),
            URLQueryItem(
                name: "file",
                value: writtenURL.deletingPathExtension().lastPathComponent
            ),
        ]
        return ReaderLocalNoteWriteReceipt(
            notePath: writtenURL.lastPathComponent,
            obsidianURL: components.url?.absoluteString ?? ""
        )
    }

    func drainPendingCreates() async {
        guard isEnabled, isConfigured, !drainingPendingCreates else {
            return
        }
        drainingPendingCreates = true
        defer { drainingPendingCreates = false }
        do {
            for request in try outbox.pending().prefix(20) {
                do {
                    guard request.vaultGeneration == vaultGeneration else {
                        throw ReaderLocalNotesError.vaultChanged
                    }
                    _ = try await createNote(
                        id: request.id,
                        name: request.name,
                        text: request.text,
                        sourceFile: request.sourceFile,
                        sourcePage: request.sourcePage
                    )
                    try outbox.remove(id: request.id)
                } catch {
                    errorMessage = "本机笔记等待同步：\(error.localizedDescription)"
                    return
                }
            }
        } catch {
            errorMessage = "本机笔记队列读取失败：\(error.localizedDescription)"
        }
    }

    nonisolated private static func writeNoteFile(
        bookmark: Data,
        markdown: String,
        baseName: String
    ) throws -> ReaderLocalNoteFileWriteResult {
        var isStale = false
        let folderURL = try URL(
            resolvingBookmarkData: bookmark,
            options: [],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        )
        guard folderURL.startAccessingSecurityScopedResource() else {
            throw ReaderLocalNotesError.folderUnavailable
        }
        defer { folderURL.stopAccessingSecurityScopedResource() }

        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(
            atPath: folderURL.path,
            isDirectory: &isDirectory
        ), isDirectory.boolValue else {
            throw ReaderLocalNotesError.folderUnavailable
        }

        let refreshedBookmark: Data?
        if isStale {
            refreshedBookmark = try folderURL.bookmarkData(
                options: .minimalBookmark,
                includingResourceValuesForKeys: [.nameKey, .isDirectoryKey],
                relativeTo: nil
            )
        } else {
            refreshedBookmark = nil
        }

        let writtenURL = try writeUniqueMarkdown(
            markdown,
            baseName: baseName,
            in: folderURL
        )
        return ReaderLocalNoteFileWriteResult(
            fileURL: writtenURL,
            refreshedBookmark: refreshedBookmark
        )
    }

    nonisolated private static func writeUniqueMarkdown(
        _ markdown: String,
        baseName: String,
        in folderURL: URL
    ) throws -> URL {
        var coordinationError: NSError?
        var result: Result<URL, Error>?
        NSFileCoordinator().coordinate(
            writingItemAt: folderURL,
            options: .forMerging,
            error: &coordinationError
        ) { coordinatedFolder in
            do {
                let fileManager = FileManager.default
                let data = Data(markdown.utf8)
                for suffix in 0..<200 {
                    let name = suffix == 0
                        ? "\(baseName).md"
                        : "\(baseName)-\(suffix).md"
                    let candidate = coordinatedFolder.appendingPathComponent(
                        name,
                        isDirectory: false
                    )
                    if fileManager.fileExists(atPath: candidate.path) {
                        if try Data(contentsOf: candidate) == data {
                            result = .success(candidate)
                            return
                        }
                        continue
                    }
                    do {
                        try data.write(
                            to: candidate,
                            options: [.withoutOverwriting]
                        )
                        result = .success(candidate)
                        return
                    } catch {
                        if fileManager.fileExists(atPath: candidate.path) {
                            if let existing = try? Data(contentsOf: candidate),
                               existing == data {
                                result = .success(candidate)
                                return
                            }
                            continue
                        }
                        throw error
                    }
                }
                throw ReaderLocalNotesError.noAvailableFileName
            } catch {
                result = .failure(error)
            }
        }
        if let coordinationError {
            throw coordinationError
        }
        guard let result else {
            throw CocoaError(.fileWriteUnknown)
        }
        return try result.get()
    }

    @discardableResult
    private func publishSharedState(
        projectionFailurePrefix: String? = nil
    ) -> Bool {
        do {
            try featureStore.writeLocalNotesState(ReaderLocalNotesSharedState(
                enabled: isEnabled,
                configured: isConfigured,
                folderName: folderName,
                vaultGeneration: vaultGeneration,
                notes: notes
            ))
            return true
        } catch {
            errorMessage = "\(projectionFailurePrefix ?? "共享笔记状态更新失败")：\(error.localizedDescription)"
            return false
        }
    }

}
