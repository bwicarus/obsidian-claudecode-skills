import Foundation
import CoreFoundation
import CryptoKit

/// Native book mutations, using the existing record/journal schema. The main
/// record, derived indexes and retry receipt either all commit or all roll back.
struct ReaderNativeBookStore {
    enum MutationError: LocalizedError {
        case invalid(String), replayConflict, unavailable
        var errorDescription: String? {
            switch self {
            case .invalid(let detail): return "书籍数据无效：" + detail
            case .replayConflict: return "同一操作编号已用于不同内容，未重复写入"
            case .unavailable: return "原生书籍数据库尚未准备好"
            }
        }
    }
    let store: ReaderNativeDataStore
    let bookID: String
    let deviceID: String
    var now: () -> Int64 = { Int64(Date().timeIntervalSince1970 * 1000) }
    var displayName: String? = nil
    var contentSHA256: String? = nil
    private var projection: ReaderNativeBookProjection { .init(store: store) }

    /// The one-time IndexedDB import runs before this call. Keep the old
    /// highlight array intact, and publish all split records and their marker
    /// together; reopening after a failed transaction simply retries it.
    func prepareHighlightsOnBoot() throws {
        try store.inTransaction {
            for kind in ["document-highlights","epub-highlights"] {
                let meta = try projection.state(kind + "-split-meta",bookID:bookID)
                if meta.revision > 0 { continue }
                let old = try projection.state(kind,bookID:bookID)
                guard let items = old.payload as? [[String:Any]], !items.isEmpty else { continue }
                var seen = Set<String>(), order:[String] = []
                let stamp = now(), mutation = "hl-split:" + kind + ":" + bookID
                for item in items {
                    guard let id = item["id"] as? String, !id.isEmpty, id.utf16.count <= 200,
                          item["deleted"] as? Bool != true, seen.insert(id).inserted else { continue }
                    order.append(id)
                    try write(collection:"native-" + kind + "-items",
                        id:"native-\(kind)-item-v1:\(bookID.utf16.count):\(bookID):\(id)",payload:item,
                        mutation:mutation + ":" + id,at:stamp)
                }
                try writeState(kind + "-split-meta",value:["order":order],expected:0,mutation:mutation + ":meta",at:stamp)
            }
        }
    }

    /// Preserve the existing repair for old inserted/deleted PDF pages. Run
    /// after file recovery, so rollback cannot restore the stale binding.
    func repairBindingsOnBoot() throws {
        try store.inTransaction {
            let current = try projection.state("document-notes-legacy",bookID:bookID)
            guard var notes = current.payload as? [[String:Any]] else { return }
            var changed = false
            for index in notes.indices {
                guard let anchor = notes[index]["anchor"] as? [String:Any], anchor["kind"] as? String == "pdf",
                      let page = anchor["page"] as? NSNumber, CFGetTypeID(page) != CFBooleanGetTypeID(),
                      page.doubleValue.isFinite, page.doubleValue.rounded() == page.doubleValue else { continue }
                for slot in ["card","html"] {
                    guard var payload = notes[index][slot] as? [String:Any], var bind = payload["bind"] as? [String:Any],
                          bind["kind"] as? String == "page-chars" else { continue }
                    let old = (bind["page"] as? NSNumber)?.doubleValue ?? (bind["page"] as? String).flatMap(Double.init)
                    guard let old, old.isFinite, old.rounded() == old, old != page.doubleValue else { continue }
                    bind["page"] = page; payload["bind"] = bind; notes[index][slot] = payload; changed = true
                }
            }
            if changed {
                _ = try writeNotes(notes,expected:current.revision,mutation:"bind-repair-" + String(current.revision),at:now())
            }
        }
    }

    /// Replay a captured local command through the same native transaction.
    /// The outbox ID also fences local effects and replication, so a lost
    /// transport acknowledgement cannot create a second note or replication.
    func applyQueuedCommand(_ command: [String:Any], nativePDF: Bool) throws -> [String:Any] {
        guard let raw = command["url"] as? String, let url = URLComponents(string:raw),
              let method = command["method"] as? String, let mutation = command["mutationId"] as? String,
              mutation.range(of:"^mut-v2-[a-f0-9]{32}$",options:.regularExpression) != nil,
              ["/pdf/api/notes","/pdf/api/highlights","/pdf/api/reading-pos"].contains(url.path) else { throw MutationError.invalid("本地待发命令") }
        var body = command["body"] as? [String:Any] ?? [:]
        let query = url.queryItems ?? []
        if method == "DELETE", !query.isEmpty {
            guard body.isEmpty, query.count == 2, Set(query.map(\.name)) == Set(["file","id"]), query.allSatisfy({ $0.value != nil }) else { throw MutationError.invalid("删除参数") }
            body = Dictionary(uniqueKeysWithValues:query.map { ($0.name,$0.value! as Any) })
        } else if !query.isEmpty { throw MutationError.invalid("本地请求查询参数") }
        guard body["file"] as? String == "localbook:" + bookID else { throw MutationError.invalid("待发命令属于其他书籍") }
        if url.path == "/pdf/api/reading-pos" {
            guard method == "POST" else { throw MutationError.invalid("续读方法") }
            try Self.validatePosition(body,file:"localbook:" + bookID)
        }
        if url.path == "/pdf/api/reading-pos", nativePDF {
            // PDFKit owns this position. Old queued web reports observe it;
            // they must not rewind the page or enqueue a duplicate replica.
            guard let current = try projection.state("reading-position",bookID:bookID).payload as? [String:Any],
                  current["kind"] as? String == "pdf" else { throw MutationError.unavailable }
            return ["ok":true,"pos":current["pos"] ?? 1]
        }
        return try store.inTransaction {
            let operation = url.path == "/pdf/api/notes" ? "note-api" : url.path == "/pdf/api/highlights" ? "highlight-api" : "position-api"
            let receipt = try perform(["bookID":bookID,"operation":operation,"mutationId":mutation,
                "value":["method":method,"body":body]])
            if operation == "note-api" {
                var outgoing = body
                if method == "POST", let note = (receipt["result"] as? [String:Any])?["note"] as? [String:Any] {
                    outgoing = note; outgoing["file"] = "localbook:" + bookID
                }
                _ = try perform(["bookID":bookID,"operation":"replication-enqueue","mutationId":mutation + ":replica",
                    "value":["url":url.path,"method":method,"body":outgoing]])
            }
            return receipt
        }
    }

    func perform(_ request: [String: Any]) throws -> [String: Any] {
        guard !bookID.isEmpty, !deviceID.isEmpty, deviceID.utf16.count <= 240,
              let mutation = request["mutationId"] as? String, !mutation.isEmpty, mutation.utf16.count <= 240,
              let operation = request["operation"] as? String, request["bookID"] as? String == bookID,
              let value = request["value"], JSONSerialization.isValidJSONObject(request) else { throw MutationError.invalid("操作身份") }
        let key = "native-book:\(bookID.utf16.count):\(bookID):\(mutation)"
        let fingerprint = SHA256.hash(data: try Self.bytes(request)).map { String(format: "%02x", $0) }.joined()
        let stamp = now()
        return try store.inTransaction {
            if let previous = try store.mutationResult(mutationId: key) {
                guard let saved = try JSONSerialization.jsonObject(with: Data(previous.utf8)) as? [String: Any],
                      saved["fingerprint"] as? String == fingerprint,
                      var receipt = saved["receipt"] as? [String: Any] else { throw MutationError.replayConflict }
                receipt["replayed"] = true
                return receipt
            }
            let expected: Int64?
            if let raw = request["expectedRevision"] {
                guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
                      number.doubleValue >= 0, number.doubleValue <= 9_007_199_254_740_991 else { throw MutationError.invalid("预期修订号") }
                expected = number.int64Value
            } else {
                guard ["reading-position", "position-api", "pdf-position", "note-api", "note-create", "note-operation", "highlight-api", "highlight-edit", "assistant-actions", "replication-enqueue", "ink-operation", "ink-sync"].contains(operation) else { throw MutationError.invalid("缺少预期修订号") }
                expected = nil
            }
            let revision: Int64
            var result: [String: Any]? = nil
            var bindingChanges: [[String: Any]] = []
            switch operation {
            case "assistant-actions":
                guard let input = value as? [String:Any] else { throw MutationError.invalid("助手操作") }
                let high = try projection.highlights("document-highlights", bookID: bookID)
                let notesState = try projection.state("document-notes-legacy", bookID: bookID)
                let undoState = try projection.state("pdf-assistant-undo", bookID: bookID)
                let receiptState = try projection.state("pdf-assistant-ops", bookID: bookID)
                func list(_ value: Any?) throws -> [[String:Any]] {
                    guard value == nil || value is [[String:Any]] else { throw MutationError.invalid("助手操作记录损坏") }
                    return value as? [[String:Any]] ?? []
                }
                let oldNotes = try list(notesState.payload)
                let changes = try ReaderNativeAssistantEdits.apply(input, file: "localbook:" + bookID,
                    highlights: high.items, highlightRevision: high.revision, notes: oldNotes, noteRevision: notesState.revision,
                    undo: list(undoState.payload), receipts: list(receiptState.payload), now: stamp)
                var highRevision = high.revision, notesRevision = notesState.revision
                if changes.touchedHighlights {
                    highRevision = try writeHighlights("document-highlights", items: changes.highlights, expected: high.revision, mutation: mutation + ":highlights", at: stamp)
                }
                if changes.touchedNotes {
                    notesRevision = try writeNotes(changes.notes, expected: notesState.revision, mutation: mutation + ":notes", at: stamp)
                    let before = Dictionary(Self.wordBindings(oldNotes).map { ($0["cid"] as! String, $0["key"] as! String) }, uniquingKeysWith: { _, last in last })
                    let after = Dictionary(Self.wordBindings(changes.notes).map { ($0["cid"] as! String, $0["key"] as! String) }, uniquingKeysWith: { _, last in last })
                    bindingChanges = Set(before.keys).union(after.keys).sorted().compactMap { id in
                        before[id] == after[id] ? nil : ["cid":id,"before":before[id] ?? "","after":after[id] ?? ""]
                    }
                }
                if changes.touchedUndo { try writeState("pdf-assistant-undo", value: changes.undo, expected: undoState.revision, mutation: mutation + ":undo", at: stamp) }
                if changes.touchedReceipts { try writeState("pdf-assistant-ops", value: changes.receipts, expected: receiptState.revision, mutation: mutation + ":ops", at: stamp) }
                let expectedState = input["expectedState"] as? [String:Any] ?? [:]
                var revisions = expectedState["revisions"] as? [String:Any] ?? expectedState
                revisions["highlights"] = highRevision; revisions["notes"] = notesRevision
                revision = max(highRevision, notesRevision)
                result = ["actions":changes.actions,"revisions":revisions,"replayed":changes.replayed,"receipt":changes.receipt.map { $0 as Any } ?? NSNull()]
            case "pdf-position":
                guard let input = value as? [String:Any] else { throw MutationError.invalid("PDF 位置") }
                let viewport = try ReaderNativeReadingPosition.validated(input)
                let previous = try projection.state("pdf-viewport",bookID:bookID)
                if let payload = previous.payload, try Self.bytes(payload) == Self.bytes(viewport) { revision = previous.revision }
                else { revision = try writeState("pdf-viewport",value:viewport,expected:previous.revision,mutation:mutation + ":viewport",at:stamp) }
                let position = try projection.state("reading-position",bookID:bookID)
                let old = position.payload as? [String:Any], page = viewport["page"] as! Int
                if (old?["pos"] as? NSNumber)?.intValue != page || old?["kind"] as? String != "pdf" {
                    try writeState("reading-position",value:["kind":"pdf","pos":page,"ts":stamp / 1000],expected:position.revision,mutation:mutation + ":position",at:stamp)
                    _ = try enqueueReplication(["url":"/pdf/api/reading-pos","method":"POST",
                        "body":["file":"localbook:" + bookID,"kind":"pdf","pos":page]],mutation:mutation + ":replication",at:stamp)
                }
                result = ["ok":true,"position":viewport]
            case "highlight-api", "highlight-edit":
                guard let input = value as? [String:Any] else { throw MutationError.invalid("划线操作") }
                let outcome = try mutateHighlight(input, edit:operation == "highlight-edit", mutation:mutation, at:stamp)
                revision = outcome.revision; result = outcome.result
            case "ink-operation":
                guard let input = value as? [String: Any] else { throw MutationError.invalid("手写操作") }
                let outcome = try mutateInk(input, mutation: mutation, at: stamp)
                revision = outcome.revision; result = outcome.result
            case "ink-sync":
                revision = try flushInk(mutation: mutation, at: stamp)
                result = ["ok": true]
            case "replication-enqueue":
                guard let command = value as? [String: Any] else { throw MutationError.invalid("复制命令") }
                let queued = try enqueueReplication(command, mutation: mutation, at: stamp)
                revision = queued.revision
                result = ["ok": true, "queued": queued.queued]
            case "note-api", "note-create", "note-operation":
                guard let api = value as? [String: Any] else { throw MutationError.invalid("便签请求") }
                let state = try projection.state("document-notes-legacy", bookID: bookID)
                guard state.payload == nil || state.payload is [[String: Any]] else { throw MutationError.invalid("便签数据损坏") }
                let notes = state.payload as? [[String: Any]] ?? []
                let method: String, body: [String:Any]
                if operation == "note-operation" {
                    guard let id = api["id"] as? String, let note = notes.first(where: { $0["id"] as? String == id }) else {
                        throw ReaderNativeNoteRules.NoteError.missing
                    }
                    let plan = try ReaderNativeNoteActions.request(api,note:note,file:"localbook:" + bookID,now:stamp)
                    method = plan.method; body = plan.body
                } else {
                    guard let verb = api["method"] as? String, let fields = api["body"] as? [String:Any] else { throw MutationError.invalid("便签请求") }
                    method = verb; body = fields
                }
                if operation == "note-create" {
                    guard method == "POST", let id = body["id"] as? String,
                          id.range(of: "^c_[a-f0-9]{32}$", options: .regularExpression) != nil,
                          !notes.contains(where: { $0["id"] as? String == id }) else { throw MutationError.invalid("新便签身份") }
                }
                let outcome = try ReaderNativeNoteRules.apply(method: method, body: body, notes: notes,
                    file: "localbook:" + bookID, now: stamp,
                    newID: { "n" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(11) })
                revision = try writeNotes(outcome.notes, expected: state.revision, mutation: mutation, at: stamp)
                result = outcome.result
                if operation == "note-operation" || operation == "note-create" {
                    _ = try enqueueReplication(["url":"/pdf/api/notes","method":method,"body":body],mutation:mutation + ":replication",at:stamp)
                }
                let before = Dictionary(Self.wordBindings(notes).map { ($0["cid"] as! String, $0["key"] as! String) }, uniquingKeysWith: { _, last in last })
                let after = Dictionary(Self.wordBindings(outcome.notes).map { ($0["cid"] as! String, $0["key"] as! String) }, uniquingKeysWith: { _, last in last })
                bindingChanges = Set(before.keys).union(after.keys).sorted().compactMap { id in
                    guard before[id] != after[id] else { return nil }
                    return ["cid": id, "before": before[id] ?? "", "after": after[id] ?? ""]
                }
            case "notes":
                guard let notes = value as? [[String: Any]] else { throw MutationError.invalid("注解列表") }
                var ids = Set<String>()
                for note in notes {
                    guard let id = note["id"] as? String, !id.isEmpty, id.utf16.count <= 240,
                          ids.insert(id).inserted else { throw MutationError.invalid("注解编号") }
                }
                revision = try writeNotes(notes, expected: expected, mutation: mutation, at: stamp)
            case "pdf-highlights", "epub-highlights":
                guard let items = value as? [[String: Any]] else { throw MutationError.invalid("划线列表") }
                revision = try writeHighlights(operation == "pdf-highlights" ? "document-highlights" : "epub-highlights",
                                               items: items, expected: expected, mutation: mutation, at: stamp)
            case "reading-position":
                guard value is [String: Any] else { throw MutationError.invalid("阅读位置") }
                revision = try writeState(operation, value: value, expected: expected, mutation: mutation, at: stamp)
            case "position-api":
                guard let api = value as? [String:Any], api["method"] as? String == "POST", let body = api["body"] as? [String:Any] else { throw MutationError.invalid("续读位置") }
                try Self.validatePosition(body,file:"localbook:" + bookID)
                let kind = body["kind"] as! String, pos = body["pos"] as! NSNumber
                let previous = try projection.state("reading-position",bookID:bookID)
                revision = try writeState("reading-position",value:["kind":kind,"pos":pos,"ts":stamp / 1000],
                    expected:previous.revision,mutation:mutation + ":position",at:stamp)
                if (previous.payload as? [String:Any])?["pos"] as? NSNumber != pos || (previous.payload as? [String:Any])?["kind"] as? String != kind {
                    _ = try enqueueReplication(["url":"/pdf/api/reading-pos","method":"POST","body":body],mutation:mutation + ":replication",at:stamp)
                }
                result = ["ok":true,"pos":pos]
            case "book-languages":
                guard let languages = value as? [String], languages.count <= 16,
                      languages.allSatisfy({ ["en", "ja", "zh", "ko", "fr", "de"].contains($0) }) else {
                    throw MutationError.invalid("书籍语言")
                }
                var seen = Set<String>()
                let languagesValue = languages.filter { seen.insert($0).inserted }
                revision = try writeState(operation, value: languagesValue, expected: expected, mutation: mutation, at: stamp)
                result = ["ok": true, "langs": languagesValue]
            case "book-crop":
                guard let crop = value as? [String: Any], Set(crop.keys) == Set(["l", "r", "t", "b"]) else {
                    throw MutationError.invalid("书籍裁边")
                }
                var normalized: [String: Double] = [:]
                for key in ["l", "r", "t", "b"] {
                    guard let number = crop[key] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                          number.doubleValue.isFinite, (0...45).contains(number.doubleValue) else { throw MutationError.invalid("裁边比例") }
                    normalized[key] = number.doubleValue
                }
                guard normalized["l"]! + normalized["r"]! < 90, normalized["t"]! + normalized["b"]! < 90 else {
                    throw MutationError.invalid("裁边范围")
                }
                revision = try writeState(operation, value: normalized, expected: expected, mutation: mutation, at: stamp)
                result = ["ok": true, "crop": normalized]
            case "ink", "epub-ink":
                guard value is [String: [[String: Any]]] else { throw MutationError.invalid("墨迹") }
                revision = try writeState(operation, value: value, expected: expected, mutation: mutation, at: stamp)
            default: throw MutationError.invalid("未登记的操作")
            }
            var receipt: [String: Any] = ["ok": true, "bookID": bookID, "mutationId": mutation, "revision": revision]
            if let result { receipt["result"] = result; receipt["bindingChanges"] = bindingChanges }
            try store.rememberMutationWithinTransaction(key, json: Self.string(["fingerprint": fingerprint, "receipt": receipt]), now: stamp)
            return receipt
        }
    }

    private func mutateHighlight(_ input: [String:Any], edit: Bool, mutation: String, at: Int64) throws -> (revision:Int64,result:[String:Any]) {
        typealias Rules = ReaderNativeHighlightRules
        let state = try projection.highlights("document-highlights",bookID:bookID)
        var items = state.items
        var method = input["method"] as? String ?? "", body = input["body"] as? [String:Any] ?? [:]
        var result: [String:Any] = ["ok":true]
        if edit {
            guard let id = input["id"] as? String, let old = items.first(where: { $0["id"] as? String == id }),
                  let action = input["op"] as? String else { throw Rules.HighlightError.missing }
            body = ["file":"localbook:" + bookID,"id":id]
            switch action {
            case "delete": method = "DELETE"
            case "color":
                let key = try Rules.string(input["value"],limit:64)
                guard key.isEmpty || Rules.palette[key] != nil else { throw Rules.HighlightError.invalid("画笔颜色") }
                // Translation/explanation body is also a note. Removing its
                // color must not silently delete the written explanation.
                let hasNote = ["note","body","sentence"].contains { !(old[$0] as? String ?? "").trimmingCharacters(in:.whitespacesAndNewlines).isEmpty }
                method = key.isEmpty && !hasNote ? "DELETE" : "PATCH"
                if method == "PATCH" { body["color"] = Rules.palette[key] ?? "" }
            case "note": method = "PATCH"; body["note"] = try Rules.string(input["value"],limit:2000)
            default: throw Rules.HighlightError.invalid("编辑动作")
            }
        }
        let allowed: Set<String> = method == "POST"
            ? ["file","id","page","rects","color","text","note","kind","sentence","body","page_w","page_h"]
            : (method == "DELETE" ? ["file","id"] : ["file","id","color","text","note","kind","sentence","body"])
        guard ["POST","PATCH","DELETE"].contains(method), Set(body.keys).isSubset(of:allowed),
              body["file"] as? String == "localbook:" + bookID else { throw Rules.HighlightError.invalid("书籍或请求字段") }
        let assistant = input["assistant"] as? Bool == true
        if method == "POST" {
            let highlight = try Rules.create(body,now:at), id = highlight["id"] as! String
            if assistant {
                guard id == body["id"] as? String, id.hasPrefix("c_") else { throw Rules.HighlightError.invalid("助手操作编号") }
                let undoState = try projection.state("pdf-assistant-undo",bookID:bookID)
                let receiptState = try projection.state("pdf-assistant-ops",bookID:bookID)
                guard undoState.payload == nil || undoState.payload is [[String:Any]], receiptState.payload == nil || receiptState.payload is [[String:Any]] else {
                    throw MutationError.invalid("划线撤销或回执记录损坏")
                }
                var undo = undoState.payload as? [[String:Any]] ?? [], receipts = receiptState.payload as? [[String:Any]] ?? []
                let creationID = "direct-highlight:" + id, fingerprint = try Rules.fingerprint(highlight)
                let existing = items.first { $0["id"] as? String == id }
                if let prior = receipts.first(where: { $0["id"] as? String == creationID }) {
                    guard let existing, let priorFingerprint = prior["fingerprint"] as? String,
                          Rules.sameFingerprint(priorFingerprint,fingerprint),
                          try Rules.sameFingerprint(Rules.fingerprint(existing),fingerprint) else { throw Rules.HighlightError.conflict }
                    return (state.revision,["ok":true,"id":id,"highlight":existing,"replayed":true])
                }
                guard existing == nil else { throw Rules.HighlightError.conflict }
                undo.append(["id":creationID,"kind":"highlight-create","targetKind":"document-highlights",
                    "expectedRevision":state.revision + 1,"ids":[id],"ts":Double(at)/1000])
                receipts.append(["id":creationID,"kind":"highlight","fingerprint":fingerprint,"ts":Double(at)/1000])
                try writeState("pdf-assistant-undo",value:Array(undo.suffix(80)),expected:undoState.revision,mutation:mutation + ":undo",at:at)
                try writeState("pdf-assistant-ops",value:Rules.boundedReceipts(receipts),expected:receiptState.revision,mutation:mutation + ":ops",at:at)
            }
            items.removeAll { $0["id"] as? String == id }; items.append(highlight)
            result["id"] = id; result["highlight"] = highlight; result["replayed"] = false
            body = highlight; body.removeValue(forKey:"time"); body["file"] = "localbook:" + bookID
        } else {
            guard let id = body["id"] as? String, let index = items.firstIndex(where: { $0["id"] as? String == id }) else { throw Rules.HighlightError.missing }
            if method == "DELETE" { items.remove(at:index); result["deleted"] = true }
            else { items[index] = try Rules.patch(body,old:items[index]); result["highlight"] = items[index] }
        }
        let revision = try writeHighlights("document-highlights",items:items,expected:state.revision,mutation:mutation,at:at)
        _ = try enqueueReplication(["url":"/pdf/api/highlights","method":method,"body":body],mutation:mutation + ":replication",at:at)
        if edit, let highlight = result["highlight"] as? [String:Any] {
            let color = highlight["color"] as? String ?? ""
            result["color"] = Rules.palette.first { $0.value.lowercased() == color.lowercased() }?.key ?? color
            result["note"] = highlight["note"] ?? ""
        }
        return (revision,result)
    }

    /// Reading the latest strokes and saving undo, ink and the deferred sync
    /// marker are one transaction. Unknown replies are safe to retry by ID.
    private func mutateInk(_ input: [String: Any], mutation: String, at: Int64) throws -> (revision: Int64, result: [String: Any]) {
        guard let action = input["action"] as? String else { throw MutationError.invalid("手写动作") }
        let state = try projection.state("ink", bookID: bookID)
        guard state.payload == nil || state.payload is NSNull || state.payload is [String: [[String: Any]]] else { throw MutationError.invalid("笔迹记录损坏") }
        let original = state.payload as? [String: [[String: Any]]] ?? [:]
        var output = original, before: [String: [[String: Any]]] = [:]
        var written = 0, removed = 0
        if ["undo", "redo", "clear"].contains(action) {
            guard let page = input["page"] as? NSNumber, CFGetTypeID(page) != CFBooleanGetTypeID(),
                  page.doubleValue > 0, page.doubleValue < 10_000_000,
                  page.doubleValue.rounded() == page.doubleValue else { throw MutationError.invalid("手写页码") }
            let key = String(page.intValue), current = original[key] ?? []
            var history = try inkHistory(page: key, matching: current)
            var undo = history["undo"] as? [[[String: Any]]] ?? [], redo = history["redo"] as? [[[String: Any]]] ?? []
            let next: [[String: Any]]
            if action == "undo" {
                guard let value = undo.popLast() else { return (state.revision, ["ok":true,"persisted":true,"changes":[]]) }
                redo.append(current); next = value
            } else if action == "redo" {
                guard let value = redo.popLast() else { return (state.revision, ["ok":true,"persisted":true,"changes":[]]) }
                undo.append(current); next = value
            } else { undo.append(current); redo = []; next = [] }
            before[key] = current
            if next.isEmpty { output.removeValue(forKey:key) } else { output[key] = next }
            history = ["undo":undo,"redo":redo,"current":next]
            try writeInkHistory(history, page:key, mutation:mutation, at:at)
        } else {
            let outcome = try ReaderNativeStrokeRules.apply(action:action, input:input, surfaces:original, now:at)
            output = outcome.surfaces; before = outcome.before; written = outcome.written; removed = outcome.removed
            for page in before.keys.sorted() {
                let prior = before[page]!, next = output[page] ?? []
                if try Self.bytes(prior) == Self.bytes(next) { continue }
                var history = try inkHistory(page:page, matching:prior)
                var undo = history["undo"] as? [[[String: Any]]] ?? []
                undo.append(prior); history = ["undo":undo,"redo":[[[String:Any]]](),"current":next]
                try writeInkHistory(history, page:page, mutation:mutation, at:at)
            }
        }
        let changes: [[String:Any]] = try before.keys.sorted().compactMap { page in
            let next = output[page] ?? []
            return try Self.bytes(before[page]!) == Self.bytes(next) ? nil : ["page":Int(page)!,"strokes":next]
        }
        if !changes.isEmpty {
            var pending = try pendingInk()
            for change in changes { pending[String(change["page"] as! Int)] = at + 60_000 }
            try writeState("ink-pending", value:pending, mutation:mutation + ":pending", at:at)
        }
        let revision = changes.isEmpty ? state.revision : try writeState("ink", value:output, expected:state.revision, mutation:mutation, at:at)
        return (revision, ["ok":true,"persisted":true,"written":written,"removed":removed,"changes":changes])
    }

    private func inkHistory(page: String, matching strokes: [[String:Any]]) throws -> [String:Any] {
        let value = try projection.state("ink-history-" + page, bookID:bookID).payload as? [String:Any]
        // Cloud imports and page mutations may replace strokes; do not let an
        // old local undo stack overwrite those changes.
        guard let value, let current = value["current"] as? [[String:Any]],
              try Self.bytes(current) == Self.bytes(strokes) else { return [:] }
        return value
    }

    private func writeInkHistory(_ input: [String:Any], page:String, mutation:String, at:Int64) throws {
        var value = input
        var undo = Array((value["undo"] as? [[[String:Any]]] ?? []).suffix(40))
        var redo = Array((value["redo"] as? [[[String:Any]]] ?? []).suffix(40))
        while true {
            value["undo"] = undo; value["redo"] = redo
            if try Self.bytes(value).count <= 8 * 1024 * 1024 || (undo.isEmpty && redo.isEmpty) { break }
            if undo.count >= redo.count && !undo.isEmpty { undo.removeFirst() } else { redo.removeFirst() }
        }
        try writeState("ink-history-" + page, value:value, mutation:mutation + ":history:" + page, at:at)
    }

    private func pendingInk() throws -> [String:Int64] {
        guard let value = try projection.state("ink-pending", bookID:bookID).payload else { return [:] }
        guard let rows = value as? [String:NSNumber] else { throw MutationError.invalid("笔迹待同步记录损坏") }
        var result: [String:Int64] = [:]
        for (key, n) in rows {
            guard key.range(of:"^[1-9][0-9]{0,7}$",options:.regularExpression) != nil,
                  CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite,
                  n.doubleValue >= 0, n.doubleValue <= 9_007_199_254_740_991,
                  n.doubleValue.rounded() == n.doubleValue else { throw MutationError.invalid("笔迹待同步时间") }
            result[key] = n.int64Value
        }
        return result
    }

    func nextInkSyncTime() throws -> Int64? { try pendingInk().values.min() }

    private func flushInk(mutation:String, at:Int64) throws -> Int64 {
        var pending = try pendingInk()
        let due = pending.keys.filter { pending[$0]! <= at }.sorted()
        guard !due.isEmpty else { return 0 }
        let state = try projection.state("ink", bookID:bookID)
        guard state.payload == nil || state.payload is NSNull || state.payload is [String:[[String:Any]]] else { throw MutationError.invalid("笔迹记录损坏") }
        let strokes = state.payload as? [String:[[String:Any]]] ?? [:]
        for page in due {
            _ = try enqueueReplication(["url":"/pdf/api/ink","method":"POST","body":["file":"localbook:" + bookID,
                "page":Int(page)!,"strokes":strokes[page] ?? []]], mutation:mutation + ":ink:" + page, at:at)
            pending.removeValue(forKey:page)
        }
        return try writeState("ink-pending",value:pending,mutation:mutation + ":settled",at:at)
    }

    /// 阅读停留（活动账本原始层）：原生 PDF 的停留秒数进同一条持久复制出站队列。
    /// 形状与网页端 30-dwell.js → reportActivity 完全一致（kind/file/client/entries/loc/at），
    /// 服务器 `_apply_activity` 只认这六个键。原生 PDF 上网页那套数不到页（没有 DOM 页），
    /// 974 起停留与定位一直是空的 —— 这里是它的原生替身。
    func recordDwell(_ entries: [(page: Int, seconds: Int)], location: [String: Any]?, at: Int64) throws {
        let rows = entries.filter { $0.page > 0 && $0.seconds > 0 }.prefix(200)
            .map { ["page": $0.page, "secs": min(86_400, $0.seconds)] as [String: Any] }
        guard !rows.isEmpty else { return }
        var body: [String: Any] = ["kind": "dwell", "file": "localbook:" + bookID, "client": "native",
                                   "entries": Array(rows), "at": at / 1000]
        if let location { body["loc"] = location }
        _ = try store.inTransaction {
            try enqueueReplication(["url": "/replication/activity", "method": "POST", "body": body],
                                   mutation: "native-dwell-" + Self.uuid(), at: at)
        }
    }

    /// The durable outbox uses the existing wire protocol. Creating the book
    /// link and its pair announcement is atomic with the first command, so a
    /// crash cannot leave an unannounced identity or an unrepeatable command.
    private func enqueueReplication(_ command: [String: Any], mutation: String, at: Int64) throws -> (revision: Int64, queued: Bool) {
        let routes: [String: Set<String>] = [
            "/pdf/api/notes": ["POST", "PATCH", "DELETE"],
            "/pdf/api/highlights": ["POST", "PATCH", "DELETE"],
            "/pdf/api/epub-highlights": ["POST", "PATCH", "DELETE"],
            "/pdf/api/userpages": ["POST", "PATCH", "DELETE"],
            "/pdf/api/ink": ["POST"], "/pdf/api/epub-ink": ["POST"],
            "/pdf/api/reading-pos": ["POST"], "/replication/activity": ["POST"],
            "/replication/diagnostic": ["POST"], "/replication/resync": ["POST"],
            "/replication/notification": ["POST"]
        ]
        guard Set(command.keys) == Set(["url", "method", "body"]),
              let url = command["url"] as? String, let method = command["method"] as? String,
              routes[url]?.contains(method) == true, let body = command["body"] as? [String: Any] else {
            throw MutationError.invalid("复制路由或参数")
        }
        if let file = body["file"], file as? String != "localbook:" + bookID { throw MutationError.invalid("复制书籍不匹配") }
        guard bookID != "localbook-welcome" else { return (0, false) }
        let existing = try projection.state("replication-link", bookID: bookID)
        let replicationID: String
        let needsPair: Bool
        if let payload = existing.payload {
            guard let link = payload as? [String: Any], let id = link["replicationBookId"] as? String,
                  id.range(of: "^repbook-[a-f0-9]{32}$", options: .regularExpression) != nil else {
                throw MutationError.invalid("复制配对记录损坏")
            }
            replicationID = id; needsPair = false
        } else { replicationID = "repbook-" + Self.uuid(); needsPair = true }
        func envelope(_ path: String, _ verb: String, _ payload: [String: Any]) -> [String: Any] {
            ["contract": "replication-command/1", "deviceId": deviceID,
             "replicationBookId": replicationID, "actor": "user",
             "op": ["mutationId": "mut-v2-" + Self.uuid(), "url": path, "method": verb, "body": payload]]
        }
        let message = envelope(url, method, body)
        guard try Self.bytes(message).count <= 5 * 1024 * 1024 else { throw MutationError.invalid("复制命令超过信封上限") }
        func append(_ message: [String: Any], suffix: String) throws -> Int64 {
            // Persistent counter is scoped to this database and never resets on
            // reload. Native IDs cannot collide with the legacy JS counter.
            let previous = Int64(try store.meta("nativeReplicationSequence") ?? "0") ?? 0
            guard previous >= 0 && previous < Int64.max, at >= 0 else { throw MutationError.invalid("复制队列序号") }
            let sequence = previous + 1
            try store.putMeta("nativeReplicationSequence", json: String(sequence))
            let rowID = bookID + ":ro-" + String(format: "%012llx-n%016llx", at, sequence)
            return try write(collection: "native-replication-outbox", id: rowID,
                payload: ["envelope": message], expected: 0, mutation: mutation + suffix, at: at)
        }
        if needsPair {
            try writeState("replication-link", value: ["replicationBookId": replicationID, "pairedAt": at / 1000],
                           expected: existing.revision, mutation: mutation + ":link", at: at)
            var pair: [String: Any] = ["peerBookId": bookID, "replicationBookId": replicationID,
                                      "displayName": String((displayName.flatMap { $0.isEmpty ? nil : $0 } ?? bookID).prefix(512))]
            if let sha = contentSHA256, sha.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil { pair["contentSha256"] = sha }
            _ = try append(envelope("/replication/pair", "POST", pair), suffix: ":pair")
        }
        return (try append(message, suffix: ":command"), true)
    }

    private static func validatePosition(_ body:[String:Any],file:String) throws {
        guard Set(body.keys) == Set(["file","kind","pos"]), body["file"] as? String == file,
              let kind = body["kind"] as? String, ["pdf","epub"].contains(kind), let pos = body["pos"] as? NSNumber,
              CFGetTypeID(pos) != CFBooleanGetTypeID(), pos.doubleValue.isFinite,
              (0...10_000_000).contains(pos.doubleValue), pos.doubleValue.rounded() == pos.doubleValue else { throw MutationError.invalid("续读位置") }
    }

    private static func uuid() -> String { UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased() }

    func writeNotes(_ notes: [[String: Any]], expected: Int64?, mutation: String, at: Int64) throws -> Int64 {
        let revision = try writeState("document-notes-legacy", value: notes, expected: expected, mutation: mutation + ":notes", at: at)
        let placements = Self.placements(notes)
        try writeState("card-placements", value: placements, mutation: mutation + ":placements", at: at)
        try writeState("entity-references", value: Self.references(placements), mutation: mutation + ":references", at: at)
        try writeState("word-bindings", value: Self.wordBindings(notes), mutation: mutation + ":words", at: at)
        return revision
    }

    @discardableResult
    func writeState(_ kind: String, value: Any, expected: Int64? = nil, mutation: String, at: Int64) throws -> Int64 {
        try write(collection: "native-" + kind, id: bookID + ":" + kind, payload: value,
                  expected: expected, mutation: mutation, at: at)
    }
    @discardableResult
    private func write(collection: String, id: String, payload: Any, expected: Int64? = nil, mutation: String, at: Int64) throws -> Int64 {
        let current = try store.record(collection: collection, id: id), revision = current?.rev ?? 0
        if let expected, expected != revision { throw ReaderNativeDataStore.StoreError.revisionConflict(collection: collection, id: id, actual: revision) }
        guard revision >= 0 && revision < 9_007_199_254_740_991 else { throw MutationError.invalid("修订号溢出") }
        let record: [String: Any] = ["schema": 1, "collection": collection, "id": id, "rev": revision+1,
            "updatedAt": at, "updatedBy": deviceID, "deleted": false,
            "value": ["id": id, "documentId": bookID, "payload": payload, "updatedAt": at]]
        // Journal mutation IDs are database-wide, whereas callers reuse IDs
        // within a book. Scope the key to book and record so another book or a
        // delimiter inside an item ID cannot alias this committed write.
        let mutationKey = try Self.bytes([bookID, collection, id, mutation])
        let mutationID = "native-" + SHA256.hash(data: mutationKey).map { String(format: "%02x", $0) }.joined()
        let json = try Self.string(record)
        let change: [String: Any] = ["mutationId": mutationID, "operation": "put", "collection": collection, "record": record]
        _ = try Self.bytes(change)
        try store.commitWithinTransaction(record: .init(collection: collection, id: id, rev: revision+1,
            updatedAt: at, deleted: false, json: json), mutationId: mutationID,
            journalJSON: { cursor in
                var envelope = change; envelope["cursor"] = cursor
                return try! Self.string(envelope) // All constituent values were validated above.
            }, expectedRev: revision, now: at)
        return revision+1
    }

    func writeHighlights(_ kind: String, items: [[String: Any]], expected: Int64?, mutation: String, at: Int64) throws -> Int64 {
        let current = try projection.highlights(kind, bookID: bookID)
        let meta = try projection.state(kind + "-split-meta", bookID: bookID)
        if let expected, current.revision != expected {
            throw ReaderNativeDataStore.StoreError.revisionConflict(collection: "native-" + kind, id: bookID, actual: current.revision)
        }
        // The existing resumable importer owns conversion of non-empty legacy
        // arrays; a normal write may not reset their collection revision.
        guard meta.payload != nil || current.items.isEmpty else { throw MutationError.unavailable }
        var prior: [String: [String: Any]] = [:], seen = Set<String>(), order: [String] = []
        for item in current.items {
            guard let id = item["id"] as? String, !id.isEmpty, prior[id] == nil else { throw MutationError.invalid("原划线编号") }
            prior[id] = item
        }
        func save(_ item: [String: Any], id: String) throws {
            try write(collection: "native-" + kind + "-items", id: "native-\(kind)-item-v1:\(bookID.utf16.count):\(bookID):\(id)",
                      payload: item, mutation: mutation + ":" + id, at: at)
        }
        for item in items {
            guard let id = item["id"] as? String, !id.isEmpty, id.utf16.count <= 200,
                  seen.insert(id).inserted, item["deleted"] as? Bool != true else { throw MutationError.invalid("划线编号重复或无效") }
            order.append(id)
            if let old = prior[id], try Self.bytes(old) == Self.bytes(item) { continue }
            try save(item, id: id)
        }
        for id in prior.keys.sorted() where !seen.contains(id) { try save(["id": id, "deleted": true, "time": at / 1000], id: id) }
        return try writeState(kind + "-split-meta", value: ["order": order], expected: meta.revision, mutation: mutation + ":meta", at: at)
    }

    static func placements(_ notes: [[String: Any]]) -> [[String: Any]] {
        notes.compactMap { note in
            guard let id = note["id"] as? String, !id.isEmpty,
                  note["card"] is [String: Any] || note["html"] is [String: Any] || note["video"] is [String: Any] else { return nil }
            let kind = note["card"] is [String: Any] ? "card" : (note["html"] is [String: Any] ? "html" : "video")
            var entityIDs: [String] = []
            for (field, keys) in [("card", ["gid", "cid", "id"]), ("html", ["cid", "id"]), ("video", ["id"])] {
                for key in keys {
                    let value = ((note[field] as? [String: Any])?[key] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    if !value.isEmpty && value.utf16.count <= 240 && !entityIDs.contains(value) { entityIDs.append(value) }
                }
            }
            func dimension(_ key: String) -> Any {
                guard let n = note[key] as? NSNumber, n.doubleValue.isFinite else { return NSNull() }; return n
            }
            return ["placementId": id, "noteId": id, "kind": kind, "anchor": note["anchor"] ?? NSNull(),
                    "w": dimension("w"), "h": dimension("h"), "collapsed": note["collapsed"] as? Bool ?? false, "entityIds": entityIDs]
        }
    }
    static func references(_ placements: [[String: Any]]) -> [[String: Any]] {
        var grouped: [String: [String: Any]] = [:]
        for placement in placements {
            for id in placement["entityIds"] as? [String] ?? [] {
                var value = grouped[id] ?? ["entityId": id, "kind": placement["kind"] ?? "", "placementIds": [String]()]
                var ids = value["placementIds"] as? [String] ?? []
                if let placementID = placement["placementId"] as? String, !ids.contains(placementID) { ids.append(placementID) }
                value["placementIds"] = ids.sorted(); grouped[id] = value
            }
        }
        return grouped.keys.sorted().compactMap { grouped[$0] }
    }
    static func wordBindings(_ notes: [[String: Any]]) -> [[String: Any]] {
        notes.enumerated().compactMap { index, note in
            guard let html = note["html"] as? [String: Any], let binding = html["bind"] as? [String: Any],
                  binding["kind"] as? String == "page-chars", let text = binding["text"] as? String,
                  let cid = html["cid"] as? String, !cid.isEmpty else { return nil }
            let key = text.filter { !$0.isWhitespace }.lowercased()
            guard !key.isEmpty, key.utf16.count <= 64 else { return nil }
            return ["cid": cid, "noteId": note["id"] ?? "", "key": key, "text": String(text.prefix(64)),
                    "page": (binding["page"] as? NSNumber)?.intValue ?? 0, "label": String((html["label"] as? String ?? "").prefix(120)),
                    "at": note["created"] as? NSNumber ?? 0, "order": index]
        }
    }
    private static func bytes(_ value: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed, .withoutEscapingSlashes])
    }
    private static func string(_ value: Any) throws -> String { String(decoding: try bytes(value), as: UTF8.self) }
}
