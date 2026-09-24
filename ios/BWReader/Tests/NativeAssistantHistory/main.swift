import Foundation
typealias H = ReaderNativeAssistantHistory
let good = H.Response(status:200, body:Data(#"{"ok":true,"messages":[{"role":"user","content":"保留原文"}]}"#.utf8))

actor Network {
    var calls: [String] = []
    var pending: CheckedContinuation<H.Response, Error>?
    var waiter: CheckedContinuation<Void, Never>?
    let held: String
    init(held: String = "") { self.held = held }
    func fetch(_ path: String, _ method: String, _ body: Data) async throws -> H.Response {
        calls.append(method + " " + path)
        if path == held {
            return try await withCheckedThrowingContinuation { continuation in
                pending = continuation; waiter?.resume(); waiter = nil
            }
        }
        try await Task.sleep(nanoseconds:20_000_000)
        return good
    }
    func waitForHeld() async { if pending == nil { await withCheckedContinuation { waiter = $0 } } }
    func release() { pending?.resume(returning: good); pending = nil }
    func count() -> Int { calls.count }
}

@main struct Test {
    static func main() async throws {
        let card: [String: Any] = ["gid": "stable-card", "cards": [["front": "Q", "back": "A"]]]
        let rows: [Any] = [
            ["role": "user", "history_id": "hu1", "content": "selected", "figures": [["id": "figure1"]]],
            ["role": "assistant", "turn_id": "t1", "content": "[语气:认真]正文[[FOLLOWUP]]为什么？[[/FOLLOWUP]]", "via": "voice"],
            ["role": "assistant", "history_id": "hp", "parts": [["kind": "card", "data": card]]],
            ["role": "assistant", "history_id": "hc", "card": card],
            NSNull(),
            ["role": "assistant", "turn_id": "invalid/id", "id": "valid-but-not-chosen", "content": "legacy"]
        ]
        let original = try JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys])
        let plans = try JSONSerialization.jsonObject(with: H.presentations(rows, mode: "normal")) as! [Any]
        let user = plans[0] as! [String: Any], answer = plans[1] as! [String: Any]
        precondition(user["turnID"] as? String == "hist_normal_hu1" && user["kind"] as? String == "user")
        precondition(answer["text"] as? String == "正文" && answer["subtitle"] as? Bool == true)
        precondition(answer["followups"] as? [String] == ["为什么？"])
        precondition((plans[2] as? [String: Any])?["kind"] as? String == "parts")
        precondition((plans[3] as? [String: Any])?["kind"] as? String == "card")
        precondition(plans[4] is NSNull && (plans[5] as? [String: Any])?["turnID"] == nil)
        let unchanged = try JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys])
        precondition(original == unchanged, "history source or card identity changed")
        let normal = try H.route("/api/assistant/history", operation:"read",mode:"normal")
        let review = try H.route("/api/assistant/history?assistant_mode=review", operation:"read",mode:"review")
        let clear = try H.route("/api/assistant/clear", operation:"clear",mode:"normal")
        let reviewClear = try H.route("/api/assistant/clear", operation:"clear",mode:"review")
        precondition(review.family != normal.family && reviewClear.family == review.family)
        precondition(String(decoding:reviewClear.body,as:UTF8.self) == #"{"assistant_mode":"review"}"#)
        for path in ["https://example.org/api/assistant/history", "//example.org/api/assistant/history",
                     "/api/assistant/history#x", "/api/assistant/history?file=other", "/api/assistant/history?assistant_mode=review",
                     "/api/assistant/%68istory", "/pdf/api/epub-convo?file=book"] {
            do { _ = try H.route(path,operation:"read",mode:"normal"); preconditionFailure("invalid scope accepted") } catch {}
        }
        do { _ = try H.route("/api/assistant/history?assistant_mode=review&assistant_mode=normal", operation:"read",mode:"review"); preconditionFailure("duplicate scope accepted") } catch {}
        let network = Network()
        let owner = H { path, method, body in try await network.fetch(path,method,body) }
        async let first = owner.read(normal)
        async let second = owner.read(normal)
        let results = try await (first, second)
        let count = await network.count()
        precondition(count == 1 && results.0.body == good.body && results.1.body == good.body)
        precondition(results.0.presentation != nil && results.0.presentation == results.1.presentation)

        // Cancellation-insensitive transport simulates a late URLSession reply.
        let delayed = Network(held:normal.path)
        let fenced = H { path, method, body in try await delayed.fetch(path,method,body) }
        let old = Task { try await fenced.read(normal) }
        await delayed.waitForHeld()
        _ = try await fenced.clear(clear)
        _ = try await fenced.read(review)
        await delayed.release()
        do { _ = try await old.value; preconditionFailure("pre-clear history escaped") } catch {}
        let afterCount = await delayed.count()
        precondition(afterCount == 3)

        let clearing = Network(held:clear.path)
        let serial = H { path, method, body in try await clearing.fetch(path,method,body) }
        let activeClear = Task { try await serial.clear(clear) }
        await clearing.waitForHeld()
        let after = Task { try await serial.read(normal) }
        await clearing.release()
        _ = try await (activeClear.value, after.value)
        let order = await clearing.calls
        precondition(order == ["POST " + clear.path, "GET " + normal.path])

        let malformed = H { _,_,_ in .init(status:200,body:Data(#"{"ok":true}"#.utf8)) }
        do { _ = try await malformed.read(normal); preconditionFailure("malformed response became empty history") } catch {}
        let unauthorized = H { _,_,_ in .init(status:401,body:Data("unauthorized".utf8)) }
        let denied = try await unauthorized.read(normal)
        precondition(denied.status == 401)
        let detachedNetwork = Network(held:normal.path)
        let detached = H { path,method,body in try await detachedNetwork.fetch(path,method,body) }
        let late = Task { try await detached.read(normal) }
        await detachedNetwork.waitForHeld(); await detached.invalidate(); await detachedNetwork.release()
        do { _ = try await late.value; preconditionFailure("old navigation response accepted") } catch {}
        print("Native history: mode isolation, single read, clear ordering, stale response cancellation and failure preservation passed")
    }
}
