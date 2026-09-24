import Foundation

/// The server remains authoritative. Swift coalesces reads, fences stale
/// results and submits a clear once; it never converts a transport failure
/// into an empty conversation or repeats an uncertain clear operation.
actor ReaderNativeAssistantHistory {
    struct Response: Sendable {
        let status: Int
        let body: Data
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
                let response = try await fetch(route.path, route.method, route.body)
                try Task.checkCancellation()
                if (200..<300).contains(response.status) {
                    guard response.body.count <= 16 * 1024 * 1024,
                          let value = try JSONSerialization.jsonObject(with: response.body) as? [String: Any],
                          value["ok"] as? Bool == true, let messages = value["messages"] as? [Any], messages.count <= 10_000 else {
                        throw Failure(message: "历史响应不完整，已保留现有对话")
                    }
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
