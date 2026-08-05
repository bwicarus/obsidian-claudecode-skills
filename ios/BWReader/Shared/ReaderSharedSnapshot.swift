import Foundation

struct ReaderSharedSnapshot: Codable, Equatable, Sendable {
    static let schema = 1

    let schema: Int
    let updatedAtMilliseconds: Int64
    let title: String
    let url: String
    let file: String
    let page: String
    let pageCount: String
    let selection: String
    let visibleText: String

    init(
        title: String,
        url: String,
        file: String = "",
        page: String = "",
        pageCount: String = "",
        selection: String = "",
        visibleText: String = "",
        now: Date = Date()
    ) {
        schema = Self.schema
        updatedAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
        self.title = String(title.prefix(1_024))
        self.url = String(url.prefix(2_048))
        self.file = String(file.prefix(2_048))
        self.page = String(page.prefix(64))
        self.pageCount = String(pageCount.prefix(64))
        self.selection = String(selection.prefix(4_096))
        self.visibleText = String(visibleText.prefix(16_384))
    }

    var updatedAt: Date {
        Date(timeIntervalSince1970: TimeInterval(updatedAtMilliseconds) / 1_000)
    }

    var pageSummary: String {
        guard !page.isEmpty else { return "最近阅读" }
        if !pageCount.isEmpty {
            return "第 \(page) 页 / 共 \(pageCount) 页"
        }
        return "第 \(page) 页"
    }

    var searchableText: String {
        let preferred = selection.isEmpty ? visibleText : selection
        return String(preferred.prefix(4_000))
    }

    func hasSameContent(as other: ReaderSharedSnapshot) -> Bool {
        title == other.title &&
            url == other.url &&
            file == other.file &&
            page == other.page &&
            pageCount == other.pageCount &&
            selection == other.selection &&
            visibleText == other.visibleText
    }
}

enum ReaderNativeFeatureAction: String, Codable, Sendable {
    case openReader
    case scanCurrentPage
    case annotateCurrentPage
    case openNativeTools
}

struct ReaderNativeFeatureRequest: Codable, Equatable, Sendable {
    let id: String
    let action: ReaderNativeFeatureAction
    let createdAtMilliseconds: Int64

    init(
        action: ReaderNativeFeatureAction,
        id: String = UUID().uuidString,
        now: Date = Date()
    ) {
        self.id = id
        self.action = action
        createdAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
    }

    var isFresh: Bool {
        let created = Date(
            timeIntervalSince1970: TimeInterval(createdAtMilliseconds) / 1_000
        )
        let age = Date().timeIntervalSince(created)
        return age >= -2 && age <= 120
    }
}

struct ReaderQuickNote: Codable, Equatable, Identifiable, Sendable {
    let id: String
    let text: String
    let createdAtMilliseconds: Int64

    init(text: String, now: Date = Date()) {
        id = UUID().uuidString
        self.text = String(text.trimmingCharacters(in: .whitespacesAndNewlines).prefix(8_000))
        createdAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
    }

    var createdAt: Date {
        Date(timeIntervalSince1970: TimeInterval(createdAtMilliseconds) / 1_000)
    }
}

struct ReaderNativeFeatureStore: Sendable {
    private static let directoryName = "NativeFeatures"
    private static let snapshotName = "reader-snapshot.json"
    private static let pendingActionName = "pending-action.json"
    private static let quickNotesName = "quick-notes.json"

    private var rootURL: URL? {
        FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier:
                ReaderNativeBridgeContract.appGroupIdentifier
        )?.appendingPathComponent(Self.directoryName, isDirectory: true)
    }

    private func requireRoot() throws -> URL {
        guard let rootURL else {
            throw CocoaError(.fileNoSuchFile)
        }
        try FileManager.default.createDirectory(
            at: rootURL,
            withIntermediateDirectories: true
        )
        return rootURL
    }

    private func url(named name: String) throws -> URL {
        try requireRoot().appendingPathComponent(name, isDirectory: false)
    }

    private func write<T: Encodable>(_ value: T, named name: String) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(value)
        try data.write(to: url(named: name), options: [.atomic])
    }

    private func read<T: Decodable>(_ type: T.Type, named name: String) throws -> T? {
        let target = try url(named: name)
        guard FileManager.default.fileExists(atPath: target.path) else {
            return nil
        }
        return try JSONDecoder().decode(type, from: Data(contentsOf: target))
    }

    @discardableResult
    func writeSnapshot(_ snapshot: ReaderSharedSnapshot) throws -> Bool {
        if let existing = readSnapshot(), existing.hasSameContent(as: snapshot) {
            return false
        }
        try write(snapshot, named: Self.snapshotName)
        return true
    }

    func readSnapshot() -> ReaderSharedSnapshot? {
        try? read(ReaderSharedSnapshot.self, named: Self.snapshotName)
    }

    func enqueue(_ action: ReaderNativeFeatureAction) throws {
        try write(ReaderNativeFeatureRequest(action: action), named: Self.pendingActionName)
    }

    func consumePendingAction() -> ReaderNativeFeatureRequest? {
        guard let request = try? read(
            ReaderNativeFeatureRequest.self,
            named: Self.pendingActionName
        ) else {
            return nil
        }
        if let target = try? url(named: Self.pendingActionName) {
            try? FileManager.default.removeItem(at: target)
        }
        return request.isFresh ? request : nil
    }

    func appendQuickNote(_ note: ReaderQuickNote) throws {
        guard !note.text.isEmpty else { return }
        var notes = readQuickNotes()
        notes.insert(note, at: 0)
        if notes.count > 100 {
            notes.removeLast(notes.count - 100)
        }
        try write(notes, named: Self.quickNotesName)
    }

    func readQuickNotes() -> [ReaderQuickNote] {
        (try? read([ReaderQuickNote].self, named: Self.quickNotesName)) ?? []
    }

    func annotationDirectory() throws -> URL {
        let directory = try requireRoot().appendingPathComponent(
            "Annotations",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        return directory
    }
}
