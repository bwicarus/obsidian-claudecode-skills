import Foundation
typealias O = ReaderNativeVocabularyOverlay
typealias R = ReaderNativeCardRules
let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
for (index, item) in (fixture["cases"] as! [[String: Any]]).enumerated() {
    let state = O.Index(item["records"] as! [O.Row])
    let local = O.localMarks(item["chars"] as! [O.Row], state: state)
    precondition(R.same(local, item["local"]!), "local overlay differs at \(index): \(local), expected \(item["local"]!)")
    let combined = O.merge(local, item["remote"] as! [O.Row])
    precondition(R.same(combined, item["combined"]!), "overlay merge differs at \(index)")
    precondition(R.same(O.visible(combined, state: state), item["visible"]!), "overlay filter differs at \(index)")
}
print("Native vocabulary overlay matches token, alias, nested mastery and geometry merge oracle")

// 生词句（2026-09-26）：真机走原生叠加层，句子要在原生里算。规则同服务端/网页版。
do {
    var chars: [O.Row] = [], spans: [(row: O.Row, lo: Int)] = [], x = 10.0, wid = 1000.0
    func put(_ text: String, y: Double, w: Double, sp: Bool = false) {
        for c in text { chars.append(["c": String(c), "x0": x, "y0": y, "x1": x + 6, "y1": y + 10, "w": sp ? -1 : w, "bk": 0, "sp": sp]); x += 6 }
    }
    func sentence(_ words: [String], marked: Set<Int>, y: Double) {
        for (i, word) in words.enumerated() {
            let lo = chars.count; wid += 1; put(word, y: y, w: wid)
            if marked.contains(i) { spans.append((row: ["lemma": word, "label_slug": "new"], lo: lo)) }
            put(i == words.count - 1 ? "." : " ", y: y, w: -1, sp: i != words.count - 1)
        }
        put(" ", y: y, w: -1, sp: true)
    }
    let twelve = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split(separator: " ").map(String.init)
    sentence(twelve, marked: [0, 3, 7], y: 100)                       // 3 个生词 → 框
    sentence(twelve.map { $0 + "s" }, marked: [1, 2], y: 100)        // 2 个 → 不框
    x = 10; sentence(twelve.map { $0 + "x" }, marked: [0, 1, 2], y: 980)  // 页脚 → 不框
    let got = O.localSentences(chars, spans: spans, visibleLemmas: Set(spans.compactMap { $0.row["lemma"] as? String }), pageHeight: 1000)
    precondition(got.count == 1, "expected exactly one vocab sentence, got \(got.count)")
    precondition(got[0]["count"] as? Int == 3 && (got[0]["text"] as? String ?? "").hasPrefix("alpha bravo"), "wrong sentence: \(got[0])")
    precondition((got[0]["firstChar"] as? [Double])?.prefix(2) == [10, 100], "first char box wrong")
    print("Native vocab sentences: ≥3 underlined, ≥10 words, footer excluded")
}

let store = try ReaderNativeDataStore(path: ":memory:")
let cache = ReaderNativePageOverlayStore()
let v = ReaderNativeVocabularyState(store: store, deviceID: "test")
_ = try v.set(["key": "word", "language": "en"], property: "lookup", enabled: true, mutation: "first")
let first = try cache.vocabulary(store)
precondition(first.enabled(["key":"word", "language":"en"], "lookup"))
let record = try store.records(collection: "vocabulary-state", limit: 10, offset: 0).first!
var envelope = try JSONSerialization.jsonObject(with: Data(record.json.utf8)) as! [String: Any]
var value = envelope["value"] as! [String: Any]; value["enabled"] = false
envelope["value"] = value; envelope["rev"] = record.rev + 1
let cursor = try store.cursor()
try store.commit(record: .init(collection: record.collection, id: record.id, rev: record.rev+1, updatedAt: 1234,
    deleted: false, json: String(decoding: R.bytes(envelope), as: UTF8.self)), mutationId: nil,
    journalJSON: nil, expectedRev: record.rev, now: 1234)
let next = try cache.vocabulary(store), nextCursor = try store.cursor()
precondition(nextCursor == cursor && !next.enabled(["key":"word", "language":"en"], "lookup"), "inbound update left a stale native projection")
for page in 1...27 {
    let entry = ReaderNativePageOverlayStore.normalized(["ok":true, "vocab_marks":[], "mastered_furi":["既知"], "cv":"server"],
        page:page, revision:"text:\(page)", savedAt:Double(page))!
    try ReaderNativePageOverlayStore.save(entry, store:store, bookID:"one", deviceID:"test")
}
let oldest = try ReaderNativePageOverlayStore.cached(store, bookID:"one", page:1, revision:"text:1")
let latest = try ReaderNativePageOverlayStore.cached(store, bookID:"one", page:27, revision:"text:27")
let changed = try ReaderNativePageOverlayStore.cached(store, bookID:"one", page:27, revision:"new-text")
let otherBook = try ReaderNativePageOverlayStore.cached(store, bookID:"two", page:27, revision:"text:27")
precondition(oldest == nil && latest != nil && changed == nil && otherBook == nil)
let before = try store.record(collection: ReaderNativePageOverlayStore.cacheCollection, id:"one:" + ReaderNativePageOverlayStore.cacheKind)!
try store.execute("CREATE TRIGGER reject_cache BEFORE INSERT ON records WHEN NEW.collection = 'native-page-overlay-enrichment-cache-v1' BEGIN SELECT RAISE(ABORT, 'cache failed'); END")
do {
    try ReaderNativePageOverlayStore.save(ReaderNativePageOverlayStore.normalized(["ok":true],page:28,revision:"text:28",savedAt:28)!, store:store,bookID:"one",deviceID:"test")
    preconditionFailure("failed cache write accepted")
} catch is ReaderNativeDataStore.StoreError {}
let after = try store.record(collection: before.collection, id:before.id)!
precondition(after.json == before.json, "failed enrichment overwrote the offline cache")
print("Native overlay cache: bounded, revision/book isolation, inbound invalidation and rollback passed")
