import Foundation
func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
func canonical(_ value: Any) throws -> Data { try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) }
var state = ReaderNativeContextSelection()
let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [[String: Any]]
for (index, item) in cases.enumerated() {
    let now = (item["now"] as! NSNumber).doubleValue
    if let command = item["command"] as? [String: Any] { try state.apply(command, now: now) }
    else { state.expire(now: now) }
    let actual = state.snapshot(maxText: 100000)
    check(try canonical(actual) == canonical(item["expected"]!), "Context graph/expiry mismatch at \(index): \(actual)")
}
let before = try canonical(state.projection)
do {
    try state.apply(["operation":"select", "id":"bad", "record":["id":"other"]], now:0)
    fatalError("malformed record accepted")
} catch is ReaderNativeContextSelection.Failure { }
check(try canonical(state.projection) == before, "rejected operation changed context")
check((state.snapshot(limit: 0)["items"] as! [[String:Any]]).isEmpty, "empty limit ignored")
let reset = ReaderNativeContextSelection()
check((reset.snapshot()["items"] as! [[String:Any]]).isEmpty, "new document retained selection")
print("Native context selection: graph/identity parity, containment, cycles, expiry, nonrenewal and cancellation passed")

var pins = ReaderNativeContextSelection()
let group: [[String:Any]] = [["front":"removed","_removed":true],["front":"原文","back":"释义","card_id":123,"source_ref":["page":8]]]
let pin = try ReaderNativeCardContext.learning(gid:"group",cards:group,index:1,parent:"turn:source")
check((pin["meta"] as! [String:Any])["active_index"] as? Int == 1,"removed slots changed selected index")
check(((pin["meta"] as! [String:Any])["cards"] as! [[String:Any]]).count == 2,"group metadata was compacted")
check((pin["source"] as! [String:Any])["card_id"] as? Int == 123,"stable Anki ID lost")
check(pin["text"] as? String == "原文 / 释义","removed cards entered outgoing text")
try pins.apply(["operation":"select","id":"paragraph","record":["id":"paragraph","parentId":"card:group","kind":"text",
    "label":"段落","text":"partial","source":[:],"meta":[:],"covers":[]]],now:0)
try pins.toggleCard(pin,now:1)
check((pins.snapshot()["items"] as! [[String:Any]]).count == 1,"whole card did not cover its selected paragraph")
try pins.toggleCard(pin,now:2)
check((pins.snapshot()["items"] as! [[String:Any]]).first?["id"] as? String == "paragraph","releasing whole card discarded child selection")
try pins.toggleCard(pin,now:3)
let other = try ReaderNativeCardContext.learning(gid:"other",cards:group,index:1,parent:"")
try pins.toggleCard(other,now:4)
check(Set((pins.snapshot()["items"] as! [[String:Any]]).compactMap { $0["label"] as? String }).count == 2,"same-name cards overwrote one another")
let image = try ReaderNativeCardContext.semantic(["cid":"image","kind":"images","title":"图","data":["items":[
    ["title":"removed","url":"secret","_gone":1],["title":"visible","src":"source","url":"private"]]]],parent:"")
check(image["text"] as? String == "图:visible[源:source](图片本身在用户屏幕上;上下文只带元数据,不含图片/URL)","image metadata policy changed")
print("Native card context: original slots, metadata, whole/child selection and same-title identity passed")
