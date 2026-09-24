import Foundation

/// The dictionary/explanation endpoints already own durable jobs. Submit once,
/// then read that same rid after a disconnected stream; never regenerate it.
actor ReaderNativeLookupStream {
    struct Failure: LocalizedError, Sendable {
        let message: String
        var errorDescription: String? { message }
    }
    struct Plan: Sendable {
        let mode: String
        let text: String
        let path: String
        let method: String
        let body: Data
        let resultPath: String

        init(mode: String, text: String, context: String, rid: String = UUID().uuidString) throws {
            guard ["jp-ai", "explain"].contains(mode), !text.isEmpty, text.utf16.count <= 2000,
                  !text.contains("\0"), UUID(uuidString: rid) != nil else {
                throw Failure(message: "解释请求无效")
            }
            self.mode = mode
            let context = String(context.prefix(320))
            self.text = mode == "explain" && text.utf16.count < 50 && context.utf16.count > text.utf16.count ? context : text
            resultPath = "/pdf/api/ai-stream-result?id=" + rid
            if mode == "jp-ai" {
                var url = URLComponents(); url.path = "/pdf/api/dict-jp-ai"
                url.queryItems = [.init(name: "word", value: text), .init(name: "context", value: context), .init(name: "rid", value: rid)]
                url.percentEncodedQuery = url.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
                guard let path = url.string else { throw Failure(message: "解释请求无效") }
                self.path = path; method = "GET"; body = Data()
            } else {
                path = "/pdf/api/explain"; method = "POST"
                body = try JSONSerialization.data(withJSONObject: ["text": self.text, "context": context, "rid": rid])
            }
        }
    }
    typealias Connect = @Sendable (ReaderNativeAssistantStream.Response, ReaderNativeAssistantStream.Chunk) async throws -> Void
    typealias Poll = @Sendable () async throws -> Data
    private let connect: Connect
    private let poll: Poll
    private let pause: @Sendable () async throws -> Void
    private let maximumPolls: Int
    private var decoder = ReaderNativeAssistantSSE()
    private var wire = Data()
    private var text = ""
    private var done = false
    private var started = false

    init(maximumPolls: Int = 240, pause: @escaping @Sendable () async throws -> Void = {
        try await Task.sleep(nanoseconds: 1_200_000_000)
    }, connect: @escaping Connect, poll: @escaping Poll) {
        self.maximumPolls = maximumPolls; self.pause = pause; self.connect = connect; self.poll = poll
    }

    func run() async throws -> String {
        guard !started else { throw Failure(message: "解释请求已启动") }; started = true
        try Task.checkCancellation()
        do { try await connect({ try await self.response($0) }, { try await self.consume($0) }) }
        catch {
            if error is CancellationError || Task.isCancelled { throw CancellationError() }
            // Authorization/server-declared errors are terminal. Only an
            // uncertain transport result can switch to read-only polling.
            if error is Failure || error is ReaderNativeAssistantStream.Failure { throw error }
        }
        try Task.checkCancellation()
        if done { return String(text.prefix(20000)) }
        if let object = try? JSONSerialization.jsonObject(with: wire) as? [String: Any] {
            guard object["ok"] as? Bool == true,
                  let result = object["explanation"] as? String ?? object["translation"] as? String else {
                throw Failure(message: object["error"] as? String ?? "解释请求未获确认")
            }
            return String(result.prefix(20000))
        }
        wire.removeAll(keepingCapacity: false)
        for attempt in 0..<maximumPolls {
            try Task.checkCancellation()
            if attempt > 0 { try await pause() }
            do {
                let data = try await poll()
                try Task.checkCancellation()
                guard data.count <= 8 * 1024 * 1024,
                      let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    throw Failure(message: "解释结果格式无效")
                }
                if let full = result["full"] as? String, full.utf16.count > text.utf16.count { text = full }
                switch result["status"] as? String {
                case "done": return String(text.prefix(20000))
                case "error": throw Failure(message: result["error"] as? String ?? "解释失败")
                case "unknown" where attempt >= 3: throw Failure(message: "解释任务不存在，未重新提交")
                case "unknown", "running": break
                default: throw Failure(message: "解释结果状态无效")
                }
            } catch {
                if error is CancellationError || Task.isCancelled { throw CancellationError() }
                if error is Failure || error is ReaderNativeAssistantStream.Failure { throw error }
            }
        }
        throw Failure(message: "解释结果尚未确认，未重新提交")
    }

    private func response(_ status: Int) throws -> Bool {
        try Task.checkCancellation()
        guard (200..<300).contains(status) else { throw Failure(message: "解释请求失败（HTTP \(status)）") }
        return true
    }
    private func consume(_ bytes: Data) throws -> Bool {
        try Task.checkCancellation()
        guard bytes.count <= 8 * 1024 * 1024 - wire.count else { throw Failure(message: "解释内容超出读取上限") }
        wire.append(bytes)
        for event in try decoder.append(bytes) {
            if event.name == "error" {
                let value = try? JSONSerialization.jsonObject(with: Data(event.data.utf8)) as? [String: Any]
                throw Failure(message: value?["error"] as? String ?? "解释失败")
            }
            if event.name == "done" { done = true; return false }
            if let value = try? JSONSerialization.jsonObject(with: Data(event.data.utf8)) as? [String: Any],
               let delta = value["text"] as? String { text += delta }
        }
        return true
    }
}
