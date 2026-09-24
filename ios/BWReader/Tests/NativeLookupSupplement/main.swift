import Foundation

actor StreamHarness {
    let scenario: String
    var sends = 0
    var polls = 0
    init(_ scenario: String) { self.scenario = scenario }
    func connect(_ response: ReaderNativeAssistantStream.Response, _ chunk: ReaderNativeAssistantStream.Chunk) async throws {
        sends += 1
        if scenario == "cancel" { throw CancellationError() }
        if scenario == "denied" { _ = try await response(403); return }
        _ = try await response(200)
        if scenario == "json" { _ = try await chunk(Data(#"{"ok":true,"explanation":"中文解释"}"#.utf8)); return }
        if scenario == "server-error" { _ = try await chunk(Data("event: error\ndata: {\"error\":\"quota\"}\n\n".utf8)); return }
        let wire = "data: {\"text\":\"中文\"}\r\n\r\n" + (scenario == "done" ? "event: done\ndata: {}\n\n" : "")
        for byte in wire.utf8 { let keepGoing = try await chunk(Data([byte])); if !keepGoing { return } }
        throw URLError(.networkConnectionLost)
    }
    func poll() -> Data {
        polls += 1
        if scenario == "lost" { return Data(#"{"status":"unknown"}"#.utf8) }
        if scenario == "timeout" { return Data(#"{"status":"running","full":"中文"}"#.utf8) }
        return Data(#"{"status":"done","full":"中文解释"}"#.utf8)
    }
    func counts() -> (Int, Int) { (sends, polls) }
}

@main struct Test {
    static func main() async throws {
        let get = try ReaderNativeLookupStream.Plan(mode: "jp-ai", text: "a+b", context: "x+y")
        precondition(get.method == "GET" && get.path.contains("a%2Bb") && get.path.contains("x%2By"))
        let post = try ReaderNativeLookupStream.Plan(mode: "explain", text: "at", context: "look at this")
        let body = try JSONSerialization.jsonObject(with: post.body) as! [String: String]
        precondition(post.text == "look at this" && body["text"] == post.text && post.resultPath.hasSuffix(body["rid"]!))
        for scenario in ["done", "disconnect", "json", "cancel", "denied", "server-error", "lost", "timeout"] {
            let harness = StreamHarness(scenario)
            let stream = ReaderNativeLookupStream(maximumPolls: 5, pause: {}, connect: {
                try await harness.connect($0, $1)
            }, poll: { await harness.poll() })
            do {
                let text = try await stream.run()
                precondition(["done", "disconnect", "json"].contains(scenario), "failure presented as success")
                precondition(text == (scenario == "done" ? "中文" : "中文解释"))
            } catch {
                precondition(!["done", "disconnect", "json"].contains(scenario), "success failed: \(error)")
            }
            let (sends, polls) = await harness.counts()
            precondition(sends == 1, "AI generation resubmitted")
            if ["done", "json", "cancel", "denied", "server-error"].contains(scenario) { precondition(polls == 0) }
            if scenario == "disconnect" { precondition(polls == 1) }
            if scenario == "lost" { precondition(polls == 4) }
            if scenario == "timeout" { precondition(polls == 5) }
            do { _ = try await stream.run(); preconditionFailure("same stream restarted") } catch {}
        }
        try await mutations()
        print("Native lookup supplement: one submission, UTF-8/SSE, resume polling, cancellation, errors and durable Anki receipts passed")
    }

    @MainActor static func mutations() async throws {
        var records: [String: String] = [:]
        var sends = 0
        let operation = UUID().uuidString
        let good = ReaderNativeLookupMutation(read: { records[$0] }, write: { records[$0] = $1 }, send: { body in
            sends += 1
            precondition(!records.isEmpty, "sent before durable reservation")
            let sent = try JSONSerialization.jsonObject(with: body) as! [String: String]
            precondition(sent["word"] == "語")
            return .init(status: 200, data: Data(#"{"ok":true,"action":"updated","note_id":12}"#.utf8))
        })
        let result = try await good.run(word: "語", operation: operation)
        precondition(result["action"] as? String == "updated")
        _ = try await good.run(word: "語", operation: operation)
        precondition(sends == 1, "confirmed operation resent")
        do { _ = try await good.run(word: "別", operation: operation); preconditionFailure("operation target changed") } catch {}
        let unknownID = UUID().uuidString
        let unknown = ReaderNativeLookupMutation(read: { records[$0] }, write: { records[$0] = $1 }, send: { _ in
            sends += 1; throw URLError(.networkConnectionLost)
        })
        do { _ = try await unknown.run(word: "語", operation: unknownID); preconditionFailure("unknown success") } catch {}
        let restored = ReaderNativeLookupMutation(read: { records[$0] }, write: { records[$0] = $1 }, send: { _ in
            preconditionFailure("unknown mutation retried after restore")
        })
        do { _ = try await restored.run(word: "語", operation: unknownID); preconditionFailure("pending success") } catch {}
        precondition(sends == 2)
        let brokenDisk = ReaderNativeLookupMutation(read: { _ in nil }, write: { _, _ in throw URLError(.cannotWriteToFile) }, send: { _ in
            preconditionFailure("sent after failed local reservation")
        })
        do { _ = try await brokenDisk.run(word: "語", operation: UUID().uuidString); preconditionFailure("write error ignored") } catch {}
    }
}
