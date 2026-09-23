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
    func perform(_ request: [String: Any]) throws -> [String: Any] {
        guard let operation = request["operation"] as? String, let args = request["arguments"] as? [Any],
              !deviceID.isEmpty, deviceID.utf16.count <= 240, !deviceID.contains("\0") else { throw R.fail("INPUT", "原生卡仓请求无效") }
        func arg(_ index: Int) -> Any? { args.indices.contains(index) ? args[index] : nil }
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
        if operation == "importLegacyBatch" { throw R.fail("UNAVAILABLE", "旧记录导入尚未完成原生迁移") }
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
            if rows.entity?.deleted == true, rows.state?.deleted == true { return try project(rows, includeDeleted: true) as Any }
            try validatePair(rows, id: id)
            try expected(rows.entity, options["ifEntityRev"], label: "ifEntityRev")
            try expected(rows.state, options["ifStateRev"], label: "ifStateRev")
            try write(Self.entities, id: id, value: value(rows.entity) ?? [:], previous: rows.entity, deleted: true, mutation: mutation + ":entity", at: at)
            try write(Self.states, id: id, value: value(rows.state) ?? [:], previous: rows.state, deleted: true, mutation: mutation + ":state", at: at)
            return try load(id, includeDeleted: true) as Any
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
        return try load(id) as Any
    }
    private func write(_ collection: String, id: String, value: [String: Any], previous: ReaderNativeDataStore.Record?,
                       deleted: Bool = false, mutation: String, at: Int64) throws {
        let revision = previous?.rev ?? 0
        guard revision >= 0, revision < 9_007_199_254_740_991 else { throw R.fail("CONFLICT", "记录 revision 已达到安全整数上限") }
        let parent: Any
        if let previous { parent = previous.deleted ? ["deleted": true] : ["deleted": false, "value": try self.value(previous) as Any] }
        else { parent = NSNull() }
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
