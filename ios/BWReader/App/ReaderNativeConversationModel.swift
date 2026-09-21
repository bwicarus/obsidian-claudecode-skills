import Combine
import Foundation
import CoreTransferable
import UniformTypeIdentifiers

struct ReaderNativeConversationPart: Identifiable {
    let id: String
    let kind: String
    let title: String
    let text: String
    let status: String
    let data: [String: Any]
    let actionId: String?
    let actionLabel: String?

    var isTool: Bool { kind == "tool" || kind == "process" }
    var isFailed: Bool { ["failed", "error", "rejected"].contains(status) }
    var isRunning: Bool { ["running", "started", "in_progress", "pending"].contains(status) }
    var isComplete: Bool { ["completed", "done", "success", "saved"].contains(status) }

    func string(_ key: String) -> String { data[key] as? String ?? "" }
    func count(_ key: String) -> Int? {
        guard let value = data[key] as? NSNumber else { return nil }
        return max(0, value.intValue)
    }

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty,
              let kind = value["kind"] as? String, !kind.isEmpty else { return nil }
        self.id = id
        self.kind = kind
        title = value["title"] as? String ?? ""
        text = value["text"] as? String ?? ""
        status = value["status"] as? String ?? "unknown"
        data = value["data"] as? [String: Any] ?? [:]
        actionId = (value["actionId"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        actionLabel = value["actionLabel"] as? String
    }
}

struct ReaderNativeConversationMessage: Identifiable {
    let id: String
    let role: String
    let text: String
    let streaming: Bool
    let parts: [ReaderNativeConversationPart]
    let reviewSelections: [ReaderNativeReviewSelection]

    var tools: [ReaderNativeConversationPart] { parts.filter(\.isTool) }
    var artifacts: [ReaderNativeConversationPart] { parts.filter { !$0.isTool && $0.kind != "text" } }

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        role = value["role"] as? String ?? "assistant"
        text = value["text"] as? String ?? ""
        streaming = value["streaming"] as? Bool ?? false
        reviewSelections = (value["reviewSelections"] as? [[String: Any]] ?? []).compactMap(ReaderNativeReviewSelection.init)
        var seen = Set<String>()
        parts = (value["parts"] as? [[String: Any]] ?? [])
            .compactMap(ReaderNativeConversationPart.init)
            .filter { seen.insert($0.id).inserted }
    }
}

struct ReaderNativeConversationVoice {
    let mode: String?
    let active: Bool
    let busy: Bool
    let label: String?

    init(_ value: [String: Any] = [:]) {
        mode = value["mode"] as? String
        active = value["active"] as? Bool ?? false
        busy = value["busy"] as? Bool ?? false
        label = value["label"] as? String
    }
}

struct ReaderNativeReviewSelection: Identifiable {
    let id: String
    let label: String
    let text: String
    let selected: Bool
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id
        label = value["label"] as? String ?? "选用回答"
        text = value["text"] as? String ?? ""
        selected = value["selected"] as? Bool ?? false
    }
}

struct ReaderNativeArtifactInspection: Identifiable {
    let id: UUID
    let title: String
    var loading = true
    var kind = ""
    var content: [String: Any] = [:]
    var error: String?
}

struct ReaderNativeContextAttachment: Identifiable {
    let id: String
    let title: String
    let text: String
    let removeID: String

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, let removeID = value["removeId"] as? String else { return nil }
        self.id = id
        self.removeID = removeID
        title = value["title"] as? String ?? "已选内容"
        text = value["text"] as? String ?? ""
    }
}

/// A projection of the existing Reader conversation. The JavaScript bridge owns
/// history, streaming reconciliation, artifact identities and all write actions.
@MainActor
final class ReaderNativeConversationModel: ObservableObject {
    @Published private(set) var scope = ""
    @Published private(set) var revision: Int64 = -1
    @Published private(set) var title = "阅读助手"
    @Published private(set) var conversationMode = "normal"
    @Published private(set) var review: [String: Any] = [:]
    @Published private(set) var ready = false
    @Published private(set) var busy = false
    @Published private(set) var legacyVisible = false
    @Published private(set) var sidebarOpen = false
    @Published private(set) var selectionText = ""
    /// 阅读器当前选中的文字。
    /// ⚠ 与 `selectionText` 不是一回事：那个来自 `__focusSel`，而
    /// `__setFocusSel` 第一行就是「助手侧栏没开就 return」—— 侧栏关着时它恒空。
    /// 选区操作条读的是这一份。
    @Published private(set) var readerSelectionText = ""
    @Published private(set) var attachments: [ReaderNativeContextAttachment] = []
    @Published private(set) var readingTools: [ReaderNativeControl] = []
    @Published private(set) var messages: [ReaderNativeConversationMessage] = []
    @Published private(set) var capabilities = Set<String>()
    @Published private(set) var voice = ReaderNativeConversationVoice()
    @Published private(set) var pendingActions = Set<String>()
    @Published private(set) var error: String?
    @Published var inspection: ReaderNativeArtifactInspection?

    /// 原生那侧发起的操作失败了，借这块已有的出声位置说出来。
    /// ⚠ 网页的 toast 在接管后是看不见的（它在被藏起来的那一层里），
    /// 所以原生路径的失败必须自己出声，否则就是彻底静默。
    func report(_ message: String) {
        error = message.isEmpty ? "操作未完成。" : message
    }
    @Published var settingsPanel: ReaderNativeSettingsModel?
    @Published var readingSettingsPanel: ReaderNativeReadingSettingsModel?
    @Published var searchPanel: ReaderNativeSearchModel?
    @Published var tocPanel: ReaderNativeTOCModel?
    @Published var navigationPanel: ReaderNativeNavigationModel?
    @Published private(set) var placements: [ReaderNativePagePlacement] = []
    // Presentation survives closing/repositioning the SwiftUI sidebar, but is
    // scoped to this conversation and never persisted as a second history.
    @Published var draft = ""
    @Published var followsLatest = true
    @Published var visibleMessageID: String?

    var commandHandler: (([String: Any]) async -> String?)?
    var inspectionHandler: (([String: Any]) async -> [String: Any])?
    var imageHandler: ((String, String) async throws -> Data)?

    func imageData(_ id: String) async throws -> Data {
        guard let imageHandler else { throw URLError(.resourceUnavailable) }
        let ticket = generation
        let data = try await imageHandler(scope, id)
        guard ticket == generation, !Task.isCancelled else { throw CancellationError() }
        return data
    }
    private var generation = UUID()
    private var retiredNavigationScopes = Set<String>()
    private var pendingSelections: [(id: String, text: String)] = []
    private var deliveringSelection = false

    // Text selection can change while a card action is saving. Coalesce each
    // text view's latest value, including release, instead of dropping it at
    // the generic button duplicate guard and leaving a permanently held chip.
    func updateTextSelection(id: String, text: String) {
        guard !id.isEmpty, supports("liveAction"), let commandHandler else { return }
        pendingSelections.removeAll { $0.id == id }
        pendingSelections.append((id, text))
        guard !deliveringSelection else { return }
        deliveringSelection = true
        let ticket = generation
        let selectionScope = scope
        Task {
            defer { if generation == ticket { deliveringSelection = false } }
            while generation == ticket, !pendingSelections.isEmpty {
                let value = pendingSelections.removeFirst()
                let failure = await commandHandler(["action": "liveAction", "scope": selectionScope,
                                                    "actionId": value.id, "text": value.text])
                if generation == ticket, !value.text.isEmpty, let failure,
                   !pendingSelections.contains(where: { $0.id == value.id }) { error = failure }
            }
        }
    }

    func receive(_ payload: [String: Any]) {
        guard (payload["version"] as? NSNumber)?.intValue == 1,
              let nextScope = payload["scope"] as? String, !nextScope.isEmpty,
              let nextRevision = (payload["revision"] as? NSNumber)?.int64Value,
              nextRevision >= 0 else {
            error = "无法读取助手界面数据，请重新打开阅读器。"
            return
        }
        guard !retiredNavigationScopes.contains(nextScope) else { return }
        if nextScope == scope, nextRevision <= revision { return }
        if nextScope != scope {
            inspection = nil
            settingsPanel = nil
            readingSettingsPanel = nil
            searchPanel = nil
            tocPanel = nil
            navigationPanel = nil
            generation = UUID()
            pendingSelections = []
            deliveringSelection = false
            pendingActions = []
            error = nil
            draft = ""
            followsLatest = true
            visibleMessageID = nil
        }
        var seen = Set<String>()
        let nextMessages = (payload["messages"] as? [[String: Any]] ?? [])
            .compactMap(ReaderNativeConversationMessage.init)
            .filter { seen.insert($0.id).inserted }
        // Publish the revision last: observers scroll only after the entire
        // snapshot is available, never after a partially replaced message list.
        scope = nextScope
        title = (payload["title"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "阅读助手"
        conversationMode = payload["conversationMode"] as? String == "review" ? "review" : "normal"
        review = payload["review"] as? [String: Any] ?? [:]
        ready = payload["ready"] as? Bool ?? false
        busy = payload["busy"] as? Bool ?? false
        legacyVisible = payload["legacyVisible"] as? Bool ?? false
        placements = (payload["placements"] as? [[String: Any]] ?? []).compactMap(ReaderNativePagePlacement.init)
        sidebarOpen = payload["sidebarOpen"] as? Bool ?? false
        selectionText = (payload["selection"] as? [String: Any])?["text"] as? String ?? ""
        readerSelectionText = (payload["readerSelection"] as? [String: Any])?["text"] as? String ?? ""
        attachments = (payload["attachments"] as? [[String: Any]] ?? []).compactMap(ReaderNativeContextAttachment.init)
        readingTools = (payload["readingTools"] as? [[String: Any]] ?? []).compactMap(ReaderNativeControl.init)
        if let navigation = payload["navigation"] as? [String: Any] { navigationPanel?.receive(navigation) }
        capabilities = Set(payload["capabilities"] as? [String] ?? [])
        voice = ReaderNativeConversationVoice(payload["voice"] as? [String: Any] ?? [:])
        messages = nextMessages
        revision = nextRevision
        noteSnapshotCost(payload["payloadBytes"] as? Int ?? 0)
    }

    /// 快照有多大、多久来一次。
    ///
    /// ⚠ 这两个数字是"用着用着就崩"这一类怀疑的**判据**：页面每次快照都要把整段
    /// 对话序列化一遍，对话越长这份字符串越大。是不是大到能把渲染进程顶掉，只能
    /// 量，不能猜 —— 我已经因为猜错返工过两轮。
    private(set) var snapshotPeakBytes = 0
    private(set) var snapshotCount = 0
    private var snapshotWindowStart = Date()

    private func noteSnapshotCost(_ bytes: Int) {
        snapshotCount += 1
        snapshotPeakBytes = max(snapshotPeakBytes, bytes)
        // 大到值得记一笔就留个面包屑（1MB）。每条都记会把面包屑冲没。
        if bytes >= 1_000_000, snapshotCount % 20 == 0 {
            ReaderNativeFaultReporter.shared.note("snap", "\(bytes / 1024)KB×\(snapshotCount)")
        }
    }

    /// 给故障报告用的一句话。
    var snapshotCostSummary: String {
        let seconds = max(1, Int(Date().timeIntervalSince(snapshotWindowStart)))
        return "snapshots=\(snapshotCount) peak=\(snapshotPeakBytes / 1024)KB in \(seconds)s"
    }

    /// 最后一条发给页面的命令（动作名 + 时刻）。
    ///
    /// ⚠ 它存在的唯一理由是：**渲染进程被杀时，页面里的线索全部跟着没了**，
    /// 而这个值活在 App 进程里，页面死了它还在。没有它，用户看到的就是
    /// 「点一下就崩」—— 没有任何东西能说出崩之前在做什么，而那正是
    /// 2026-09-22 这次查起来最费劲的地方。
    /// 只留动作名和时刻，**不留参数**（参数里可能有选区正文这类内容）。
    private(set) var lastCommandAction = ""
    private(set) var lastCommandAt: Date?

    func resetForNavigation() {
        inspection = nil
        settingsPanel = nil
        readingSettingsPanel = nil
        searchPanel = nil
        tocPanel = nil
        navigationPanel = nil
        if !scope.isEmpty { retiredNavigationScopes.insert(scope) }
        generation = UUID()
        scope = ""
        pendingSelections = []
        deliveringSelection = false
        revision = -1
        title = "阅读助手"
        conversationMode = "normal"
        review = [:]
        ready = false
        busy = false
        legacyVisible = false
        placements = []
        sidebarOpen = false
        selectionText = ""
        readerSelectionText = ""
        attachments = []
        readingTools = []
        messages = []
        capabilities = []
        voice = ReaderNativeConversationVoice()
        pendingActions = []
        error = nil
        draft = ""
        followsLatest = true
        visibleMessageID = nil
    }

    func supports(_ action: String) -> Bool { capabilities.contains(action) }
    func isPerforming(_ action: String) -> Bool { pendingActions.contains(action) }

    func inspect(_ part: ReaderNativeConversationPart) async {
        guard supports("inspectArtifact"), let actionID = part.actionId,
              messages.contains(where: { $0.parts.contains(where: { $0.actionId == actionID }) }),
              let inspectionHandler else {
            error = "此项内容暂不可读取，请刷新后重试。"
            return
        }
        let ticket = generation
        let id = UUID()
        inspection = ReaderNativeArtifactInspection(id: id, title: part.title)
        let receipt = await inspectionHandler(["action": "inspectArtifact", "scope": scope, "actionId": actionID])
        guard generation == ticket, !Task.isCancelled, inspection?.id == id else { return }
        var result = ReaderNativeArtifactInspection(id: id, title: part.title)
        result.loading = false
        if receipt["ok"] as? Bool == true, let detail = receipt["detail"] as? [String: Any] {
            result.kind = detail["kind"] as? String ?? ""
            result.content = detail["content"] as? [String: Any] ?? [:]
        } else {
            result.error = receipt["error"] as? String ?? "读取失败，请重试。"
        }
        inspection = result
    }
    func clearError() { error = nil }

    @discardableResult
    func performReview(_ key: String, values: [String: Any] = [:]) async -> Bool {
        var value = values
        value["key"] = key
        if value["contextKey"] == nil { value["contextKey"] = review["contextKey"] as? String ?? "" }
        if value["cardId"] == nil { value["cardId"] = (review["current"] as? [String: Any])?["id"] as? String ?? "" }
        return await perform("reviewAction", parameters: ["value": value])
    }

    func touchPageCard(_ id: String) async {
        // Reading renews the existing floating-card timer; it must not disable
        // the ongoing native drag/scroll gesture as a pending user command.
        guard let inspectionHandler else { return }
        _ = await inspectionHandler(["action": "liveAction", "scope": scope, "actionId": id])
    }

    @discardableResult
    func perform(_ action: String, parameters: [String: Any] = [:]) async -> Bool {
        if action == "openSettings", supports("nativeReadingSettings"), let inspectionHandler {
            readingSettingsPanel = ReaderNativeReadingSettingsModel(scope: scope, request: inspectionHandler)
            return true
        }
        if action == "openNavigation", supports("nativeNavigation"), let inspectionHandler {
            navigationPanel = ReaderNativeNavigationModel(scope: scope, request: inspectionHandler)
            return true
        }
        if action == "openTOC", supports("nativeTOC"), let inspectionHandler {
            tocPanel = ReaderNativeTOCModel(scope: scope, request: inspectionHandler)
            return true
        }
        if action == "openSearch", supports("nativeSearch"), let inspectionHandler {
            searchPanel = ReaderNativeSearchModel(scope: scope, request: inspectionHandler)
            return true
        }
        if action == "openModels", supports("nativeSettings"), let inspectionHandler {
            settingsPanel = ReaderNativeSettingsModel(scope: scope, request: inspectionHandler)
            return true
        }
        guard supports(action) else {
            error = "当前页面尚未提供这项操作。"
            return false
        }
        guard ready || ["refresh", "showLegacy", "hideLegacy", "toggleAssistant", "liveAction"].contains(action) else {
            error = "助手仍在准备，请稍后重试。"
            return false
        }
        guard let commandHandler else {
            error = "助手尚未连接，请重新打开阅读器。"
            return false
        }
        guard !pendingActions.contains(action) else { return false }
        if action == "openArtifact" || action == "action" {
            guard let id = parameters["actionId"] as? String,
                  messages.contains(where: { $0.parts.contains(where: { $0.actionId == id }) }) else {
                error = "这项操作已经更新，请使用当前卡片上的按钮。"
                return false
            }
        }
        var command = parameters
        command["action"] = action
        command["scope"] = scope
        let ticket = generation
        pendingActions.insert(action)
        error = nil
        lastCommandAction = action
        lastCommandAt = Date()
        // 面包屑：页面/App 被杀时这是唯一还活着的"当时在做什么"。
        ReaderNativeFaultReporter.shared.note("cmd", action)
        defer { if generation == ticket { pendingActions.remove(action) } }
        let failure = await commandHandler(command)
        guard !Task.isCancelled, generation == ticket else { return false }
        if let failure {
            error = failure.isEmpty ? "操作未完成，请重试。" : failure
            // ⚠ 失败也要留痕：能看见的只有侧栏里一行红字，而侧栏多半没开着 ——
            //   这正是"点了没反应"一直查不出来的原因。
            ReaderNativeFaultReporter.shared.report(
                code: "BW_NATIVE_COMMAND_FAILED", message: action, detail: error ?? "")
            return false
        }
        return true
    }
}

struct ReaderNativeControl: Identifiable {
    let id: String
    let key: String
    let title: String
    let disabled: Bool
    let destructive: Bool

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        key = value["key"] as? String ?? ""
        title = value["title"] as? String ?? "操作"
        disabled = value["disabled"] as? Bool ?? false
        destructive = value["destructive"] as? Bool ?? false
    }
}

/// Only Reader card handles can be dropped onto the native book surface.
/// No card content is copied or persisted by the drag session.
struct ReaderNativeCardTransfer: Codable, Transferable {
    let scope: String
    let actionID: String

    static var transferRepresentation: some TransferRepresentation {
        CodableRepresentation(contentType: UTType(exportedAs: "space.bwicarus.reader-card-handle"))
    }
}
