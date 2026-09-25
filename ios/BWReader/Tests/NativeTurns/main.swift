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

// Source references retain full bodies in Swift, not in an additional WebKit
// payload. Stale versions and original-slot mismatches reject atomically.
let sourceText = String(repeating:"原文😀", count:12000)
try run("draft",["text":sourceText,"itemId":"source-answer"],tid:"source")
try run("append",["part":["kind":"tool","tool":"reader_card","result":sourceText]],tid:"source")
let sourceState = output(try run("append",["part":["kind":"cards","gid":"g-slots","cards":[["front":"first"],["front":"second"]]]],tid:"source"),id:"source")
let sourceParts = sourceState["parts"] as! [O]
let presentation = sourceState["presentation"] as! O
let sourceReference: O = ["tid":"source","revision":presentation["revision"]!]
let sourceMessage: O = ["id":"message-source","nativeTurnRef":sourceReference,
    "text":"stale web text", "parts":[
        ["id":"tool-slot","data":["nativeTurnPart":["id":sourceParts[1]["_nativeID"]!]],"actionId":"inspect-tool"],
        ["id":"card-slot","data":["nativeTurnPart":["id":sourceParts[2]["_nativeID"]!,"cardIndex":1],"dragId":"place-second"]]
    ]]
let resolved = try store.conversationMessage(sourceMessage)
check(resolved["text"] as? String == sourceText && resolved["streaming"] as? Bool == true,"native live text was replaced by a web preview")
let resolvedParts = resolved["parts"] as! [O]
let toolContent = ((resolvedParts[0]["data"] as! O)["nativeDetail"] as! O)["content"] as! O
check(toolContent["result"] as? String == sourceText && resolvedParts[0]["actionId"] as? String == "inspect-tool","full tool original or operation identity lost")
let cardData = resolvedParts[1]["data"] as! O
let cardContent = (cardData["nativeDetail"] as! O)["content"] as! O
check((cardContent["card"] as! O)["front"] as? String == "second" && cardContent["cardIndex"] as? Int == 1 && cardData["dragId"] as? String == "place-second","original card slot was renumbered")
check((presentation["parts"] as! [O])[1]["nativePartID"] as? String == sourceParts[1]["_nativeID"] as? String,"source identity missing from compatibility projection")
check(!(sourceState["persistedParts"] as! [O]).contains { $0["nativePartID"] != nil },"temporary source identity leaked to persisted history")
var brokenMessage = sourceMessage
brokenMessage["parts"] = [["id":"bad","data":["nativeTurnPart":["id":sourceParts[2]["_nativeID"]!,"cardIndex":9]]]]
do { _ = try store.conversationMessage(brokenMessage); fatalError("invalid original slot accepted") } catch is ReaderNativeTurnStore.Failure { }
try run("status",["text":"已完成","done":true],tid:"source")
do { _ = try store.conversationMessage(sourceMessage); fatalError("late manifest replaced newer source state") } catch is ReaderNativeTurnStore.Failure { }
let plain: O = ["id":"legacy","text":"历史正文","parts":[]]
check(try store.conversationMessage(plain)["text"] as? String == "历史正文","plain history was discarded")
let mediaCard: O = ["cid":"media-original","kind":"images","data":["items":[["title":"first","url":"https://example.com/a"],["title":"second","url":"https://example.com/b"]]]]
let mediaState = output(try run("append",["part":["kind":"card","card":mediaCard]],tid:"media"),id:"media")
let mediaVersion = (mediaState["presentation"] as! O)["revision"]!
let mediaID = (mediaState["parts"] as! [O])[0]["_nativeID"]!
let mediaMessage: O = ["id":"media","nativeTurnRef":["tid":"media","revision":mediaVersion],
    "parts":[["id":"media-part","data":["nativeTurnPart":["id":mediaID]]]]]
let historyBefore = try JSONSerialization.data(withJSONObject:store.historyPayload(tid:"media",mode:"normal",file:"book",page:1,absorb:[])!,options:[.sortedKeys])
try store.removeMedia(card:mediaCard,index:1)
let mediaResolved = try store.conversationMessage(mediaMessage)
let mediaVisible = (((mediaResolved["parts"] as! [O])[0]["data"] as! O)["nativeDetail"] as! O)["content"] as! O
let visibleItems = (mediaVisible["data"] as! O)["items"] as! [O]
check(visibleItems[0]["_gone"] == nil && visibleItems[1]["_gone"] as? Int == 1,"media removal hid the wrong original slot")
let historyAfter = try JSONSerialization.data(withJSONObject:store.historyPayload(tid:"media",mode:"normal",file:"book",page:1,absorb:[])!,options:[.sortedKeys])
check(historyBefore == historyAfter,"local media disposition rewrote append-only history")
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
let operationState = output(try run("append",["part":["kind":"hlcard","file":"book","items":[["id":"old-highlight","pdf_page":4,"text":"原文","undone":true]]]],tid:"operations"),id:"operations")
let operationPartID = (operationState["parts"] as! [O])[0]["_nativeID"]!
let redone = output(try run("operationState",["parts":[["id":operationPartID,"items":[["index":0,"id":"restored-highlight","undone":false,"gone":false]]]]],tid:"operations"),id:"operations")
let operationMessage = try store.conversationMessage(["id":"operations","nativeTurnRef":["tid":"operations","revision":(redone["presentation"] as! O)["revision"]!],
    "parts":[["id":"operations-part","data":["nativeTurnPart":["id":operationPartID]]]]])
let operationOriginal = (((operationMessage["parts"] as! [O])[0]["data"] as! O)["nativeDetail"] as! O)["content"] as! O
let operationItem = (operationOriginal["items"] as! [O])[0]
check(operationItem["id"] as? String == "restored-highlight" && operationItem["pdf_page"] as? Int == 4 && operationItem["text"] as? String == "原文","redo lost the replacement identity or changed its source anchor")
// 侧栏每次原子重载都用同一个 hist_ 轮次号回放历史：回放必须是重建，不能每次多叠一份。
let replayParts: [O] = [["kind":"text","text":"东京明天多云","origin":"voice"],["kind":"text","text":"卡片已发到 Reader","origin":"runner"]]
let firstReplay = output(try run("import",["parts":replayParts],tid:"hist_normal_h_x"),id:"hist_normal_h_x")
let secondReplay = output(try run("import",["parts":replayParts],tid:"hist_normal_h_x"),id:"hist_normal_h_x")
check((secondReplay["parts"] as! [O]).count == 2,"history replay appended a second copy of the same turn")
check((firstReplay["parts"] as! [O]).map { $0["_nativeID"] as! String } == (secondReplay["parts"] as! [O]).map { $0["_nativeID"] as! String },
      "history replay changed part identities and would flicker")
// App 替服务器执行的工具是服务器那次调用的镜像：先到先显示，服务器那条到了就并掉，不重复计步。
try run("append",["part":["kind":"tool","tool":"reader_context_snapshot","label":"读取页面","origin":"app","result":"完成 · 33 ms"]],tid:"mirror")
check(store.turns["mirror"]!.parts.count == 1,"app tool shown before the runner call arrived was hidden")
try run("append",["part":["kind":"tool","tool":"reader_snapshot.reader_context_snapshot","origin":"runner","call_id":"exec-1","status":"completed"]],tid:"mirror")
try run("append",["part":["kind":"tool","tool":"reader_context_snapshot","label":"读取页面","origin":"app","result":"完成 · 20 ms"]],tid:"mirror")
try run("append",["part":["kind":"tool","tool":"webSearch","origin":"runner","call_id":"exec-2","status":"completed"]],tid:"mirror")
check(store.turns["mirror"]!.parts.filter { $0["kind"] as? String == "tool" }.count == 2,"app mirror of a runner call counted as an extra step")
// 空草稿是「撤掉草稿」：既不新建一条空转的「正在回复」，也要把已有草稿收掉。
let emptyDraft = output(try run("draft",["text":"","itemId":"v-1","origin":"voice","role":"assistant"],tid:"retarget"),id:"retarget")
check((emptyDraft["presentation"] as! O)["streaming"] as? Bool == false,"an empty draft left a spinning reply")
try run("draft",["text":"整体来说","itemId":"v-2","origin":"voice","role":"assistant"],tid:"retarget")
let cleared = output(try run("draft",["text":"","itemId":"v-2","origin":"voice","role":"assistant"],tid:"retarget"),id:"retarget")
check((cleared["presentation"] as! O)["streaming"] as? Bool == false && (cleared["parts"] as! [O]).isEmpty,"clearing a draft left it streaming")
print("Native turns: live/final reconciliation, invocation dedupe, independent drafts, rename, progress, atomic rejection and stable card identity passed")
