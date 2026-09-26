import Foundation

func check(_ condition:Bool,_ message:String) { if !condition { fatalError(message) } }
func message(_ id:String,_ text:String,_ card:String = "") -> [String:Any] {
    ["id":id,"role":"assistant","text":text,"streaming":false,
     "parts":card.isEmpty ? [] : [["id":id + "-card","kind":"general","data":["text":card]]]]
}
// 迁出 P4（2026-09-27 清理）：网页不再投影消息，增量协议（apply / applyEvents）随之删除；
// 这里只剩指纹 —— 本机对话缓存去重与媒体替换核对用它。
let one = [message("a","同一句"),message("b","答案","旧卡")]
check(ReaderNativeConversationStore.fingerprint(one) == ReaderNativeConversationStore.fingerprint(one),"fingerprint not stable")
check(ReaderNativeConversationStore.fingerprint(one) != ReaderNativeConversationStore.fingerprint([message("a","同一句"),message("b","答案","新卡")]),
      "card-only change did not change the fingerprint")
check(ReaderNativeConversationStore.fingerprint(one) != ReaderNativeConversationStore.fingerprint(Array(one.reversed())),"order ignored")
check(ReaderNativeConversationStore.fingerprint([["bad": Date()]]) == nil,"non-JSON value fingerprinted")
print("Native conversation fingerprint: stable, card-sensitive, order-sensitive, rejects non-JSON passed")

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
