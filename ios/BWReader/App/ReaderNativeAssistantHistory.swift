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

    /// 占位消息 → 原生消息（保留占位的 id，部件 id 由它派生）。
    func resolve(_ placeholder: [String: Any]) -> [String: Any]? {
        guard let ref = placeholder["nativeHistoryRef"] as? String, let id = placeholder["id"] as? String else { return nil }
        let pieces = ref.split(separator: "#", maxSplits: 1).map(String.init)
        guard pieces.count == 2, let index = Int(pieces[1]),
              let entry = entries.last(where: { $0.token == pieces[0] }),
              entry.messages.indices.contains(index), var message = entry.messages[index] else { return nil }
        message["id"] = id
        message["parts"] = (message["parts"] as? [[String: Any]] ?? []).enumerated().map { offset, part in
            var part = part, data = part["data"] as? [String: Any] ?? [:]
            let partID = id + "-h" + String(offset)
            part["id"] = partID; data["nativeActionKey"] = partID; part["data"] = data
            return part
        }
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
        for key in ["videos", "undo_cards", "actions"] {
            if let values = record[key] as? [Any], !values.isEmpty { unmigrated[key, default: 0] += values.count }
        }
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
