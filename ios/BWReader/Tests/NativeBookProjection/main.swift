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
