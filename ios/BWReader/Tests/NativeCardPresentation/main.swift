import Foundation

let fixtures = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [[String: Any]]
func json(_ value: Any) -> String {
    String(decoding: try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes]), as: UTF8.self)
}
for (index, item) in fixtures.enumerated() {
    let input = item["input"] as! [String: Any]
    let actual = ReaderNativeCardPresentation.interaction(input)!
    precondition(json(actual) == json(item["expected"]!), "card presentation differs \(index):\n\(json(actual))\n\(json(item["expected"]!))")
    let controls = actual["controls"] as! [[String: Any]]
    let fields = actual["fields"] as! [[String: Any]]
    var ids: [String: String] = [:]
    for control in controls { let key = control["key"] as! String; ids[key] = "action:" + key }
    for field in fields { let key = "edit-" + (field["key"] as! String); ids[key] = "action:" + key }
    let data = ReaderNativeCardPresentation.project(["nativeCard": input, "nativeCardActions": ids])
    precondition(data["live"] as? Bool == true)
    precondition((data["controls"] as! [[String: Any]]).allSatisfy { ($0["id"] as! String).hasPrefix("action:") })
    precondition(json(data["faces"]!) == json((actual["presentation"] as! [String: Any])["faces"]!))
}
print("Native card presentation: \(fixtures.count) browser parity cases passed")

let draft: [String: Any] = ["nativeCard": ["gid": "card_test", "cardIndex": 0, "entityRev": 1, "stateRev": 1,
    "card": ["type": "basic", "front": "old", "back": "answer", "_st": "draft"]],
    "nativeCardActions": ["edit-front": "edit", "reveal": "show"]]
var record: [String: Any] = ["gid": "card_test", "entityRev": 1, "stateRev": 2,
    "cards": [["type": "basic", "front": "old", "back": "answer"]],
    "states": ["0": ["phase": "draft", "exactState": ["front": "edited"]]]]
let changed = ReaderNativeCardPresentation.applying(record, to: draft)!
precondition((changed["fields"] as! [[String: Any]])[0]["value"] as? String == "edited")
precondition(((changed["nativeCard"] as! [String: Any])["stateRev"] as! NSNumber).intValue == 2)
var stale = record; stale["stateRev"] = 1
precondition(json(ReaderNativeCardPresentation.applying(stale, to: changed)!) == json(changed), "late snapshot reverted native edit")
record["stateRev"] = 3
record["states"] = ["0": ["phase": "confirmed", "exactState": ["_st": "learn", "_showBack": true],
    "projections": ["anki": ["readerpc": ["status": "succeeded"], "ankimobile": ["status": "unknown"]]]]]
let confirmed = ReaderNativeCardPresentation.applying(record, to: changed)!
precondition(confirmed["editable"] as? Bool == false)
precondition((confirmed["notice"] as! String).contains("结果未知"), "successful alternative exporter cleared unknown outcome")
var tombstone = record; tombstone["stateRev"] = 4; tombstone["deleted"] = true
precondition(ReaderNativeCardPresentation.applying(tombstone, to: confirmed) == nil)
var other = record; other["gid"] = "card_other"
precondition(json(ReaderNativeCardPresentation.applying(other, to: draft)!) == json(draft))
print("Native card receipts: immediate edited fields, revision fences, pending precedence and removal passed")
