import Foundation

actor Harness {
    var requests: [[String: Any]] = []
    var events: [ReaderNativeAssistantEvent] = []
    let scenario: String
    init(_ scenario: String) { self.scenario = scenario }
    func connect(_ data: Data, _ response: ReaderNativeAssistantStream.Response,
                 _ chunk: ReaderNativeAssistantStream.Chunk) async throws {
        requests.append(try JSONSerialization.jsonObject(with: data) as! [String: Any])
        if scenario == "gone" { _ = try await response(410); return }
        if scenario == "forbidden" { _ = try await response(403); return }
        _ = try await response(200)
        if scenario == "cancel" { throw URLError(.networkConnectionLost) }
        if requests.count == 1 {
            if scenario == "unknown-first" { throw URLError(.networkConnectionLost) }
            _ = try await chunk(Data("event: meta\ndata: {}\n\nevent: answer\ndata: \"你好\"\n\nevent: actions\ndata: [{\"id\":\"one\"}]\n\nevent: answer\ndata: \"unfinished".utf8))
            throw URLError(.networkConnectionLost)
        }
        _ = try await chunk(Data("event: meta\ndata: {}\n\nevent: answer\ndata: \"你好！\"\n\nevent: done\ndata: {}\n\n".utf8))
    }
    func deliver(_ batch: [ReaderNativeAssistantEvent]) throws {
        if scenario == "consumer-failure" { throw URLError(.cannotParseResponse) }
        events.append(contentsOf: batch)
    }
    func verify() {
        if scenario == "gone" || scenario == "forbidden" || scenario == "consumer-failure" {
            precondition(requests.count == 1, "terminal failure was retried"); return
        }
        precondition(requests.count == 2)
        precondition(requests[0]["message"] as? String == "test")
        precondition(Set(requests[1].keys) == Set(["rid","from","assistant_mode"]), "initial mutation was replayed")
        precondition(requests[1]["from"] as? Int == (scenario == "unknown-first" ? 0 : 2))
        precondition(events.filter { $0.name == "actions" }.count == (scenario == "unknown-first" ? 0 : 1))
        precondition(events.last?.name == "done")
    }
}

@main struct Test {
    static func main() async throws {
        let wire = Data("\u{feff}: heartbeat\r\nevent: answer\r\ndata: \"日本😀\"\r\n\r\nevent: actions\rdata: {\rdata: \"id\":1}\r\revent: done\ndata: {}\n\n".utf8)
        let expected = [ReaderNativeAssistantEvent(name: "answer", data: "\"日本😀\""),
                        .init(name: "actions", data: "{\n\"id\":1}"), .init(name: "done", data: "{}")]
        for split in 0...wire.count {
            var decoder = ReaderNativeAssistantSSE()
            let first = try decoder.append(Data(wire.prefix(split)))
            let rest = try decoder.append(Data(wire.dropFirst(split)))
            precondition(first + rest == expected, "UTF-8 or line boundary differs at \(split)")
        }
        var bytewise = ReaderNativeAssistantSSE(), all: [ReaderNativeAssistantEvent] = []
        for byte in wire { all += try bytewise.append(Data([byte])) }
        precondition(all == expected)
        var partial = ReaderNativeAssistantSSE()
        let pending = try partial.append(Data("event: actions\ndata: {\"id\":1}".utf8))
        precondition(pending.isEmpty, "incomplete mutation applied")
        var bounded = ReaderNativeAssistantSSE(maximumBytes: 16)
        do { _ = try bounded.append(Data(repeating: 65, count: 17)); preconditionFailure("unbounded event") }
        catch is ReaderNativeAssistantStream.Failure {}
        let initial = Data(#"{"rid":"c1_2","message":"test","context":{"selected":"語"},"assistant_mode":"normal","turn_id":"t"}"#.utf8)
        for scenario in ["resume", "unknown-first", "gone", "forbidden", "consumer-failure"] {
            let harness = Harness(scenario)
            let stream = try ReaderNativeAssistantStream(initial: initial, maximumRetries: 2, retryDelay: {_ in},
                connect: { data, response, chunk in try await harness.connect(data, response, chunk) },
                deliver: { try await harness.deliver($0) })
            do {
                let result = try await stream.run()
                precondition(scenario != "forbidden" && scenario != "consumer-failure")
                precondition(result == (scenario == "gone" ? .gone : .done))
            } catch {
                precondition(scenario == "forbidden" || scenario == "consumer-failure", "unexpected failure \(error)")
            }
            await harness.verify()
        }
        let harness = Harness("cancel")
        let stream = try ReaderNativeAssistantStream(initial: initial,
            retryDelay: {_ in try await Task.sleep(nanoseconds: 10_000_000_000)},
            connect: { data, response, chunk in try await harness.connect(data, response, chunk) }, deliver: {_ in})
        let running = Task { try await stream.run() }
        try await Task.sleep(nanoseconds: 10_000_000); running.cancel()
        do { _ = try await running.value; preconditionFailure("cancellation ignored") } catch is CancellationError {}
        print("Native SSE boundaries, continuation cursor, unknown submission, errors and cancellation passed")
    }
}
