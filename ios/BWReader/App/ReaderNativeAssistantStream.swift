import Foundation

/// Incremental SSE decoding stays off the UI actor. Byte buffering preserves
/// split UTF-8 characters, CRLF/CR/LF boundaries, comments and multiline data.
struct ReaderNativeAssistantEvent: Sendable, Equatable {
    let name: String
    let data: String
}

struct ReaderNativeAssistantSSE {
    private var line: [UInt8] = []
    private var name = "message"
    private var data: [String] = []
    private var eventBytes = 0
    private var sawCR = false
    private var firstLine = true
    let maximumBytes: Int

    init(maximumBytes: Int = 8 * 1024 * 1024) { self.maximumBytes = maximumBytes }

    mutating func append(_ bytes: Data) throws -> [ReaderNativeAssistantEvent] {
        var events: [ReaderNativeAssistantEvent] = []
        for byte in bytes {
            if sawCR { sawCR = false; if byte == 10 { continue } }
            if byte == 10 || byte == 13 {
                var text = String(decoding: line, as: UTF8.self)
                if firstLine { firstLine = false; if text.hasPrefix("\u{feff}") { text.removeFirst() } }
                line.removeAll(keepingCapacity: true)
                if text.isEmpty {
                    if !data.isEmpty { events.append(.init(name: name, data: data.joined(separator: "\n"))) }
                    name = "message"; data.removeAll(keepingCapacity: true); eventBytes = 0
                } else if !text.hasPrefix(":") {
                    let split = text.firstIndex(of: ":")
                    let field = split.map { String(text[..<$0]) } ?? text
                    var value = split.map { String(text[text.index(after: $0)...]) } ?? ""
                    if value.first == " " { value.removeFirst() }
                    if field == "event" { name = value.isEmpty ? "message" : value }
                    else if field == "data" { data.append(value) }
                    eventBytes += text.utf8.count + 1
                    guard eventBytes <= maximumBytes else { throw ReaderNativeAssistantStream.Failure("对话事件超过读取上限") }
                }
                sawCR = byte == 13
            } else {
                guard line.count < maximumBytes - eventBytes else { throw ReaderNativeAssistantStream.Failure("对话事件超过读取上限") }
                line.append(byte)
            }
        }
        return events
    }
    // EOF never dispatches an incomplete event: a continuation replays it from
    // the last acknowledged cursor instead of applying a partial action.
}

actor ReaderNativeAssistantStream {
    struct Failure: LocalizedError, Sendable {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }
    enum Result: String, Sendable { case done, gone, exhausted }
    typealias Response = @Sendable (Int) async throws -> Bool
    typealias Chunk = @Sendable (Data) async throws -> Bool
    typealias Connect = @Sendable (Data, Response, Chunk) async throws -> Void
    typealias Deliver = @Sendable ([ReaderNativeAssistantEvent]) async throws -> Void

    private let initial: Data
    private let rid: String
    private let mode: String
    private let connect: Connect
    private let deliver: Deliver
    private let retryDelay: @Sendable (Int) async throws -> Void
    private let maximumRetries: Int
    private var cursor = 0
    private var result: Result?
    private var decoder = ReaderNativeAssistantSSE()
    private var deliveryFailure: Error?
    private var started = false

    init(initial: Data, maximumRetries: Int = 40,
         retryDelay: @escaping @Sendable (Int) async throws -> Void = { attempt in
             try await Task.sleep(nanoseconds: UInt64(min(400 * attempt, 2000)) * 1_000_000)
         }, connect: @escaping Connect, deliver: @escaping Deliver) throws {
        guard initial.count <= 8 * 1024 * 1024,
              let value = try JSONSerialization.jsonObject(with: initial) as? [String: Any],
              let rid = value["rid"] as? String, !rid.isEmpty, rid.utf8.count <= 128,
              rid.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95 }),
              value["message"] is String, value["from"] == nil,
              let mode = value["assistant_mode"] as? String, ["normal", "review"].contains(mode) else {
            throw Failure("对话请求缺少有效任务编号或模式")
        }
        self.initial = initial; self.rid = rid; self.mode = mode
        self.maximumRetries = maximumRetries; self.retryDelay = retryDelay
        self.connect = connect; self.deliver = deliver
    }

    func run() async throws -> Result {
        guard !started else { throw Failure("同一对话请求不能重复启动") }
        started = true
        for attempt in 0...maximumRetries {
            try Task.checkCancellation()
            if attempt > 0 { try await retryDelay(attempt) }
            decoder = ReaderNativeAssistantSSE()
            // An uncertain first submission must never be sent a second time.
            let body = attempt == 0 ? initial : try JSONSerialization.data(withJSONObject: [
                "rid": rid, "from": cursor, "assistant_mode": mode
            ])
            do {
                try await connect(body, { status in try await self.response(status) },
                                  { bytes in try await self.consume(bytes) })
            } catch {
                try Task.checkCancellation()
                if let deliveryFailure { throw deliveryFailure }
                // Validation, authorization and consumer failures are terminal;
                // only transport failures should open a continuation.
                if error is Failure { throw error }
            }
            try Task.checkCancellation()
            if let result { return result }
        }
        return .exhausted
    }

    private func response(_ status: Int) throws -> Bool {
        try Task.checkCancellation()
        if status == 410 { result = .gone; return false }
        if (400...499).contains(status), status != 408, status != 429 { throw Failure("对话请求被拒绝（HTTP \(status)）") }
        guard (200...299).contains(status) else { throw URLError(.badServerResponse) }
        return true
    }

    private func consume(_ bytes: Data) async throws -> Bool {
        try Task.checkCancellation()
        var events = try decoder.append(bytes).filter { $0.name != "meta" }
        if let end = events.firstIndex(where: { $0.name == "done" }) { events = Array(events[...end]) }
        guard !events.isEmpty else { return true }
        do { try await deliver(events) }
        catch { deliveryFailure = error; throw error }
        try Task.checkCancellation()
        cursor += events.count
        if events.last?.name == "done" { result = .done; return false }
        return true
    }
}
