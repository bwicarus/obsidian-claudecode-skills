import Foundation

typealias Q = ReaderNativeReviewQueue
@MainActor final class Fixture {
    var cache: String?
    var local: Q.Object = ["hasLocalCards": false, "entries": [], "dueTotal": 0]
    var failLocal = false, failSave = false, failPost = false, failGet = false, hold = false
    var held: CheckedContinuation<Void, Never>?
    var calls: [(String, Q.Object)] = []
    var reply: Q.Object = ["ok": true, "cards": [["id": 7, "question": "問", "answer": "答"]], "due_total": 3]
    var stamp = 10_000_000.0
    lazy var service = Q(local: { [self] in
        if failLocal { throw Q.Failure(message: "repository unavailable") }; return local
    }, read: { [self] in cache }, write: { [self] text in
        if failSave { throw Q.Failure(message: "disk full") }; cache = text
    }, fetch: { [self] path, method, data in
        calls.append((method, data.isEmpty ? [:] : try JSONSerialization.jsonObject(with: data) as! Q.Object))
        let value = reply
        if hold { hold = false; await withCheckedContinuation { held = $0 } }
        if method == "POST" ? failPost : failGet { throw Q.Failure(message: "offline") }
        return .init(status: 200, data: try JSONSerialization.data(withJSONObject: value))
    }, now: { [self] in stamp })
    func input(scope: String = "current", page: Int = 4, force: Bool = true) -> Q.Object {
        ["request": UUID().uuidString, "contextKey": "ctx-book-\(page)", "context": ["file": "localbook:book", "page": page], "scope": scope, "force": force]
    }
}

@main struct Tests {
    @MainActor static func main() async throws {
        let local = Fixture(); local.local = ["hasLocalCards": true, "entries": [], "dueTotal": 0]
        let empty = try await local.service.load(local.input())
        precondition(empty["kind"] as? String == "local" && local.calls.isEmpty)
        local.failLocal = true
        do { _ = try await local.service.load(local.input()); preconditionFailure("local failure fell through") } catch {}
        precondition(local.calls.isEmpty)

        let remote = Fixture()
        remote.reply["related_total"] = 0
        let current = try await remote.service.load(remote.input())
        precondition(((current["snapshot"] as! Q.Object)["cards"] as! [Q.Object]).isEmpty)
        precondition(remote.calls[0].0 == "POST")
        let all = try await remote.service.load(remote.input(scope: "all", force: false))
        precondition(remote.calls.count == 2 && remote.calls[1].0 == "GET")
        precondition(((all["snapshot"] as! Q.Object)["cards"] as! [Q.Object]).count == 1)
        remote.failGet = true
        let cached = try await remote.service.load(remote.input(scope: "all", force: false))
        precondition(cached["kind"] as? String == "cache" && remote.calls.count == 2)

        let fallback = Fixture(); fallback.failPost = true
        let due = try await fallback.service.load(fallback.input())
        precondition(due["kind"] as? String == "fallback" && fallback.calls.map(\.0) == ["POST", "GET"])
        fallback.failGet = true
        let offline = try await fallback.service.load(fallback.input())
        precondition(offline["kind"] as? String == "offline")
        do { _ = try await fallback.service.load(fallback.input(page: 9)); preconditionFailure("other page cache leaked") } catch {}

        let legacy = Fixture()
        var old = legacy.input(force: false)
        old["legacyCache"] = ["client_context_key": "ctx-book-4", "ts": legacy.stamp - 100,
            "cards": [["id": 8, "question": "cached"]], "index": 0, "due_total": 0, "related_total": 0, "completed_ids": [1, 2]]
        old["rejectedIds"] = ["2"]
        let imported = try await legacy.service.load(old)
        let importedSnapshot = imported["snapshot"] as! Q.Object
        precondition(imported["kind"] as? String == "cache" && legacy.calls.isEmpty && legacy.cache != nil)
        precondition((importedSnapshot["completed_ids"] as! [NSNumber]).map(\.intValue) == [1])
        precondition(importedSnapshot["due_total"] as? Int == 0)
        let prior = legacy.cache
        let staleSave = try legacy.service.save(importedSnapshot, request: UUID().uuidString)
        precondition(!staleSave && prior == legacy.cache)

        let disk = Fixture(); disk.failSave = true
        do { _ = try await disk.service.load(disk.input()); preconditionFailure("disk failure hidden") }
        catch { precondition(error.localizedDescription == "disk full") }
        precondition(disk.calls.count == 1 && disk.cache == nil)

        let delayed = Fixture(); delayed.hold = true
        let earlier = delayed.input()
        let loading = Task { try await delayed.service.load(earlier) }
        while delayed.held == nil { await Task.yield() }
        let latest = try await delayed.service.load(delayed.input(page: 5))
        let retained = delayed.cache
        delayed.held?.resume(); delayed.held = nil
        do { _ = try await loading.value; preconditionFailure("stale load published") } catch {}
        precondition(delayed.cache == retained && (latest["snapshot"] as! Q.Object)["client_context_key"] as? String == "ctx-book-5")

        let cancelled = Fixture(); cancelled.hold = true
        let request = cancelled.input()
        let pending = Task { try await cancelled.service.load(request) }
        while cancelled.held == nil { await Task.yield() }
        cancelled.service.cancel(request["request"] as! String)
        cancelled.held?.resume(); cancelled.held = nil
        do { _ = try await pending.value; preconditionFailure("cancelled load published") } catch {}
        precondition(cancelled.cache == nil && cancelled.calls.count == 1)

        let arrived = Fixture(); arrived.hold = true
        let wait = Task { try await arrived.service.load(arrived.input()) }
        while arrived.held == nil { await Task.yield() }
        arrived.local = ["hasLocalCards": true, "entries": [], "dueTotal": 0]
        arrived.held?.resume(); arrived.held = nil
        let replacement = try await wait.value
        precondition(replacement["kind"] as? String == "local" && arrived.cache == nil)
        print("Native review acquisition: local authority, scopes, cache, cancellation and failures passed")
    }
}
