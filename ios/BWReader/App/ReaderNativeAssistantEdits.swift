import Foundation
import CoreFoundation
import CryptoKit

/// Applies the existing assistant edit protocol to an immutable book snapshot.
/// BookStore owns the transaction; no action is exposed before all records,
/// indexes and undo receipts commit. Page-card entity sagas use their own owner.
enum ReaderNativeAssistantEdits {
    typealias Object = [String: Any]
    struct Failure: LocalizedError {
        let message: String
        let conflict: Bool
        init(_ message: String, conflict: Bool = false) { self.message = message; self.conflict = conflict }
        var errorDescription: String? { message }
    }
    struct Outcome {
        var highlights: [Object], notes: [Object], undo: [Object], receipts: [Object]
        var touchedHighlights = false, touchedNotes = false, touchedUndo = false, touchedReceipts = false
        var actions: [Any] = []
        var replayed = false
        var receipt: Object?
    }
    private static let refresh: Object = ["fn": "_nativePDFRefreshAnnotations", "args": [Any]()]

    static func apply(_ input: Object, file: String, highlights: [Object], highlightRevision: Int64,
                      notes: [Object], noteRevision: Int64, undo: [Object], receipts: [Object], now: Int64) throws -> Outcome {
        guard let actions = input["actions"] as? [Any], actions.count <= 1000,
              JSONSerialization.isValidJSONObject(actions) else { throw Failure("PDF 助手动作列表无效") }
        let expected = input["expectedState"] as? Object ?? [:]
        let revisions = expected["revisions"] as? Object ?? expected
        var out = Outcome(highlights: highlights, notes: notes, undo: undo, receipts: receipts)
        var received: [String: Object] = [:]
        for receipt in receipts { if let id = receipt["id"] as? String { received[id] = receipt } }
        func assertRevision(_ kind: String) throws {
            let current = kind == "highlights" ? highlightRevision : noteRevision
            guard let value = integer(revisions[kind]), value == current,
                  current < 9_007_199_254_740_991 else { throw Failure("本机书籍已变化，未应用旧操作", conflict: true) }
        }
        for raw in actions {
            guard let action = raw as? Object, let descriptor = descriptor(action) else { out.actions.append(raw); continue }
            let (kind, data) = descriptor
            guard !kind.hasPrefix("page-card") else { throw Failure("页面卡片操作必须由实体事务处理") }
            let args = action["args"] as? [Any] ?? []
            let operationID = (kind == "undo" ? args.first as? String : data["native_operation_id"] as? String) ?? ""
            guard operationID.range(of: "^npdf_[0-9a-f]{24}$", options: .regularExpression) != nil else {
                throw Failure("PDF 助手本机动作缺少可信操作编号")
            }
            let fingerprint = SHA256.hash(data: try bytes(action)).map { String(format: "%02x", $0) }.joined()
            if let previous = received[operationID] {
                // Older receipts did not store a fingerprint; their original
                // operation ID still prevents replay of already committed work.
                if let saved = previous["fingerprint"] as? String, saved != fingerprint {
                    throw Failure("同一操作编号对应不同的助手改动", conflict: true)
                }
                out.replayed = true; out.receipt = previous; out.actions.append(refresh); continue
            }
            var undone: Object?
            switch kind {
            case "highlight":
                try assertRevision("highlights")
                let values = try items(data).map { try highlight($0, now: now) }
                try uniqueNewIDs(values, existing: out.highlights)
                out.highlights += values; out.touchedHighlights = true
                out.undo.append(["id": operationID, "kind": "highlight-create", "targetKind": "document-highlights",
                    "expectedRevision": highlightRevision + 1, "ids": values.map { $0["id"]! }, "ts": now / 1000])
                out.touchedUndo = true
            case "note-create":
                try assertRevision("notes")
                let values = try items(data).map { item -> Object in
                    guard let value = item["note"] as? Object else { throw Failure("便签快照无效") }
                    return try note(value, fallbackID: item["id"], file: file, now: now)
                }
                try uniqueNewIDs(values, existing: out.notes)
                out.notes += values; out.touchedNotes = true
                out.undo.append(["id": operationID, "kind": "note-create", "targetKind": "document-notes-legacy",
                    "expectedRevision": noteRevision + 1, "ids": values.map { $0["id"]! }, "ts": now / 1000])
                out.touchedUndo = true
            case "note-edit":
                try assertRevision("notes")
                var saved: [Object] = []
                for item in try items(data) {
                    let id = try recordID(item["id"]), old = try fields(item["old"]), new = try fields(item["new"])
                    guard let index = out.notes.firstIndex(where: { $0["id"] as? String == id }), equalFields(out.notes[index], old) else {
                        throw Failure("本机便签已变化，未覆盖新内容", conflict: true)
                    }
                    out.notes[index]["text"] = new["text"]; out.notes[index]["color"] = new["color"]
                    out.notes[index]["updated"] = now / 1000
                    saved.append(["id": id, "old": old, "current": new])
                }
                out.touchedNotes = true; out.touchedUndo = true
                out.undo.append(["id": operationID, "kind": "note-edit", "items": saved,
                    "targetKind": "document-notes-legacy", "expectedRevision": noteRevision + 1, "ts": now / 1000])
            case "undo":
                guard let last = out.undo.last, let target = last["targetKind"] as? String,
                      ["document-highlights", "document-notes-legacy"].contains(target) else { throw Failure("没有可安全撤销的 PDF 本机改动") }
                let current = target == "document-highlights" ? highlightRevision : noteRevision
                if let expected = last["expectedRevision"], integer(expected) != current {
                    throw Failure("最近的本机改动已变化，未撤销", conflict: true)
                }
                switch last["kind"] as? String {
                case "highlight-create", "note-create":
                    let isHighlight = last["kind"] as? String == "highlight-create"
                    guard target == (isHighlight ? "document-highlights" : "document-notes-legacy"),
                          let ids = last["ids"] as? [String], !ids.isEmpty, Set(ids).count == ids.count else { throw Failure("撤销记录不完整") }
                    try assertRevision(isHighlight ? "highlights" : "notes")
                    let wanted = Set(ids), values = isHighlight ? out.highlights : out.notes
                    guard values.filter({ wanted.contains($0["id"] as? String ?? "") }).count == wanted.count else {
                        throw Failure("撤销对象已变化", conflict: true)
                    }
                    let remaining = values.filter { !wanted.contains($0["id"] as? String ?? "") }
                    if isHighlight { out.highlights = remaining; out.touchedHighlights = true }
                    else { out.notes = remaining; out.touchedNotes = true }
                case "note-edit":
                    guard target == "document-notes-legacy", let edits = last["items"] as? [Object], !edits.isEmpty else { throw Failure("撤销记录不完整") }
                    try assertRevision("notes")
                    // Restore in reverse order when one batch edited the same note twice.
                    for item in edits.reversed() {
                        let id = try recordID(item["id"]), current = try fields(item["current"]), old = try fields(item["old"])
                        guard let index = out.notes.firstIndex(where: { $0["id"] as? String == id }), equalFields(out.notes[index], current) else {
                            throw Failure("便签已变化，未撤销旧编辑", conflict: true)
                        }
                        out.notes[index]["text"] = old["text"]; out.notes[index]["color"] = old["color"]
                        out.notes[index]["updated"] = now / 1000
                    }
                    out.touchedNotes = true
                default: throw Failure("最近的本机改动无法安全撤销")
                }
                out.undo.removeLast(); out.touchedUndo = true; undone = last
                if let index = out.undo.lastIndex(where: { $0["targetKind"] as? String == target }) { out.undo[index]["expectedRevision"] = current + 1 }
            default: throw Failure("未知助手改动")
            }
            var receipt: Object = ["id": operationID, "kind": kind, "ts": now / 1000, "fingerprint": fingerprint]
            if let undone {
                receipt["undone"] = ["id": undone["id"] ?? "", "kind": undone["kind"] ?? ""]
                receipt["remaining"] = out.undo.count
            }
            out.receipts.append(receipt); received[operationID] = receipt; out.receipt = receipt; out.touchedReceipts = true
            if kind == "undo" { out.actions.append(refresh) }
            else {
                var safe = action, value = data, safeArgs = args
                value["file"] = file; safeArgs[0] = value; safe["args"] = safeArgs; out.actions.append(safe)
            }
        }
        out.undo = Array(out.undo.suffix(80))
        out.receipts = try ReaderNativeHighlightRules.boundedReceipts(out.receipts)
        return out
    }

    static func descriptor(_ action: Object) -> (String, Object)? {
        if action["fn"] as? String == "_nativePDFUndoLast" { return ("undo", [:]) }
        guard action["fn"] as? String == "_assistEdit", let args = action["args"] as? [Any], let data = args.first as? Object else { return nil }
        if data["type"] as? String == "highlight" { return ("highlight", data) }
        let type = data["type"] as? String ?? "", op = data["op"] as? String ?? ""
        if type == "note", ["create", "edit"].contains(op) { return ("note-" + op, data) }
        if type == "page-card", ["edit", "delete"].contains(op) { return ("page-card-" + op, data) }
        return nil
    }
    private static func items(_ data: Object) throws -> [Object] {
        guard let values = data["items"] as? [Object], !values.isEmpty, values.count <= 2000 else { throw Failure("助手改动没有有效项目") }
        return values
    }
    private static func uniqueNewIDs(_ values: [Object], existing: [Object]) throws {
        var ids = Set(existing.compactMap { $0["id"] as? String })
        guard values.allSatisfy({ ids.insert($0["id"] as? String ?? "").inserted }) else { throw Failure("本机对象编号冲突", conflict: true) }
    }
    private static func recordID(_ raw: Any?) throws -> String {
        guard let id = raw as? String, id.range(of: "^[A-Za-z0-9_-]{2,96}$", options: .regularExpression) != nil else { throw Failure("本机对象编号无效") }
        return id
    }
    private static func integer(_ raw: Any?) -> Int64? {
        let value: Double?
        if let n = raw as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() { value = n.doubleValue }
        else if let s = raw as? String { value = Double(s) } else { value = nil }
        guard let value, value.isFinite, value >= 0, value <= 9_007_199_254_740_991, value.rounded() == value else { return nil }
        return Int64(value)
    }
    private static func fields(_ raw: Any?) throws -> Object {
        guard let value = raw as? Object, Set(value.keys).isSubset(of: ["text", "color"]) else { throw Failure("便签修改快照无效") }
        let color = try ReaderNativeHighlightRules.string(value["color"], limit: 64, fallback: "#ffffff").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !color.isEmpty else { throw Failure("便签颜色无效") }
        return ["text": try ReaderNativeHighlightRules.string(value["text"], limit: 8000), "color": color]
    }
    private static func equalFields(_ note: Object, _ fields: Object) -> Bool {
        (note["text"] as? String ?? "") == (fields["text"] as? String ?? "") &&
        ((note["color"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "#ffffff") == (fields["color"] as? String ?? "#ffffff")
    }
    private static func highlight(_ input: Object, now: Int64) throws -> Object {
        var body = input
        if let page = input["pdf_page"], !(page is NSNull) { body["page"] = page }
        let id = try recordID(input["id"])
        var result = try ReaderNativeHighlightRules.create(body, now: now)
        result["id"] = id; result["time"] = integer(input["time"]) ?? now / 1000
        return result
    }
    static func note(_ input: Object, fallbackID: Any?, file: String, now: Int64) throws -> Object {
        let id = try recordID(input["id"] ?? fallbackID)
        var body = input.filter { ["anchor", "text", "color", "w", "h", "collapsed", "strokes", "video", "card", "html", "iar"].contains($0.key) }
        body["file"] = file
        var result = try ReaderNativeNoteRules.apply(method: "POST", body: body, notes: [], file: file, now: now, newID: { id }).notes[0]
        result["created"] = integer(input["created"]) ?? now / 1000
        result["updated"] = integer(input["updated"]) ?? now / 1000
        return result
    }
    private static func bytes(_ value: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes])
    }
}
