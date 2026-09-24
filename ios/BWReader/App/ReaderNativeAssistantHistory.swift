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

    func invalidate() {
        epoch = UUID()
        reads.values.forEach { $0.task.cancel() }; reads.removeAll()
        clears.values.forEach { $0.task.cancel() }; clears.removeAll()
        revisions.removeAll()
    }
}
