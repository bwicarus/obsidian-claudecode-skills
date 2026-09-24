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
    static func turnState() throws {
        var turn = ReaderNativeAssistantTurn()
        func event(_ name: String, _ value: Any) throws -> ReaderNativeAssistantEvent {
            .init(name: name, data: String(decoding: try JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed]), as: UTF8.self))
        }
        _ = try turn.consume(event("answer", "第一个增量"))
        let tool = try turn.consume(event("tool2", ["name": "lookup"]))
        precondition(tool["sawTool"] as? Bool == true && tool["answer"] == nil)
        let answer = try turn.consume(event("answer", "[语气:认真]完整回答[[FOLLOWUP]]为什么[[/FOLLOWUP]]"))
        precondition(answer["displayText"] as? String == "完整回答" && turn.answer.hasPrefix("[语气:"))
        precondition(answer["voiceText"] as? String == "[语气:认真]完整回答")
        _ = try turn.consume(event("tool2", ["name": "do_task", "task_id": "task-one"]))
        precondition(turn.sawTool && turn.sawCLICard)
        let done = try turn.consume(event("done", [:]))
        precondition(done["done"] as? Bool == true && done["answer"] == nil)
        do { _ = try turn.consume(event("answer", "late")); preconditionFailure("post-completion event accepted") }
        catch is ReaderNativeAssistantStream.Failure {}
        var invalid = ReaderNativeAssistantTurn()
        do { _ = try invalid.consume(event("answer", ["wrong": "type"])); preconditionFailure("non-text answer accepted") }
        catch is ReaderNativeAssistantStream.Failure {}
        let failed = try invalid.consume(event("error", "连接失败"))
        precondition(failed["answer"] as? String == "⚠️ 连接失败")
        precondition(turn.completion(aborted:false)["target"] as? String == "task")
        let stopped = turn.completion(aborted:true)
        precondition((stopped["followups"] as? [String])?.isEmpty == true && stopped["statusText"] as? String == "已停止")
        var restored = ReaderNativeAssistantTurn()
        try restored.restore(["content":"[语气:认真]恢复正文[[FOLLOWUP]]下一步[[/FOLLOWUP]]","ts":123,"trace":[["model":"test"]]])
        let final = restored.completion(aborted:false)
        precondition(final["displayText"] as? String == "恢复正文" && final["recoveredAt"] as? Double == 123)
        precondition(final["followups"] as? [String] == ["下一步"] && final["target"] as? String == "answer")
        do { try restored.restore(["content":"别的轮次"]); preconditionFailure("overwrote a live/recovered answer") }
        catch is ReaderNativeAssistantStream.Failure {}
        let textOnly = [try event("answer", "文字增量"), try event("tool2", ["name": "lookup"]), try event("done", [:])]
        let fastBatch = try ReaderNativeAssistantEventBatch(textOnly)
        precondition(fastBatch.actions.isEmpty, "non-mutation events crossed the document commit adapter")
        let fastResult = try fastBatch.committed([])
        precondition(fastResult == textOnly, "native text order or bytes changed")
        let firstAction = try event("actions", [["fn": "first"]]), secondAction = try event("actions", [["fn": "second"]])
        let mixed = try ReaderNativeAssistantEventBatch([textOnly[0], firstAction, textOnly[1], secondAction, textOnly[2]])
        precondition(mixed.actions == [firstAction, secondAction])
        let firstReceipt = try event("actions", [["fn": "first-committed"]]), secondReceipt = try event("actions", [["fn": "second-committed"]])
        let mixedResult = try mixed.committed([firstReceipt, secondReceipt])
        precondition(mixedResult == [textOnly[0], firstReceipt, textOnly[1], secondReceipt, textOnly[2]], "action receipts changed the stream cursor positions")
        do { _ = try mixed.committed([firstReceipt]); preconditionFailure("missing receipt exposed completion") }
        catch is ReaderNativeAssistantStream.Failure {}
        do { _ = try mixed.committed([firstReceipt, textOnly[0]]); preconditionFailure("non-action receipt accepted") }
        catch is ReaderNativeAssistantStream.Failure {}
        do { _ = try ReaderNativeAssistantEventBatch([firstAction, event("actions", "not an array")]); preconditionFailure("malformed batch started a partial mutation") }
        catch is ReaderNativeAssistantStream.Failure {}
        if CommandLine.arguments.count > 1 {
            let fixtures = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [[String: Any]]
            for fixture in fixtures {
                let answer = fixture["answer"] as! String, result = ReaderNativeAssistantTurn.content(answer)
                precondition(result.voiceText == fixture["voiceText"] as? String, "voice differs: \(answer)")
                precondition(result.displayText == fixture["displayText"] as? String, "display differs: \(answer)")
                precondition(result.finalDisplayText == fixture["finalDisplayText"] as? String, "final differs: \(answer)")
                precondition(result.followups == fixture["followups"] as? [String], "followups differ: \(answer)")
            }
        }
    }
    static func requestPolicy() throws {
        func plan(_ context: [String: Any], message: String = "", mode: String = "normal", noBook: Bool = false) throws -> [String: Any] {
            try ReaderNativeAssistantRequest(["message": message, "context": context, "assistant_mode": mode,
                                               "no_book": noBook, "voice": 1], identity: { "fixed-turn" }).body
        }
        let book: [String: Any] = ["page": 12, "current_section_idx": 4, "section": "chapter", "selection_sentence": "ambient",
                                   "selection_anchor": ["block": 5], "visible_text": "whole page", "selection": "selected phrase",
                                   "figures": [["kind": "figure", "id": "fig1"]], "pinned_context": [["id": "card1"]]]
        let withoutBook = try plan(book, message: "  explain  ", noBook: true)
        let frozen = withoutBook["context"] as! [String: Any]
        precondition(withoutBook["message"] as? String == "explain")
        precondition(withoutBook["rid"] as? String == "cfixed-turn" && withoutBook["turn_id"] as? String == "tfixed-turn")
        precondition(frozen["page"] as? Int == 0 && frozen["no_book"] as? Bool == true)
        for field in ["current_section_idx", "section", "selection_sentence", "selection_anchor", "visible_text"] {
            precondition(frozen[field] == nil, "implicit book context leaked: \(field)")
        }
        precondition(frozen["selection"] as? String == "selected phrase")
        precondition((frozen["figures"] as? [Any])?.count == 1 && (frozen["pinned_context"] as? [Any])?.count == 1)
        precondition(book["page"] as? Int == 12 && book["visible_text"] as? String == "whole page", "source context mutated")
        let cases: [([String: Any], String)] = [
            (["figures": [["kind": "note"]], "selection": "x"], "讲讲这个便签"),
            (["figures": [["kind": "note"], ["kind": "figure"]]], "讲讲这张图"),
            (["notes": [["id": "n"]]], "讲讲这个便签"),
            (["focus_sel": ["kind": "formula", "text": "x=1"]], "讲讲这个公式"),
            (["focus_sel": ["kind": "text", "text": "paragraph"]], "讲讲这段"),
            (["selection": "phrase"], "讲讲这段")
        ]
        for (context, expected) in cases {
            let result = try plan(context)
            precondition(result["message"] as? String == expected)
        }
        let review = try plan([:], message: "考考我", mode: "review")
        precondition(review["voice"] == nil && withoutBook["voice"] as? Int == 1)
        let emptyContexts: [[String: Any]] = [[:], ["selection": "  \n"], ["visible_text": "only implicit book text"]]
        for context in emptyContexts {
            do { _ = try plan(context); preconditionFailure("empty explicit request accepted") }
            catch is ReaderNativeAssistantRequest.Failure {}
        }
        do { _ = try plan([:], message: String(repeating: "a", count: 32001)); preconditionFailure("oversized message accepted") }
        catch is ReaderNativeAssistantRequest.Failure {}
        let first = try ReaderNativeAssistantRequest(["message": "same", "context": [:], "assistant_mode": "normal"])
        let second = try ReaderNativeAssistantRequest(["message": "same", "context": [:], "assistant_mode": "normal"])
        precondition(first.body["rid"] as? String != second.body["rid"] as? String, "distinct user turns share identity")
    }
    static func main() async throws {
        try requestPolicy()
        try turnState()
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
