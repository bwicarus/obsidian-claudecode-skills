import Foundation
import SwiftSoup

/// Read the canonical notes, including books whose old derived word index was
/// never rebuilt. Parsing and HTML text extraction do not occupy the UI actor.
@MainActor
final class ReaderNativeWordCards {
    struct Card: Sendable { let cid: String; let label: String; let text: String }
    private let store: ReaderNativeDataStore
    private var revision: UInt64?
    private var cache: [String: [Card]] = [:]
    init(store: ReaderNativeDataStore) { self.store = store }

    func lookup(lemma: String, word: String) async throws -> [[String: String]] {
        let key = String(decoding: try JSONSerialization.data(withJSONObject: [lemma, word]), as: UTF8.self)
        for _ in 0..<2 {
            try Task.checkCancellation()
            let generation = store.generation(collection: "native-document-notes-legacy")
            if revision != generation { revision = generation; cache.removeAll() }
            if let value = cache[key] { return value.map { ["cid": $0.cid, "label": $0.label, "text": $0.text] } }
            let records = try store.liveRecords(collection: "native-document-notes-legacy", limit: 1000).map(\.json)
            let task = Task.detached(priority: .userInitiated) { try Self.project(records, lemma: lemma, word: word) }
            let cards = try await withTaskCancellationHandler { try await task.value } onCancel: { task.cancel() }
            try Task.checkCancellation()
            guard generation == store.generation(collection: "native-document-notes-legacy") else { continue }
            if cache.count >= 32 { cache.removeAll() }
            cache[key] = cards
            return cards.map { ["cid": $0.cid, "label": $0.label, "text": $0.text] }
        }
        throw ReaderNativeLookupRequest.Failure(message: "关联卡片正在更新，请重试")
    }

    nonisolated static func project(_ records: [String], lemma: String, word: String) throws -> [Card] {
        func normalize(_ value: String) -> String {
            value.replacingOccurrences(of: "[\\s\\uFEFF]+", with: "", options: .regularExpression).lowercased()
        }
        let keys = Set([normalize(lemma), normalize(word)].filter { !$0.isEmpty })
        var matches: [(card: [String: Any], at: Double, order: Int, position: Int)] = []
        for raw in records {
            try Task.checkCancellation()
            guard let record = try JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [String: Any],
                  let value = record["value"] as? [String: Any] else {
                throw ReaderNativeLookupRequest.Failure(message: "关联卡片记录无效")
            }
            let notes: [[String: Any]]
            if value["payload"] == nil || value["payload"] is NSNull { notes = [] }
            else if let list = value["payload"] as? [[String: Any]] { notes = list }
            else { throw ReaderNativeLookupRequest.Failure(message: "关联卡片记录无效") }
            // The old index resolves repeated cid entries to their last live
            // note before returning them. Keep the same identity behavior.
            var live: [String: [String: Any]] = [:]
            for note in notes {
                if let html = note["html"] as? [String: Any], let cid = html["cid"] as? String { live[cid] = html }
            }
            for (index, note) in notes.enumerated() {
                guard let html = note["html"] as? [String: Any], let cid = html["cid"] as? String, !cid.isEmpty,
                      let bind = html["bind"] as? [String: Any], bind["kind"] as? String == "page-chars",
                      let text = bind["text"] as? String, keys.contains(normalize(text)), normalize(text).utf16.count <= 64,
                      let current = live[cid], let currentBind = current["bind"] as? [String: Any],
                      currentBind["kind"] as? String == "page-chars", normalize(currentBind["text"] as? String ?? "") == normalize(text),
                      !(current["content"] as? String ?? "").isEmpty else { continue }
                matches.append((current, (note["created"] as? NSNumber)?.doubleValue ?? 0, index, matches.count))
            }
        }
        matches.sort { a, b in a.at != b.at ? a.at < b.at : (a.order != b.order ? a.order < b.order : a.position < b.position) }
        return try matches.prefix(12).map { row in
            let document = try SwiftSoup.parseBodyFragment(row.card["content"] as? String ?? "")
            try document.select(".rc-note-dict,.rc-note-dict-note,script,style").remove()
            // The old detached DOM used textContent: no artificial whitespace
            // is inserted between adjacent inline nodes.
            func content(_ node: Node) -> String {
                if let text = node as? TextNode { return text.getWholeText() }
                return node.getChildNodes().map(content).joined()
            }
            let text = document.body().map(content) ?? ""
            let label = row.card["label"] as? String ?? ""
            return Card(cid: row.card["cid"] as? String ?? "", label: label.isEmpty ? "卡片" : label,
                text: String(text.replacingOccurrences(of: "\n{3,}", with: "\n\n", options: .regularExpression)
                    .trimmingCharacters(in: .whitespacesAndNewlines).prefix(1200)))
        }
    }
}
