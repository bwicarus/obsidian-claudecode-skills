import Foundation

func check(_ condition: Bool, _ message: String) {
    if !condition { fatalError(message) }
}
let store = try ReaderNativeDataStore(path: ":memory:")
let projection = ReaderNativeBookProjection(store: store)
let book = "book_%_📖"
func put(_ kind: String, _ payload: Any, revision: Int64 = 1, id: String? = nil,
         document: String = book, deleted: Bool = false) throws {
    let key = id ?? document + ":" + kind
    let object: [String: Any] = ["schema": 1, "collection": "native-" + kind,
        "id": key, "rev": revision, "updatedAt": 100, "deleted": deleted,
        "value": ["id": key, "documentId": document, "payload": payload]]
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    try store.commit(record: .init(collection: "native-" + kind, id: key, rev: revision,
        updatedAt: 100, deleted: deleted, json: String(decoding: data, as: UTF8.self)),
        mutationId: nil, journalJSON: nil, expectedRev: nil, now: 100)
}
func payload(_ kind: ReaderBookUserStateDomainName) throws -> Any {
    let domain = try projection.exportReadingDomains(bookID: book).first { $0.name == kind }!
    _ = try ReaderBookUserStatePackageCodec.validateDomainPayload(domain, localExport: true)
    return try JSONSerialization.jsonObject(with: Data(domain.payloadJson.utf8), options: [.fragmentsAllowed])
}

check(try projection.exportReadingDomains(bookID: book).allSatisfy(\.empty), "new book must be empty")
try put("reading-position", ["page": 45, "fraction": 0.25])
try put("document-notes-legacy", [["id": "n1", "anchor": ["kind": "pdf", "page": 45],
                                  "html": ["cid": "c1", "content": "原卡片"]]], revision: 9)
check((try payload(.notes) as? [[String: Any]])?.first?["id"] as? String == "n1", "note identity lost")
try put("document-highlights", [["id": "legacy", "text": "old"]], revision: 3)
check(((try payload(.highlights) as? [String: Any])?["pdf"] as? [[String: Any]])?.first?["id"] as? String == "legacy", "legacy record must remain readable")

let prefix = "native-document-highlights-item-v1:\(book.utf16.count):\(book):"
try put("document-highlights-split-meta", ["order": ["b", "a", "deleted", "b"]], revision: 4)
for id in ["a", "b", "orphan", "deleted"] {
    try put("document-highlights-items", ["id": id, "text": id, "deleted": id == "deleted"], id: prefix + id)
}
try put("document-highlights-items", ["id": "alien"], id: "native-document-highlights-item-v1:8:book_XX_:alien", document: "book_XX_")
let ids = ((try payload(.highlights) as? [String: Any])?["pdf"] as? [[String: Any]])?.compactMap { $0["id"] as? String }
check(ids == ["b", "a", "orphan"], "split ordering, tombstone or book isolation failed")
try put("ink", ["45": [["t": "pen", "pts": [[0, 0], [1, 1]]], ["t": "region", "id": "r1"]]])
let ink = ((try payload(.ink) as? [String: Any])?["pdf"] as? [String: [[String: Any]]])?["45"]
let regions = ((try payload(.closedRegions) as? [String: Any])?["pdf"] as? [String: [[String: Any]]])?["45"]
check(ink?.count == 1 && ink?.first?["t"] as? String == "pen", "region leaked into ink")
check(regions?.count == 1 && regions?.first?["id"] as? String == "r1", "region lost")
check(try store.journalCount() == 0 && store.cursor() == 0, "projection changed store or queued sync")
let first = try projection.exportReadingDomains(bookID: book)
check(first == (try projection.exportReadingDomains(bookID: book)), "same state must have stable digests")
try put("ink", NSNull(), revision: 2)
check(try projection.exportReadingDomains(bookID: book).first { $0.name == .ink }!.empty, "null legacy state must be empty")
try put("document-notes-legacy", "corrupt", revision: 10)
do {
    _ = try projection.exportReadingDomains(bookID: book)
    fatalError("corrupt data was presented as an empty book")
} catch { }
print("Native book projection: identity, split/legacy, tombstones, scope, domains, stable reads and corrupt-data checks passed")

let writing = try ReaderNativeDataStore(path: ":memory:")
let business = ReaderNativeBookStore(store: writing, bookID: book, deviceID: "test-device", now: { 100_000 })
let read = ReaderNativeBookProjection(store: writing)
let notes: [[String: Any]] = [["id": "placement-1", "anchor": ["kind":"pdf", "page":45], "created":100,
    "html": ["cid":"card-1", "content":"原文仍保留", "label":"SARS", "bind":["kind":"page-chars", "page":45, "text":"SARS"]]]]
let save: [String: Any] = ["mutationId":"save-1", "bookID":book, "operation":"notes", "value":notes, "expectedRevision":0]
check((try business.perform(save)["revision"] as? NSNumber)?.int64Value == 1, "native note write failed")
let writtenCursor = try writing.cursor()
check(writtenCursor == 4, "notes and three derived indexes must commit together")
check(try business.perform(save)["replayed"] as? Bool == true, "retry was not recognized")
check(try writing.cursor() == writtenCursor, "retry created another write")
let words = try read.state("word-bindings", bookID: book).payload as! [[String: Any]]
check(words.first?["cid"] as? String == "card-1" && words.first?["key"] as? String == "sars", "word index identity differs")
let placements = try read.state("card-placements", bookID: book).payload as! [[String: Any]]
check(placements.first?["entityIds"] as? [String] == ["card-1"], "placement confused with card ID")
var conflict = save; conflict["value"] = [["id":"changed"]]
do { _ = try business.perform(conflict); fatalError("mutation ID reused for different content") }
catch ReaderNativeBookStore.MutationError.replayConflict { }
var stale = save; stale["mutationId"] = "stale"
do { _ = try business.perform(stale); fatalError("stale snapshot overwrote notes") }
catch ReaderNativeDataStore.StoreError.revisionConflict { }
// Simulate a disk/constraint failure after the main note write but before the
// last derived index. No partial note, journal or receipt may survive.
try writing.execute("CREATE TRIGGER fail_words BEFORE UPDATE ON records WHEN NEW.collection = 'native-word-bindings' BEGIN SELECT RAISE(ABORT, 'simulated index failure'); END")
var failing = save; failing["mutationId"] = "fail"; failing["expectedRevision"] = 1
failing["value"] = [["id":"replacement"]]
do { _ = try business.perform(failing); fatalError("expected index failure") }
catch ReaderNativeDataStore.StoreError.sql { }
check(try read.state("document-notes-legacy", bookID: book).revision == 1 && writing.cursor() == writtenCursor, "partial commit survived failure")
try writing.execute("DROP TRIGGER fail_words")
check((try business.perform(failing)["revision"] as? NSNumber)?.int64Value == 2, "failed transaction poisoned retry")

let highlights: [[String: Any]] = [["id":"h1", "page":45, "text":"SARS"], ["id":"h2", "page":44, "text":"エボラ"]]
let make: [String: Any] = ["mutationId":"hl1", "bookID":book, "operation":"pdf-highlights", "value":highlights, "expectedRevision":0]
_ = try business.perform(make)
let item2 = "native-document-highlights-item-v1:\(book.utf16.count):\(book):h2"
var remove = make; remove["mutationId"] = "hl2"; remove["expectedRevision"] = 1; remove["value"] = [highlights[1]]
_ = try business.perform(remove)
check(try read.highlights("document-highlights", bookID: book).items.count == 1, "deleted highlight still live")
check(try writing.record(collection: "native-document-highlights-items", id: item2)?.rev == 1, "unchanged highlight was needlessly rewritten")
let item1 = "native-document-highlights-item-v1:\(book.utf16.count):\(book):h1"
let tombstone = try writing.record(collection: "native-document-highlights-items", id: item1)!
let recordJSON = try JSONSerialization.jsonObject(with: Data(tombstone.json.utf8)) as! [String: Any]
check(((recordJSON["value"] as? [String: Any])?["payload"] as? [String: Any])?["deleted"] as? Bool == true, "highlight deletion lost its tombstone")
print("Native book mutations: atomic indexes, journal, retry identity, stale-write rejection and tombstones passed")

let create: [String: Any] = ["bookID": book, "mutationId": "api-create", "operation": "note-api", "value": [
    "method": "POST", "body": ["file": "localbook:" + book, "id": "c_12345678", "anchor": ["kind":"pdf", "page":45],
        "html": ["cid":"card-sars", "content":"SARS 原卡", "bind":["kind":"page-chars", "page":45, "text":"SARS"]],
        "w":"300", "strokes":NSNull()]]]
let created = try business.perform(create)
let newNote = (created["result"] as! [String: Any])["note"] as! [String: Any]
check(newNote["id"] as? String == "c_12345678" && (newNote["w"] as? NSNumber)?.intValue == 300, "native API changed identity or legacy numeric width")
check((created["bindingChanges"] as? [[String: Any]])?.first?["after"] as? String == "sars", "word binding event missing")
let createCursor = try writing.cursor()
check(try business.perform(create)["replayed"] as? Bool == true && writing.cursor() == createCursor, "native API retry created another note")
func patchRequest(_ name: String, _ fields: [String: Any], method: String = "PATCH") -> [String: Any] {
    var body: [String: Any] = ["file":"localbook:" + book, "id":"c_12345678"]
    fields.forEach { body[$0.key] = $0.value }
    return ["bookID":book, "mutationId":name, "operation":"note-api", "value":["method":method, "body":body]]
}
_ = try business.perform(patchRequest("api-patch", ["text":"补充", "anchor":["kind":"pdf", "page":"u_abcd", "x":0.5]]))
let retained = (try read.state("document-notes-legacy", bookID: book).payload as! [[String: Any]]).first { $0["id"] as? String == "c_12345678" }!
check((retained["html"] as? [String: Any])?["content"] as? String == "SARS 原卡", "patch discarded unmodified content")
let badCursor = try writing.cursor()
do { _ = try business.perform(patchRequest("api-bad", ["anchor":["kind":"pdf", "page":true]])); fatalError("boolean page accepted") }
catch ReaderNativeNoteRules.NoteError.invalid { }
check(try writing.cursor() == badCursor, "invalid patch left a journal record")
let removed = try business.perform(patchRequest("api-delete", [:], method:"DELETE"))
check((removed["bindingChanges"] as? [[String: Any]])?.first?["before"] as? String == "sars", "deletion did not invalidate word binding")
check((try read.state("word-bindings", bookID:book).payload as! [[String: Any]]).isEmpty, "deleted note left a stale word index")
do { _ = try business.perform(patchRequest("api-missing", ["text":"x"])); fatalError("editing a deleted note recreated it") }
catch ReaderNativeNoteRules.NoteError.missing { }
print("Native note API: create/patch/delete, replay, validation, virtual pages and binding changes passed")
let otherBook = book + "-second"
let otherBusiness = ReaderNativeBookStore(store: writing, bookID: otherBook, deviceID: "test-device", now: { 100_000 })
var reused = save; reused["bookID"] = otherBook
_ = try otherBusiness.perform(reused)
check(try read.state("document-notes-legacy", bookID: otherBook).revision == 1, "same operation ID in another book was suppressed")
