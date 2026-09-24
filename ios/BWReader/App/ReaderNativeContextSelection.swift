import Foundation

/// Ephemeral selection graph. The App owns expiry and the maximal-node
/// projection; document content, card IDs and persistent records are untouched.
struct ReaderNativeContextSelection {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    private var records: [Data: [String: Any]] = [:]
    private var selected = Set<Data>()
    private var deadlines: [Data: TimeInterval] = [:]
    private(set) var revision = 0
    let expireMs: Double

    init(expireMs: Double = 300_000) { self.expireMs = expireMs }
    private func key(_ id: String) -> Data { Data(id.utf8) }
    private func id(_ key: Data) -> String { String(decoding: key, as: UTF8.self) }
    private func sorted(_ values: Set<Data>) -> [Data] {
        values.sorted { id($0).utf16.lexicographicallyPrecedes(id($1).utf16) }
    }
    private func checkedID(_ value: Any?, optional: Bool = false) throws -> String {
        guard let value = value as? String, (optional || !value.isEmpty), value.utf16.count <= 512,
              !value.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }) else {
            throw Failure(message: "上下文标识无效")
        }
        return value
    }
    private func normalized(_ value: [String: Any]) throws -> [String: Any] {
        let id = try checkedID(value["id"]), parent = try checkedID(value["parentId"], optional: true)
        guard key(id) != key(parent), let kind = value["kind"] as? String, kind.utf16.count <= 80,
              let label = value["label"] as? String, label.utf16.count <= 240,
              let text = value["text"] as? String, let covers = value["covers"] as? [String],
              let source = value["source"], let meta = value["meta"],
              JSONSerialization.isValidJSONObject([source, meta]) else {
            throw Failure(message: "上下文内容无效")
        }
        var edges = Set<Data>()
        for target in covers {
            let target = try checkedID(target, optional: true)
            if !target.isEmpty, key(target) != key(id) { edges.insert(key(target)) }
        }
        return ["id": id, "parentId": parent, "kind": kind, "label": label, "text": text,
                "covers": sorted(edges).map(self.id), "source": source, "meta": meta]
    }
    private func equal(_ lhs: [String: Any]?, _ rhs: [String: Any]) -> Bool {
        guard let lhs else { return false }
        return (try? JSONSerialization.data(withJSONObject: lhs, options: [.sortedKeys])) ==
               (try? JSONSerialization.data(withJSONObject: rhs, options: [.sortedKeys]))
    }
    private mutating func setSelected(_ key: Data, on: Bool, now: TimeInterval) {
        guard records[key] != nil, selected.contains(key) != on else { return }
        if on {
            selected.insert(key)
            if expireMs > 0 { deadlines[key] = now + expireMs / 1000 }
        } else { selected.remove(key); deadlines.removeValue(forKey: key) }
        revision += 1
    }

    /// Commands contain normalized records from compatibility clients. Native
    /// callers can use the same API without creating a page/card DOM node.
    mutating func apply(_ command: [String: Any], now: TimeInterval) throws {
        let operation = command["operation"] as? String ?? ""
        guard ["upsert", "select", "toggle", "deselect", "remove", "clear"].contains(operation) else {
            throw Failure(message: "不支持的上下文操作")
        }
        // Validate fully before changing state, including combined upsert/select.
        let record = try (command["record"] as? [String: Any]).map(normalized)
        let itemID = operation == "clear" ? "" : try checkedID(command["id"])
        if let record, key(record["id"] as! String) != key(itemID) {
            throw Failure(message: "上下文对象不一致")
        }
        if operation == "upsert", record == nil { throw Failure(message: "缺少上下文内容") }
        if command["selected"] != nil, !(command["selected"] is Bool) { throw Failure(message: "选中状态无效") }
        expire(now: now)
        let itemKey = key(itemID)
        if let record {
            var changed = !equal(records[itemKey], record)
            records[itemKey] = record
            // Legacy upsert(selected:) does not arm/renew a timer. Keep this
            // distinction from an explicit false -> true selection transition.
            if let on = command["selected"] as? Bool, selected.contains(itemKey) != on {
                if on { selected.insert(itemKey) } else { selected.remove(itemKey) }
                changed = true
            }
            if changed { revision += 1 }
        }
        switch operation {
        case "select": setSelected(itemKey, on: command["on"] as? Bool ?? true, now: now)
        case "toggle": setSelected(itemKey, on: !selected.contains(itemKey), now: now)
        case "deselect": setSelected(itemKey, on: false, now: now)
        case "remove":
            if records.removeValue(forKey: itemKey) != nil || selected.contains(itemKey) {
                selected.remove(itemKey); deadlines.removeValue(forKey: itemKey); revision += 1
            }
        case "clear":
            if !selected.isEmpty { selected.removeAll(); deadlines.removeAll(); revision += 1 }
        default: break
        }
    }

    @discardableResult
    mutating func expire(now: TimeInterval) -> Bool {
        let before = revision
        for key in deadlines.filter({ $0.value <= now }).keys {
            deadlines.removeValue(forKey: key)
            setSelected(key, on: false, now: now)
        }
        return revision != before
    }
    var nextDeadline: TimeInterval? { deadlines.values.min() }
    func covers(_ coverer: Data, _ candidate: Data) -> Bool {
        guard coverer != candidate, let record = records[coverer] else { return false }
        var ancestry = Set<Data>(), current = candidate
        while !current.isEmpty, ancestry.insert(current).inserted {
            current = key(records[current]?["parentId"] as? String ?? "")
        }
        if ancestry.contains(coverer) { return true }
        var seen = Set<Data>(), queue = (record["covers"] as? [String] ?? []).map(key), index = 0
        while index < queue.count {
            let current = queue[index]; index += 1
            guard !current.isEmpty, seen.insert(current).inserted else { continue }
            if ancestry.contains(current) { return true }
            queue.append(contentsOf: (records[current]?["covers"] as? [String] ?? []).map(key))
        }
        return false
    }
    func snapshot(maxText: Int = 2500, limit: Int = Int.max) -> [String: Any] {
        let keys = sorted(selected.filter { records[$0] != nil })
        let positions = Dictionary(uniqueKeysWithValues: keys.enumerated().map { ($1, $0) })
        let effective = keys.filter { candidate in
            !keys.contains { coverer in
                covers(coverer, candidate) && (!covers(candidate, coverer) || positions[coverer]! < positions[candidate]!)
            }
        }
        let items = effective.prefix(max(0, limit)).compactMap { key -> [String: Any]? in
            guard var record = records[key], let text = record["text"] as? String else { return nil }
            record["text"] = String(decoding: text.utf16.prefix(max(0, maxText)), as: UTF16.self)
            return record
        }
        return ["contract": "context-selection/1", "items": items]
    }
    /// Review selection uses the same expiry and parent/child exclusion graph
    /// as message attachments. It never depends on a hidden answer element.
    mutating func selectReview(_ itemID: String, cardKey: String, on: Bool, now: TimeInterval) throws {
        let itemID = try checkedID(itemID)
        guard !cardKey.isEmpty, let record = records[key(itemID)],
              ["review-answer", "review-answer-segment"].contains(record["kind"] as? String ?? ""),
              let meta = record["meta"] as? [String: Any], meta["review_mode"] as? Bool == true,
              let expected = meta["card_key"] as? String, key(expected) == key(cardKey) else {
            throw Failure(message: "回答已更新或属于另一张复习卡")
        }
        try apply(["operation": "select", "id": itemID, "on": on], now: now)
    }

    func reviewPairs(cardKey: String) -> [[String: Any]] {
        guard !cardKey.isEmpty else { return [] }
        struct Group {
            let question: String
            let card: [String: Any]
            var parts: [(Double, String)] = []
            var ids: [String] = []
        }
        var groups: [Data: Group] = [:]
        for record in snapshot(maxText: 20_000, limit: 120)["items"] as? [[String: Any]] ?? [] {
            guard let meta = record["meta"] as? [String: Any], meta["review_mode"] as? Bool == true,
                  key(meta["card_key"] as? String ?? "") == key(cardKey),
                  ["review-answer", "review-answer-segment"].contains(record["kind"] as? String ?? ""),
                  let id = record["id"] as? String else { continue }
            let answer = key(meta["answer_id"] as? String ?? id)
            var group = groups[answer] ?? Group(question: meta["question"] as? String ?? "", card: meta["card"] as? [String: Any] ?? [:])
            let index = (meta["segment_index"] as? NSNumber)?.doubleValue ?? 0
            group.parts.append((index.isFinite ? index : 0, record["text"] as? String ?? ""))
            group.ids.append(id); groups[answer] = group
        }
        return sorted(Set(groups.keys)).compactMap { answer in
            let group = groups[answer]!
            // Equal indices retain snapshot order, like the original stable sort.
            let text = group.parts.enumerated().sorted {
                $0.element.0 == $1.element.0 ? $0.offset < $1.offset : $0.element.0 < $1.element.0
            }.map { $0.element.1 }.filter { !$0.isEmpty }.joined(separator: "\n\n")
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
            return ["question": group.question, "answer": text, "selection_ids": group.ids, "card": group.card]
        }
    }
    var projection: [String: Any] {
        var pairs: [String: [[String: Any]]] = [:]
        for record in selected.compactMap({ records[$0] }) {
            guard let meta = record["meta"] as? [String: Any], meta["review_mode"] as? Bool == true,
                  let cardKey = meta["card_key"] as? String, pairs[cardKey] == nil else { continue }
            pairs[cardKey] = reviewPairs(cardKey: cardKey)
        }
        return ["revision": revision, "selected": sorted(selected).map(id),
         "selectedRecords": sorted(selected).compactMap { records[$0] },
         "snapshot": snapshot(maxText: Int.max), "reviewPairs": pairs]
    }
}
