import Foundation

/// The server remains authoritative. Swift coalesces reads, fences stale
/// results and submits a clear once; it never converts a transport failure
/// into an empty conversation or repeats an uncertain clear operation.
actor ReaderNativeAssistantHistory {
    struct Response: Sendable {
        let status: Int
        let body: Data
        var presentation: Data? = nil
    }
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    typealias Fetch = @Sendable (String, String, Data) async throws -> Response
    private let fetch: Fetch
    private var epoch = UUID()
    private var reads: [String: (id: UUID, task: Task<Response, Error>)] = [:]
    private var clears: [String: (id: UUID, task: Task<Response, Error>)] = [:]
    private var revisions: [String: Int] = [:]

    init(fetch: @escaping Fetch) { self.fetch = fetch }

    struct Route {
        let path: String
        let family: String
        let method: String
        let body: Data
    }

    static func route(_ path: String, operation: String, mode: String) throws -> Route {
        guard ["read", "clear"].contains(operation), ["normal", "review"].contains(mode),
              path.utf16.count <= 8192, path.hasPrefix("/"), !path.hasPrefix("//"),
              let parts = URLComponents(string: path), parts.scheme == nil, parts.host == nil,
              parts.fragment == nil else { throw Failure(message: "对话历史参数无效") }
        let items = parts.queryItems ?? []
        guard Set(items.map(\.name)).count == items.count else { throw Failure(message: "重复的会话参数") }
        let suffix = operation == "read" ? "history" : "clear"
        let family: String
        if parts.percentEncodedPath == "/api/assistant/" + suffix {
            if operation == "read", mode == "review" {
                guard items.count == 1, items[0].name == "assistant_mode", items[0].value == "review" else {
                    throw Failure(message: "复习会话不匹配")
                }
            } else if !items.isEmpty { throw Failure(message: "会话参数不匹配") }
            family = mode
        } else { throw Failure(message: "未登记的对话历史接口") }
        let body = operation == "clear" && mode == "review"
            ? Data(#"{"assistant_mode":"review"}"#.utf8) : Data()
        return Route(path: path, family: family, method: operation == "read" ? "GET" : "POST", body: body)
    }

    /// Presentation only: original history bytes and entities remain intact.
    /// Invalid rows retain their position so the existing per-row recovery can
    /// report them without discarding unrelated valid messages.
    static func presentations(_ messages: [Any], mode: String) throws -> Data {
        let projections: [Any] = messages.map { item in
            guard let message = item as? [String: Any], let role = message["role"] as? String,
                  ["user", "assistant"].contains(role) else { return NSNull() }
            let raw = message["content"] as? String ?? ""
            var result: [String: Any] = ["mode": mode]
            func nonempty(_ value: Any?) -> Bool {
                guard let value, !(value is NSNull) else { return false }
                if let text = value as? String { return !text.isEmpty }
                if let number = value as? NSNumber { return number.doubleValue != 0 }
                return true
            }
            let direct = [message["turn_id"], message["id"], message["rid"]].first(where: nonempty) ?? nil
            for candidate in [message["history_id"], direct] where nonempty(candidate) {
                let value: String
                if let text = candidate as? String { value = text }
                else if let number = candidate as? NSNumber { value = number.stringValue }
                else { continue }
                guard !value.isEmpty, value.utf8.count <= 160,
                      value.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || [45,46,58,95].contains($0) }) else { continue }
                result["turnID"] = "hist_" + mode + "_" + value; break
            }
            if role == "user" { result["kind"] = "user"; result["text"] = raw }
            else if let parts = message["parts"] as? [Any], !parts.isEmpty { result["kind"] = "parts" }
            else if nonempty(message["card"]) { result["kind"] = "card" }
            else {
                let content = ReaderNativeAssistantTurn.content(raw)
                result["kind"] = "answer"; result["text"] = content.finalDisplayText
                result["followups"] = content.followups
                result["subtitle"] = message["via"] as? String == "voice"
            }
            return result
        }
        return try JSONSerialization.data(withJSONObject: projections)
    }

    func read(_ route: Route) async throws -> Response {
        let lease = epoch
        if let clearing = clears[route.family] { _ = try? await clearing.task.value }
        try Task.checkCancellation()
        guard epoch == lease else { throw CancellationError() }
        let revision = revisions[route.family, default: 0]
        let job: (id: UUID, task: Task<Response, Error>)
        if let existing = reads[route.family] { job = existing }
        else {
            guard reads.count < 4 else { throw Failure(message: "历史请求过多，请稍后再试") }
            job = (UUID(), Task { [fetch] in
                var response = try await fetch(route.path, route.method, route.body)
                try Task.checkCancellation()
                if (200..<300).contains(response.status) {
                    guard response.body.count <= 16 * 1024 * 1024,
                          let value = try JSONSerialization.jsonObject(with: response.body) as? [String: Any],
                          value["ok"] as? Bool == true, let messages = value["messages"] as? [Any], messages.count <= 10_000 else {
                        throw Failure(message: "历史响应不完整，已保留现有对话")
                    }
                    response.presentation = try Self.presentations(messages, mode: route.family)
                }
                return response
            })
            reads[route.family] = job
        }
        defer { if reads[route.family]?.id == job.id { reads.removeValue(forKey: route.family) } }
        let response = try await job.task.value
        guard epoch == lease, revisions[route.family, default: 0] == revision else { throw CancellationError() }
        return response
    }

    func clear(_ route: Route) async throws -> Response {
        let lease = epoch
        let job: (id: UUID, task: Task<Response, Error>)
        if let existing = clears[route.family] { job = existing }
        else {
            revisions[route.family, default: 0] += 1
            reads.removeValue(forKey: route.family)?.task.cancel()
            job = (UUID(), Task { [fetch] in try await fetch(route.path, route.method, route.body) })
            clears[route.family] = job
        }
        defer { if clears[route.family]?.id == job.id { clears.removeValue(forKey: route.family) } }
        let response = try await job.task.value
        guard epoch == lease else { throw CancellationError() }
        return response
    }

    /// Recovery reads only; never submits a second task. Older servers omit
    /// request identity, so retain their last-assistant fallback. When an
    /// identity is present it must match this request, not a neighboring turn.
    func recover(_ route: Route, rid: String, turnID: String,
                 wait: @Sendable () async throws -> Void = { try await Task.sleep(nanoseconds: 800_000_000) }) async throws -> [String: Any]? {
        let lease = epoch, revision = revisions[route.family, default: 0]
        for attempt in 0..<3 {
            guard epoch == lease, revisions[route.family, default: 0] == revision else { throw CancellationError() }
            let response = try await read(route)
            guard epoch == lease, revisions[route.family, default: 0] == revision else { throw CancellationError() }
            guard (200..<300).contains(response.status),
                  let value = try JSONSerialization.jsonObject(with: response.body) as? [String: Any],
                  let messages = value["messages"] as? [[String: Any]], let last = messages.last else { return nil }
            if last["role"] as? String == "assistant", let text = last["content"] as? String, !text.isEmpty {
                if let savedRID = last["rid"] as? String, !savedRID.isEmpty, savedRID != rid { return nil }
                if let savedTurn = last["turn_id"] as? String, !savedTurn.isEmpty, savedTurn != turnID { return nil }
                return last
            }
            if attempt < 2 { try await wait(); try Task.checkCancellation() }
        }
        return nil
    }

    func invalidate() {
        epoch = UUID()
        reads.values.forEach { $0.task.cancel() }; reads.removeAll()
        clears.values.forEach { $0.task.cancel() }; clears.removeAll()
        revisions.removeAll()
    }
}

/// 历史回放的**原生内容**（迁出 P1，2026-09-26）。网页层按原顺序为每条历史放一个
/// 占位节点（只带 ref），侧栏投影到原生时按 ref 换成这里建好的消息 —— 正文、上下文、
/// 追问、旧版卡片都由 Swift 从历史记录直接生成，不再从网页 DOM 抓。
/// 带 parts 的轮次仍走 TurnStore（本来就是原生数据），这里返回 nil。
@MainActor
final class ReaderNativeHistoryMessages {
    private var entries: [(token: String, messages: [[String: Any]?])] = []
    var onDiagnostic: ((String) -> Void)?
    /// 退回网页文字助手（语音核心不在）时，用户那句话同时交给原生对话流。
    var onLiveUser: (([String: Any]) -> Void)?

    /// 建好一批，返回 token；只保留最近几批（重同步会带着旧 ref 再来）。
    func store(_ raw: [Any], mode: String) -> (token: String, built: [Bool]) {
        var unmigrated: [String: Int] = [:]
        let messages = raw.enumerated().map { Self.build($0.element, index: $0.offset, unmigrated: &unmigrated) }
        let token = UUID().uuidString
        entries.append((token, messages))
        if entries.count > 6 { entries.removeFirst(entries.count - 6) }
        let built = messages.filter { $0 != nil }.count
        let missing = unmigrated.sorted { $0.key < $1.key }.map { "\($0.key)×\($0.value)" }.joined(separator: " ")
        onDiagnostic?("历史原生化：\(mode) 共 \(raw.count) 条，原生生成 \(built) 条" + (missing.isEmpty ? "" : "；尚未迁移：" + missing))
        return (token, messages.map { $0 != nil })
    }

    /// 正在进行的一轮（迁出 P2a）：单条消息，返回 ref。
    func storeLive(_ message: [String: Any]) -> String {
        let token = "live-" + UUID().uuidString
        entries.append((token, [message]))
        if entries.count > 24 { entries.removeFirst(entries.count - 24) }
        return token + "#0"
    }

    /// 发送时定格的请求 → 侧栏里「你」那一条：正文 + 上下文一行（页码/选中/图）。
    static func liveUser(_ body: [String: Any]) -> [String: Any] {
        var message: [String: Any] = ["role": "user", "streaming": false, "title": "", "statusText": "", "parts": [[String: Any]](),
                                      "text": displayAttachments(body["message"] as? String ?? "")]
        let context = body["context"] as? [String: Any] ?? [:]
        let line = contextLine(context)
        if !line.isEmpty { message["contextLine"] = line }
        return message
    }

    static func build(_ item: Any, index: Int, unmigrated: inout [String: Int]) -> [String: Any]? {
        guard let record = item as? [String: Any], let role = record["role"] as? String,
              ["user", "assistant"].contains(role) else { return nil }
        let content = record["content"] as? String ?? ""
        var message: [String: Any] = ["role": role, "streaming": false, "title": "", "statusText": "", "parts": [[String: Any]]()]
        if role == "user" {
            message["text"] = displayAttachments(content)
            let line = contextLine(record)
            if !line.isEmpty { message["contextLine"] = line }
            return message
        }
        if let parts = record["parts"] as? [Any], !parts.isEmpty { return nil }
        if let card = record["card"] as? [String: Any], !card.isEmpty {
            let kind = card["kind"] as? String ?? "artifact", title = card["title"] as? String ?? "生成物"
            var original = card
            if (original["cid"] as? String ?? "").isEmpty { original["cid"] = "hist-card-" + String(index) }
            message["text"] = ""
            message["parts"] = [["kind": kind, "title": "", "text": "", "status": "unknown", "actionLabel": "查看原件",
                                 "data": ["nativeDetail": ["kind": kind, "title": title, "content": original]]]]
            return message
        }
        let parsed = ReaderNativeAssistantTurn.content(content)
        message["text"] = parsed.finalDisplayText
        if !parsed.followups.isEmpty { message["followups"] = parsed.followups }
        if record["via"] as? String == "voice" { message["subtitle"] = true }
        // P1 遗留（2026-09-26 补）：三种旧附件也由原生显示，不再只记「尚未迁移」。
        var parts: [[String: Any]] = []
        if let videos = record["videos"] as? [[String: Any]], !videos.isEmpty {
            // 原生视频卡直接吃这些条目（id / title / channel / src / url），播放、收藏、拖到书页都由原生视频卡负责。
            let card: [String: Any] = ["kind": "videos", "cid": "hist-videos-" + String(index), "title": "相关视频",
                                       "data": ["items": Array(videos.prefix(12))]]
            parts.append(["kind": "videos", "title": "", "text": "", "status": "unknown", "actionLabel": "查看原件",
                          "data": ["nativeDetail": ["kind": "videos", "title": "相关视频", "content": card]]])
        }
        if let undo = record["undo_cards"] as? [[String: Any]], !undo.isEmpty {
            // 旧式撤销卡（新数据已是轮次里的操作记录卡）：只显示做了什么、在哪一页；撤销按钮不再提供。
            let lines = undo.compactMap { card -> String? in
                guard card["undo_id"] != nil else { return nil }
                let label = card["label"] as? String ?? "完成"
                let page = (card["page"] as? NSNumber)?.intValue ?? Int(card["page"] as? String ?? "")
                return "✓ " + label + (page.map { " · 第 \($0) 页" } ?? "")
            }
            if !lines.isEmpty {
                let text = message["text"] as? String ?? ""
                message["text"] = ([text] + lines).filter { !$0.isEmpty }.joined(separator: "\n\n")
                unmigrated["undo_cards(只显示)", default: 0] += lines.count
            }
        }
        if let actions = record["actions"] as? [[String: Any]], !actions.isEmpty {
            for (offset, action) in actions.prefix(8).enumerated() {
                let title = (action["label"] as? String) ?? (action["title"] as? String) ?? "阅读器操作"
                parts.append(["kind": "artifact", "title": "", "text": "", "status": "unknown", "actionLabel": "查看原件",
                              "data": ["nativeDetail": ["kind": "artifact", "title": title,
                                       "content": action.merging(["cid": "hist-action-\(index)-\(offset)"]) { old, _ in old }]]])
            }
        }
        if !parts.isEmpty { message["parts"] = parts }
        return message
    }

    /// 旧记录（2026-09-26 之前）把附件清单原样存成「【用户附加文件…】\n[{json}]」；
    /// 显示成与新记录一致的缩略图引用（图片）或文件名（其它）。
    static func displayAttachments(_ content: String) -> String {
        guard let marker = content.range(of: "【用户附加文件") else { return content }
        let head = content[..<marker.lowerBound].trimmingCharacters(in: .whitespacesAndNewlines)
        guard let open = content[marker.upperBound...].firstIndex(of: "["),
              let data = String(content[open...]).data(using: .utf8),
              let items = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return content }
        let lines = items.map { item -> String in
            let name = item["name"] as? String ?? "附件"
            let path = item["path"] as? String ?? ""
            let parts = path.split(separator: "/")
            if let index = parts.firstIndex(of: "assistant-attachments"), parts.indices.contains(index + 1),
               (item["mime"] as? String ?? "").hasPrefix("image/") {
                return "![\(name)](/assistant-attachments/thumb/\(parts[index + 1]))"
            }
            return "📎 " + name
        }
        return ([head.isEmpty ? "" : head] + lines).filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    /// 用户消息下的一行上下文：第几页 · 选中了什么 · 几张图（原网页 _ctxCard 的原生版）。
    static func contextLine(_ record: [String: Any]) -> String {
        var bits: [String] = []
        if let page = (record["page"] as? NSNumber)?.intValue, page > 0 { bits.append("第 \(page) 页") }
        else if let section = record["section"] as? String, !section.isEmpty { bits.append(String(section.prefix(40))) }
        if let selection = record["selection"] as? String {
            let clean = selection.split(whereSeparator: \.isNewline).joined(separator: " ").trimmingCharacters(in: .whitespaces)
            if !clean.isEmpty { bits.append("选中：" + (clean.count > 40 ? String(clean.prefix(40)) + "…" : clean)) }
        }
        if let figures = record["figures"] as? [Any], !figures.isEmpty { bits.append("\(figures.count) 张图") }
        return bits.joined(separator: " · ")
    }
}

/// SSE 解析器的并发外壳（onChunk 是 @Sendable）。
actor ReaderNativeFeedSSEBox {
    private var parser = ReaderNativeAssistantSSE()
    func append(_ data: Data) throws -> [ReaderNativeAssistantEvent] { try parser.append(data) }
}

/// 原生对话流（迁出 P2，2026-09-26）：普通会话侧栏的消息列表由 Swift 自己维护 ——
/// 历史由原生读、语音核心的实时事件（assistant-history）由原生订阅并写进 TurnStore，
/// 排序、去重、从「进行中」过渡到「已落库」都在这里。网页层不再渲染这两类内容。
///
/// 消息身份：有 turn_id 的一律 `m:<role>:<turn_id>` —— 实时那条与落库后的历史那条同一个 id，
/// 过渡时侧栏不闪、不重复。没有 turn_id 的旧记录用历史编号。
@MainActor
final class ReaderNativeConversationFeed {
    enum Entry { case plain([String: Any]), turn(tid: String, id: String) }
    private struct StreamState { var revision = -1; var final = false }

    var applyTurns: (([[String: Any]]) throws -> Void)?
    var turnMessage: ((String, String) -> [String: Any]?)?
    var readHistory: ((String) async throws -> [Any])?
    var subscribe: ((@escaping @MainActor ([String: Any]) -> Void) async throws -> Void)?
    var acknowledge: ((String) -> Void)?
    var publish: (([[String: Any]], String) -> Void)?
    var log: ((String) -> Void)?
    /// 迁出 P3：语音轮开始（stream:"start"）时把服务器的轮次号交给网页，
    /// App 现场执行的工具/结果卡就挂进同一轮（网页 __bwLiveTurnId）。
    var announceLiveTurn: ((String) -> Void)?
    /// 迁出 P3：网页那条轮次通道（RC.turnCard）写进来的轮次，只在对话流当前会话与侧栏一致时收编。
    var adoptsWebTurns: (() -> Bool)?

    /// 迁出 P4b：对话流跟随侧栏的会话（普通 / 复习）；历史、事件、清空都按这个会话。
    private(set) var mode = "normal"
    private(set) var started = false
    private var history: [Entry] = []
    private var live: [(tid: String, id: String)] = []
    private var extras: [[String: Any]] = []      // 退回网页文字助手时的原生用户话（P2a）
    private var streams: [String: StreamState] = [:]
    private var historyTask: Task<Void, Never>?
    private var reloadAgain = false
    private var eventsTask: Task<Void, Never>?
    private var reloadTimer: Task<Void, Never>?
    private var generation = 0

    static func viewID(_ tid: String, role: String) -> String { role == "user" ? "user:" + tid : tid }
    static func messageID(turn: String, role: String) -> String { "m:" + role + ":" + turn }

    func start() {
        guard !started else { return }
        started = true
        log?("对话流：原生接管" + (mode == "review" ? "复习" : "普通") + "会话")
        reloadHistory(reason: "start")
        startEvents()
        // 历史回来之前先出一次（空列表 → 侧栏显示本机缓存），网页已不再投影消息，别让侧栏空着。
        emit()
    }

    /// 侧栏切换普通 / 复习会话：两边的消息、进行中轮次、事件闸门互不相干，整体换一套再按服务器重读。
    /// 立即出一次空列表，让侧栏先显示新会话的本机缓存，而不是停在旧会话上。
    func setMode(_ next: String) {
        let value = next == "review" ? "review" : "normal"
        guard value != mode else { return }
        let wasStarted = started
        reset()
        mode = value
        if wasStarted { start() }
    }

    func reset() {
        generation += 1
        historyTask?.cancel(); historyTask = nil; eventsTask?.cancel(); eventsTask = nil; reloadTimer?.cancel(); reloadTimer = nil
        history = []; live = []; extras = []; streams = [:]; started = false; reloadAgain = false
    }

    // MARK: 历史

    func reloadHistory(reason: String) {
        guard started else { return }
        if historyTask != nil { reloadAgain = true; return }
        let ticket = generation
        historyTask = Task { @MainActor [weak self] in
            guard let self else { return }
            defer { if self.generation == ticket { self.historyTask = nil } }
            do {
                guard let read = self.readHistory else { return }
                let records = try await read(self.mode)
                guard self.generation == ticket, !Task.isCancelled else { return }
                try self.adopt(records)
                self.log?("对话流：历史 \(records.count) 条（\(reason)），实时 \(self.live.count) 条")
            } catch is CancellationError {
            } catch {
                guard self.generation == ticket else { return }
                self.log?("对话流：历史读取失败（\(reason)）：\(error.localizedDescription)")
            }
            if self.generation == ticket, self.reloadAgain { self.reloadAgain = false; self.historyTask = nil; self.reloadHistory(reason: "queued") }
        }
    }

    private func adopt(_ records: [Any]) throws {
        var next: [Entry] = [], commands: [[String: Any]] = [], unmigrated: [String: Int] = [:]
        for (index, item) in records.enumerated() {
            guard let record = item as? [String: Any], let role = record["role"] as? String, ["user", "assistant"].contains(role) else { continue }
            let turnID = (record["turn_id"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            let fallback = Self.historyID(record, index: index)
            let id = turnID.map { Self.messageID(turn: $0, role: role) } ?? "h:" + fallback
            if role == "assistant", let parts = record["parts"] as? [Any], !parts.isEmpty {
                let tid = turnID ?? "hist_" + mode + "_" + fallback
                commands.append(["action": "open", "tid": tid, "historyReplay": true,
                                 "meta": ["via": record["via"] ?? "", "threadId": record["thread_id"] ?? "", "turnId": tid]])
                commands.append(["action": "import", "tid": tid, "parts": parts])
                next.append(.turn(tid: tid, id: id))
                continue
            }
            guard var message = ReaderNativeHistoryMessages.build(record, index: index, unmigrated: &unmigrated) else { continue }
            message["id"] = id
            message["parts"] = (message["parts"] as? [[String: Any]] ?? []).enumerated().map { offset, part in
                var part = part, data = part["data"] as? [String: Any] ?? [:]
                let partID = id + "-h" + String(offset)
                part["id"] = partID; data["nativeActionKey"] = partID; part["data"] = data
                return part
            }
            next.append(.plain(message))
        }
        try applyTurns?(commands)
        history = next
        // 已落库的实时轮退出「进行中」列表（同一个 id 已在历史里）。
        let known = Set(next.map { entry -> String in
            switch entry { case .plain(let m): return m["id"] as? String ?? ""; case .turn(_, let id): return id }
        })
        live.removeAll { known.contains($0.id) || ($0.tid.hasPrefix("native-reply:") && turnMessage?($0.tid, $0.id)?["streaming"] as? Bool != true) }
        extras.removeAll { known.contains($0["id"] as? String ?? "") || extras.count > 8 }
        if !unmigrated.isEmpty { log?("对话流：历史里尚未原生化的附件 " + unmigrated.map { "\($0.key)×\($0.value)" }.sorted().joined(separator: " ")) }
        emit()
    }

    private static func historyID(_ record: [String: Any], index: Int) -> String {
        for key in ["history_id", "id", "rid"] {
            if let value = record[key] as? String, !value.isEmpty { return value }
            if let value = record[key] as? NSNumber { return value.stringValue }
        }
        return "i" + String(index)
    }

    // MARK: 实时事件

    private func startEvents() {
        let ticket = generation
        eventsTask = Task { @MainActor [weak self] in
            var failures = 0
            while let self, self.generation == ticket, !Task.isCancelled {
                do {
                    guard let subscribe = self.subscribe else { return }
                    try await subscribe { [weak self] event in
                        guard let self, self.generation == ticket else { return }
                        failures = 0
                        self.handle(event)
                    }
                } catch is CancellationError { return
                } catch {
                    failures += 1
                    if failures == 1 || failures % 10 == 0 { self.log?("对话流：事件流断开（第 \(failures) 次）：\(error.localizedDescription)") }
                }
                guard self.generation == ticket, !Task.isCancelled else { return }
                // 断线期间可能漏掉事件：重连前补读一次历史。
                self.reloadHistory(reason: "reconnect")
                let delay = min(30.0, 2.0 * pow(2.0, Double(min(failures, 4))))
                try? await Task.sleep(for: .seconds(delay))
            }
        }
    }

    func handle(_ event: [String: Any]) {
        guard started, event["kind"] as? String == "assistant-history" else { return }
        let eventMode = (event["assistant_mode"] as? String) ?? (event["mode"] as? String) ?? "normal"
        guard (eventMode == "review" ? "review" : "normal") == mode else { return }
        guard let tid = event["turn_id"] as? String, tid.range(of: "^[A-Za-z0-9_.:-]{1,160}$", options: .regularExpression) != nil else { return }
        let stream = event["stream"] as? String ?? ""
        let origin = event["origin"] as? String ?? "runner", role = event["role"] as? String ?? "assistant"
        func state(_ role: String, _ item: String) -> Bool {
            let key = tid + "|" + origin + ":" + role + ":" + (item.isEmpty ? "legacy" : item)
            var value = streams[key] ?? StreamState()
            if let revision = (event["streamRevision"] as? NSNumber)?.intValue {
                if revision < value.revision { return false }
                value.revision = revision
            }
            streams[key] = value
            if streams.count > 1024 { streams.removeAll() }
            return true
        }
        do {
            switch stream {
            case "start":
                // 迁出 P3：P2 让网页不再看事件流，连「本轮身份」一起丢了 —— 语音工具长条与结果卡
                // 因此落进网页的本地临时轮次，对话流里看不见。现在由原生把轮次号交给网页。
                guard streams[tid + "|final"] == nil else { return }
                announceLiveTurn?(tid)
                return
            case "delta":
                let item = event["item_id"] as? String ?? ""
                guard state(role, item), streams[tid + "|final"] == nil else { return }
                let view = Self.viewID(tid, role: role)
                try applyTurns?([["action": "draft", "tid": view, "text": event["content"] as? String ?? "", "role": role,
                                  "itemId": item, "origin": origin]])
                track(view, id: Self.messageID(turn: tid, role: role))
            case "parts", "final":
                let final = stream == "final"
                var commands: [[String: Any]] = []
                for message in event["messages"] as? [[String: Any]] ?? [] {
                    guard let messageRole = message["role"] as? String, ["user", "assistant"].contains(messageRole) else { continue }
                    let messageTid = (message["turn_id"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? tid
                    guard state(messageRole, message["item_id"] as? String ?? "") else { continue }
                    let messageFinal = final && message["stream_final"] as? Bool != false
                    let view = Self.viewID(messageTid, role: messageRole)
                    commands.append(["action": "reconcile", "tid": view, "message": message,
                                     "options": ["final": messageFinal, "origin": origin]])
                    track(view, id: Self.messageID(turn: messageTid, role: messageRole))
                    if messageFinal { streams[messageTid + "|final"] = StreamState(revision: 0, final: true); acknowledge?(messageTid) }
                }
                for absorbed in event["absorbed_ids"] as? [String] ?? [] where absorbed != tid && !absorbed.isEmpty {
                    commands.append(["action": "drop", "tid": absorbed])
                    live.removeAll { $0.tid == absorbed }
                }
                try applyTurns?(commands)
                if final { scheduleReload() }
            default:
                // 旧式通知（只有 turn_id）：服务器只说「这一轮有变化」，按历史补。
                scheduleReload()
            }
            emit()
        } catch {
            log?("对话流：实时事件未应用（\(stream)）：\(error.localizedDescription)")
        }
    }

    private func track(_ tid: String, id: String) {
        if !live.contains(where: { $0.id == id }) { live.append((tid, id)) }
    }

    private func scheduleReload() {
        reloadTimer?.cancel()
        reloadTimer = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .milliseconds(900))
            guard !Task.isCancelled else { return }
            self?.reloadHistory(reason: "final")
        }
    }

    // MARK: 语音事件（迁出 P3）

    /// 网页轮次通道（rc-voicecall 的工具长条 busy/idle、结果卡、流程进度、草稿镜像）一批提交之后调用。
    /// 已在对话流里的轮次只重出一次；新轮次在普通会话时收编（按首次出现排在末尾），
    /// 被改名/丢弃的轮次退出「进行中」列表 —— 与 P2 同一个消息身份 `m:assistant:<轮次>`，
    /// 服务器那条落库后自然由历史接手。
    func observeWebTurns(changed: [String], removed: [String]) {
        guard started, !(changed.isEmpty && removed.isEmpty) else { return }
        var dirty = false
        if !removed.isEmpty {
            let gone = Set(removed), before = live.count
            live.removeAll { gone.contains($0.tid) }
            dirty = live.count != before
        }
        let known = Set(live.map(\.tid)).union(history.compactMap { entry -> String? in
            if case .turn(let tid, _) = entry { return tid }
            return nil
        })
        let adopt = adoptsWebTurns?() ?? false
        var adopted: [String] = []
        for tid in changed {
            if known.contains(tid) { dirty = true; continue }
            // 用户话由服务器事件驱动（user:<轮次>）；网页这条只收助手侧的轮次。
            guard adopt, !tid.hasPrefix("user:"), !tid.hasPrefix("hist_") else { continue }
            track(tid, id: Self.messageID(turn: tid, role: "assistant"))
            adopted.append(tid); dirty = true
        }
        if !adopted.isEmpty { log?("对话流：收编语音轮次 " + adopted.map { String($0.prefix(40)) }.joined(separator: " ")) }
        if dirty { emit() }
    }

    /// 迁出 P4a：普通会话里网页直接写进对话区的提示（语音出错等）不再经 DOM 抓取，
    /// 由网页在挂载那一刻交给原生，作为一条「提示」进对话流。
    func appendNote(_ text: String) {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard started, !value.isEmpty else { return }
        noteSerial += 1
        extras.append(["id": "note:" + String(noteSerial), "role": "system", "text": String(value.prefix(2000)),
                       "streaming": false, "title": "", "statusText": "", "parts": [[String: Any]]()])
        if extras.count > 24 { extras.removeFirst(extras.count - 24) }
        emit()
    }
    private var noteSerial = 0

    // MARK: 退回网页文字助手时（语音核心不在）

    func appendExtra(_ message: [String: Any]) { extras.append(message); emit() }
    /// 会话清空后：本地列表一起清，再按服务器重读（只清对话流当前这一种会话）。
    func cleared(mode cleared: String) {
        guard cleared == mode else { return }
        history = []; live = []; extras = []; streams = [:]
        emit(); reloadHistory(reason: "cleared")
    }
    func nativeReply(_ tid: String) {
        track(tid, id: "m:reply:" + tid)
        emit()
    }

    // MARK: 输出

    func emit() {
        guard started else { return }
        var seen = Set<String>(), output: [[String: Any]] = []
        func add(_ message: [String: Any]?) {
            guard let message, let id = message["id"] as? String, seen.insert(id).inserted else { return }
            output.append(message)
        }
        let liveIDs = Set(live.map(\.id))
        for entry in history {
            switch entry {
            case .plain(let message): if !liveIDs.contains(message["id"] as? String ?? "") { add(message) }
            case .turn(let tid, let id): if !liveIDs.contains(id) { add(turnMessage?(tid, id)) }
            }
        }
        for message in extras { add(message) }
        for item in live { add(turnMessage?(item.tid, item.id)) }
        publish?(output, mode)
    }
}

/// 迁出 P4b：复习会话里助手回答的「选用」选择项由原生生成（原为网页 rc-review `_presentationSelections`
/// 按 DOM 节点登记，投影时附在消息上）。身份算法与网页一致：整条 `review-answer:<fnv(卡\n问\n答)>`，
/// 段落 `<整条>:part:<序号>:<fnv("native\n"+段落)>`；记录字段与网页 `_recordSelection` 同形，
/// 原生选择图（ReaderNativeContextSelection）直接收，`selectReview` / `reviewPairs` 不用改。
/// 一条回答第一次以「已完成」出现时绑定当时的复习卡；只有绑定的卡仍是当前卡才给选择项（与网页一致）。
struct ReaderNativeReviewAnswers {
    private var bindings: [String: String] = [:]

    /// 与 rc-review `_hash` 同一算法（按 UTF-16 码元的 FNV-1a，32 位，小写十六进制不补零）。
    static func hash(_ value: String) -> String {
        var h: UInt32 = 2_166_136_261
        for unit in value.utf16 { h ^= UInt32(unit); h = h &* 16_777_619 }
        return String(h, radix: 16)
    }

    /// 与网页 `text.split(/\n\s*\n/).filter(part => part.trim())` 同义。
    static func segments(_ text: String) -> [String] {
        let marker = "\u{1F}"
        return text.replacingOccurrences(of: #"\n\s*\n"#, with: marker, options: .regularExpression)
            .components(separatedBy: marker)
            .filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }

    mutating func reset() { bindings = [:] }

    /// 返回带 `reviewSelections` 的消息与要登记进原生选择图的记录。`card` 是当前复习卡（review.current）。
    mutating func decorate(_ messages: [[String: Any]], card: [String: Any]?,
                           isSelected: (String) -> Bool) -> (messages: [[String: Any]], records: [[String: Any]]) {
        guard let card, let cardKey = card["id"] as? String, !cardKey.isEmpty else { return (messages, []) }
        var identity: [String: Any] = [:]
        for key in ["card_id", "note_id", "local_id", "entity_id", "source_ref", "source_url", "deck"] {
            identity[key] = card[key] as? String ?? ""
        }
        identity["entity_index"] = card["entity_index"] ?? NSNull()
        let source: [String: Any] = ["surface": "assistant-review", "card_id": identity["card_id"] ?? "",
                                     "entity_id": identity["entity_id"] ?? ""]
        var question = "", output: [[String: Any]] = [], records: [[String: Any]] = []
        if bindings.count > 2048 { bindings.removeAll() }
        for var message in messages {
            let role = message["role"] as? String ?? "", text = message["text"] as? String ?? ""
            if role == "user" { question = text; output.append(message); continue }
            guard role == "assistant", message["streaming"] as? Bool != true, let id = message["id"] as? String,
                  !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { output.append(message); continue }
            let bound = bindings[id] ?? cardKey
            bindings[id] = bound
            guard bound == cardKey else { output.append(message); continue }
            let answerID = "review-answer:" + Self.hash(cardKey + "\n" + question + "\n" + text)
            let parts = Self.segments(text)
            let childIDs = parts.enumerated().map { answerID + ":part:" + String($0.offset) + ":" + Self.hash("native\n" + $0.element) }
            func record(_ id: String, _ body: String, _ index: Int) -> [String: Any] {
                ["id": id, "kind": index < 0 ? "review-answer" : "review-answer-segment",
                 "label": index < 0 ? "复习整条回答" : "复习回答段落 " + String(index + 1), "text": body,
                 "parentId": index < 0 ? "" : answerID, "covers": index < 0 ? childIDs : [String](),
                 "source": source,
                 "meta": ["review_mode": true, "answer_id": answerID, "segment_index": index, "question": question,
                          "card_key": cardKey, "card": identity] as [String: Any]]
            }
            let batch = [record(answerID, text, -1)] + parts.enumerated().map { record(childIDs[$0.offset], $0.element, $0.offset) }
            records += batch
            message["reviewSelections"] = batch.map { item -> [String: Any] in
                let itemID = item["id"] as? String ?? ""
                return ["id": itemID, "label": item["label"] ?? "", "text": item["text"] ?? "", "selected": isSelected(itemID)]
            }
            output.append(message)
        }
        return (output, records)
    }
}
