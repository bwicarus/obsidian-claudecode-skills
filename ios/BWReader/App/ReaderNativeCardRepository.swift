import Foundation
import CryptoKit

/// The App's card command owner. A command reads, validates and commits the
/// entity/state pair, causal parents, journal and retry receipt in one SQLite
/// transaction. Browser clients continue using their existing repository.
struct ReaderNativeCardRepository {
    typealias R = ReaderNativeCardRules
    let store: ReaderNativeDataStore
    let deviceID: String
    var now: () -> Int64 = { Int64(Date().timeIntervalSince1970 * 1000) }
    static let entities = "card-entities", states = "card-states"

    private struct Pair {
        var entity: ReaderNativeDataStore.Record?
        var state: ReaderNativeDataStore.Record?
    }
    private func pair(_ id: String) throws -> Pair {
        try Pair(entity: store.record(collection: Self.entities, id: id), state: store.record(collection: Self.states, id: id))
    }
    private func record(_ row: ReaderNativeDataStore.Record?) throws -> [String: Any]? {
        guard let row else { return nil }
        guard let value = try JSONSerialization.jsonObject(with: Data(row.json.utf8)) as? [String: Any],
              value["id"] as? String == row.id, value["collection"] as? String == row.collection,
              (value["rev"] as? NSNumber)?.int64Value == row.rev, value["deleted"] as? Bool == row.deleted else {
            throw R.fail("CORRUPT", "卡组记录信封损坏")
        }
        return value
    }
    private func value(_ row: ReaderNativeDataStore.Record?) throws -> [String: Any]? {
        guard let data = try record(row) else { return nil }
        return try R.object(data["value"], "record.value", code: "CORRUPT")
    }
    private func validatePair(_ pair: Pair, id: String) throws {
        guard (pair.entity == nil) == (pair.state == nil) else { throw R.fail("PARTIAL", "卡组内容与状态不完整") }
        if pair.entity?.deleted == true || pair.state?.deleted == true { throw R.fail("TOMBSTONED", "卡组已删除，不能隐式复活：" + id) }
    }
    private func project(_ pair: Pair, includeDeleted: Bool) throws -> [String: Any]? {
        if pair.entity == nil && pair.state == nil { return nil }
        guard let entity = pair.entity, let state = pair.state, entity.id == state.id, entity.deleted == state.deleted else {
            throw R.fail("PARTIAL", "卡组 collection 身份或墓碑不一致")
        }
        let id = try R.id(entity.id)
        var result: [String: Any] = ["contract": "card-repository/1", "id": id, "cid": id, "gid": id,
            "deleted": entity.deleted, "entityRev": entity.rev, "stateRev": state.rev]
        if entity.deleted { return includeDeleted ? result : nil }
        guard let entityValue = try value(entity), let stateValue = try value(state) else { throw R.fail("PARTIAL", "卡组内容与状态不完整") }
        for (item, contract) in [(entityValue, "card-entity/1"), (stateValue, "card-state/1")] {
            guard item["contract"] as? String == contract, (item["schema"] as? NSNumber)?.intValue == 1,
                  try R.identity(item, generate: false) == id else { throw R.fail("CORRUPT", "卡组 schema 或身份损坏") }
        }
        let normalized = try R.entity(id: id, cards: entityValue["cards"] as Any,
            source: entityValue["source"] as Any, created: entityValue["createdAt"] as Any, updated: entityValue["contentUpdatedAt"] as Any)
        let cards = normalized["cards"] as! [[String: Any]]
        for key in ["cards", "source", "createdAt", "contentUpdatedAt"] { result[key] = normalized[key] }
        result["states"] = try R.states(stateValue["states"], count: cards.count)
        return result
    }
    func load(_ id: String, includeDeleted: Bool = false) throws -> [String: Any]? {
        try project(pair(R.id(id)), includeDeleted: includeDeleted)
    }
    func snapshot(includeDeleted: Bool = false) throws -> [[String: Any]] {
        let entityRows = try store.records(collection: Self.entities, idPrefix: "")
        let stateRows = try store.records(collection: Self.states, idPrefix: "")
        let entities = Dictionary(uniqueKeysWithValues: entityRows.map { ($0.id, $0) })
        let states = Dictionary(uniqueKeysWithValues: stateRows.map { ($0.id, $0) })
        return try Set(entities.keys).union(states.keys).sorted().compactMap { id in
            try project(Pair(entity: entities[id], state: states[id]), includeDeleted: includeDeleted)
        }
    }
    /// Read a coherent, bounded review batch directly from the canonical store.
    /// The web compatibility observer receives only selected cards, never the
    /// entire collection. Ordering matches rc-review: overdue first, then new.
    func reviewQueue(limit: Int = 30) throws -> [String: Any] {
        guard (1...200).contains(limit) else { throw R.fail("INPUT", "复习批次大小无效") }
        return try store.inTransaction {
            let stamp = Double(now())
            var available = 0
            var due: [(order: Double, id: String, index: Int, value: [String: Any])] = []
            var fresh: [(order: Double, id: String, index: Int, value: [String: Any])] = []
            for record in try snapshot() {
                guard record["deleted"] as? Bool != true,
                      let cards = record["cards"] as? [[String: Any]],
                      let states = record["states"] as? [String: Any], let id = record["id"] as? String else { continue }
                let identity = record.filter { ["id", "source", "entityRev", "stateRev"].contains($0.key) }
                for (index, card) in cards.enumerated() {
                    guard let state = states[String(index)] as? [String: Any], state["phase"] as? String == "confirmed",
                          state["removed"] as? Bool != true, (state["flags"] as? [String: Any])?["archived"] as? Bool != true else { continue }
                    available += 1
                    let review = state["review"] as? [String: Any] ?? [:]
                    let status = (review["status"] as? String ?? "new").lowercased()
                    if ["unavailable", "suspended", "buried"].contains(status) { continue }
                    let isNew = status == "new"
                    let order = (try? R.number(isNew ? state["confirmedAt"] : review["dueAt"], "review order")) ?? 0
                    if !isNew && order > stamp { continue }
                    let entry: [String: Any] = ["record": identity, "card": card, "state": state, "cardIndex": index, "due": !isNew]
                    if isNew { fresh.append((order, id, index, entry)) }
                    else { due.append((order, id, index, entry)) }
                }
            }
            func sorted(_ items: [(order: Double, id: String, index: Int, value: [String: Any])]) -> [[String: Any]] {
                items.sorted { a, b in
                    if a.order != b.order { return a.order < b.order }
                    if a.id != b.id { return a.id < b.id }
                    return a.index < b.index
                }.map(\.value)
            }
            return ["hasLocalCards": available > 0, "dueTotal": due.count,
                    "entries": Array((sorted(due) + sorted(fresh)).prefix(limit))]
        }
    }
    func perform(_ request: [String: Any]) throws -> [String: Any] {
        guard let operation = request["operation"] as? String, let args = request["arguments"] as? [Any],
              !deviceID.isEmpty, deviceID.utf16.count <= 240, !deviceID.contains("\0") else { throw R.fail("INPUT", "原生卡仓请求无效") }
        func arg(_ index: Int) -> Any? { args.indices.contains(index) ? args[index] : nil }
        // reviewQueue owns its read transaction; do not nest SQLite BEGINs.
        if operation == "reviewQueue" {
            let options = arg(0) as? [String: Any] ?? [:]
            try R.fields(options, ["limit"], "review queue")
            let limit = try R.integer(options["limit"] ?? 30, "review limit")
            guard (1...200).contains(limit) else { throw R.fail("INPUT", "复习批次大小无效") }
            return ["ok": true, "result": try reviewQueue(limit: Int(limit)), "changes": []]
        }
        return try store.inTransaction {
            if operation == "load" {
                let options = arg(1) as? [String: Any] ?? [:]
                return ["ok": true, "result": try load(R.id(arg(0)), includeDeleted: options["includeDeleted"] as? Bool == true) as Any? ?? NSNull(), "changes": []]
            }
            if operation == "snapshot" {
                let options = arg(0) as? [String: Any] ?? [:]
                return ["ok": true, "result": try snapshot(includeDeleted: options["includeDeleted"] as? Bool == true), "changes": []]
            }
            guard let mutation = request["mutationId"] as? String, !mutation.isEmpty,
                  mutation.utf8.count <= 470, !mutation.contains("\0") else { throw R.fail("MUTATION", "mutationId 无效") }
            let key = "native-card-command:" + mutation
            let fingerprint = SHA256.hash(data: try R.bytes(["operation": operation, "arguments": args])).map { String(format: "%02x", $0) }.joined()
            if let remembered = try store.mutationResult(mutationId: key) {
                let saved = try R.object(JSONSerialization.jsonObject(with: Data(remembered.utf8)), "mutation receipt")
                guard saved["fingerprint"] as? String == fingerprint, let result = saved["result"] else { throw R.fail("MUTATION_REUSED", "mutationId 已被不同内容使用") }
                return ["ok": true, "result": result, "changes": [], "replayed": true]
            }
            let cursor = try store.cursor(), stamp = now()
            let result = try execute(operation, args: args, mutation: mutation, at: stamp)
            try store.rememberMutationWithinTransaction(key,
                json: String(decoding: R.bytes(["fingerprint": fingerprint, "result": result]), as: UTF8.self), now: stamp)
            let changes = try store.journal(after: cursor, limit: 1024).map { try JSONSerialization.jsonObject(with: Data($0.json.utf8)) }
            return ["ok": true, "result": result, "changes": changes]
        }
    }
    private func expected(_ row: ReaderNativeDataStore.Record?, _ supplied: Any?, label: String) throws {
        guard R.has(supplied) else { return }
        let requested = try R.integer(supplied, label), actual = row?.rev ?? 0
        guard requested == actual else { throw R.fail("CONFLICT", "\(label) 与当前版本不一致（当前 \(actual)，你给的 \(requested)）") }
    }
    private func execute(_ operation: String, args: [Any], mutation: String, at: Int64) throws -> Any {
        func arg(_ index: Int) -> Any? { args.indices.contains(index) ? args[index] : nil }
        if operation == "interact" {
            return try interact(R.object(arg(0), "card interaction"), mutation: mutation, at: at)
        }
        if operation == "commitReview" {
            return try commitReview(R.object(arg(0), "review command"), mutation: mutation, at: at)
        }
        if operation == "adoptReviewSchedule" {
            return try adoptReviewSchedule(R.object(arg(0), "review schedule"), mutation: mutation, at: at)
        }
        if operation == "importLegacyBatch" {
            return try importLegacy(arg(0), options: arg(1) as? [String: Any] ?? [:], mutation: mutation, at: at)
        }
        let creates = ["registerDraft", "saveConfirmedCard"].contains(operation)
        let input = creates ? try R.object(arg(0), "card input") : [:]
        let id = creates ? try R.identity(input, generate: true) : try R.id(arg(0))
        let rows = try pair(id)
        var optionIndex = 2
        if creates || operation == "tombstone" { optionIndex = 1 }
        if operation == "patchState" { optionIndex = 3 }
        if operation == "recordAnkiReceipt" { optionIndex = 4 }
        let options = arg(optionIndex) as? [String: Any] ?? [:]
        if operation == "tombstone" {
            guard rows.entity != nil || rows.state != nil else { throw R.fail("NOT_FOUND", "卡组不存在") }
            if rows.entity?.deleted == true, rows.state?.deleted == true { return try project(rows, includeDeleted: true)! }
            try validatePair(rows, id: id)
            try expected(rows.entity, options["ifEntityRev"], label: "ifEntityRev")
            try expected(rows.state, options["ifStateRev"], label: "ifStateRev")
            try write(Self.entities, id: id, value: value(rows.entity) ?? [:], previous: rows.entity, deleted: true, mutation: mutation + ":entity", at: at)
            try write(Self.states, id: id, value: value(rows.state) ?? [:], previous: rows.state, deleted: true, mutation: mutation + ":state", at: at)
            return try load(id, includeDeleted: true)!
        }
        try validatePair(rows, id: id)
        let previous = try project(rows, includeDeleted: false)
        guard creates || previous != nil else { throw R.fail("NOT_FOUND", "卡组不存在") }
        let oldEntity = try value(rows.entity), oldState = try value(rows.state)
        var cards = previous?["cards"] as? [[String: Any]] ?? []
        var source = previous?["source"] as? [String: Any] ?? [:]
        var states = previous?["states"] as? [String: Any] ?? [:]
        var nextEntity: [String: Any]?, nextState: [String: Any]?
        switch operation {
        case "registerDraft":
            cards = try R.cardsOf(input); source = try R.source(input["source"])
            if let previous {
                let storedSource = previous["source"] as? [String: Any] ?? [:]
                if options["requireDraftIdForReplay"] as? Bool == true {
                    guard !R.string(source["draftId"]).isEmpty,
                          R.string(source["draftId"]) == R.string(storedSource["draftId"]) else { throw R.fail("SOURCE_CONFLICT", "已有卡组必须用相同 draftId 显式重放") }
                }
                guard R.same(previous["cards"] as Any, cards) else { throw R.fail("CONTENT_CONFLICT", "相同 gid 的草稿 cards 发生分叉") }
                guard R.same(storedSource, source) else { throw R.fail("SOURCE_CONFLICT", "相同 gid 的草稿 source 发生分叉") }
                return previous
            }
            states = try R.freshStates(cards.count)
            nextEntity = try R.entity(id: id, cards: cards, source: source, created: at, updated: at)
            nextState = try R.stateValue(id: id, states: states, count: cards.count)
        case "saveConfirmedCard":
            if previous == nil { cards = try R.cardsOf(input) }
            else if input["cards"] is [Any] {
                let replacement = try R.cards(input["cards"])
                guard cards.count == replacement.count else { throw R.fail("TRANSITION", "确认阶段不得改变批内卡片数量") }
                cards = replacement
            }
            if R.has(input["source"]) { source = try R.source(input["source"]) }
            else { source = try R.source(previous?["source"]) }
            let index = R.has(input["cardIndex"]) ? try R.integer(input["cardIndex"], "cardIndex") : (cards.count == 1 ? 0 : -1)
            guard index >= 0, index < cards.count else { throw R.fail("CARD_INDEX", "saveConfirmedCard 必须提供有效 cardIndex") }
            if previous != nil, input["card"] is [String: Any] { cards[Int(index)] = try R.card(input["card"]) }
            if previous == nil { states = try R.freshStates(cards.count) }
            var current = try R.object(states[String(index)], "state", code: "CORRUPT")
            guard current["removed"] as? Bool != true else { throw R.fail("CARD_REMOVED", "已删除的批内卡片不能隐式复活") }
            current["phase"] = "confirmed"
            let confirmedAt = (current["confirmedAt"] as? NSNumber)?.int64Value ?? 0
            current["confirmedAt"] = confirmedAt > 0 ? confirmedAt : at
            if (current["review"] as? [String: Any])?["status"] as? String == "unavailable" { current["review"] = R.defaultReview("confirmed") }
            states[String(index)] = try R.state(current)
            let unchanged = oldEntity != nil && R.same(oldEntity?["cards"] as Any, cards) && R.same(oldEntity?["source"] as Any, source)
            nextEntity = try R.entity(id: id, cards: cards, source: source,
                created: oldEntity?["createdAt"] ?? at, updated: unchanged ? oldEntity!["contentUpdatedAt"]! : at)
            nextState = try R.stateValue(id: id, states: states, count: cards.count)
            if let previous, R.same(oldEntity as Any, nextEntity as Any), R.same(oldState as Any, nextState as Any) { return previous }
        case "replaceEntity", "replaceContent":
            let replacement = operation == "replaceContent" ? ["cards": arg(1) ?? NSNull()] : try R.object(arg(1), "entity replacement")
            try R.fields(replacement, ["cards", "source"], "entity replacement")
            guard !replacement.isEmpty else { throw R.fail("INPUT", "entity replacement 至少需要 cards 或 source") }
            if replacement["cards"] != nil {
                let changed = try R.cards(replacement["cards"])
                guard changed.count == cards.count else { throw R.fail("TRANSITION", "内容修改不得改变批内卡片数量") }
                cards = changed
            }
            if replacement["source"] != nil { source = try R.source(replacement["source"]) }
            if R.same(previous?["cards"] as Any, cards), R.same(previous?["source"] as Any, source) { return previous! }
            nextEntity = try R.entity(id: id, cards: cards, source: source, created: oldEntity!["createdAt"]!, updated: at)
        case "removeDraftCard", "removeCard", "patchState", "recordAnkiReceipt":
            if operation == "recordAnkiReceipt" { try expected(rows.entity, options["ifEntityRev"], label: "ifEntityRev") }
            let index = try R.integer(arg(1), "cardIndex")
            guard index < cards.count else { throw R.fail("CARD_INDEX", "cardIndex 超出卡组") }
            let key = String(index)
            var current = try R.object(states[key], "state", code: "CORRUPT")
            if operation == "removeCard" || operation == "removeDraftCard" {
                if operation == "removeDraftCard", current["phase"] as? String != "draft" { throw R.fail("TRANSITION", "已确认卡片不能按草稿删除") }
                if current["removed"] as? Bool == true {
                    if operation == "removeCard" { try expected(rows.state, options["ifStateRev"], label: "ifStateRev") }
                    return previous!
                }
                current["removed"] = true
            } else {
                var patch: [String: Any]
                if operation == "recordAnkiReceipt" {
                    var receipt = arg(3) as? [String: Any] ?? [:]; receipt["target"] = arg(2) ?? NSNull()
                    patch = ["ankiReceipt": receipt]
                } else { patch = try R.object(arg(2), "state patch", code: "STATE") }
                try R.fields(patch, ["review", "flags", "projections", "ankiReceipt", "exactState"], "state patch")
                guard !patch.isEmpty else { throw R.fail("STATE", "state patch 不能为空") }
                if current["removed"] as? Bool == true, !(patch.count == 1 && patch["ankiReceipt"] != nil) { throw R.fail("CARD_REMOVED", "已删除的批内卡片只能追加 Anki 投影回执") }
                var projections = patch["projections"]
                if R.has(patch["ankiReceipt"]) {
                    var receipt = try R.object(patch["ankiReceipt"], "ankiReceipt", code: "RECEIPT")
                    let target = try R.target(R.string(receipt.removeValue(forKey: "target")).trimmingCharacters(in: .whitespacesAndNewlines))
                    projections = ["anki": [target: receipt]]
                }
                current["review"] = try R.review(patch["review"], previous: current["review"] as? [String: Any])
                current["flags"] = try R.flags(patch["flags"], previous: current["flags"] as? [String: Any])
                current["projections"] = try R.projections(projections, previous: current["projections"] as? [String: Any])
                if patch["exactState"] != nil { current["exactState"] = try R.exactState(patch["exactState"]) }
            }
            states[key] = try R.state(current)
            nextState = try R.stateValue(id: id, states: states, count: cards.count)
            if R.same(oldState as Any, nextState as Any) { return previous! }
        default: throw R.fail("INPUT", "不支持的卡仓操作：" + operation)
        }
        if let nextEntity {
            try expected(rows.entity, options["ifEntityRev"], label: "ifEntityRev")
            try write(Self.entities, id: id, value: nextEntity, previous: rows.entity, mutation: mutation + ":entity", at: at)
        }
        if let nextState {
            try expected(rows.state, options["ifStateRev"], label: "ifStateRev")
            try write(Self.states, id: id, value: nextState, previous: rows.state, mutation: mutation + ":state", at: at)
        }
        return try load(id)!
    }
    /// Keep the existing local scheduling rule. Only the execution location
    /// changes: current revisions, schedule and the review event commit together.
    static func scheduledReview(_ previous: [String: Any], ease: Int, reviewedAt: Int64) throws -> [String: Any] {
        guard (1...4).contains(ease), reviewedAt >= 0 else { throw R.fail("INPUT", "评分或复习时刻无效") }
        let prior = try R.number(previous["intervalDays"] ?? 0, "intervalDays")
        var interval = ease == 1 ? 0 : ease == 2 ? max(1, prior > 0 ? prior * 1.2 : 1)
            : ease == 3 ? max(1, prior > 0 ? prior * 2.5 : 1) : max(4, prior > 0 ? prior * 3.5 : 4)
        interval = (interval * 100).rounded(.toNearestOrAwayFromZero) / 100
        let due = Double(reviewedAt) + (ease == 1 ? 600_000 : (interval * 86_400_000).rounded(.toNearestOrAwayFromZero))
        return try R.review(["status": ease == 1 ? "relearning" : "review", "dueAt": due,
            "lastReviewedAt": reviewedAt, "intervalDays": interval, "ease": ease,
            "reps": R.number(previous["reps"] ?? 0, "reps") + 1,
            "lapses": R.number(previous["lapses"] ?? 0, "lapses") + (ease == 1 ? 1 : 0)])
    }

    private func commitReview(_ input: [String: Any], mutation: String, at: Int64) throws -> [String: Any] {
        try R.fields(input, ["gid", "cardIndex", "entityRev", "stateRev", "aid", "ease", "reviewedAt", "file", "ankiCardId"], "review command")
        let id = try R.id(input["gid"]), index = try R.integer(input["cardIndex"], "cardIndex")
        let aid = try R.text(input["aid"], "aid", 256, required: true)
        let reviewedAt = try R.integer(input["reviewedAt"], "reviewedAt"), ease = try R.integer(input["ease"], "ease")
        guard (1...4).contains(ease) else { throw R.fail("INPUT", "评分无效") }
        let digest = SHA256.hash(data: try R.bytes([id, index, aid])).map { String(format: "%02x", $0) }.joined()
        let historyID = "native-review:" + digest, historyCollection = "native-review-history"
        let fingerprint = SHA256.hash(data: try R.bytes(input)).map { String(format: "%02x", $0) }.joined()
        guard let current = try load(id), let cards = current["cards"] as? [[String: Any]], index < cards.count,
              let states = current["states"] as? [String: Any], let state = states[String(index)] as? [String: Any] else {
            throw R.fail("NOT_FOUND", "复习卡片已删除")
        }
        if let previous = try store.record(collection: historyCollection, id: historyID) {
            let saved = try R.object(JSONSerialization.jsonObject(with: Data(previous.json.utf8)), "review history")
            guard (saved["value"] as? [String: Any])?["fingerprint"] as? String == fingerprint else {
                throw R.fail("MUTATION_REUSED", "同一复习编号已有不同评分")
            }
            return current
        }
        guard state["phase"] as? String == "confirmed", state["removed"] as? Bool != true,
              (state["flags"] as? [String: Any])?["archived"] as? Bool != true else { throw R.fail("TRANSITION", "当前卡片不能复习") }
        let previous = state["review"] as? [String: Any] ?? [:]
        guard !["unavailable", "suspended", "buried"].contains(previous["status"] as? String ?? "new") else {
            throw R.fail("TRANSITION", "当前卡片已暂停复习")
        }
        let entityRev = try R.integer(input["entityRev"], "entityRev"), stateRev = try R.integer(input["stateRev"], "stateRev")
        guard try R.integer(current["entityRev"], "current entityRev") == entityRev,
              try R.integer(current["stateRev"], "current stateRev") == stateRev else {
            throw R.fail("CONFLICT", "卡片或复习状态已更新，请刷新后评分")
        }
        let options: [String: Any] = ["ifEntityRev": entityRev, "ifStateRev": stateRev]
        let next = try Self.scheduledReview(previous, ease: Int(ease), reviewedAt: reviewedAt)
        let patched = try execute("patchState", args: [id, index, ["review": next], options], mutation: mutation + ":rating", at: at) as! [String: Any]
        let event: [String: Any] = ["id": "revlog:" + id + ":" + String(index) + ":" + aid,
            "source": "reader", "file": try R.text(input["file"] ?? "", "file", 4096), "aid": aid,
            "gid": id, "index": index, "ease": ease, "reviewedAt": reviewedAt,
            "ankiCardId": try R.text(input["ankiCardId"] ?? "", "ankiCardId", 256)]
        let history: [String: Any] = ["schema": 1, "collection": historyCollection, "id": historyID,
            "rev": 1, "updatedAt": at, "updatedBy": deviceID, "deleted": false,
            "value": ["fingerprint": fingerprint, "event": event]]
        // Local recovery history is not a new sync collection. Existing account-
        // scoped event delivery consumes the committed review as before.
        _ = try store.commitWithinTransaction(record: .init(collection: historyCollection, id: historyID, rev: 1,
            updatedAt: at, deleted: false, json: String(decoding: R.bytes(history), as: UTF8.self)),
            mutationId: historyID, journalJSON: nil, expectedRev: 0, now: at)
        return patched
    }

    /// A delayed Anki interval may refine only the exact local review it was
    /// requested for. Never rebuild counters from the pre-rating web snapshot.
    private func adoptReviewSchedule(_ input: [String: Any], mutation: String, at: Int64) throws -> [String: Any] {
        try R.fields(input, ["gid", "cardIndex", "aid", "reviewedAt", "next", "expectedReview", "entityRev"], "review schedule")
        let id = try R.id(input["gid"]), index = try R.integer(input["cardIndex"], "cardIndex")
        let aid = try R.text(input["aid"], "aid", 256, required: true)
        let reviewedAt = try R.integer(input["reviewedAt"], "reviewedAt")
        let expected = try R.object(input["expectedReview"], "expectedReview")
        let next = try R.object(input["next"], "next")
        // Anki learning intervals are signed: negative values are seconds.
        // Generic repository numbers are non-negative, so do not use R.number.
        let signed = (next["interval"] as? NSNumber)?.doubleValue ?? Double(R.string(next["interval"]))
        guard let interval = signed, interval.isFinite else { throw R.fail("INPUT", "Anki interval 无效") }
        guard interval != 0 else { return ["applied": false, "reason": "no-interval"] }
        guard let current = try load(id), let states = current["states"] as? [String: Any],
              let state = states[String(index)] as? [String: Any] else {
            throw R.fail("NOT_FOUND", "复习卡片已删除")
        }
        var review = state["review"] as? [String: Any] ?? [:]
        guard state["phase"] as? String == "confirmed", state["removed"] as? Bool != true,
              (state["flags"] as? [String: Any])?["archived"] as? Bool != true,
              R.same(review, expected), (review["lastReviewedAt"] as? NSNumber)?.int64Value == reviewedAt,
              try R.integer(current["entityRev"], "entityRev") == R.integer(input["entityRev"], "expected entityRev") else {
            return ["applied": false, "reason": "stale", "record": current]
        }
        let digest = SHA256.hash(data: try R.bytes([id, index, aid])).map { String(format: "%02x", $0) }.joined()
        guard let row = try store.record(collection: "native-review-history", id: "native-review:" + digest),
              let history = try JSONSerialization.jsonObject(with: Data(row.json.utf8)) as? [String: Any],
              let event = (history["value"] as? [String: Any])?["event"] as? [String: Any],
              (event["reviewedAt"] as? NSNumber)?.int64Value == reviewedAt,
              R.same(event["ease"] as Any, review["ease"] as Any) else {
            throw R.fail("CONFLICT", "无法确认这次 Anki 回执对应的本地评分")
        }
        let days = interval > 0 ? interval : abs(interval) / 86400
        let roundedDays = floor(days * 100 + 0.5) / 100
        let due = Double(reviewedAt) + floor(days * 86400000 + 0.5)
        guard due.isFinite, due <= 9_007_199_254_740_991 else { throw R.fail("INPUT", "Anki 到期时间无效") }
        // Preserve ease, reps, lapses, status and all other committed fields.
        review["intervalDays"] = roundedDays; review["dueAt"] = due; review["scheduleSource"] = "anki-fsrs"
        if R.same(review, expected) { return ["applied": false, "reason": "unchanged", "record": current] }
        let updated = try execute("patchState", args: [id, index, ["review": review], ["ifStateRev": current["stateRev"]!]],
            mutation: mutation + ":schedule", at: at)
        return ["applied": true, "record": updated]
    }

    /// Direct Swift UI actions share the repository transaction, including the
    /// exact-state compatibility projection. A stale visible card cannot write
    /// over a newer card or turn an already confirmed card back into a draft.
    private func interact(_ input: [String: Any], mutation: String, at: Int64) throws -> [String: Any] {
        let id = try R.id(input["gid"]), index = try R.integer(input["cardIndex"], "cardIndex")
        guard let current = try load(id), let cards = current["cards"] as? [[String: Any]], index < cards.count,
              let states = current["states"] as? [String: Any], let state = states[String(index)] as? [String: Any] else {
            throw R.fail("NOT_FOUND", "卡片已删除或更新")
        }
        let entityRev = try R.integer(input["entityRev"], "entityRev"), stateRev = try R.integer(input["stateRev"], "stateRev")
        guard entityRev == (current["entityRev"] as? NSNumber)?.int64Value,
              stateRev == (current["stateRev"] as? NSNumber)?.int64Value else { throw R.fail("CONFLICT", "卡片已经更新，请使用最新卡面") }
        let reveal = input["action"] as? String == "reveal"
        guard state["phase"] as? String == (reveal ? "confirmed" : "draft"), state["removed"] as? Bool != true else { throw R.fail("TRANSITION", "这张卡的状态不允许当前操作") }
        var exact = state["exactState"] as? [String: Any] ?? [:]
        guard !["_addPending", "_removePending", "_ratingPending", "_syncPending"].contains(where: { exact[$0] as? Bool == true }) else {
            throw R.fail("TRANSITION", "卡片还有尚未确认的操作，请勿重复提交")
        }
        let options: [String: Any] = ["ifEntityRev": entityRev, "ifStateRev": stateRev]
        switch input["action"] as? String {
        case "reveal":
            guard !["done", "preview"].contains(exact["_st"] as? String ?? "") else { throw R.fail("TRANSITION", "这张卡已经显示答案") }
            exact["_showBack"] = true
            return try execute("patchState", args: [id, index, ["exactState": exact], options], mutation: mutation + ":reveal", at: at) as! [String: Any]
        case "edit":
            let fields = cards[Int(index)]["type"] as? String == "cloze" ? ["cloze"] : ["front", "back"]
            guard let field = input["field"] as? String, fields.contains(field), let text = input["text"] as? String,
                  text.utf16.count <= 24000 else { throw R.fail("INPUT", "草稿字段或内容无效") }
            exact[field] = text
            return try execute("patchState", args: [id, index, ["exactState": exact], options], mutation: mutation + ":edit", at: at) as! [String: Any]
        case "del":
            return try execute("removeDraftCard", args: [id, index, options], mutation: mutation + ":remove", at: at) as! [String: Any]
        case "add":
            let content = cards.enumerated().map { offset, source -> [String: Any] in
                var card = source
                let saved = (states[String(offset)] as? [String: Any])?["exactState"] as? [String: Any] ?? [:]
                let fields = source["type"] as? String == "cloze" ? ["cloze"] : ["front", "back"]
                for field in fields { if let value = saved[field] { card[field] = value } }
                return card
            }
            let saved = try execute("saveConfirmedCard", args: [["gid": id, "cards": content, "cardIndex": index], options], mutation: mutation + ":confirm", at: at) as! [String: Any]
            exact["_st"] = "learn"; exact["_showBack"] = false; exact["_addPending"] = false
            exact["_addQueued"] = false; exact["_addAid"] = NSNull()
            exact["_ratingUnavailable"] = true; exact["_ratingUnavailableReason"] = "not-exported"
            return try execute("patchState", args: [id, index, ["exactState": exact], ["ifStateRev": saved["stateRev"]!]], mutation: mutation + ":confirmed-state", at: at) as! [String: Any]
        default: throw R.fail("INPUT", "不支持的原生卡片动作")
        }
    }

    private func importLegacy(_ value: Any?, options: [String: Any], mutation: String, at: Int64) throws -> [Any] {
        guard let items = value as? [Any], !items.isEmpty, items.count <= 500 else { throw R.fail("LEGACY", "legacy batch 必须包含 1-500 条记录") }
        let specs = try items.map(R.legacy)
        var seen = Set<String>(), results: [Any] = []
        for spec in specs {
            let id = spec["id"] as! String
            guard seen.insert(id).inserted else { throw R.fail("LEGACY", "legacy batch 含重复 gid：" + id) }
            let rows = try pair(id)
            guard (rows.entity == nil) == (rows.state == nil) else { throw R.fail("PARTIAL", "legacy 导入遇到半条本地卡组：" + id) }
            if rows.entity != nil, options["missingOnly"] as? Bool == true {
                results.append(try project(rows, includeDeleted: false) as Any? ?? NSNull()); continue
            }
            try validatePair(rows, id: id)
            let cards = spec["cards"] as! [[String: Any]], source = spec["source"] as! [String: Any]
            let incoming = spec["states"] as! [String: Any]
            if rows.entity == nil {
                let timestamp = (spec["timestamp"] as! NSNumber).int64Value
                let stamp = timestamp > 0 ? timestamp : at
                try write(Self.entities, id: id, value: R.entity(id: id, cards: cards, source: source, created: stamp, updated: stamp),
                    previous: nil, mutation: mutation + ":" + id + ":entity", at: at)
                try write(Self.states, id: id, value: R.stateValue(id: id, states: incoming, count: cards.count),
                    previous: nil, mutation: mutation + ":" + id + ":state", at: at)
            } else {
                let current = try project(rows, includeDeleted: false)!
                guard R.same(current["cards"]!, cards) else { throw R.fail("LEGACY_CONFLICT", "legacy cards 与本地同 gid 内容分叉：" + id) }
                let currentSource = current["source"] as! [String: Any]
                let ref = R.string((currentSource["legacy"] as? [String: Any])?["source_ref"])
                let incomingRef = R.string((source["legacy"] as? [String: Any])?["source_ref"])
                if currentSource["kind"] as? String == "pi-legacy-card-registry", !ref.isEmpty, !incomingRef.isEmpty, ref != incomingRef { throw R.fail("LEGACY_CONFLICT", "legacy source_ref 与本地同 gid 来源分叉：" + id) }
                var states = current["states"] as! [String: Any]
                for key in incoming.keys.sorted() {
                    var local = states[key] as! [String: Any]
                    let remote = incoming[key] as! [String: Any]
                    let localExact = local["exactState"] as! [String: Any], remoteExact = remote["exactState"] as! [String: Any]
                    if !localExact.isEmpty, !remoteExact.isEmpty, !R.same(localExact, remoteExact) { throw R.fail("LEGACY_CONFLICT", "legacy state 与本地同 gid/index 状态分叉：" + id + "/" + key) }
                    if localExact.isEmpty, !remoteExact.isEmpty {
                        local["exactState"] = remoteExact
                        if local["phase"] as? String == "draft" {
                            for field in ["phase", "confirmedAt", "review"] { local[field] = remote[field] }
                        }
                    }
                    var projections = local["projections"] as! [String: Any]
                    var anki = projections["anki"] as! [String: Any]
                    let remoteAnki = (remote["projections"] as! [String: Any])["anki"] as! [String: Any]
                    for (target, receipt) in remoteAnki where anki[target] == nil { anki[target] = receipt }
                    projections["anki"] = anki; local["projections"] = projections
                    states[key] = try R.state(local)
                }
                let state = try R.stateValue(id: id, states: states, count: cards.count)
                if !R.same(try self.value(rows.state)!, state) {
                    try write(Self.states, id: id, value: state, previous: rows.state, mutation: mutation + ":" + id + ":state", at: at)
                }
            }
            results.append(try load(id)!)
        }
        return results
    }
    private func write(_ collection: String, id: String, value: [String: Any], previous: ReaderNativeDataStore.Record?,
                       deleted: Bool = false, mutation: String, at: Int64) throws {
        let revision = previous?.rev ?? 0
        if let remembered = try store.mutationResult(mutationId: mutation) {
            let saved = try R.object(JSONSerialization.jsonObject(with: Data(remembered.utf8)), "mutation record")
            guard saved["collection"] as? String == collection, saved["id"] as? String == id,
                  saved["deleted"] as? Bool == deleted, R.same(saved["value"] as Any, value) else { throw R.fail("MUTATION_REUSED", "mutationId 已被不同内容使用") }
            return
        }
        guard revision >= 0, revision < 9_007_199_254_740_991 else { throw R.fail("CONFLICT", "记录 revision 已达到安全整数上限") }
        let parent: Any
        if let previous {
            if previous.deleted { parent = ["deleted": true] }
            else { parent = ["deleted": false, "value": try self.value(previous)!] as [String: Any] }
        } else { parent = NSNull() }
        try R.bounded(parent, "record.causal.parent", 512 * 1024)
        let record: [String: Any] = ["schema": 1, "collection": collection, "id": id, "rev": revision + 1,
            "updatedAt": at, "updatedBy": deviceID, "deleted": deleted, "value": value,
            "causal": ["contract": "record-parent-state/1", "parent": parent]]
        let data = try R.bytes(record)
        let change: [String: Any] = ["mutationId": mutation, "operation": deleted ? "remove" : "put", "collection": collection, "record": record]
        _ = try R.bytes(change)
        try store.commitWithinTransaction(record: .init(collection: collection, id: id, rev: revision + 1,
            updatedAt: at, deleted: deleted, json: String(decoding: data, as: UTF8.self)),
            mutationId: mutation, journalJSON: { cursor in
                var item = change; item["cursor"] = cursor
                return String(decoding: try! R.bytes(item), as: UTF8.self)
            }, expectedRev: revision, now: at)
    }
}
