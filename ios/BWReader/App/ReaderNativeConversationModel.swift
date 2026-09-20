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

    var tools: [ReaderNativeConversationPart] { parts.filter(\.isTool) }
    var artifacts: [ReaderNativeConversationPart] { parts.filter { !$0.isTool && $0.kind != "text" } }

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        role = value["role"] as? String ?? "assistant"
        text = value["text"] as? String ?? ""
        streaming = value["streaming"] as? Bool ?? false
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

/// A projection of the existing Reader conversation. The JavaScript bridge owns
/// history, streaming reconciliation, artifact identities and all write actions.
@MainActor
final class ReaderNativeConversationModel: ObservableObject {
    @Published private(set) var scope = ""
    @Published private(set) var revision: Int64 = -1
    @Published private(set) var title = "阅读助手"
    @Published private(set) var conversationMode = "normal"
    @Published private(set) var ready = false
    @Published private(set) var busy = false
    @Published private(set) var legacyVisible = false
    @Published private(set) var sidebarOpen = false
    @Published private(set) var selectionText = ""
    @Published private(set) var readingTools: [ReaderNativeControl] = []
    @Published private(set) var messages: [ReaderNativeConversationMessage] = []
    @Published private(set) var capabilities = Set<String>()
    @Published private(set) var voice = ReaderNativeConversationVoice()
    @Published private(set) var pendingActions = Set<String>()
    @Published private(set) var error: String?
    // Presentation survives closing/repositioning the SwiftUI sidebar, but is
    // scoped to this conversation and never persisted as a second history.
    @Published var draft = ""
    @Published var followsLatest = true
    @Published var visibleMessageID: String?

    var commandHandler: (([String: Any]) async -> String?)?
    private var generation = UUID()
    private var retiredNavigationScopes = Set<String>()

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
            generation = UUID()
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
        ready = payload["ready"] as? Bool ?? false
        busy = payload["busy"] as? Bool ?? false
        legacyVisible = payload["legacyVisible"] as? Bool ?? false
        sidebarOpen = payload["sidebarOpen"] as? Bool ?? false
        selectionText = (payload["selection"] as? [String: Any])?["text"] as? String ?? ""
        readingTools = (payload["readingTools"] as? [[String: Any]] ?? []).compactMap(ReaderNativeControl.init)
        capabilities = Set(payload["capabilities"] as? [String] ?? [])
        voice = ReaderNativeConversationVoice(payload["voice"] as? [String: Any] ?? [:])
        messages = nextMessages
        revision = nextRevision
    }

    func resetForNavigation() {
        if !scope.isEmpty { retiredNavigationScopes.insert(scope) }
        generation = UUID()
        scope = ""
        revision = -1
        title = "阅读助手"
        conversationMode = "normal"
        ready = false
        busy = false
        legacyVisible = false
        sidebarOpen = false
        selectionText = ""
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
    func clearError() { error = nil }

    @discardableResult
    func perform(_ action: String, parameters: [String: Any] = [:]) async -> Bool {
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
        defer { if generation == ticket { pendingActions.remove(action) } }
        let failure = await commandHandler(command)
        guard !Task.isCancelled, generation == ticket else { return false }
        if let failure {
            error = failure.isEmpty ? "操作未完成，请重试。" : failure
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
