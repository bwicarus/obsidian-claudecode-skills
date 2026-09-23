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
