import Foundation

/// Owns prepare -> frozen preview -> explicit commit. The compatibility review
/// adapter only publishes this owner's results; it never repeats its requests.
@MainActor
final class ReaderNativeReviewImprovements {
    typealias Object = [String: Any]
    struct Response { let status: Int; let data: Data }
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    typealias Fetch = (String, Data) async throws -> Response
    private let fetch: Fetch
    private let read: (String) throws -> String?
    private let write: (String, String) throws -> Void
    private var lease = ""
    private var context = ""
    private var cardKey = ""
    private var draft: Object?
    private var commits: [String: Object] = [:]
    private var preparing: Task<Response, Error>?
    private var committing = false

    init(fetch: @escaping Fetch, read: @escaping (String) throws -> String?,
         write: @escaping (String, String) throws -> Void) {
        self.fetch = fetch; self.read = read; self.write = write
    }

    func invalidate(_ expected: String? = nil) {
        guard expected == nil || equal(expected ?? "", lease) else { return }
        preparing?.cancel(); preparing = nil
        lease = ""; context = ""; cardKey = ""; draft = nil; commits = [:]
        // An already dispatched commit must finish its durable receipt, even
        // when the user changes books. Cancellation cannot undo a remote write.
    }

    private func equal(_ a: String, _ b: String) -> Bool { a.utf16.elementsEqual(b.utf16) }
    private func required(_ value: Any?, _ name: String, max: Int = 4096) throws -> String {
        guard let value = value as? String, !value.isEmpty, value.utf8.count <= max else {
            throw Failure(message: "复习改进缺少有效的 " + name)
        }
        return value
    }
    private func bytes(_ value: Object) throws -> Data {
        guard JSONSerialization.isValidJSONObject(value) else { throw Failure(message: "草稿数据无效") }
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        guard data.count <= 8 * 1024 * 1024 else { throw Failure(message: "草稿数据过大") }
        return data
    }
    private func object(_ response: Response) throws -> Object {
        guard response.data.count <= 8 * 1024 * 1024,
              let value = try JSONSerialization.jsonObject(with: response.data) as? Object else {
            throw Failure(message: "草稿服务返回无效数据")
        }
        return value
    }
    private func matches(_ input: Object) -> Bool {
        equal(input["lease"] as? String ?? "", lease) && !lease.isEmpty &&
        equal(input["contextKey"] as? String ?? "", context) &&
        equal(input["cardKey"] as? String ?? "", cardKey)
    }
    private func projection() -> Object { ["draft": draft as Any? ?? NSNull(), "commits": commits] }

    func prepare(_ input: Object) async throws -> Object {
        guard !committing else { throw Failure(message: "上一次草稿写入尚未确认，请稍候") }
        let id = try required(input["lease"], "轮次", max: 80)
        guard UUID(uuidString: id) != nil else { throw Failure(message: "草稿轮次无效") }
        let nextContext = try required(input["contextKey"], "上下文")
        let nextCard = try required(input["cardKey"], "卡片编号")
        guard let card = input["card"] as? Object,
              let pairs = input["pairs"] as? [Object], !pairs.isEmpty, pairs.count <= 200,
              let target = input["target"] as? String, ["anki", "note", "all"].contains(target),
              let verbosity = input["verbosity"] as? String, ["concise", "verbose"].contains(verbosity) else {
            throw Failure(message: "请选用当前复习回答，并指定草稿目标")
        }
        let entity = try required(card["entity_id"], "知识实体")
        var semantic: Object = ["entity_id": entity, "entity_index": card["entity_index"] ?? NSNull()]
        for key in ["local_id", "source_ref", "source_url", "deck"] { semantic[key] = card[key] as? String ?? "" }
        semantic["id"] = card["card_id"] ?? NSNull()
        semantic["anki_note_id"] = card["anki_note_id"] ?? NSNull()
        semantic["front"] = card["question"] as? String ?? card["front"] as? String ?? ""
        semantic["back"] = card["answer"] as? String ?? card["back"] as? String ?? ""
        let selected: [Object] = try pairs.map {
            guard let question = $0["question"] as? String, let answer = $0["answer"] as? String,
                  !answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw Failure(message: "所选回答不完整")
            }
            return ["question": question, "answer": answer]
        }
        let payload = try bytes(["entity_id": entity, "entity_index": semantic["entity_index"]!,
            "card": semantic, "pairs": selected, "target": target, "verbosity": verbosity])
        invalidate(); lease = id; context = nextContext; cardKey = nextCard
        draft = ["ok": false, "busy": true, "error": "正在生成草稿…", "_card_key": cardKey]
        let work = Task { [fetch] in try await fetch("/api/assistant/card-improvement-draft", payload) }
        preparing = work
        defer { if matches(input) { preparing = nil } }
        do {
            let response = try await work.value
            try Task.checkCancellation()
            guard matches(input) else { throw CancellationError() }
            var value = try object(response)
            guard (200..<300).contains(response.status), value["ok"] as? Bool == true else {
                throw Failure(message: value["error"] as? String ?? "草稿生成失败")
            }
            _ = try required(value["draft_id"], "已冻结的草稿编号", max: 1024)
            guard let targets = value["targets"] as? [String], !targets.isEmpty,
                  Set(targets).count == targets.count,
                  targets.allSatisfy({ ["anki", "note"].contains($0) && (target == "all" || target == $0) }),
                  value["drafts"] is Object else { throw Failure(message: "草稿缺少预览或写入目标") }
            value["busy"] = false; value["_card_key"] = cardKey; draft = value
        } catch {
            guard matches(input), !Task.isCancelled else { throw CancellationError() }
            draft = ["ok": false, "busy": false, "error": error.localizedDescription, "_card_key": cardKey]
        }
        return projection()
    }

    private func receiptKey(_ id: String, _ target: String) -> String {
        "native-review-improvement:" + Data(id.utf8).base64EncodedString() + ":" + target
    }
    private func save(_ value: Object, key: String) throws {
        try write(key, String(decoding: bytes(value), as: UTF8.self))
    }

    func commit(_ input: Object) async throws -> Object {
        guard matches(input), let draft, draft["ok"] as? Bool == true,
              let id = draft["draft_id"] as? String, equal(input["draftId"] as? String ?? "", id),
              input["confirmed"] as? Bool == true,
              let target = input["target"] as? String, ["anki", "note"].contains(target),
              (draft["targets"] as? [String] ?? []).contains(target) else {
            throw Failure(message: "请确认当前卡片、草稿和写入目标")
        }
        guard !committing else { throw Failure(message: "草稿正在写入，请勿重复提交") }
        let key = receiptKey(id, target)
        if let text = try read(key) {
            guard text.utf8.count <= 8 * 1024 * 1024,
                  let stored = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? Object,
                  stored["draft_id"] as? String == id, stored["target"] as? String == target else {
                throw Failure(message: "草稿提交记录损坏，未重新发送")
            }
            if stored["status"] as? String == "succeeded" {
                commits[target] = ["busy": false, "ok": true, "message": stored["message"] ?? "已确认写入"]
            } else {
                commits[target] = ["busy": false, "ok": false, "unknown": true,
                    "message": "上次写入结果待确认，未重复发送。请先核对目标中的实际内容。"]
            }
            return projection()
        }
        let payload = try bytes(["draft_id": id, "target": target])
        // If this durable reservation fails, no network mutation is sent.
        try save(["draft_id": id, "target": target, "status": "pending"], key: key)
        committing = true
        commits[target] = ["busy": true, "ok": false, "message": "正在提交…"]
        defer { committing = false }
        var result: Object
        do {
            let response = try await fetch("/api/assistant/card-improvement-commit", payload)
            let value = try object(response)
            guard (200..<300).contains(response.status), value["ok"] as? Bool == true else {
                throw Failure(message: value["error"] as? String ?? "写入未获确认")
            }
            let summary = value["summary"] as? String ?? value["message"] as? String ?? "执行完成"
            try save(["draft_id": id, "target": target, "status": "succeeded", "message": summary], key: key)
            result = ["busy": false, "ok": true, "message": summary]
        } catch {
            // The reservation remains across reload/termination. Never replay
            // an uncertain write just because the visible draft changed.
            result = ["busy": false, "ok": false, "unknown": true,
                "message": "写入未获确认，未自动重试：" + error.localizedDescription]
        }
        guard matches(input) else { throw CancellationError() }
        commits[target] = result
        return projection()
    }
}
