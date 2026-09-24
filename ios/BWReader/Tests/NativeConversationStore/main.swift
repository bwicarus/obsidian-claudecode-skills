import Foundation

func check(_ condition:Bool,_ message:String) { if !condition { fatalError(message) } }
func message(_ id:String,_ text:String,_ card:String = "") -> [String:Any] {
    ["id":id,"role":"assistant","text":text,"streaming":false,
     "parts":card.isEmpty ? [] : [["id":id + "-card","kind":"general","data":["text":card]]]]
}
func batch(_ revision:Int,_ base:Int,_ order:[String],_ upserts:[[String:Any]],reset:Bool = false) -> [String:Any] {
    ["contract":"reader-native-conversation-delta/1","revision":revision,"baseRevision":base,"reset":reset,"order":order,"upserts":upserts]
}
var store = ReaderNativeConversationStore()
let first = batch(1,0,["a","b"],[message("a","同一句"),message("b","答案","旧卡")],reset:true)
check(try store.apply(first,scope:"book-a"),"initial message batch ignored")
check(try !store.apply(first,scope:"book-a"),"duplicate delivery applied twice")
let before = ReaderNativeConversationStore.fingerprint(store.messages)
do { _ = try store.apply(batch(3,2,["a","b"],[message("a","丢失一轮")]),scope:"book-a"); fatalError("missing base accepted") }
catch ReaderNativeConversationStore.Failure.missingBase { }
check(ReaderNativeConversationStore.fingerprint(store.messages) == before,"failed delta partially updated store")
_ = try store.apply(batch(2,1,["b","a"],[message("b","答案","新卡")]),scope:"book-a")
check(store.messages.first?["id"] as? String == "b","message order not retained")
check(ReaderNativeConversationStore.fingerprint(store.messages) != before,"card-only change disappeared")
let beforeMalformed = ReaderNativeConversationStore.fingerprint(store.messages)
do { _ = try store.apply(batch(3,2,["b","c"],[message("b","不能写入")]),scope:"book-a"); fatalError("missing ordered message accepted") }
catch ReaderNativeConversationStore.Failure.missingBase { }
check(ReaderNativeConversationStore.fingerprint(store.messages) == beforeMalformed,"incomplete batch removed a message")
_ = try store.apply(batch(4,0,["b"],[message("b","重新同步")],reset:true),scope:"book-a")
check(store.messages.count == 1 && store.messages.first?["text"] as? String == "重新同步","full resync did not replace authoritative order")
do { _ = try store.apply(batch(5,4,["b"],[]),scope:"book-b"); fatalError("old messages leaked into a new scope") }
catch ReaderNativeConversationStore.Failure.missingBase { }
_ = try store.apply(batch(1,0,["c"],[message("c","另一本书")],reset:true),scope:"book-b")
check(store.messages.count == 1 && store.messages.first?["id"] as? String == "c","scope reset retained old messages")
_ = try store.apply(batch(2,1,[],[]),scope:"book-b")
check(store.messages.isEmpty,"clear retained stale messages")
print("Native conversation store: ordered deltas, duplicate delivery, missing-base recovery, card updates, clear and scope isolation passed")

func events(_ revision:Int,_ base:Int,_ events:[[String:Any]],_ updates:[[String:Any]],reset:Bool = false) -> [String:Any] {
    ["contract":"reader-native-conversation-delta/2","revision":revision,"baseRevision":base,"reset":reset,"events":events,"upserts":updates]
}
func place(_ id:String,_ group:String = "thread",before:String? = nil,from:String? = nil) -> [String:Any] {
    var event:[String:Any] = ["action":"place","id":id,"group":group]
    if let before { event["before"] = before }; if let from { event["from"] = from }; return event
}
var lifecycle = ReaderNativeConversationStore()
_ = try lifecycle.apply(events(1,0,[place("reply"),place("question",before:"reply")],[message("reply","流式回答"),message("question","晚到的转写")],reset:true),scope:"native")
check(lifecycle.messages.map { $0["id"] as! String } == ["question","reply"],"late voice source reordered the conversation")
_ = try lifecycle.apply(events(2,1,[place("reply","aborted-history"),["action":"clear","group":"aborted-history"]],[]),scope:"native")
check(lifecycle.messages.count == 2,"aborted replay erased a matching active message")
_ = try lifecycle.apply(events(3,2,[place("history","stage"),place("reply","stage"),place("reply","stage",before:"reply",from:"thread"),["action":"adopt","group":"stage"]],[message("history","已有记录")]),scope:"native")
check(lifecycle.messages.map { $0["id"] as! String } == ["history","reply"],"history adoption lost the live response")
let stableLifecycle = ReaderNativeConversationStore.fingerprint(lifecycle.messages)
do { _ = try lifecycle.apply(events(4,3,[["action":"clear","group":"thread"],["action":"invalid"]],[]),scope:"native");fatalError("invalid event accepted") }
catch ReaderNativeConversationStore.Failure.malformed { }
check(ReaderNativeConversationStore.fingerprint(lifecycle.messages) == stableLifecycle,"invalid event partially cleared the history")
_ = try lifecycle.apply(events(4,3,[["action":"remove","group":"thread","id":"reply"]],[]),scope:"native")
check(lifecycle.messages.count == 1,"removed source remained visible")
do { _ = try lifecycle.apply(events(5,4,[place("leak")],[message("leak","old")]),scope:"another");fatalError("old-scope lifecycle accepted") }
catch ReaderNativeConversationStore.Failure.missingBase { }
_ = try lifecycle.apply(events(1,0,[place("new")],[message("new","新会话")],reset:true),scope:"another")
check(lifecycle.messages.first?["id"] as? String == "new","full source recovery retained old members")
print("Native conversation lifecycle: late voice, atomic history adoption, cancellation, removal and scope isolation passed")

func artifact(_ kind: String, _ original: [String: Any], data: [String: Any] = [:]) -> [String: Any] {
    var input = data
    input["nativeDetail"] = ["kind": kind, "title": "原件", "content": original]
    return ReaderNativeConversationProjection.part(["id": "original-slot", "kind": kind,
        "title": "", "text": "", "data": input, "actionId": "scope:a:original"])
}
let tool = artifact("tool", ["tool": "reader_card", "status": "done", "steps": [
    ["status": "completed"], ["error": "失败"], ["status": "pending"], ["result": NSNull()]]])
let toolData = tool["data"] as! [String: Any]
check(tool["status"] as? String == "completed" && tool["title"] as? String == "reader_card", "native tool status/title lost")
check(toolData["stepCount"] as? Int == 4 && toolData["successCount"] as? Int == 1 && toolData["failureCount"] as? Int == 1 && toolData["runningCount"] as? Int == 1, "tool step outcomes conflated")
check(artifact("tool", ["status": "done", "error": "拒绝"])["status"] as? String == "failed", "success label hid an error")
let longText = String(repeating: "完整原文😀", count: 8000)
let fact = artifact("fact", ["kind": "fact", "cid": "original", "data": ["answer": longText]], data: ["dragId": "drag-original", "selectId": "select-original"])
let factData = fact["data"] as! [String: Any]
check(factData["answer"] as? String == longText, "long body was truncated into a preview")
check((fact["text"] as? String)?.count == 1600 && fact["actionId"] as? String == "scope:a:original" && factData["dragId"] as? String == "drag-original", "preview replaced original command identity")
let anki = artifact("anki", ["gid": "g", "cardIndex": 2, "card": ["front": "旧题面"]], data: ["draft": true, "nativeCard": ["gid": "g", "cardIndex": 2, "card": ["question": "已编辑题面", "answer": "答案"]]])
let ankiData = anki["data"] as! [String: Any]
check(ankiData["front"] as? String == "已编辑题面" && ankiData["back"] as? String == "答案", "historical event replaced committed Anki fields")
check((ankiData["nativeCard"] as? [String: Any])?["cardIndex"] as? Int == 2, "removed group slots were renumbered")
let weather = artifact("weather", ["data": ["lo": 0, "hi": 12, "cond": "雨", "tip": longText]])["data"] as! [String: Any]
check(weather["lo"] as? Int == 0 && weather["tip"] as? String == longText, "weather values were dropped or truncated")
let legacy: [String: Any] = ["id": "old", "kind": "artifact", "text": "旧原件", "data": ["html": "<b>旧原件</b>"]]
check(ReaderNativeConversationProjection.part(legacy)["text"] as? String == "旧原件", "legacy inspection changed without a native contract")
print("Native artifact presentation: complete originals, live card revisions, original slots, tool outcomes and action identities passed")
