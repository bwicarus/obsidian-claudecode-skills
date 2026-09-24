import Foundation
typealias F = ReaderNativeFigures
func equal(_ a: Any, _ b: Any) throws -> Bool {
    try JSONSerialization.data(withJSONObject: a, options: [.sortedKeys,.withoutEscapingSlashes]) == JSONSerialization.data(withJSONObject: b, options: [.sortedKeys,.withoutEscapingSlashes])
}
let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
for (n, item) in (fixture["cases"] as! [[String: Any]]).enumerated() {
    let matches = try equal(F.normalized(item["figures"] as! [[String: Any]], page: 3), item["rows"]!)
    precondition(matches, "Figure projection differs \(n)")
}
for row in fixture["rounding"] as! [[Any]] {
    precondition(F.fixed3((row[0] as! NSNumber).doubleValue) == row[1] as! String, "Figure identity rounding differs \(row)")
}
let ink = F.ink(fixture["strokes"] as! [[String: Any]], box: [0,0,0.9,0.9])
let inkMatches = try equal(ink, fixture["ink"]!); precondition(inkMatches)
let state = F(); state.open("one")
let raw: [String: Any] = ["ok":true, "figures":[["bbox":[0,0,1,1],"caption":"one"]]]
_ = try state.accept(raw, page: 1)
let id = state.figures(page: 1)!.first!["id"] as! String
_ = try state.setAttached(true, id: id, page: 1)
let first = state.projection(ink: [:]), oldEpoch = state.epoch
let token = (first["items"] as! [[String: Any]]).first!["token"] as! String
_ = try state.setAttached(true, id: id, page: 1)
precondition(state.revision == 1, "retry inverted or duplicated attachment")
_ = try state.setAttached(false, id: id, page: 1)
_ = try state.setAttached(true, id: id, page: 1)
precondition(!state.consume(epoch: oldEpoch, tokens: [token]), "late consume removed reattachment")
for page in 2...40 { _ = try state.accept(raw, page: page) }
precondition(state.figures(page: 1) == nil && state.hasAttachments, "page eviction removed explicit attachment")
let live = state.projection(ink: ["1":fixture["strokes"] as! [[String: Any]]])
precondition((live["items"] as! [[String: Any]]).first!["has_ink"] as? Bool == true, "attachment missed subsequent ink")
state.open("two")
precondition(!state.hasAttachments && !state.consume(epoch: oldEpoch, tokens: [token]), "cross-book state leak")
print("Native figures, identities, live ink, retry and consumption fences passed")
