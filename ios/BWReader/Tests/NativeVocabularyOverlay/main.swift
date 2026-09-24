import Foundation
typealias O = ReaderNativeVocabularyOverlay
typealias R = ReaderNativeCardRules
let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
for (index, item) in (fixture["cases"] as! [[String: Any]]).enumerated() {
    let state = O.Index(item["records"] as! [O.Row])
    let local = O.localMarks(item["chars"] as! [O.Row], state: state)
    precondition(R.same(local, item["local"]!), "local overlay differs at \(index): \(local), expected \(item["local"]!)")
    let combined = O.merge(local, item["remote"] as! [O.Row])
    precondition(R.same(combined, item["combined"]!), "overlay merge differs at \(index)")
    precondition(R.same(O.visible(combined, state: state), item["visible"]!), "overlay filter differs at \(index)")
}
print("Native vocabulary overlay matches token, alias, nested mastery and geometry merge oracle")
