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
    var data: [String: Any]
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
        let value = ReaderNativeConversationProjection.part(value)
        guard let id = value["id"] as? String, !id.isEmpty,
              let kind = value["kind"] as? String, !kind.isEmpty else { return nil }
        self.id = id
        self.kind = kind
        title = value["title"] as? String ?? ""
        text = value["text"] as? String ?? ""
        status = value["status"] as? String ?? "unknown"
        var input = value["data"] as? [String:Any] ?? [:]
        let nativeActions = input["nativeActionKey"] as? String == id
        let owner = nativeActions ? "native-original:" + id : value["actionId"] as? String
        if let owner, !owner.isEmpty { input["nativeActionOwner"] = owner }
        if nativeActions, let owner {
            if !["tool","process"].contains(kind) { input["selectId"] = "native-text:" + owner }
            if ["anki","weather","news","fact","general","images","videos"].contains(kind) {
                input["dragId"] = "native-place:" + owner
            }
        }
        data = ReaderNativeMediaArtifact.project(ReaderNativeCardPresentation.project(input))
        if let owner = input["nativeActionOwner"] as? String {
            let detail = input["nativeDetail"] as? [String:Any], original = detail?["content"] as? [String:Any]
            let gid = (input["nativeCard"] as? [String:Any])?["gid"] as? String
            let cid = original?["cid"] as? String
            if let identity = gid ?? cid, !identity.isEmpty,
               gid != nil || ["weather","news","fact","general","images","videos"].contains(detail?["kind"] as? String ?? "") {
                data["pinId"] = "native-pin:" + owner
                data["nativePinContextID"] = "card:" + identity
            }
        }
        actionId = owner.flatMap { $0.isEmpty ? nil : $0 }
        actionLabel = value["actionLabel"] as? String
    }
}

struct ReaderNativeConversationMessage: Identifiable {
    let id: String
    let role: String
    let text: String
    let streaming: Bool
    let title: String
    let statusText: String
    let progressSummary: String
    var parts: [ReaderNativeConversationPart]
    let reviewSelections: [ReaderNativeReviewSelection]
    /// 原生历史（迁出 P1）：用户消息的上下文一行、回答后的追问建议。
    let contextLine: String
    let followups: [String]

    var tools: [ReaderNativeConversationPart] { parts.filter(\.isTool) }
    var artifacts: [ReaderNativeConversationPart] { parts.filter { !$0.isTool && $0.kind != "text" } }

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        role = value["role"] as? String ?? "assistant"
        text = value["text"] as? String ?? ""
        streaming = value["streaming"] as? Bool ?? false
        title = value["title"] as? String ?? ""
        statusText = value["statusText"] as? String ?? ""
        if let progress = value["progress"] as? [String: Any], let states = progress["states"] as? [Any], !states.isEmpty {
            let total = max(states.count, (progress["total"] as? NSNumber)?.intValue ?? 0)
            let done = states.filter { $0 as? String == "done" }.count
            let failed = states.filter { $0 as? String == "err" }.count
            let running = states.filter { $0 as? String == "run" }.count
            progressSummary = "成功 \(done)，失败 \(failed)，运行中 \(running)，待处理 \(total - done - failed - running)"
        } else { progressSummary = "" }
        reviewSelections = (value["reviewSelections"] as? [[String: Any]] ?? []).compactMap(ReaderNativeReviewSelection.init)
        contextLine = value["contextLine"] as? String ?? ""
        followups = (value["followups"] as? [String] ?? []).filter { !$0.isEmpty }.prefix(4).map { $0 }
        var seen = Set<String>()
        parts = (value["parts"] as? [[String: Any]] ?? [])
            .compactMap(ReaderNativeConversationPart.init)
            .filter { seen.insert($0.id).inserted }
    }
}

/// 底部字幕条（网页 #vc-cap）的内容。原生接管后网页层透明，这条字幕由原生画。
struct ReaderNativeCaptions: Equatable {
    struct Line: Equatable, Identifiable {
        let id: Int
        let kind: String        // line / status / ok / error / wait
        let user: Bool
        let previous: Bool
        let text: String
    }
    var on = false
    var lines: [Line] = []
    /// 原版字幕开关（语音设置卡里的 rc-voice-sub，默认开）：关了就什么字幕都不出。
    var enabled = true

    init(_ value: [String: Any] = [:]) {
        on = value["on"] as? Bool ?? false
        enabled = value["enabled"] as? Bool ?? true
        lines = (value["lines"] as? [[String: Any]] ?? []).enumerated().map { index, line in
            Line(id: index, kind: line["kind"] as? String ?? "line", user: line["user"] as? Bool ?? false,
                 previous: line["previous"] as? Bool ?? false, text: line["text"] as? String ?? "")
        }
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

/// Native message ownership and presentation. Remaining legacy action owners
/// communicate through scoped commands while their migration is completed.
@MainActor
final class ReaderNativeConversationModel: ObservableObject {
    @Published private(set) var scope = ""
    @Published private(set) var revision: Int64 = -1
    @Published private(set) var title = "阅读助手"
    @Published private(set) var conversationMode = "normal"
    @Published private(set) var review: [String: Any] = [:]
    private var committedReviewPresentation: [String: Any]?
    private func reviewPresentation(_ incoming: [String: Any]) -> [String: Any] {
        var result = incoming
        if let native = committedReviewPresentation, native["lease"] as? String == incoming["lease"] as? String,
           ((native["revision"] as? NSNumber)?.int64Value ?? 0) > ((incoming["revision"] as? NSNumber)?.int64Value ?? 0) {
            result.merge(native) { _, value in value }
            result["contextKey"] = native["lease"]
        }
        return ReaderNativeReviewFaces.state(result)
    }
    func acceptReviewPresentation(_ value: [String: Any]) {
        guard let lease = value["lease"] as? String, !lease.isEmpty, lease == review["lease"] as? String,
              ((value["revision"] as? NSNumber)?.int64Value ?? 0) > ((review["revision"] as? NSNumber)?.int64Value ?? 0) else { return }
        committedReviewPresentation = value
        review = reviewPresentation(review)
    }
    @Published private(set) var ready = false
    @Published private(set) var busy = false
    @Published private(set) var legacyVisible = false
    @Published private(set) var sidebarOpen = false
    /// 卡片收藏夹里有几张（原版右下角收藏夹按钮上的数字）。
    @Published private(set) var favoritesCount = 0
    private var nativeFavoritesCount: Int?
    func setNativeFavoritesCount(_ value: Int?) {
        nativeFavoritesCount = value
        if let value, favoritesCount != value { favoritesCount = value }
    }
    @Published private(set) var selectionText = ""
    /// 阅读器当前选中的文字。
    /// ⚠ 与 `selectionText` 不是一回事：那个来自 `__focusSel`，而
    /// `__setFocusSel` 第一行就是「助手侧栏没开就 return」—— 侧栏关着时它恒空。
    /// 选区操作条读的是这一份。
    @Published private(set) var readerSelectionText = ""
    @Published private(set) var attachments: [ReaderNativeContextAttachment] = []
    let mediaDraft = ReaderNativeMediaDraft()
    @Published private(set) var readingTools: [ReaderNativeControl] = []
    @Published private(set) var messages: [ReaderNativeConversationMessage] = []
    @Published private(set) var captions = ReaderNativeCaptions()
    /// 正在显示的是本机缓存（页面还没交来历史，或历史取不到）。
    @Published private(set) var showingCachedMessages = false
    private var lastCachedAt = Date.distantPast
    private var lastCachedSignature = ""
    /// 读过一次就留在内存里：空快照一秒能来好几次，不能每次读盘。
    private var cachedByMode: [String: [ReaderNativeConversationMessage]] = [:]
    /// 刚清空过的模式：在出现新对话之前不再拿缓存回填（删盘是异步的，会有竞态）。
    private var cacheSuppressed = Set<String>()
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

    private var committedCards: [String: [String: Any]] = [:]
    private var removedMedia: [String:[Int:String]] = [:]
    private var selectedContextIDs = Set<String>()
    private var contextRecords: [[String:Any]]?
    private var figureAttachments: [ReaderNativeContextAttachment] = []
    private func refreshContextAttachments() {
        guard let contextRecords else { return }
        attachments = figureAttachments + contextRecords.compactMap { record in
            guard let id = record["id"] as? String else { return nil }
            return ReaderNativeContextAttachment(["id":scope + ":context:" + id,
                "title":record["label"] as? String ?? "已选内容",
                "text":String((record["text"] as? String ?? "").prefix(180)),"removeId":"native-context-remove:" + id])
        }
    }
    private func applyCommittedCards(_ parts: [ReaderNativeConversationPart]) -> [ReaderNativeConversationPart] {
        parts.compactMap { source in
            let part = applySelectionState(applyMediaState(source))
            guard let input = part.data["nativeCard"] as? [String: Any], let gid = input["gid"] as? String,
                  let record = committedCards[gid] else { return part }
            guard let data = ReaderNativeCardPresentation.applying(record, to: part.data) else { return nil }
            var updated = part; updated.data = data; return updated
        }
    }

    private func applySelectionState(_ source: ReaderNativeConversationPart) -> ReaderNativeConversationPart {
        guard let id = source.data["nativePinContextID"] as? String else { return source }
        var part = source; part.data["pinned"] = selectedContextIDs.contains(id); return part
    }

    private func applyMediaState(_ source: ReaderNativeConversationPart) -> ReaderNativeConversationPart {
        guard var detail = source.data["nativeDetail"] as? [String:Any],
              var card = detail["content"] as? [String:Any], let cid = card["cid"] as? String,
              ["images","videos"].contains(card["kind"] as? String ?? ""),
              var data = card["data"] as? [String:Any], var originals = data["items"] as? [[String:Any]] else { return source }
        for (index, digest) in removedMedia[cid] ?? [:] where originals.indices.contains(index) {
            if ReaderNativeConversationStore.fingerprint([originals[index]]) == digest { originals[index]["_gone"] = 1 }
        }
        var part = source
        data["items"] = originals; card["data"] = data; detail["content"] = card; part.data["nativeDetail"] = detail
        part.data["items"] = (source.data["items"] as? [[String:Any]] ?? []).compactMap { item -> [String:Any]? in
            guard let index = item["index"] as? Int, originals.indices.contains(index),
                  (originals[index]["_gone"] as? NSNumber)?.boolValue != true else { return nil }
            var result = item; result["selected"] = selectedContextIDs.contains("card:\(cid)/item:\(index)"); return result
        }
        return part
    }

    func acceptContextSelection(_ projection: [String:Any]) {
        contextRecords = (projection["snapshot"] as? [String:Any])?["items"] as? [[String:Any]] ?? []
        refreshContextAttachments()
        let ids = Set(projection["selected"] as? [String] ?? [])
        guard ids != selectedContextIDs else { return }
        selectedContextIDs = ids
        messages = messages.map { var value = $0; value.parts = applyCommittedCards(value.parts); return value }
        mergePlacements()
    }

    func acceptMediaRemoval(card: [String:Any], index: Int) {
        guard let cid = card["cid"] as? String, let items = (card["data"] as? [String:Any])?["items"] as? [[String:Any]],
              items.indices.contains(index), let digest = ReaderNativeConversationStore.fingerprint([items[index]]) else { return }
        removedMedia[cid,default:[:]][index] = digest
        messages = messages.map { var value = $0; value.parts = value.parts.map(applyMediaState); return value }
        mergePlacements()
    }

    func mediaAction(_ token: String) -> (card:[String:Any], index:Int, action:String)? {
        guard !token.isEmpty else { return nil }
        for part in messages.flatMap(\.parts) + placements.flatMap(\.parts) {
            guard let card = (part.data["nativeDetail"] as? [String:Any])?["content"] as? [String:Any],
                  ["images","videos"].contains(card["kind"] as? String ?? "") else { continue }
            for item in part.data["items"] as? [[String:Any]] ?? [] {
                guard let index = item["index"] as? Int else { continue }
                if item["selectID"] as? String == token { return (card,index,"toggle") }
                if item["removeID"] as? String == token { return (card,index,"remove") }
            }
        }
        return nil
    }
    func acceptCardRecord(_ record: [String: Any]) {
        guard let gid = record["gid"] as? String, record["contract"] as? String == "card-repository/1" else { return }
        if let previous = committedCards[gid] {
            let oldEntity = (previous["entityRev"] as? NSNumber)?.int64Value ?? 0
            let oldState = (previous["stateRev"] as? NSNumber)?.int64Value ?? 0
            let newEntity = (record["entityRev"] as? NSNumber)?.int64Value ?? 0
            let newState = (record["stateRev"] as? NSNumber)?.int64Value ?? 0
            if oldEntity > newEntity || oldState > newState || (oldEntity == newEntity && oldState == newState) { return }
        }
        committedCards[gid] = record
        messages = messages.map { value in var next = value; next.parts = applyCommittedCards(value.parts); return next }
        mergePlacements()
    }

    func nativeCardAction(_ token: String) -> (input: [String: Any], key: String)? {
        let parts = messages.flatMap(\.parts) + placements.flatMap(\.parts)
        for part in parts {
            guard let input = part.data["nativeCard"] as? [String: Any],
                  let ids = part.data["nativeCardActions"] as? [String: String],
                  let match = ids.first(where: { $0.value == token }) else { continue }
            return (input, match.key)
        }
        return nil
    }
    func artifactPart(_ token: String, field: String? = nil) -> ReaderNativeConversationPart? {
        guard !token.isEmpty else { return nil }
        return (messages.flatMap(\.parts) + placements.flatMap(\.parts)).first { part in
            if let field { return part.data[field] as? String == token }
            return part.actionId == token
        }
    }
    private var nativeHTMLNotes: [ReaderNativePagePlacement]? = nil
    private var webPlacements: [ReaderNativePagePlacement] = []
    func setNativeHTMLNotes(_ values: [[String:Any]]?) {
        nativeHTMLNotes = values.map { $0.compactMap(ReaderNativePagePlacement.init) }
        mergePlacements()
    }
    private func mergePlacements() {
        let combined = nativeHTMLNotes.map { notes in
            webPlacements.filter { !($0.fromNote && $0.parts.count == 1 && $0.parts.first?.kind == "general") } + notes
        } ?? webPlacements
        placements = combined.map { value in var next = value; next.parts = applyCommittedCards(value.parts); return next }
    }
    // Presentation survives closing/repositioning the SwiftUI sidebar, but is
    // scoped to this conversation and never persisted as a second history.
    @Published var draft = ""
    @Published var followsLatest = true
    @Published var visibleMessageID: String?

    var commandHandler: (([String: Any]) async -> String?)?
    /// 诊断出口（接到服务器的 client-log）。跳序、重新同步失败、作废范围丢包 —— 以前全都只在
    /// 界面上闪一下，日志里一条没有，用户说「重试也失败」时无从查起。
    var onDiagnostic: ((String) -> Void)?
    var inspectionHandler: (([String: Any]) async -> [String: Any])?
    var imageHandler: ((String, String) async throws -> Data)?
    var videoRequestHandler: ((String, String, String, String) async throws -> [String: Any])?

    func videoRequest(path: String, method: String, body: String) async throws -> [String: Any] {
        guard let videoRequestHandler else { throw URLError(.resourceUnavailable) }
        let ticket = generation
        let result = try await videoRequestHandler(scope, path, method, body)
        guard ticket == generation, !Task.isCancelled else { throw CancellationError() }
        return result
    }

    private let inlineMedia = ReaderNativeInlineMedia()
    private func containsMediaDocument(_ content: String) -> Bool {
        func contains(_ value: Any) -> Bool {
            if let text = value as? String { return text == content }
            if let values = value as? [Any] { return values.contains(where: contains) }
            if let values = value as? [String: Any] { return values.values.contains(where: contains) }
            return false
        }
        return messages.contains { $0.text == content || $0.parts.contains { $0.text == content || contains($0.data) } }
            || placements.contains { $0.parts.contains { $0.text == content || contains($0.data) } }
            || contains(review) || (inspection.map { contains($0.content) } ?? false)
    }
    func inlineImages(content: String, format: String) -> [String: String] {
        guard containsMediaDocument(content) else { return [:] }
        return inlineMedia.images(content: content, format: format)
    }
    func nativeInlineResource(_ token: String) -> String? {
        inlineMedia.resource(token, isCurrent: containsMediaDocument)
    }

    func nativeArtifactResource(_ token: String) -> String? {
        for part in messages.flatMap(\.parts) + placements.flatMap(\.parts) {
            guard part.data["nativeDetail"] != nil else { continue }
            for item in part.data["items"] as? [[String: Any]] ?? [] where item["mediaID"] as? String == token {
                guard let route = item["nativeRoute"] as? String, !route.isEmpty else { return nil }
                return route
            }
        }
        return nil
    }

    func imageData(_ id: String) async throws -> Data {
        guard let imageHandler else { throw URLError(.resourceUnavailable) }
        let ticket = generation
        let data = try await imageHandler(scope, id)
        guard ticket == generation, !Task.isCancelled else { throw CancellationError() }
        return data
    }
    private var generation = UUID()
    private var conversationStore = ReaderNativeConversationStore()
    private var messageResyncPending = false
    private var retiredNavigationScopes = Set<String>()
    private var reportedRetiredDrop = Set<String>()
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
        guard !retiredNavigationScopes.contains(nextScope) else {
            if reportedRetiredDrop.insert(nextScope).inserted {
                onDiagnostic?("对话更新被丢弃：范围已作废 scope=\(nextScope.prefix(20)) rev=\(nextRevision)")
            }
            return
        }
        if nextScope == scope, nextRevision <= revision { return }
        let rawMessages: [[String:Any]]
        let changedMessages: Bool
        if let batch = payload["messageDelta"] as? [String:Any] {
            do {
                changedMessages = try conversationStore.apply(batch,scope:nextScope)
                rawMessages = conversationStore.messages
                messageResyncPending = false
            } catch {
                self.error = error.localizedDescription
                onDiagnostic?("对话增量未应用：\(error.localizedDescription) scope=\(nextScope.prefix(20)) "
                    + "have=\(conversationStore.revision)@\(conversationStore.scope.prefix(20)) "
                    + "base=\(batch["baseRevision"] ?? "?") next=\(batch["revision"] ?? "?") "
                    + "reset=\(batch["reset"] ?? "?") contract=\(batch["contract"] ?? "?")")
                requestMessageResync(scope:nextScope)
                return
            }
        } else if let values = payload["messages"] as? [[String:Any]] {
            rawMessages = values; changedMessages = true
        } else {
            guard nextScope == conversationStore.scope,
                  (payload["messageRevision"] as? NSNumber)?.int64Value == conversationStore.revision else {
                onDiagnostic?("对话快照与本地消息版本不一致：have=\(conversationStore.revision) "
                    + "page=\(payload["messageRevision"] ?? "?")")
                requestMessageResync(scope:nextScope); return
            }
            rawMessages = []; changedMessages = false
        }
        if nextScope != scope {
            inlineMedia.reset()
            committedCards = [:]
            removedMedia = [:]
            committedReviewPresentation = nil
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
        var nextMessages = changedMessages ? rawMessages
            .compactMap(ReaderNativeConversationMessage.init)
            .filter { seen.insert($0.id).inserted } : messages
        let nextMode = payload["conversationMode"] as? String == "review" ? "review" : "normal"
        if changedMessages, ReaderNativeConversationCache.hasConversation(rawMessages) {
            cacheSuppressed.remove(nextMode)
            rememberConversation(rawMessages, mode: nextMode)
            showingCachedMessages = false
        } else if changedMessages, !cacheSuppressed.contains(nextMode) {
            // 页面还没把历史交过来（或取失败）：先给本机缓存，别让侧栏空着。
            let cached = cachedMessages(nextMode)
            showingCachedMessages = !cached.isEmpty
            if !cached.isEmpty { nextMessages = cached + nextMessages.filter { $0.role != "user" && $0.role != "assistant" } }
        }
        // Publish the revision last: observers scroll only after the entire
        // snapshot is available, never after a partially replaced message list.
        scope = nextScope
        title = (payload["title"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "阅读助手"
        conversationMode = nextMode
        review = reviewPresentation(payload["review"] as? [String: Any] ?? [:])
        ready = payload["ready"] as? Bool ?? false
        busy = payload["busy"] as? Bool ?? false
        legacyVisible = payload["legacyVisible"] as? Bool ?? false
        webPlacements = (payload["placements"] as? [[String: Any]] ?? []).compactMap(ReaderNativePagePlacement.init)
        mergePlacements()
        sidebarOpen = payload["sidebarOpen"] as? Bool ?? false
        favoritesCount = nativeFavoritesCount ?? (payload["favoritesCount"] as? NSNumber)?.intValue ?? 0
        selectionText = (payload["selection"] as? [String: Any])?["text"] as? String ?? ""
        readerSelectionText = (payload["readerSelection"] as? [String: Any])?["text"] as? String ?? ""
        let attachmentValues = payload["attachments"] as? [[String:Any]] ?? []
        figureAttachments = attachmentValues.filter { $0["kind"] as? String == "figure" }.compactMap(ReaderNativeContextAttachment.init)
        if contextRecords != nil { refreshContextAttachments() }
        else { attachments = attachmentValues.compactMap(ReaderNativeContextAttachment.init) }
        readingTools = (payload["readingTools"] as? [[String: Any]] ?? []).compactMap(ReaderNativeControl.init)
        if let navigation = payload["navigation"] as? [String: Any] { navigationPanel?.receive(navigation) }
        capabilities = Set(payload["capabilities"] as? [String] ?? [])
        voice = ReaderNativeConversationVoice(payload["voice"] as? [String: Any] ?? [:])
        let nextCaptions = ReaderNativeCaptions(payload["captions"] as? [String: Any] ?? [:])
        if nextCaptions != captions { captions = nextCaptions }
        if feedActive && nextMode == "normal" { publishFeed() }
        else if changedMessages { messages = nextMessages.map { value in var next = value; next.parts = applyCommittedCards(value.parts); return next } }
        revision = nextRevision
        noteSnapshotCost(payload["payloadBytes"] as? Int ?? 0)
    }

    // MARK: 原生对话流（迁出 P2）
    /// 设了之后，普通会话的消息只来自原生对话流；网页投影里的消息不再用（复习会话仍走网页）。
    private(set) var feedActive = false
    private var feedRaw: [[String: Any]] = []
    func applyFeed(_ raw: [[String: Any]]) {
        feedActive = true; feedRaw = raw
        guard conversationMode == "normal" else { return }
        publishFeed()
    }
    private func publishFeed() {
        var seen = Set<String>()
        let next = feedRaw.compactMap(ReaderNativeConversationMessage.init).filter { seen.insert($0.id).inserted }
        if next.isEmpty {
            let cached = cachedMessages("normal")
            showingCachedMessages = !cached.isEmpty
            messages = cached
            return
        }
        if ReaderNativeConversationCache.hasConversation(feedRaw) { rememberConversation(feedRaw, mode: "normal") }
        showingCachedMessages = false
        messages = next.map { value in var item = value; item.parts = applyCommittedCards(value.parts); return item }
    }

    func requestMessageResync(scope:String) {
        guard !messageResyncPending else { return }
        messageResyncPending = true
        Task { [weak self] in
            let failure = await self?.commandHandler?(["action":"resyncMessages","scope":scope])
            if let failure { self?.onDiagnostic?("对话重新同步失败：" + (failure.isEmpty ? "（无原因）" : failure)) }
            self?.messageResyncPending = false
        }
    }

    /// 写本机缓存。⚠ 节流：快照一秒能来好几次；内容没变或 3 秒内写过就跳过。
    /// 流式回复还在长的时候不写（写进去的是半句话）。
    private func rememberConversation(_ raw: [[String: Any]], mode: String) {
        guard !raw.contains(where: { $0["streaming"] as? Bool == true }) else { return }
        guard let digest = ReaderNativeConversationStore.fingerprint(raw) else { return }
        let signature = mode + "|" + digest
        guard signature != lastCachedSignature, Date().timeIntervalSince(lastCachedAt) > 3 else { return }
        lastCachedSignature = signature
        lastCachedAt = Date()
        cachedByMode[mode] = nil
        ReaderNativeConversationCache.save(raw, mode: mode)
    }

    private func cachedMessages(_ mode: String) -> [ReaderNativeConversationMessage] {
        if let hit = cachedByMode[mode] { return hit }
        var seen = Set<String>()
        let loaded = ReaderNativeConversationCache.load(mode)
            .compactMap(ReaderNativeConversationMessage.init)
            .filter { seen.insert($0.id).inserted }
        cachedByMode[mode] = loaded
        return loaded
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

    /// 新页面已提交：旧页面从此不可能再发消息，作废名单可以清空了。
    /// ⚠ 必须清：同一本书的新页面算出的 scope 与旧页面**相同**（按账号+书哈希），
    ///   不清的话新页面的所有对话更新都被当成旧页面的迟到消息永久丢弃 ——
    ///   这就是「切后台回来侧栏不同步、显示故障」（2026-09-26 查明）。
    func navigationCommitted() {
        retiredNavigationScopes.removeAll()
        reportedRetiredDrop.removeAll()
    }

    /// `retireScope`：页面还活着（正常导航）时，旧页面可能有迟到消息，要作废它的范围；
    /// 网页进程已被杀时旧页面不存在，作废只会误伤重载后的同一本书。
    func resetForNavigation(retireScope: Bool = true) {
        inlineMedia.reset()
        committedCards = [:]
        removedMedia = [:]; selectedContextIDs = []; contextRecords = nil; figureAttachments = []
        conversationStore = ReaderNativeConversationStore(); messageResyncPending = false
        inspection = nil
        settingsPanel = nil
        readingSettingsPanel = nil
        searchPanel = nil
        tocPanel = nil
        navigationPanel = nil
        if retireScope, !scope.isEmpty { retiredNavigationScopes.insert(scope) }
        generation = UUID()
        scope = ""
        pendingSelections = []
        deliveringSelection = false
        revision = -1
        title = "阅读助手"
        conversationMode = "normal"
        review = [:]
        committedReviewPresentation = nil
        ready = false
        busy = false
        legacyVisible = false
        placements = []; webPlacements = []; nativeHTMLNotes = nil
        sidebarOpen = false
        selectionText = ""
        readerSelectionText = ""
        attachments = []
        readingTools = []
        messages = []
        capabilities = []
        voice = ReaderNativeConversationVoice()
        captions = ReaderNativeCaptions()
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
        guard ready || ["refresh", "snapshot", "hideLegacy", "toggleAssistant", "liveAction"].contains(action) else {
            error = "助手仍在准备，请稍后重试。"
            return false
        }
        guard let commandHandler else {
            error = "助手尚未连接，请重新打开阅读器。"
            return false
        }
        guard !pendingActions.contains(action) else { return false }
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
            // ⚠ 失败**只留面包屑，不各发一条上报**（2026-09-22 改）。
            //   上一版每次失败都 report 一次，而 report 会落盘 + 触发整个发件箱重投。
            //   在服务器书上这类失败是**成串**的（一次翻页能来九条
            //   `BW_PI_GATEWAY_REMOTE_BOOK`），于是诊断机制自己变成了负载源 ——
            //   而用户报的正是"关掉服务器就不闪退了"。
            //   面包屑是内存里的环形缓冲，够便宜；真出事时它会跟着崩溃报告一起走。
            ReaderNativeFaultReporter.shared.note("fail", action + ":" + (error ?? ""))
            return false
        }
        if action == "clearConversation" {
            // 清空就连本机缓存一起清，否则下一次快照为空时缓存又把旧对话摆回来。
            ReaderNativeConversationCache.clear(conversationMode)
            cachedByMode[conversationMode] = []
            cacheSuppressed.insert(conversationMode)
            lastCachedSignature = ""
            showingCachedMessages = false
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
    static let contentType = UTType(exportedAs: "space.bwicarus.reader-card-handle", conformingTo: .data)

    static var transferRepresentation: some TransferRepresentation {
        DataRepresentation(contentType: contentType) { value in
            try JSONEncoder().encode(value)
        } importing: { data in
            try JSONDecoder().decode(Self.self, from: data)
        }
    }
}
