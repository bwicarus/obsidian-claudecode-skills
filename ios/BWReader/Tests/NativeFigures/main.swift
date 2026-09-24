import Foundation
import CoreFoundation
typealias F = ReaderNativeFigures
func equal(_ a: Any, _ b: Any) throws -> Bool {
    // JSONDecoder's decimal NSNumber spelling may differ after conversion to
    // Double even though the coordinate is identical. Identity strings remain
    // exact and have separate IEEE-754 toFixed oracle cases below.
    if let x = a as? NSNumber, let y = b as? NSNumber {
        return (CFGetTypeID(x) == CFBooleanGetTypeID()) == (CFGetTypeID(y) == CFBooleanGetTypeID()) && x.doubleValue == y.doubleValue
    }
    if let x = a as? String, let y = b as? String { return x == y }
    if let x = a as? [Any], let y = b as? [Any] {
        guard x.count == y.count else { return false }
        return try zip(x,y).allSatisfy { try equal($0.0,$0.1) }
    }
    if let x = a as? [String:Any], let y = b as? [String:Any] {
        guard Set(x.keys) == Set(y.keys) else { return false }
        return try x.allSatisfy { try equal($0.value,y[$0.key]!) }
    }
    return a is NSNull && b is NSNull
}
let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
for (n, item) in (fixture["cases"] as! [[String: Any]]).enumerated() {
    let matches = try equal(F.normalized(item["figures"] as! [[String: Any]], page: 3), item["rows"]!)
    precondition(matches, "Figure projection differs \(n): \(F.normalized(item["figures"] as! [[String: Any]], page: 3)) expected \(item["rows"]!)")
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
