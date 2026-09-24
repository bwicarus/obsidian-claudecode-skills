import Foundation

func testNativeAssistantEdits() throws {
    typealias Object = [String:Any]
    let db = try ReaderNativeDataStore(path: ":memory:"), book = "assistant-edits"
    let writer = ReaderNativeBookStore(store: db, bookID: book, deviceID: "test", now: { 500_000 })
    let projection = ReaderNativeBookProjection(store: db)
    func op(_ c: String) -> String { "npdf_" + String(repeating: c, count: 24) }
    func action(_ c: String, _ type: String, _ operation: String, _ items: [Object]) -> Object {
        ["fn":"_assistEdit","args":[["type":type,"op":operation,"native_operation_id":op(c),"file":"remote-file","items":items]]]
    }
    func commit(_ mutation: String, _ actions: [Object], high: Int = 0, notes: Int = 0) throws -> Object {
        let receipt = try writer.perform(["bookID":book,"mutationId":mutation,"operation":"assistant-actions",
            "value":["actions":actions,"expectedState":["revisions":["highlights":high,"notes":notes,"ink":9]]]])
        return receipt["result"] as! Object
    }
    func notes() throws -> [Object] { try projection.state("document-notes-legacy",bookID:book).payload as? [Object] ?? [] }
    let highlight: Object = ["id":"h_original","pdf_page":44,"rects":[[1,2,10,20]],"text":"接種","color":"#ff0","time":123]
    let note: Object = ["id":"n_original","anchor":["kind":"pdf","page":44],"text":"original","color":"#fff","created":123,"updated":123,
        "html":["cid":"card_original","content":"<b>original</b>","bind":["kind":"page-chars","page":44,"text":"接種"]]]
    let batch = [action("1","highlight","",[highlight]),action("2","note","create",[["id":"n_original","note":note]])]
    try db.execute("CREATE TRIGGER fail_assistant BEFORE INSERT ON records WHEN NEW.collection = 'native-pdf-assistant-ops' BEGIN SELECT RAISE(ABORT, 'receipt failure'); END")
    do { _ = try commit("first",batch); fatalError("partial assistant transaction accepted") }
    catch ReaderNativeDataStore.StoreError.sql { }
    check(try db.cursor() == 0 && notes().isEmpty && projection.highlights("document-highlights",bookID:book).items.isEmpty, "assistant failure leaked data or journal")
    try db.execute("DROP TRIGGER fail_assistant")
    let saved = try commit("first",batch)
    check((saved["revisions"] as? Object)?["ink"] as? Int == 9, "unrelated authority revision lost")
    check(try notes()[0]["id"] as? String == "n_original", "assistant replaced stable note identity")
    check(try (notes()[0]["created"] as? NSNumber)?.intValue == 123, "assistant changed original timestamp")
    check(try (projection.state("word-bindings",bookID:book).payload as? [Object])?.first?["cid"] as? String == "card_original", "assistant skipped derived word binding")
    let shown = (saved["actions"] as! [Object])[0]["args"] as! [Object]
    check(shown[0]["file"] as? String == "localbook:" + book, "UI received remote file identity")
    let beforeReplay = try db.cursor()
    check(try commit("retry",batch)["replayed"] as? Bool == true, "semantic retry duplicated assistant work")
    check(try db.cursor() == beforeReplay, "semantic retry wrote records")
    var changed = highlight; changed["text"] = "different"
    do { _ = try commit("collision",[action("1","highlight","",[changed])],high:1,notes:1); fatalError("same ID with different edits accepted") }
    catch let e as ReaderNativeAssistantEdits.Failure { check(e.conflict,"wrong collision failure") }
    let edit = action("3","note","edit",[["id":"n_original","old":["text":"original","color":"#fff"],"new":["text":"first","color":"#fff"]],
        ["id":"n_original","old":["text":"first","color":"#fff"],"new":["text":"second","color":"#fff"]]])
    _ = try commit("edit",[edit],high:1,notes:1)
    _ = try commit("undo-edit",[["fn":"_nativePDFUndoLast","args":[op("4")]]],high:1,notes:2)
    check(try notes()[0]["text"] as? String == "original", "reverse edit order did not restore original")
    _ = try commit("undo-note",[["fn":"_nativePDFUndoLast","args":[op("5")]]],high:1,notes:3)
    check(try notes().isEmpty, "prior undo revision was not advanced")
    _ = try commit("undo-highlight",[["fn":"_nativePDFUndoLast","args":[op("6")]]],high:1,notes:4)
    check(try projection.highlights("document-highlights",bookID:book).items.isEmpty, "highlight undo did not remove original ID")
    do { _ = try commit("stale",[action("7","highlight","",[highlight])],high:1,notes:4); fatalError("stale revision accepted") }
    catch let e as ReaderNativeAssistantEdits.Failure { check(e.conflict,"wrong stale failure") }
    do { _ = try commit("duplicate",[action("8","highlight","",[highlight,highlight])],high:2,notes:4); fatalError("duplicate action IDs accepted") }
    catch let e as ReaderNativeAssistantEdits.Failure { check(e.conflict,"wrong duplicate failure") }
    check(try projection.highlights("document-highlights",bookID:book).items.isEmpty, "rejected actions changed the book")
    print("Native assistant edits: stable IDs, atomic rollback, semantic replay, CAS and undo chain passed")
}

func check(_ condition: Bool, _ message: String) {
    if !condition { fatalError(message) }
}
try testNativeAssistantEdits()
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

let queueStore = try ReaderNativeDataStore(path: ":memory:")
let queue = ReaderNativeBookStore(store: queueStore, bookID: book, deviceID: "device-native", now: { 123456 },
                                displayName: "原生书籍", contentSHA256: String(repeating: "a", count: 64))
let enqueue: [String: Any] = ["bookID": book, "mutationId": "enqueue-1", "operation": "replication-enqueue",
    "value": ["url":"/pdf/api/notes", "method":"POST", "body":["file":"localbook:" + book, "id":"c_12345678"]]]
// A failure at the second half must roll back link, pair, counter and command.
try queueStore.execute("CREATE TRIGGER fail_queue BEFORE INSERT ON records WHEN NEW.collection = 'native-replication-outbox' BEGIN SELECT RAISE(ABORT, 'queue failed'); END")
do { _ = try queue.perform(enqueue); fatalError("outbox disk failure was hidden") } catch ReaderNativeDataStore.StoreError.sql { }
check(try ReaderNativeBookProjection(store:queueStore).state("replication-link", bookID:book).payload == nil, "unannounced replication link survived rollback")
check(try queueStore.cursor() == 0 && queueStore.meta("nativeReplicationSequence") == nil, "partial outbox survived rollback")
try queueStore.execute("DROP TRIGGER fail_queue")
_ = try queue.perform(enqueue)
func envelopes() throws -> [[String: Any]] {
    try queueStore.records(collection:"native-replication-outbox", idPrefix:book + ":").sorted { $0.id < $1.id }.map { record in
        let row = try JSONSerialization.jsonObject(with:Data(record.json.utf8)) as! [String:Any]
        return ((row["value"] as! [String:Any])["payload"] as! [String:Any])["envelope"] as! [String:Any]
    }
}
let messages = try envelopes()
check(messages.count == 2, "first command must include exactly one pair announcement")
let pairOp = messages[0]["op"] as! [String:Any], noteOp = messages[1]["op"] as! [String:Any]
check(pairOp["url"] as? String == "/replication/pair" && noteOp["url"] as? String == "/pdf/api/notes", "pair must precede command")
check((pairOp["body"] as! [String:Any])["contentSha256"] as? String == String(repeating:"a", count:64), "full-content identity omitted")
check(messages[0]["replicationBookId"] as? String == messages[1]["replicationBookId"] as? String, "pair and command identities differ")
check(try queue.perform(enqueue)["replayed"] as? Bool == true && envelopes().count == 2, "retry duplicated outbox command")
var second = enqueue; second["mutationId"] = "enqueue-2"
_ = try queue.perform(second)
check(try envelopes().count == 3, "existing link was paired again")
var forbidden = enqueue; forbidden["mutationId"] = "forbidden"; forbidden["value"] = ["url":"https://example.com", "method":"POST", "body":[:]]
do { _ = try queue.perform(forbidden); fatalError("arbitrary replication route accepted") } catch ReaderNativeBookStore.MutationError.invalid { }
check(try envelopes().count == 3, "invalid command changed outbox")
print("Native replication enqueue: atomic pairing, crash rollback, ordering and stable retry identities passed")

let inkStore = try ReaderNativeDataStore(path:":memory:")
var inkTime: Int64 = 200_000
let inkWriter = ReaderNativeBookStore(store:inkStore,bookID:book,deviceID:"pencil",now:{inkTime})
let inkReader = ReaderNativeBookProjection(store:inkStore)
func inkRequest(_ id:String, _ action:String, _ extra:[String:Any] = [:]) -> [String:Any] {
    var input: [String:Any] = ["action":action,"opId":id,"page":45,
        "segments":[["surfaceId":"page:45","points":[[0.1,0.1],[0.2,0.2]],"width":4,"color":"#123456"]]]
    extra.forEach { input[$0.key] = $0.value }
    return ["bookID":book,"mutationId":id,"operation":"ink-operation","value":input]
}
func inkCount() throws -> Int { (try inkReader.state("ink",bookID:book).payload as? [String:[[String:Any]]])?["45"]?.count ?? 0 }
// Ink, undo and pending sync are committed together, not before persistence.
try inkStore.execute("CREATE TRIGGER fail_ink BEFORE INSERT ON records WHEN NEW.collection = 'native-ink' BEGIN SELECT RAISE(ABORT, 'ink failed'); END")
let pen = inkRequest("pen-1","commit")
do { _ = try inkWriter.perform(pen); fatalError("ink disk failure swallowed") } catch ReaderNativeDataStore.StoreError.sql { }
check(try inkStore.cursor() == 0 && inkWriter.nextInkSyncTime() == nil, "failed ink left history or pending sync")
try inkStore.execute("DROP TRIGGER fail_ink")
_ = try inkWriter.perform(pen)
check(try inkCount() == 1 && inkWriter.nextInkSyncTime() == 260_000, "pen not durable or quiet time wrong")
let inkCursor = try inkStore.cursor()
check(try inkWriter.perform(pen)["replayed"] as? Bool == true && inkStore.cursor() == inkCursor, "Pencil retry duplicated a stroke")
_ = try inkWriter.perform(inkRequest("undo-1","undo"))
check(try inkCount() == 0, "native undo did not restore previous page")
_ = try inkWriter.perform(inkRequest("redo-1","redo"))
check(try inkCount() == 1, "native redo lost a stroke")
_ = try inkWriter.perform(inkRequest("region-1","createRegion",["regionId":"r1","segments":[
    ["surfaceId":"page:45","points":[[0.5,0.5],[0.8,0.5],[0.8,0.8],[0.5,0.8]]]]]))
check(try inkCount() == 2, "region not saved")
_ = try inkWriter.perform(inkRequest("erase-1","erase",["segments":[["surfaceId":"page:45","points":[[0.6,0.6]]]]]))
check(try inkCount() == 1, "polygon interior was not erased")
_ = try inkWriter.perform(["bookID":book,"mutationId":"early-flush","operation":"ink-sync","value":[:]])
check(try inkStore.records(collection:"native-replication-outbox",idPrefix:book + ":").isEmpty, "ink synced before quiet period")
inkTime = 270_000
// A failed outbox write retains the pending page for later retry.
try inkStore.execute("CREATE TRIGGER fail_send BEFORE INSERT ON records WHEN NEW.collection = 'native-replication-outbox' BEGIN SELECT RAISE(ABORT, 'send failed'); END")
let flush: [String:Any] = ["bookID":book,"mutationId":"flush","operation":"ink-sync","value":[:]]
do { _ = try inkWriter.perform(flush); fatalError("outbox failure swallowed") } catch ReaderNativeDataStore.StoreError.sql { }
check(try inkWriter.nextInkSyncTime() != nil, "failed flush lost pending ink")
try inkStore.execute("DROP TRIGGER fail_send")
_ = try inkWriter.perform(flush)
check(try inkWriter.nextInkSyncTime() == nil, "successful flush retained pending marker")
let inkMessages = try inkStore.records(collection:"native-replication-outbox",idPrefix:book + ":")
check(inkMessages.count == 2, "quiet period did not coalesce pen, undo, redo and erase into one page update plus pair")
let currentInk = try inkReader.state("ink",bookID:book)
_ = try inkWriter.perform(["bookID":book,"mutationId":"remote-update","operation":"ink","expectedRevision":currentInk.revision,
    "value":["45":[["t":"pen","p":[[0.9,0.9]]]]]])
_ = try inkWriter.perform(inkRequest("stale-undo","undo"))
check(try inkCount() == 1, "old undo overwrote externally replaced strokes")
print("Native Pencil: atomic save, retry, undo/redo, erasure, deferred outbox and stale history checks passed")

let noteStore = try ReaderNativeDataStore(path:":memory:")
let noteWriter = ReaderNativeBookStore(store:noteStore,bookID:book,deviceID:"cards",now:{300_000})
let noteRead = ReaderNativeBookProjection(store:noteStore)
let originalNote: [String:Any] = ["id":"c_12345678","anchor":["kind":"pdf","page":45,"x":0.2,"y":0.2],"w":260,"h":180,
    "html":["cid":"source-id","content":"原卡片正文","bind":["kind":"page-chars","page":45,"from":0,"to":3,"text":"SARS"]]]
_ = try noteWriter.perform(["bookID":book,"mutationId":"seed","operation":"notes","expectedRevision":0,"value":[originalNote]])
func noteAction(_ id:String,_ input:[String:Any]) -> [String:Any] {
    var value = input; value["id"] = "c_12345678"; value["opId"] = id
    return ["bookID":book,"mutationId":id,"operation":"note-operation","value":value]
}
let moved = try noteWriter.perform(noteAction("move",["action":"update","changes":["anchor":["page":44,"x":0.4,"y":0.3],"bind":NSNull(),"form":"min"]]))
let movedNote = (moved["result"] as! [String:Any])["note"] as! [String:Any]
let html = movedNote["html"] as! [String:Any]
check(html["cid"] as? String == "source-id" && html["content"] as? String == "原卡片正文", "moving a card changed its content/identity")
check((html["bind"] as! [String:Any])["page"] as? Int == 45 && html["form"] as? String == "full", "missing new geometry unbound the card or allowed a pinned min form")
let geo = String(decoding:try JSONSerialization.data(withJSONObject:ReaderNativeNoteActions.geometry(movedNote),options:.sortedKeys),as:UTF8.self)
let drawNote = noteAction("draw-note",["action":"ink","kind":"commit","geometry":geo,"aspectRatio":1.4,
    "segments":[["points":[[0.1,0.1],[0.2,0.2]],"width":2]]])
_ = try noteWriter.perform(drawNote)
let noteCursor = try noteStore.cursor()
check(try noteWriter.perform(drawNote)["replayed"] as? Bool == true && noteStore.cursor() == noteCursor, "card Pencil retry duplicated its sync command")
_ = try noteWriter.perform(noteAction("resize",["action":"update","changes":["w":320,"h":200]]))
var staleDraw = drawNote; staleDraw["mutationId"] = "draw-stale"
do { _ = try noteWriter.perform(staleDraw); fatalError("stale card geometry accepted") } catch ReaderNativeNoteRules.NoteError.invalid { }
// Outbox failure must roll back the card edit and all derived indexes.
try noteStore.execute("CREATE TRIGGER fail_card_send BEFORE INSERT ON records WHEN NEW.collection = 'native-replication-outbox' BEGIN SELECT RAISE(ABORT, 'send failed'); END")
let beforeFailedMove = try noteRead.state("document-notes-legacy",bookID:book).revision
do { _ = try noteWriter.perform(noteAction("failed-move",["action":"update","changes":["w":600,"h":400]])); fatalError("card outbox failure hidden") }
catch ReaderNativeDataStore.StoreError.sql { }
check(try noteRead.state("document-notes-legacy",bookID:book).revision == beforeFailedMove, "card edit survived failed outbox commit")
print("Native card actions: stable identity, bind preservation, geometry fencing, retry and atomic replication passed")

let highlightStore = try ReaderNativeDataStore(path:":memory:")
let highlightWriter = ReaderNativeBookStore(store:highlightStore,bookID:book,deviceID:"highlights",now:{400_000})
let highlightRead = ReaderNativeBookProjection(store:highlightStore)
let highlightBody: [String:Any] = ["file":"localbook:" + book,"id":"c_abcddcba","page":45,"rects":[[30.123,40.125,10.1,20.0]],
    "text":"原文","sentence":"所在句","color":"#fff59d","page_w":600,"page_h":800]
func highlightRequest(_ id: String, _ input: [String:Any], edit: Bool = false) -> [String:Any] {
    ["bookID":book,"mutationId":id,"operation":edit ? "highlight-edit" : "highlight-api","value":input]
}
let addHighlight = highlightRequest("add-highlight",["method":"POST","body":highlightBody,"assistant":true])
try highlightStore.execute("CREATE TRIGGER fail_highlight_send BEFORE INSERT ON records WHEN NEW.collection = 'native-replication-outbox' BEGIN SELECT RAISE(ABORT, 'send failed'); END")
do { _ = try highlightWriter.perform(addHighlight); fatalError("highlight outbox failure swallowed") } catch ReaderNativeDataStore.StoreError.sql { }
check(try highlightRead.highlights("document-highlights",bookID:book).items.isEmpty, "highlight survived failed commit")
check(try highlightRead.state("pdf-assistant-undo",bookID:book).payload == nil, "undo survived rolled back highlight")
try highlightStore.execute("DROP TRIGGER fail_highlight_send")
let highlightResult = try highlightWriter.perform(addHighlight)["result"] as! [String:Any]
let savedHighlight = highlightResult["highlight"] as! [String:Any]
check(savedHighlight["rects"] as? [[Double]] == [[10.1,20,30.12,40.13]], "PDF rectangle normalization drift")
let highlightCursor = try highlightStore.cursor()
_ = try highlightWriter.perform(addHighlight)
var retryHighlight = addHighlight; retryHighlight["mutationId"] = "new-transport-id"
let retriedHighlight = try highlightWriter.perform(retryHighlight)
check((retriedHighlight["result"] as! [String:Any])["replayed"] as? Bool == true, "assistant stable creation was not deduplicated")
check(try highlightStore.cursor() == highlightCursor, "highlight retry re-enqueued replication")
check((try highlightRead.state("pdf-assistant-undo",bookID:book).payload as! [[String:Any]]).count == 1, "duplicate undo entry")
// A new transport request with the same creation ID may not overwrite content.
var changedHighlight = highlightBody; changedHighlight["text"] = "changed"
do { _ = try highlightWriter.perform(highlightRequest("conflict",["method":"POST","body":changedHighlight,"assistant":true])); fatalError("conflicting creation overwrote highlight") }
catch ReaderNativeHighlightRules.HighlightError.conflict { }
let cleared = try highlightWriter.perform(highlightRequest("clear-color",["id":"c_abcddcba","op":"color","value":""],edit:true))
check((cleared["result"] as! [String:Any])["deleted"] as? Bool != true, "color removal erased sentence context")
let recolored = try highlightWriter.perform(highlightRequest("blue",["id":"c_abcddcba","op":"color","value":"blue"],edit:true))
let recoloredBody = (recolored["result"] as! [String:Any])["highlight"] as! [String:Any]
check(recoloredBody["color"] as? String == "#a3d4ff", "editor persisted an invalid named color")
check(recoloredBody["rects"] as? [[Double]] == savedHighlight["rects"] as? [[Double]], "editing changed highlight geometry")
_ = try highlightWriter.perform(highlightRequest("delete-highlight",["id":"c_abcddcba","op":"delete"],edit:true))
check(try highlightRead.highlights("document-highlights",bookID:book).items.isEmpty, "deleted native highlight still visible")
do { _ = try highlightWriter.perform(highlightRequest("deleted-replay",["method":"POST","body":highlightBody,"assistant":true])); fatalError("deleted creation silently resurrected") }
catch ReaderNativeHighlightRules.HighlightError.conflict { }
let pendingReceipts: [[String:Any]] = [["id":"pending","contract":"reader-native-page-card-action/1","state":"preparing"]]
    + (0..<200).map { ["id":"r\($0)","ts":$0] }
let boundedReceipts = try ReaderNativeHighlightRules.boundedReceipts(pendingReceipts)
check(boundedReceipts.count == 160 && boundedReceipts.first?["id"] as? String == "pending", "receipt pruning lost interrupted recovery authority")
print("Native highlights: atomic undo/outbox, stable retry, field-preserving edits, color removal and tombstones passed")

let outbound = ReaderNativeReplicationOutbox(store:highlightStore)
let firstPending = try outbound.pending(limit:1).first!
let outboundCursor = try highlightStore.cursor()
try outbound.acknowledge(firstPending,mutationID:"wrong",outcome:"accepted",now:500_000)
try outbound.acknowledge(firstPending,mutationID:firstPending.mutationID,outcome:"partial",now:500_000)
check(try highlightStore.cursor() == outboundCursor, "wrong/partial acknowledgment consumed an envelope")
try outbound.acknowledge(firstPending,mutationID:firstPending.mutationID,outcome:"accepted",now:500_000)
let ackCursor = try highlightStore.cursor()
try outbound.acknowledge(firstPending,mutationID:firstPending.mutationID,outcome:"accepted",now:500_001)
check(try highlightStore.cursor() == ackCursor, "ack retry created another tombstone")
check(try outbound.pending(limit:1).first?.row.id != firstPending.row.id, "tombstone hid later pending commands under LIMIT")
let remaining = try outbound.pending()
for entry in remaining { try outbound.acknowledge(entry,mutationID:entry.mutationID,outcome:"accepted",now:500_002) }
check(try outbound.pending().isEmpty, "native queue retained accepted commands")
check(try highlightStore.record(collection:ReaderNativeReplicationOutbox.collection,id:firstPending.row.id)?.deleted == true, "ack hard-deleted record instead of retaining tombstone")
print("Native replication queue: exact acknowledgments, retry safety and live reads past tombstones passed")

let positionStore = try ReaderNativeDataStore(path:":memory:")
let positionWriter = ReaderNativeBookStore(store:positionStore,bookID:book,deviceID:"viewport",now:{600_000})
let positionRead = ReaderNativeBookProjection(store:positionStore)
var viewport:[String:Any] = ["page":45,"fraction":0.37,"scale":1.25,"mode":"continuous","spreadOffset":0,"cropEnabled":false]
func savePosition(_ id:String) throws {
    _ = try positionWriter.perform(["bookID":book,"mutationId":id,"operation":"pdf-position","value":viewport])
}
try savePosition("first-position")
let positionQueueCount = try ReaderNativeReplicationOutbox(store:positionStore).pending().count
viewport["fraction"] = 0.81
try savePosition("scroll-position")
check(try ReaderNativeReplicationOutbox(store:positionStore).pending().count == positionQueueCount,"scrolling same page re-enqueued reading position")
let restoredViewport = try ReaderNativeReadingPosition.restore(store:positionStore,bookID:book,total:90)!
check(restoredViewport["page"] as? Int == 45 && restoredViewport["fraction"] as? Double == 0.81,"native continuation lost page fraction")
try positionStore.execute("CREATE TRIGGER fail_position_send BEFORE INSERT ON records WHEN NEW.collection = 'native-replication-outbox' BEGIN SELECT RAISE(ABORT, 'send failed'); END")
viewport["page"] = 46
do { try savePosition("failed-position"); fatalError("position outbox failure swallowed") } catch ReaderNativeDataStore.StoreError.sql { }
check((try positionRead.state("reading-position",bookID:book).payload as! [String:Any])["pos"] as? Int == 45,"page changed after failed commit")
check((try positionRead.state("pdf-viewport",bookID:book).payload as! [String:Any])["page"] as? Int == 45,"viewport survived rolled back position")
_ = try positionWriter.perform(["bookID":book,"mutationId":"remote-position","operation":"reading-position","value":["kind":"pdf","pos":100,"ts":700]])
let clampedPosition = try ReaderNativeReadingPosition.restore(store:positionStore,bookID:book,total:90)!
check(clampedPosition["page"] as? Int == 90 && clampedPosition["fraction"] as? Int == 0,"remote page reused previous page fraction or exceeded document")
print("Native reading position: durable continuation, scroll coalescing, atomic replication and remote-page arbitration passed")

let createStore = try ReaderNativeDataStore(path: ":memory:")
let createWriter = ReaderNativeBookStore(store: createStore, bookID: book, deviceID: "create", now: { 700_000 })
let createRead = ReaderNativeBookProjection(store: createStore)
let stickyID = "c_" + String(repeating: "a", count: 32)
let stickyBody: [String: Any] = ["file": "localbook:" + book, "id": stickyID,
    "anchor": ["kind": "pdf", "page": 3, "x": 0.5, "y": 0.5], "color": "#ffffff", "w": 260, "h": 180]
let stickyRequest: [String: Any] = ["bookID": book, "mutationId": "native-create", "operation": "note-create",
    "value": ["method": "POST", "body": stickyBody]]
try createStore.execute("CREATE TRIGGER fail_note_send BEFORE INSERT ON records WHEN NEW.collection = 'native-replication-outbox' BEGIN SELECT RAISE(ABORT, 'send failed'); END")
do { _ = try createWriter.perform(stickyRequest); fatalError("creation outbox failure swallowed") } catch ReaderNativeDataStore.StoreError.sql {}
check(try createRead.state("document-notes-legacy", bookID: book).payload == nil, "created note survived failed transaction")
check(try createStore.cursor() == 0, "creation failure left partial index writes")
try createStore.execute("DROP TRIGGER fail_note_send")
let createdReceipt = try createWriter.perform(stickyRequest)
check((createdReceipt["result"] as? [String: Any])?["id"] as? String == stickyID, "local creation changed stable note ID")
let createdNotes = try createRead.state("document-notes-legacy", bookID: book).payload as! [[String: Any]]
check(createdNotes.count == 1 && createdNotes[0]["color"] as? String == "#ffffff", "native default note differs from original")
let creationPending = try ReaderNativeReplicationOutbox(store: createStore).pending()
check(creationPending.count == 2, "first creation must queue pairing and exactly one note command")
let creationPaths = try creationPending.map { entry -> String in
    let envelope = try JSONSerialization.jsonObject(with: entry.envelope) as! [String: Any]
    return (envelope["op"] as! [String: Any])["url"] as! String
}
check(creationPaths == ["/replication/pair", "/pdf/api/notes"], "pairing must precede the only creation command")
let sentCreation = String(decoding: creationPending[1].envelope, as: UTF8.self)
check(sentCreation.contains(stickyID) && sentCreation.contains("/pdf/api/notes"), "outgoing note lost its local identity")
let creationCursor = try createStore.cursor()
_ = try createWriter.perform(stickyRequest)
check(try createStore.cursor() == creationCursor, "creation replay created a second note or queued twice")
var collision = stickyRequest; collision["mutationId"] = "collision"
do { _ = try createWriter.perform(collision); fatalError("same note ID replaced on creation") } catch ReaderNativeBookStore.MutationError.invalid {}
print("Native note creation: stable ID, white default, atomic replication, rollback and replay passed")

let settingsStore = try ReaderNativeDataStore(path: ":memory:")
let settingsWriter = ReaderNativeBookStore(store: settingsStore, bookID: book, deviceID: "settings", now: { 800_000 })
let settingsRead = ReaderNativeBookProjection(store: settingsStore)
let languageRequest: [String: Any] = ["bookID": book, "mutationId": "languages", "operation": "book-languages",
    "expectedRevision": 0, "value": ["ja", "en", "ja"]]
_ = try settingsWriter.perform(languageRequest)
check(try settingsRead.state("book-languages", bookID: book).payload as? [String] == ["ja", "en"], "languages changed order or failed deduplication")
let settingsCursor = try settingsStore.cursor()
_ = try settingsWriter.perform(languageRequest)
check(try settingsStore.cursor() == settingsCursor, "language retry wrote twice")
let cropRequest: [String: Any] = ["bookID": book, "mutationId": "crop", "operation": "book-crop",
    "expectedRevision": 0, "value": ["l": 1.25, "r": 2.5, "t": 3, "b": 4]]
try settingsStore.execute("CREATE TRIGGER fail_settings BEFORE INSERT ON journal BEGIN SELECT RAISE(ABORT, 'journal failed'); END")
do { _ = try settingsWriter.perform(cropRequest); fatalError("crop journal error swallowed") } catch ReaderNativeDataStore.StoreError.sql {}
check(try settingsRead.state("book-crop", bookID: book).payload == nil, "failed crop persisted")
try settingsStore.execute("DROP TRIGGER fail_settings")
_ = try settingsWriter.perform(cropRequest)
check(try (settingsRead.state("book-crop", bookID: book).payload as? [String: Double])?["l"] == 1.25, "crop fraction lost")
var staleCrop = cropRequest; staleCrop["mutationId"] = "crop-stale"
do { _ = try settingsWriter.perform(staleCrop); fatalError("stale crop accepted") } catch ReaderNativeDataStore.StoreError.revisionConflict {}
var invalidCrop = cropRequest; invalidCrop["mutationId"] = "crop-bad"; invalidCrop["expectedRevision"] = 1
invalidCrop["value"] = ["l": 45, "r": 45, "t": 0, "b": 0]
do { _ = try settingsWriter.perform(invalidCrop); fatalError("empty crop accepted") } catch ReaderNativeBookStore.MutationError.invalid {}
let otherSettings = ReaderNativeBookProjection(store: settingsStore)
check(try otherSettings.state("book-crop", bookID: "different-book").payload == nil, "book settings leaked")
print("Native book settings: language order, fractional crop, revision conflict, replay and rollback passed")

let originalVideo: [String:Any] = ["id":"video-note", "anchor":["kind":"pdf","page":1,"x":20,"y":30],
    "video":["id":"dQw4w9WgXcQ","src":"yt","title":"original","start":2,"end":19,"cc":true],"strokes":[]]
let videoChange = try ReaderNativeNoteActions.request(["id":"video-note","action":"video",
    "changes":["id":"dQw4w9WgXcQ","start":8,"loop":true]],note:originalVideo,file:book,now:900)
let changedVideo = videoChange.body["video"] as! [String:Any]
check(changedVideo["start"] as? Int == 8 && changedVideo["end"] as? Int == 19 && changedVideo["src"] as? String == "yt", "video patch destroyed prior playback fields")
do { _ = try ReaderNativeNoteActions.request(["id":"video-note","action":"video","changes":["id":"different"]],note:originalVideo,file:book,now:900); fatalError("stale video replaced") }
catch ReaderNativeNoteRules.NoteError.invalid {}
