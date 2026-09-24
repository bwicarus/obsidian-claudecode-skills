import Foundation

typealias O = [String:Any]
func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
let db = try ReaderNativeDataStore(path:":memory:"), global = try ReaderNativeDataStore(path:":memory:")
let writer = ReaderNativeBookStore(store:db,bookID:"book",deviceID:"test",now:{ 900_000 })
let repository = ReaderNativeCardRepository(store:global,deviceID:"test",now:{ 900_000 })
let engine = ReaderNativePageCardActions(book:writer,repository:repository), projection = ReaderNativeBookProjection(store:db)
let cardID = "card_" + String(repeating:"a",count:24), placement = "placement_original"
let entity = try repository.perform(["operation":"registerDraft","mutationId":"seed","arguments":[[
    "id":cardID,"cid":cardID,"gid":cardID,"cards":[["type":"basic","front":"original","back":"answer","deck":"Japanese","tags":["medical"],"reason":"selected"]],"source":["kind":"selection","documentId":"localbook:book"]]]])["result"] as! O
let raw: O = ["id":placement,"anchor":["kind":"pdf","page":7,"x":0.2,"y":0.3],"created":100,"updated":100,
    "card":["id":cardID,"cid":cardID,"gid":cardID,"cards":entity["cards"]!,"bind":["kind":"page-chars","page":7,"from":2,"to":3,"text":"word"],"contextText":"original"]]
let before = try ReaderNativeAssistantEdits.note(raw,fallbackID:placement,file:"localbook:book",now:900_000)
_ = try writer.perform(["bookID":"book","mutationId":"seed-note","operation":"notes","expectedRevision":0,"value":[before]])
func note() throws -> O? { (try projection.state("document-notes-legacy",bookID:"book").payload as? [O])?.first }
func receipts() throws -> [O] { try projection.state("pdf-assistant-ops",bookID:"book").payload as? [O] ?? [] }
func command(_ c: String, operation: String = "edit", before: O, expected: Int64, numbered: Bool = true) throws -> O {
    var after = before, card = before["card"] as! O
    let faces: [O] = [["type":"basic","front":"changed","back":"new answer"]]
    card["cards"] = faces; card["contextText"] = "changed new answer"; after["card"] = card; after["updated"] = 900
    let number: Any = numbered ? 1 : NSNull()
    let plan: O = ["id":placement,"page":7,"number":number,"expectedRevision":expected,"before":before,
        "after":operation == "edit" ? after as Any : NSNull(),"replacementCards":operation == "edit" ? faces as Any : NSNull(),"canonicalId":cardID]
    return ["operation":"apply","operationId":"pcard_" + String(repeating:c,count:24),"kind":"page-card-" + operation,
        "fingerprint":"fingerprint-" + c,"requestFingerprint":"request-" + c,"plan":plan,
        "expectedState":["revisions":["notes":expected]]]
}
let apply = try command("1",before:before,expected:1)
// Fail the book half AFTER the canonical entity has changed. The persisted
// intent must survive and recovery must not repeat the semantic entity edit.
try db.execute("CREATE TRIGGER fail_page_note BEFORE INSERT ON records WHEN NEW.collection = 'native-document-notes-legacy' BEGIN SELECT RAISE(ABORT,'page write failed'); END")
do { _ = try engine.perform(apply); fatalError("partial saga reported success") }
catch ReaderNativeDataStore.StoreError.sql { }
check(try receipts().first?["state"] as? String == "preparing", "lost recovery intent")
check(try ReaderNativeCardRules.same(note()!,before),"failed placement write changed the book")
let changedEntity = try repository.perform(["operation":"load","arguments":[cardID]])["result"] as! O
let canonicalRevision = changedEntity["entityRev"] as! Int64
check((changedEntity["cards"] as? [O])?.first?["front"] as? String == "changed", "fault was not after entity commit")
check((changedEntity["cards"] as? [O])?.first?["deck"] as? String == "Japanese", "entity metadata lost")
try db.execute("DROP TRIGGER fail_page_note")
_ = try engine.perform(["operation":"recover"])
check(try receipts().first?["state"] as? String == "done", "recovery did not finish placement")
let recoveredEntity = try repository.perform(["operation":"load","arguments":[cardID]])["result"] as! O
check(recoveredEntity["entityRev"] as? Int64 == canonicalRevision, "recovery wrote entity twice")
check(try (note()?["card"] as? O)?["cards"] is [O],"recovery removed card faces")
let cursor = try db.cursor()
check((try engine.perform(apply)["result"] as? O)?["replayed"] as? Bool == true,"replay not recognized")
check(try db.cursor() == cursor,"replay wrote book again")
let id = apply["operationId"] as! String
_ = try engine.perform(["operation":"transition","operationId":id,"action":"undo"])
check(try ReaderNativeCardRules.same(note()!,before),"undo did not restore exact geometry/identity/content")
_ = try engine.perform(["operation":"transition","operationId":id,"action":"redo"])
check(try (note()?["card"] as? O)?["cards"] is [O],"redo lost card")
// A stable identity may cross an unrelated list revision; numbered requests may not.
let current = try note()!, revision = try projection.state("document-notes-legacy",bookID:"book").revision
var deletion = try command("2",operation:"delete",before:current,expected:revision-1)
do { _ = try engine.perform(deletion); fatalError("stale number accepted") }
catch let e as ReaderNativeAssistantEdits.Failure { check(e.conflict,"wrong stale-number error") }
deletion = try command("2",operation:"delete",before:current,expected:revision-1,numbered:false)
_ = try engine.perform(deletion)
check(try note() == nil,"delete did not remove placement")
_ = try engine.perform(["operation":"transition","operationId":deletion["operationId"]!,"action":"undo"])
check(try ReaderNativeCardRules.same(note()!,current),"delete undo lost original card")
var collision = apply; collision["fingerprint"] = "changed"
do { _ = try engine.perform(collision); fatalError("operation ID reused for different content") }
catch let e as ReaderNativeAssistantEdits.Failure { check(e.conflict,"wrong replay error") }
// Do not overwrite a newer user edit while undoing an old operation.
var newer = try note()!; newer["text"] = "new user note"
_ = try writer.perform(["bookID":"book","mutationId":"user-change","operation":"notes","expectedRevision":try projection.state("document-notes-legacy",bookID:"book").revision,"value":[newer]])
do { _ = try engine.perform(["operation":"transition","operationId":id,"action":"undo"]); fatalError("undo overwrote user edit") }
catch let e as ReaderNativeAssistantEdits.Failure { check(e.conflict,"wrong concurrent-edit error") }
_ = try engine.perform(["operation":"recover"])
check(try note()?["text"] as? String == "new user note","recovery overwrote user edit")
check(try receipts().first?["state"] as? String == "conflicted","conflicting recovery remained pending forever")
// Exercise the actual command path, rather than trusting a prepared test plan.
let directRevision = try projection.state("document-notes-legacy",bookID:"book").revision
let directInput: O = ["operation":"edit","operationId":"pcard_" + String(repeating:"3",count:24),"expectedId":placement,"expectedRevision":directRevision,"number":1,
    "replacement":["cards":[["type":"basic","front":"native command","back":"kept identity"]]]]
let pageView: O = ["contract":"reader-local-page-card-projection/1","page":7,"revision":directRevision,"cards":[["id":placement,"number":1]]]
_ = try engine.perform(["operation":"direct","input":directInput,"projection":pageView])
let directCard = try repository.perform(["operation":"load","arguments":[cardID]])["result"] as! O
check((directCard["cards"] as? [O])?.first?["front"] as? String == "native command","direct edit missed canonical entity")
check((directCard["cards"] as? [O])?.first?["deck"] as? String == "Japanese","direct edit discarded existing metadata")
let replayCursor = try db.cursor()
check((try engine.perform(["operation":"direct","input":directInput,"projection":NSNull()])["result"] as? O)?["replayed"] as? Bool == true,"exact replay still depends on current page projection")
check(try db.cursor() == replayCursor,"direct replay committed twice")
var changedRequest = directInput; changedRequest["replacement"] = ["cards":[["type":"basic","front":"different","back":"value"]]]
do { _ = try engine.perform(["operation":"direct","input":changedRequest,"projection":pageView]); fatalError("direct operation collision accepted") }
catch let e as ReaderNativeAssistantEdits.Failure { check(e.conflict,"wrong direct collision error") }
print("Native page-card saga: atomic placement, interrupted entity commit recovery, replay, undo/redo, metadata and conflicts passed")

// Assistant preparation uses original character indexes and the same marker
// order as the native page, without rendering or querying a web text layer.
let characterSource: O = ["pageWidth":300,"pageHeight":400,"source":"embedded","chars":[
    ["c":"左","x0":10,"y0":10,"x1":20,"y1":20,"bk":0],
    ["c":"右","x0":70,"y0":10,"x1":80,"y1":20,"bk":0],
    ["c":"下","x0":10,"y0":70,"x1":20,"y1":80,"bk":1]]]
func contextNote(_ id: String, index: Int, text: String) -> O {
    ["id":id,"anchor":["kind":"pdf","page":7],"html":["content":"<b>" + text + "</b>",
        "bind":["kind":"page-chars","page":7,"from":index,"to":index,"text":text]]]
}
let contextNotes: [O] = [contextNote("lower",index:2,text:"下"),contextNote("right",index:1,text:"右"),
    contextNote("left",index:0,text:"左"),["id":"free","anchor":["kind":"pdf","page":7],
        "card":["cards":[["q":"<b>問い</b>","a":"答え"]]]]]
let contextState: O = ["contract":"reader-native-pdf-assistant-state/1","file":"localbook:book",
    "revisions":["notes":12,"highlights":0,"ink":0,"user_pages":0],"notes":contextNotes,
    "highlights":[],"ink":[:],"user_pages":[]]
let contextInput: O = ["rid":"same-request","turn_id":"same-turn","context":["page":7,"selected_text":"右"]]
let preparedContext = try ReaderNativePDFContext.prepare(contextInput,authority:contextState,sources:[7:characterSource])
let selectedContext = preparedContext["context"] as! O
let preparedState = selectedContext["native_local_state"] as! O
let contextProjection = preparedState["page_cards"] as! O
let contextRows = (contextProjection["pages"] as! O)["7"] as! [O]
check(contextRows.compactMap { $0["id"] as? String } == ["left","right","lower","free"],"native marker order differs from assistant numbering")
check(contextRows[1]["number"] as? Int == 2 && contextRows[3]["number"] is NSNull,"unbound card acquired a numbered anchor")
check(contextRows[3]["text"] as? String == "問い / 答え","legacy q/a face content was lost")
check(Set((contextRows[0]["bind"] as! O).keys) == ["kind","page","from","to","text"],"binding no longer matches the server contract")
check(selectedContext["selected_text"] as? String == "右" && preparedContext["turn_id"] as? String == "same-turn","preparation replaced request identity or selection")
check((selectedContext["visible_text"] as? String ?? "").contains("左"),"native source text was omitted")
var partial = contextInput; partial["context"] = ["page":7,"pages":[7,8],"visible_text":"original selected passage"]
let partialContext = try ReaderNativePDFContext.prepare(partial,authority:contextState,sources:[7:characterSource])["context"] as! O
check((partialContext["native_local_state"] as! O)["page_cards"] == nil,"partial page map falsely claimed complete numbering")
check(partialContext["visible_text"] as? String == "original selected passage","existing passage was replaced")
check(ReaderNativePDFContext.pages(["pages":[7,"7",0,"bad",8],"page":9]) == [7,8,9],"page list admitted invalid or duplicate entries")
print("Native PDF context: source geometry, numbering, original identities, legacy faces and missing-source behavior passed")
