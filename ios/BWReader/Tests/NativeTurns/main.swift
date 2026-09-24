import Foundation

typealias O = [String:Any]
func check(_ value: Bool, _ message: String) { if !value { fatalError(message) } }
var store = ReaderNativeTurnStore()
@discardableResult func run(_ action: String, _ values: O = [:], tid: String = "turn") throws -> O {
    var input = values; input["action"] = action; input["tid"] = tid
    return try store.apply(input)
}
func output(_ result: O, id: String = "turn") -> O { (result["turns"] as! [O]).first { $0["id"] as? String == id }! }
func publicParts(_ result: O, id: String = "turn") -> [O] { output(result,id:id)["persistedParts"] as! [O] }

try run("open")
let started = try run("draft",["text":"partial","itemId":"answer","origin":"runner","role":"assistant"])
check(publicParts(started).isEmpty,"partial answer became durable history")
try run("reconcile",["message":["parts":[["kind":"text","item_id":"answer","origin":"runner","text":"older"]]],"options":[:]])
check(store.turns["turn"]!.parts.first?["text"] as? String == "partial","late history replaced a live draft")
let completed = try run("reconcile",["message":["item_id":"answer","parts":[["kind":"text","item_id":"answer","origin":"runner","text":"complete"]]],"options":["final":true]])
check(publicParts(completed).first?["text"] as? String == "complete","final answer not committed")
try run("append",["part":["kind":"tool","tool":"read","call_id":"call-a","status":"running"]])
try run("append",["part":["kind":"tool","tool":"read","call_id":"call-a","status":"completed","result":"ok"]])
try run("append",["part":["kind":"tool","tool":"read","call_id":"call-a","status":"running"]])
try run("append",["part":["kind":"tool","tool":"read","call_id":"call-b","status":"completed","result":"second"]])
let tools = store.turns["turn"]!.parts.filter { $0["kind"] as? String == "tool" }
check(tools.count == 2 && tools[0]["status"] as? String == "completed","invocation identity lost or completion regressed")
try run("draft",["text":"first","itemId":"speaker-a","origin":"runner","role":"user"],tid:"user")
try run("draft",["text":"second","itemId":"speaker-b","origin":"runner","role":"user"],tid:"user")
let oneFrozen = try run("freeze",["itemId":"speaker-a","origin":"runner","role":"user"],tid:"user")
check(publicParts(oneFrozen,id:"user").count == 1,"freezing one item froze another concurrent item")
try run("open",tid:"user-final")
let renamed = try run("rename",["newTid":"user-final"],tid:"user")
check(store.turns["user"] == nil && store.turns["user-final"]!.parts.count == 2,"rename lost a concurrent item")
check((output(renamed,id:"user-final")["presentation"] as! O)["streaming"] as? Bool == true,"renamed draft stopped streaming")
try run("progress",["event":["total":3,"step":1,"status":"done"]])
try run("progress",["event":["total":3,"step":2,"status":"error"]])
let progress = store.turns["turn"]!.progress!["states"] as! [Any]
check(progress[0] as? String == "done" && progress[1] as? String == "err" && progress[2] is NSNull,"progress slots were reset on an update")
let revision = store.revision
do { try run("append",["part":["text":"missing kind"]]); fatalError("malformed part accepted") }
catch is ReaderNativeTurnStore.Failure { }
check(store.revision == revision,"failed command partially committed")
let cards: [O] = [["type":"basic","front":"word","back":"meaning"]]
try run("import",["parts":[["kind":"cards","cards":cards,"draft":true]]],tid:"history")
let gid = store.turns["history"]!.parts.first?["gid"] as! String
check(gid == ReaderNativeTurnStore.cardID("history:0"),"history card identity changed")
try run("reset")
check(store.turns.isEmpty,"reset retained another mode's turns")
try run("import",["parts":[["kind":"cards","cards":cards,"draft":true]]],tid:"history")
check(store.turns["history"]!.parts.first?["gid"] as? String == gid,"reopening history generated a new card")
try run("import",["parts":[["kind":"cards","seq":12,"cards":cards]]],tid:"gaps")
try run("append",["part":["kind":"card","card":["title":"later"]]],tid:"gaps")
check(store.turns["gaps"]!.parts[0]["gid"] as? String == ReaderNativeTurnStore.cardID("gaps:12"),"noncontiguous history generated a different entity")
check((store.turns["gaps"]!.parts[1]["card"] as? O)?["cid"] as? String == "tc_gaps_13","following part reused a previous identity")
let beforeInvalid = store.revision
for sequence in ([-1, Double.infinity, true, 1.5, Int64.max] as [Any]) {
    do { try run("append",["part":["kind":"card","seq":sequence]],tid:"gaps"); fatalError("invalid sequence accepted") }
    catch is ReaderNativeTurnStore.Failure { }
}
check(store.revision == beforeInvalid,"invalid sequence changed the turn")
try run("draft",["text":"unsaved","itemId":"live"],tid:"gaps")
let voice = try store.voicePayload(tid:"gaps",metadata:["assistant_mode":"normal","file":"localbook:book","page":44,"user":"再发一次","assistant":"正在发送","clip":"recording","parts":[["kind":"text","text":"stale caller"]]])
check((voice["parts"] as? [O])?.count == 2,"voice save included an unfinished draft or caller snapshot")
check(voice["user"] as? String == "再发一次" && voice["clip"] as? String == "recording" && voice["turn_id"] as? String == "gaps","voice identity or recording was dropped")
check(voice["upsert_only"] == nil,"completed voice log stopped creating its original conversation row")
print("Native turns: live/final reconciliation, invocation dedupe, independent drafts, rename, progress, atomic rejection and stable card identity passed")
