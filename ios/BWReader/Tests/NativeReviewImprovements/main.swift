import Foundation

typealias R = ReaderNativeReviewImprovements
@MainActor final class Fixture {
    var records: [String: String] = [:]
    var calls: [(String, R.Object)] = []
    var failWrite = false, failFetch = false, hold = false
    var held: CheckedContinuation<Void, Never>?
    var reply: R.Object = ["ok": true, "draft_id": "frozen-1", "targets": ["anki", "note"],
        "drafts": ["cards": [["front": "前", "back": "后"]], "note": ["content": "原笔记"]]]
    lazy var service = make()
    func make() -> R {
        R(fetch: { [self] path, data in
            calls.append((path, try JSONSerialization.jsonObject(with: data) as! R.Object))
            let result = reply
            if hold { hold = false; await withCheckedContinuation { held = $0 } }
            if failFetch { throw R.Failure(message: "offline") }
            return .init(status: 200, data: try JSONSerialization.data(withJSONObject: result))
        }, read: { [self] in records[$0] }, write: { [self] key, text in
            if failWrite { throw R.Failure(message: "disk full") }; records[key] = text
        })
    }
    func input(card: String = "card_x_i2") -> R.Object {
        ["lease": UUID().uuidString, "contextKey": "current-book:all", "cardKey": card,
         "card": ["entity_id": "card_x", "entity_index": 2, "card_id": "123", "anki_note_id": 789,
                  "question": "原始题面", "answer": "原始答案", "source_ref": "localbook:原书"],
         "pairs": [["question": "为什么", "answer": "已选第一段"]], "target": "all", "verbosity": "concise"]
    }
    func confirmation(_ input: R.Object, target: String = "anki") -> R.Object {
        var value = input; value["draftId"] = "frozen-1"; value["target"] = target; value["confirmed"] = true
        return value
    }
}

@main struct Tests {
    @MainActor static func main() async throws {
        let f = Fixture(), input = Fixture().input()
        let preview = try await f.service.prepare(input)
        precondition((preview["draft"] as! R.Object)["draft_id"] as? String == "frozen-1")
        let body = f.calls[0].1, card = body["card"] as! R.Object
        precondition(card["id"] as? String == "123" && card["entity_index"] as? Int == 2)
        precondition(card["source_ref"] as? String == "localbook:原书" && body["verbosity"] as? String == "concise")
        precondition((body["pairs"] as! [R.Object])[0]["answer"] as? String == "已选第一段")
        precondition(f.records.isEmpty)
        var invalid = f.confirmation(input); invalid["confirmed"] = false
        do { _ = try await f.service.commit(invalid); preconditionFailure("unconfirmed commit") } catch {}
        invalid = f.confirmation(input); invalid["cardKey"] = "sibling"
        do { _ = try await f.service.commit(invalid); preconditionFailure("stale card commit") } catch {}
        precondition(f.calls.count == 1)
        f.reply = ["ok": true, "summary": "写入完成"]
        let committed = try await f.service.commit(f.confirmation(input))
        precondition(((committed["commits"] as! [String: R.Object])["anki"]!)["ok"] as? Bool == true)
        _ = try await f.service.commit(f.confirmation(input))
        precondition(f.calls.count == 2 && f.calls[1].1.count == 2)
        _ = try await f.service.commit(f.confirmation(input, target: "note"))
        precondition(f.calls.count == 3 && f.records.count == 2)

        let disk = Fixture(), diskInput = Fixture().input()
        _ = try await disk.service.prepare(diskInput); disk.failWrite = true
        do { _ = try await disk.service.commit(disk.confirmation(diskInput)); preconditionFailure("write without reservation") } catch {}
        precondition(disk.calls.count == 1)

        let unknown = Fixture(), unknownInput = Fixture().input()
        _ = try await unknown.service.prepare(unknownInput); unknown.failFetch = true
        let failed = try await unknown.service.commit(unknown.confirmation(unknownInput))
        precondition(((failed["commits"] as! [String: R.Object])["anki"]!)["unknown"] as? Bool == true)
        _ = try await unknown.service.commit(unknown.confirmation(unknownInput))
        precondition(unknown.calls.count == 2)
        unknown.failFetch = false
        let reloaded = unknown.make(), reloadedInput = unknown.input()
        _ = try await reloaded.prepare(reloadedInput)
        let recovered = try await reloaded.commit(unknown.confirmation(reloadedInput))
        precondition(((recovered["commits"] as! [String: R.Object])["anki"]!)["unknown"] as? Bool == true)
        precondition(unknown.calls.count == 3, "restart must not replay unknown mutation")

        let late = Fixture(), lateInput = Fixture().input()
        late.hold = true
        let first = Task { try await late.service.prepare(lateInput) }
        while late.held == nil { await Task.yield() }
        let secondInput = late.input(card: "card_new_i0")
        late.service.invalidate(lateInput["lease"] as? String)
        _ = try await late.service.prepare(secondInput)
        late.service.invalidate(lateInput["lease"] as? String) // late old cancel must not clear new preview
        late.held?.resume(); late.held = nil
        do { _ = try await first.value; preconditionFailure("old preview returned after changing card") } catch {}
        late.reply = ["ok": true]
        _ = try await late.service.commit(late.confirmation(secondInput))
        precondition(late.calls.count == 3)

        let navigation = Fixture(), navigationInput = Fixture().input()
        _ = try await navigation.service.prepare(navigationInput)
        navigation.reply = ["ok": true, "summary": "已在原目标写入"]
        navigation.hold = true
        let pending = Task { try await navigation.service.commit(navigation.confirmation(navigationInput)) }
        while navigation.held == nil { await Task.yield() }
        do { _ = try await navigation.service.commit(navigation.confirmation(navigationInput)); preconditionFailure("concurrent mutation") } catch {}
        navigation.service.invalidate()
        navigation.held?.resume(); navigation.held = nil
        do { _ = try await pending.value; preconditionFailure("old receipt changed new view") } catch {}
        precondition(navigation.records.values.contains { $0.contains("succeeded") }, "navigation lost known receipt")
        precondition(navigation.calls.count == 2)

        let bad = Fixture(); bad.reply["targets"] = ["unsupported"]
        let badResult = try await bad.service.prepare(bad.input())
        precondition((badResult["draft"] as! R.Object)["ok"] as? Bool == false)
        precondition(bad.records.isEmpty)
        print("Native review draft identity, confirmation, cancellation and durable receipts passed")
    }
}
