import Foundation

private func readerUTF8Prefix(
    _ value: String,
    maximumBytes: Int
) -> String {
    let data = Data(value.utf8)
    guard data.count > maximumBytes else { return value }
    var end = maximumBytes
    while end > max(0, maximumBytes - 4) {
        if let result = String(
            data: Data(data.prefix(end)),
            encoding: .utf8
        ) {
            return result
        }
        end -= 1
    }
    return ""
}

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

struct ReaderLocalNoteProjection: Codable, Equatable, Identifiable, Sendable {
    let id: String
    let title: String
    let fileName: String
    let content: String
    let contentHash: String
    let contentTruncated: Bool
    let sourceFile: String
    let sourcePage: Int
    let createdAtMilliseconds: Int64
    let pendingExport: Bool?

    init(
        id: String = UUID().uuidString,
        title: String,
        fileName: String,
        content: String,
        sourceFile: String,
        sourcePage: Int,
        pendingExport: Bool = false,
        now: Date = Date()
    ) {
        let contentLimit = 131_072
        self.id = id
        self.title = readerUTF8Prefix(title, maximumBytes: 2_048)
        self.fileName = readerUTF8Prefix(fileName, maximumBytes: 2_048)
        self.content = readerUTF8Prefix(content, maximumBytes: contentLimit)
        contentHash = ReaderLocalNoteFormat.contentHash(content)
        contentTruncated = content.utf8.count > contentLimit
        self.sourceFile = readerUTF8Prefix(sourceFile, maximumBytes: 8_192)
        self.sourcePage = max(0, sourcePage)
        createdAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
        self.pendingExport = pendingExport
    }

    var preview: String {
        readerUTF8Prefix(content, maximumBytes: 2_048)
    }
}

struct ReaderLocalNotesSharedState: Codable, Equatable, Sendable {
    static let schema = 2

    let schema: Int
    let enabled: Bool
    let configured: Bool
    let folderName: String
    let vaultGeneration: String
    let updatedAtMilliseconds: Int64
    let notes: [ReaderLocalNoteProjection]

    init(
        enabled: Bool,
        configured: Bool,
        folderName: String,
        vaultGeneration: String,
        notes: [ReaderLocalNoteProjection],
        now: Date = Date()
    ) {
        schema = Self.schema
        self.enabled = enabled
        self.configured = configured
        self.folderName = readerUTF8Prefix(folderName, maximumBytes: 2_048)
        self.vaultGeneration = vaultGeneration
        updatedAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
        self.notes = Array(notes.prefix(50))
    }

    static let unavailable = ReaderLocalNotesSharedState(
        enabled: false,
        configured: false,
        folderName: "",
        vaultGeneration: "",
        notes: []
    )
}

/// 系统投影共享数据（2026-08-27）：App 写、widget 读，一份 JSON 三个
/// 功能分区（复习 / 待办通知 / 同步状态），对应三个独立小组件。
struct ReaderWidgetSystemData: Codable, Equatable, Sendable {
    struct Review: Codable, Equatable, Sendable {
        var due: Int
        var newCards: Int
        var atMs: Int64
    }

    struct NotificationItem: Codable, Equatable, Sendable {
        var id: String
        var title: String
        var kind: String
        var state: String
        // 两个后加的字段都设成可选：老缓存里没有它们，非可选会让
        // 整份缓存解码失败（"升级把旧数据全废掉"是最容易忽略的一种
        // 回归）。widget 侧排到点通知需要这两个。
        var body: String?
        var dueAtMs: Int64?
    }

    /// 展示板（2026-09-05 用户要求）：电脑上的 AI 或固定程序申请一块分了区的
    /// 板子往里放内容，这里是它在设备侧的投影。
    ///
    /// ⚠ 和 body/dueAtMs 同一条纪律：**新字段一律可选**。非可选会让老缓存
    ///   整份解码失败 —— "升级把旧数据全废掉"是最容易忽略的一种回归。
    struct Board: Codable, Equatable, Sendable {
        struct Section: Codable, Equatable, Sendable {
            var title: String
            var lines: [String]
        }

        /// 一张卡 = 一个方块 = 一条信息（2026-09-05 用户改版）。
        /// 只带 id/alt/sha：图在 Windows 渲好，按 sha 取；HTML 不下发到设备。
        struct Card: Codable, Equatable, Sendable {
            var id: String
            var alt: String
            var sha: String
            var updatedAtMs: Int64
        }

        var code: String
        var title: String
        var updatedAtMs: Int64
        var sections: [Section]
        /// 可选：老缓存没有这个字段，非可选会让整份缓存解码失败。
        var cards: [Card]?
    }

    var review: Review?
    var notifications: [NotificationItem]
    var lastSyncAtMs: Int64
    var updatedAtMs: Int64
    var boards: [Board]?
    /// 板子读不到时桥给的错误码。**不折成空数组** —— 空板子会被当成权威，
    /// 于是"桥那边出问题了"看起来像"AI 什么都没写"。
    var boardsError: String?
}

struct ReaderNativeFeatureStore: Sendable {
    private static let directoryName = "NativeFeatures"
    private static let snapshotName = "reader-snapshot.json"
    private static let pendingActionName = "pending-action.json"
    private static let quickNotesName = "quick-notes.json"
    private static let localNotesStateName = "local-notes-state.json"

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

    func writeLocalNotesState(_ state: ReaderLocalNotesSharedState) throws {
        try write(state, named: Self.localNotesStateName)
    }

    func readLocalNotesState() -> ReaderLocalNotesSharedState {
        (try? loadLocalNotesState()) ?? .unavailable
    }

    // ── 系统投影数据（2026-08-27，分功能小组件）─────────────────────
    // App 每次系统投影同步时写入；widget timeline provider 只读。
    // 快照模型（不假装实时）：每份数据带 updatedAtMs，widget 界面上
    // 永远显示"数据时刻"。
    func writeWidgetSystemData(_ value: ReaderWidgetSystemData) throws {
        try write(value, named: Self.widgetSystemDataName)
    }

    func readWidgetSystemData() -> ReaderWidgetSystemData? {
        try? read(ReaderWidgetSystemData.self, named: Self.widgetSystemDataName)
    }

    private static let widgetSystemDataName = "widget-system.json"

    func loadLocalNotesState() throws -> ReaderLocalNotesSharedState? {
        try read(
            ReaderLocalNotesSharedState.self,
            named: Self.localNotesStateName
        )
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
